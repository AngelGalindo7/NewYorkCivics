"""Stage 1 (Fetch) — severe 311 complaint enrichment via Socrata. NYC-SPECIFIC, STRUCTURED.

Single responsibility: pull recent, *severe* 311 service requests (housing-habitability and
safety complaints) from NYC Open Data (Socrata dataset ``erm2-nwe9``) for buildings the
digest already surfaces, aggregate them per building, and surface one low-key
*context-on-a-building* event per building. The dataset is clean structured JSON, so this
connector emits records directly and skips Parse and Extract entirely (structured feeds never
call the LLM).

Resident value: "in the last few months, neighbors filed 7 heat/hot-water complaints about
this building." A pattern of severe complaints is an accountability signal that threads onto
the same BBL as the building's permits and violations. It is *context*, not an action item:
there is nothing to attend by a date, so it never leads the "Act on this" section.

Honesty (the project's core discipline): a 311 service request is a *resident's report* to the
City, not a confirmed violation. The summary says so plainly and never asserts that the
reported condition exists -- only that complaints were filed, which is the verifiable fact.

Why an allowlist + aggregation (not every ticket): 311 is the noisiest civic feed -- noise,
parking, and quality-of-life complaints dominate the volume and are not building context. We
keep only a tight allowlist of habitability/safety complaint types, and we collapse a
building's tickets into ONE summary event (with row-exact links to the contributing tickets)
so a single building can never flood the digest with dozens of lines. The enrichment is also
bounded to the BBLs the digest already surfaces, so volume is capped by the building feed.

Shared machinery: the generic Socrata pull (:class:`~ingest.sources.nyc.dob_hpd.SocrataFeed` +
:func:`~ingest.sources.nyc.dob_hpd.iter_feed`) is reused rather than re-rolled -- one ticket is
"just another scoped row." Only the dataset id, the severe-type allowlist, the per-building
aggregation, and the recency window are new here.

Design notes
------------
- Structured in, so no LLM ever: the plain-English summary is a deterministic template.
- NYC-specific knowledge stays here: the dataset id, the complaint-type allowlist, and the
  recency window live in this connector.
- The summary threads onto ``bbl:<BBL>`` so it groups with a building's permits/violations;
  per-source quirks (the per-type counts) go in ``extras``.
- The fact that complaints were filed is verifiable, so ``confidence=1.0``,
  ``status=ACCEPTED`` -- but the wording is careful that a complaint is not a violation.
- The dataset is BBL-native and carries lat/lng, so the summary needs no geocoding and bands
  itself; ``source_record_id`` is the building's BBL (one summary per building).

================================ DECISION RECORD ================================
Severe-311 enrichment scope (2026-06-14) — SPEC_NEXT_PHASE §B.2 ("severity-filtered 311").

  WHAT: attach a per-building summary of recent severe 311 complaints to East Harlem buildings
        as ACCEPTED, row-cited context -- a direct accountability signal, carefully framed as
        reports rather than confirmed conditions.
  DATASET (verified live against NYC Open Data, 2026-06-14):
    - erm2-nwe9 "311 Service Requests from 2020 to Present": BBL-native (``bbl`` 10-digit),
      carries ``unique_key`` (per-ticket id), ``complaint_type``, ``descriptor``, ``status``,
      ``created_date`` (ISO), ``latitude``/``longitude``, ``community_board`` ("11 MANHATTAN").
  ALLOWLIST (confirmed against the live CB11 complaint-type vocabulary, 2026-06-14): the
        habitability/safety types only. Noise (the single largest category), parking, and
        quality-of-life complaints are excluded. DOB construction complaints are left to the
        DOB/ECB-violations connector to avoid double-signaling.
  AGGREGATION: one summary event per building (not per ticket), with up to
        ``_MAX_CITATIONS`` row-exact links to the most recent contributing tickets, over a
        ``DEFAULT_LOOKBACK_DAYS``-day window.
  ENRICHMENT, NOT A FIREHOSE: bounded to BBLs the digest already surfaces; weighted below a
        permit so a complaint cluster is context, not a headline.
=================================================================================
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ingest.extract.schemas import Citation, CivicEvent, RecordStatus
from ingest.observability import get_logger
from ingest.sources.nyc import citations
from ingest.sources.nyc.dob_hpd import SocrataFeed, iter_feed

log = get_logger(__name__)

SOURCE_ID_311 = "nyc_311_habitability"

# Socrata dataset id (the verifiable row link is built from this — see citations.py).
DATASET_311 = "erm2-nwe9"  # 311 Service Requests from 2020 to Present (bbl-native)

# Severe habitability/safety complaint types, as the exact strings the dataset stores
# (confirmed against the live CB11 vocabulary 2026-06-14). Noise/parking/quality-of-life are
# deliberately excluded as non-context; DOB construction types are left to the DOB/ECB
# connector. Tunable, NYC-specific.
SEVERE_COMPLAINT_TYPES = (
    "HEAT/HOT WATER",
    "PLUMBING",
    "WATER LEAK",
    "PAINT/PLASTER",
    "UNSANITARY CONDITION",
    "ELECTRIC",
    "FLOORING/STAIRS",
    "SAFETY",
    "Lead",
    "Elevator",
)

# Reader-facing plain-language labels for the allowlisted types (the digest must not ship raw
# bureaucratic strings). Anything not mapped falls back to a lowercased form.
_PLAIN_LABEL = {
    "HEAT/HOT WATER": "heat/hot water",
    "PLUMBING": "plumbing",
    "WATER LEAK": "water leak",
    "PAINT/PLASTER": "paint/plaster",
    "UNSANITARY CONDITION": "unsanitary conditions",
    "ELECTRIC": "electrical",
    "FLOORING/STAIRS": "flooring/stairs",
    "SAFETY": "safety",
    "Lead": "lead",
    "Elevator": "elevator",
}

DEFAULT_LOOKBACK_DAYS = 90  # only recent complaints are useful context
_MAX_TICKETS = 500  # hard fetch bound across all surfaced buildings (a safety cap)
_MAX_CITATIONS = 5  # row-exact links per building summary (most recent first)


def _sql_in(values: tuple[str, ...]) -> str:
    """Render a SoQL ``IN`` list of single-quoted trusted constants."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _valid_bbl(raw: Any) -> str | None:
    """Return a well-formed 10-digit BBL string, else ``None`` (fail soft, not guess)."""
    value = str(raw or "").strip()
    return value if len(value) == 10 and value.isdigit() else None


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "")).date()
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plain(complaint_type: str) -> str:
    return _PLAIN_LABEL.get(complaint_type, complaint_type.lower())


