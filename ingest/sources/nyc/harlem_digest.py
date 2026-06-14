"""End-to-end Harlem digest demo (NYC-SPECIFIC application wiring).

Proves the headline — *a neighbor reads one email and knows what they need to know
this week* — on LIVE structured data with NO database. This is application wiring,
not new machinery: it pulls East Harlem events from the structured connectors
(:mod:`ingest.sources.nyc.dob_hpd`), then drives the city-agnostic Deliver path
(match -> rank -> build_digest -> render -> file sink) for one sample subscriber.

Boundary: NYC-SPECIFIC (knows the East Harlem subscriber + which feeds to pull), so
it lives in ``nyc/``. The Deliver stages it calls never mention NYC (Rule 4).

Storage note: nothing is persisted. Events stream from Socrata through memory; the
only artifact is the rendered digest written by the v1 file sink. Postgres+PostGIS
(Store, Stage 5) is Phase 1 — until then this runs DB-free, by design.

Run:  python -m ingest.sources.nyc.harlem_digest
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import islice

from ingest.deliver.digest import build_digest, render_markdown
from ingest.deliver.match import match_subscriber
from ingest.deliver.send import send_digest
from ingest.extract.schemas import CivicEvent
from ingest.observability import get_logger
from ingest.sources.nyc.building_grades import discover_energy_grades
from ingest.sources.nyc.dob_hpd import (
    DOB_PERMITS_FEED,
    HPD_VIOLATIONS_FEED,
    _dob_permit_to_event,
    _hpd_violation_to_event,
    discover_displacement_signals,
    iter_feed,
)
from ingest.sources.nyc.legistar import discover_cd_hearings
from ingest.sources.nyc.zap_api import _zap_project_to_event, iter_zap_events

log = get_logger(__name__)

# A sample confirmed subscriber in East Harlem (the only v1 user state — Rule 16).
# In production this row comes from subscribers.py (signup -> GeoSupport geocode).
SAMPLE_SUBSCRIBER = {
    "email": "neighbor@example.com",
    "address": "123 East 116th Street, New York, NY 10029",
    "bbl": "1016500030",
    "latitude": 40.7969,
    "longitude": -73.9410,
    "zip": "10029",
    "community_district": "111",
}

_RECENT_PERMITS = (
    "job_type in ('A1','NB','DM') AND (issuance_date like '%2025' or issuance_date like '%2026')"
)


def gather_live_events(
    *,
    per_feed: int = 6,
    include_signal: bool = False,
    signals: int = 3,
    include_zap: bool = True,
    include_legistar: bool = True,
    legistar_days: int = 30,
    include_grades: bool = False,
) -> list[CivicEvent]:
    """Pull a bounded slice of recent East Harlem events from the live feeds.

    ``include_signal`` is off by default: the displacement signal does a full
    cross-feed scan (thousands of rows over slow NYC Open Data) and is too heavy for
    an interactive demo. The signal is still exercised by the offline sample path and
    its own demo (``python -m ingest.sources.nyc.dob_hpd``).

    ``include_zap`` is on by default: ZAP is a snapshot pull (no cursor); the scoped
    East Harlem slice is small enough for interactive use.

    ``include_legistar`` pulls upcoming Land Use Committee / City Council hearings
    for the next ``legistar_days`` days (Phase 1 gate). Hearings have no per-building
    BBL so they land in the ``in_your_area`` band of the digest (all of East Harlem).

    ``include_grades`` is off by default (flip on after a spot-check): it enriches the
    buildings already surfaced above with their Local Law 33 energy letter grade,
    surfacing only the below-average (D/F) grades as low-key building context. It is
    bounded to the surfaced BBLs, so it never firehoses; coordinates are carried over
    from the surfaced event so the grade threads into the same proximity band as the
    building's permits/violations (Rule 7).
    """
    events: list[CivicEvent] = []
    events += list(
        iter_feed(
            HPD_VIOLATIONS_FEED,
            # OPEN only: a cured (closed) violation must not be presented as active.
            where="class = 'C' AND violationstatus = 'Open'",
            limit=per_feed,
            order="inspectiondate DESC",
        )
    )
    events += list(
        iter_feed(DOB_PERMITS_FEED, where=_RECENT_PERMITS, limit=per_feed, order="dobrundate DESC")
    )
    if include_zap:
        events += list(iter_zap_events(limit=per_feed))
    if include_legistar:
        # Phase 1 gate: "upcoming hearings in CD X returns correct dates for next 30 days."
        events += discover_cd_hearings("MN11", days_ahead=legistar_days)
    if include_signal:
        events += list(islice(discover_displacement_signals(), signals))
    if include_grades:
        events += _enrich_with_energy_grades(events)
    return events


def _enrich_with_energy_grades(events: list[CivicEvent]) -> list[CivicEvent]:
    """Pull D/F energy grades for the buildings already surfaced, threaded to their BBL.

    Bounded to the surfaced BBLs so it cannot firehose. A grade row carries no
    coordinates, so we carry over the lat/lng of a co-located surfaced event on the
    same BBL — accurate, since it is literally the same building — so the grade lands
    in the same proximity band and groups with that building's events (Rule 7).
    """
    coords_by_bbl: dict[str, tuple[float, float]] = {
        ev.bbl: (ev.latitude, ev.longitude)
        for ev in events
        if ev.bbl and ev.latitude is not None and ev.longitude is not None
    }
    bbls = {ev.bbl for ev in events if ev.bbl}
    enriched: list[CivicEvent] = []
    for grade in discover_energy_grades(bbls=bbls):
        coords = coords_by_bbl.get(grade.bbl or "")
        if coords is not None:
            grade = grade.model_copy(update={"latitude": coords[0], "longitude": coords[1]})
        enriched.append(grade)
    return enriched


def _sample_events() -> list[CivicEvent]:
    """Offline fallback: realistic East Harlem records run through the real mappers.

    Used only when the live API is unreachable, so the demo still renders end-to-end.
    Source links resolve by pattern but ids are illustrative.
    """
    today = date.today().isoformat()
    # v, p and the signal all sit on BBL 1016500030 (block 1650 / lot 30) — the
    # subscriber's own building — so they thread into one group (Rule 7).
    v = _hpd_violation_to_event(
        {
            "violationid": "DEMO1001",
            "class": "C",
            "housenumber": "123",
            "streetname": "EAST 116 STREET",
            "boroid": "1",
            "block": "1650",
            "lot": "30",
            "inspectiondate": f"{today}T00:00:00.000",
            "originalcorrectbydate": "2026-05-10T00:00:00.000",
            "novdescription": "NO HEAT OR HOT WATER IN ENTIRE BUILDING",
            "zip": "10029",
            "currentstatus": "Violation Open",
        }
    )
    p = _dob_permit_to_event(
        {
            "permit_si_no": "DEMO2001",
            "job_type": "A1",
            "house__": "123",
            "street_name": "EAST 116 STREET",
            "borough": "MANHATTAN",
            "block": "1650",
            "lot": "30",
            "issuance_date": "03/15/2026",
            "gis_latitude": "40.7969",
            "gis_longitude": "-73.9410",
            "owner_s_business_name": "ACME HOLDINGS LLC",
        }
    )
    # A different building a couple blocks away (lands in a wider band).
    nb = _dob_permit_to_event(
        {
            "permit_si_no": "DEMO2002",
            "job_type": "NB",
            "house__": "200",
            "street_name": "EAST 117 STREET",
            "borough": "MANHATTAN",
            "block": "1651",
            "lot": "5",
            "issuance_date": "04/02/2026",
            "gis_latitude": "40.8005",
            "gis_longitude": "-73.9360",
        }
    )
    from ingest.sources.nyc.dob_hpd import _displacement_event

    signal = _displacement_event("1016500030", [v], [p], date.today())

    # ZAP land-use application on the same building (BBL 1016500030) — threads with
    # HPD/DOB events into one building group (Rule 7). Hearing date is in the future
    # so it surfaces as an upcoming action item in the digest.
    z = _zap_project_to_event(
        {
            "project_id": "P2024M0042",
            "ulurp_numbers": "C 240042 ZMM",
            "project_brief": (
                "Proposed rezoning from R7-2 to R8A to facilitate construction of a "
                "12-story mixed-use building with 80 affordable units."
            ),
            "public_status": "In Public Review",
            "applicant_name": "East Harlem Realty LLC",
            "lead_action": "Zoning Map Amendment",
            "community_district": "M11",
            "primary_address": "123 EAST 116 STREET",
            "certified_referred": f"{today}T00:00:00.000",
            "hearing_date_1": "2026-06-30T00:00:00.000",
        },
        bbl_value="1016500030",
    )
    # Local Law 33 energy grade on the subscriber's own building (same BBL) — threads
    # with the HPD/DOB events into one building group as low-key context (Rule 7).
    from ingest.sources.nyc.building_grades import _energy_grade_to_event

    grade = _energy_grade_to_event(
        {
            "bbl": "1016500030",
            "address": "123 EAST 116 STREET",
            "boroughname": "MANHATTAN",
            "building_class": "D1",
            "letterscore": "F",
            "energy_star_score": "12",
            "dof_gross_square_footage": "84000",
            "building_count": "1",
        }
    )
    return [v, p, nb, signal, z, grade]


def gather_events() -> tuple[list[CivicEvent], bool]:
    """Return (events, is_live). Falls back to sample data if the API is unreachable."""
    try:
        events = gather_live_events()
        if events:
            return events, True
        log.warning("live feeds returned no events; using sample data")
    except Exception as exc:  # network/API unavailable -> still produce the artifact
        log.warning("live fetch failed (%s); using sample data", exc)
    return _sample_events(), False


def run() -> None:
    import sys

    if hasattr(sys.stdout, "reconfigure"):  # ensure unicode prints on any console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from ingest.config import get_settings
    from ingest.deliver.review import dump_pending

    events, is_live = gather_events()
    matched = match_subscriber(SAMPLE_SUBSCRIBER, events)
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=datetime.now(UTC).date())

    print(f"\n=== Harlem digest demo ({'LIVE data' if is_live else 'SAMPLE data (offline)'}) ===")
    print(f"Subject: {digest['subject']}")
    print(
        f"Items: {digest['item_count']}  |  need attention: {digest['needs_attention_count']}"
        f"  |  review required: {digest['review_required']}"
    )

    # Human-review-then-send: a digest with flagged items must be cleared by a person
    # before it can go out. The dev bypass clears it inline for offline/CI demos; without
    # the bypass we park the digest for the real reviewer and stop — no fabricated approval.
    if digest["review_required"]:
        settings = get_settings()
        if settings.bypass_human_review:
            print(
                "\n[dev bypass] BYPASS_HUMAN_REVIEW is set — clearing the queue (CI/offline only)."
            )
            digest = {**digest, "review_required": False, "review_items": []}
        else:
            pending_path = dump_pending(digest, SAMPLE_SUBSCRIBER)
            print(f"\nHUMAN-REVIEW QUEUE: {len(digest['review_items'])} item(s) need review:")
            for title in digest["review_items"]:
                print(f"  - {title}")
            print(f"\nSaved pending digest to: {pending_path}")
            print("Run the reviewer to clear it and send:")
            print("  python -m ingest.deliver.review")
            return

    path = send_digest(digest, SAMPLE_SUBSCRIBER)
    print(f"\nDigest written to: {path}\n")
    print("----- rendered body -----")
    print(render_markdown(digest))


if __name__ == "__main__":
    run()