def _ticket_to_event(rec: Mapping[str, Any]) -> CivicEvent:
    """Map one raw 311 row to a per-ticket :class:`CivicEvent` (internal scaffolding).

    These per-ticket events are NOT emitted directly; :func:`discover_service_requests`
    aggregates them into one summary per building. Each carries its own row-exact citation so
    the summary can link back to the exact tickets it counts.
    """
    now = datetime.now(UTC)
    record_bbl = _valid_bbl(rec.get("bbl"))
    unique_key = str(rec.get("unique_key") or "").strip()
    complaint_type = (rec.get("complaint_type") or "").strip()
    addr = (rec.get("incident_address") or "").strip().title() or None

    ticket_citations: list[Citation] = []
    if unique_key:
        ticket_citations.append(
            citations.socrata_row(
                DATASET_311,
                "unique_key",
                unique_key,
                label=f"311 complaint #{unique_key} (NYC Open Data)",
                retrieved_at=now,
            )
        )

    return CivicEvent(
        source_id=SOURCE_ID_311,
        source_record_id=unique_key or f"{record_bbl}:{rec.get('created_date')}",
        bbl=record_bbl,
        action_type="service_request",  # per-ticket marker; the summary re-labels
        title=f"311 complaint: {_plain(complaint_type)}" if complaint_type else "311 complaint",
        address=addr,
        event_date=_parse_iso(rec.get("created_date")),
        latitude=_to_float(rec.get("latitude")),
        longitude=_to_float(rec.get("longitude")),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=ticket_citations,
        extras={
            "complaint_type": complaint_type,
            "descriptor": rec.get("descriptor"),
            "status": rec.get("status"),
            "agency": rec.get("agency"),
            "created_date": rec.get("created_date"),
        },
        extracted_at=now,
    )


def _summarize_building(bbl: str, tickets: list[CivicEvent]) -> CivicEvent:
    """Collapse one building's severe 311 tickets into a single context event.

    The summary states only the verifiable fact -- that N complaints were filed -- and is
    explicit that a 311 complaint is a resident report, not a confirmed violation. It carries
    up to :data:`_MAX_CITATIONS` row-exact links to the most recent contributing tickets. The
    caller (:func:`discover_service_requests`) guarantees a non-empty list of tickets that each
    carry a row-exact citation, so every summary ships with at least one verifiable link.
    """
    now = datetime.now(UTC)
    # Most recent first so the capped citations and the headline date reflect what's current.
    tickets = sorted(tickets, key=lambda t: t.event_date or date.min, reverse=True)
    n = len(tickets)
    addr = next((t.address for t in tickets if t.address), None)
    coords = next(
        ((t.latitude, t.longitude) for t in tickets if t.latitude is not None), (None, None)
    )
    most_recent = next((t.event_date for t in tickets if t.event_date), None)

    counts: dict[str, int] = defaultdict(int)
    for t in tickets:
        counts[str(t.extras.get("complaint_type") or "")] += 1
    # Breakdown ordered by frequency, in plain language (blank types dropped).
    ordered = [
        (ct, c) for ct, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True) if ct
    ]
    breakdown = ", ".join(f"{_plain(ct)} ({c})" for ct, c in ordered)
    top_label = _plain(ordered[0][0]) if ordered and ordered[0][0] else "various"

    summary = (
        f"Neighbors filed {n} severe 311 complaint{'s' if n != 1 else ''} about "
        f"{addr or 'this building'} in the last {DEFAULT_LOOKBACK_DAYS} days"
        + (f": {breakdown}" if breakdown else "")
        + "."
    )
    if most_recent is not None:
        summary += f" Most recent: {most_recent.isoformat()}."
    summary += " A 311 complaint is a resident's report to the City, not a confirmed violation."

    # The exact row backing each counted ticket (capped, most recent first).
    summary_citations: list[Citation] = []
    for t in tickets[:_MAX_CITATIONS]:
        summary_citations.extend(c for c in t.citations if c.kind == "data_source")

    return CivicEvent(
        source_id=SOURCE_ID_311,
        source_record_id=bbl,  # one summary per building
        bbl=bbl,
        # threads onto the building so the summary groups with its permits/violations
        project_thread_id=f"bbl:{bbl}",
        action_type="habitability_complaints",
        title=f"{n} recent 311 complaint{'s' if n != 1 else ''} ({top_label})",
        summary=summary,
        address=addr,
        # Context, not an action item: like the energy grade, it carries no event_date or
        # deadline, so it can never float into the forward-looking "Act on this" lead (a
        # complaint filed *today* would otherwise read as actionable). The most-recent date is
        # shown in the summary and kept in extras instead.
        event_date=None,
        latitude=coords[0],
        longitude=coords[1],
        confidence=1.0,  # the filing is a fact (the wording does not assert the condition)
        status=RecordStatus.ACCEPTED,
        citations=summary_citations,
        extras={
            "complaint_count": n,
            "complaint_breakdown": dict(ordered),
            "lookback_days": DEFAULT_LOOKBACK_DAYS,
            "most_recent": most_recent.isoformat() if most_recent else None,
        },
        extracted_at=now,
    )


SERVICE_REQUEST_FEED = SocrataFeed(
    source_id=SOURCE_ID_311,
    dataset_id=DATASET_311,
    primary_key=("unique_key",),
    mapper=_ticket_to_event,
    scope_where=f"complaint_type in {_sql_in(SEVERE_COMPLAINT_TYPES)}",
)


def discover_service_requests(
    bbls: Iterable[str] | None = None,
    *,
    asof: date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int | None = _MAX_TICKETS,
) -> Iterator[CivicEvent]:
    """Yield one severe-311 summary event per East Harlem building (deterministic, no LLM).

    Args:
        bbls: Restrict to these BBLs — the enrichment path. When the runner passes the BBLs
            already surfaced by the HPD/DOB/ZAP feeds, the volume is bounded by the building
            feed, so it can never firehose. ``None`` is treated as "no buildings to enrich"
            and yields nothing (this connector is enrichment-only — it does not do an
            unbounded neighborhood-wide pull, because 311 volume would be enormous).
        asof: Reference date for the recency window (default: today).
        lookback_days: Only complaints filed within this many days are counted.
        limit: Hard cap on raw tickets fetched across all buildings (a safety bound).

    Yields:
        One ACCEPTED summary :class:`CivicEvent` per building with >=1 severe complaint.
    """
    bbl_list = tuple(sorted({b for b in (bbls or ()) if _valid_bbl(b)}))
    if not bbl_list:
        return  # enrichment-only: no surfaced buildings -> nothing to do (never an open pull)

    asof = asof or date.today()
    cutoff = (asof - timedelta(days=lookback_days)).isoformat()
    where = f"bbl in {_sql_in(bbl_list)} AND created_date > '{cutoff}'"

    by_bbl: dict[str, list[CivicEvent]] = defaultdict(list)
    for ticket in iter_feed(SERVICE_REQUEST_FEED, where=where, limit=limit):
        # Only count tickets that can be verified: an ACCEPTED summary auto-ships (it skips the
        # human-review gate), so it must never name a building on the strength of a ticket with
        # no row-exact link. A ticket whose unique_key was blank carries no citation and is
        # dropped here, so every emitted summary ships with at least one verifiable link.
        if ticket.bbl and any(c.kind == "data_source" for c in ticket.citations):
            by_bbl[ticket.bbl].append(ticket)

    for bbl in sorted(by_bbl):
        yield _summarize_building(bbl, by_bbl[bbl])


# --------------------------------------------------------------------------- #
# Runnable demo: `python -m ingest.sources.nyc.service_requests`               #
# --------------------------------------------------------------------------- #
def _demo() -> None:
    # ASCII-only output so it prints on any console (Windows cp1252 included).
    # A few real East Harlem BBLs to exercise the enrichment path.
    sample_bbls = ["1016500030", "1016520001", "1017540001", "1016010001"]
    print("\n=== Severe 311 summaries for sample East Harlem buildings ===")
    for ev in discover_service_requests(bbls=sample_bbls):
        print(f"  {ev.title}  |  {ev.address or 'n/a'}  |  BBL {ev.bbl}")
        print(f"      {ev.summary}")
        print(f"      verify ({len(ev.citations)} link(s)):")
        for c in ev.citations[:3]:
            print(f"        - {c.label}: {c.url}")


if __name__ == "__main__":
    _demo()
