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

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from itertools import islice
from typing import Any

from ingest.deliver.digest import build_digest, render_markdown
from ingest.deliver.match import match_subscriber
from ingest.deliver.send import send_digest
from ingest.deliver.subscribers import load_subscribers
from ingest.extract import extractor
from ingest.extract.schemas import Citation, CivicEvent
from ingest.observability import get_logger
from ingest.parse import ParsedDoc, pdf_text
from ingest.sources.nyc import cb_agenda, corroborate, ulurp_packet
from ingest.sources.nyc.building_grades import discover_energy_grades
from ingest.sources.nyc.dob_hpd import (
    DOB_NOW_PERMITS_FEED,
    DOB_PERMITS_FEED,
    HPD_VIOLATIONS_FEED,
    _dob_permit_to_event,
    _hpd_violation_to_event,
    discover_displacement_signals,
    iter_feed,
)
from ingest.sources.nyc.legistar import (
    discover_cd_hearings,
    enrich_with_agenda,
    find_matter_by_ulurp,
)
from ingest.sources.nyc.permitted_events import discover_permitted_events
from ingest.sources.nyc.service_requests import discover_service_requests
from ingest.sources.nyc.zap_api import _zap_project_to_event, iter_zap_events

log = get_logger(__name__)

# NYC-specific acronym glossary — passed to render_markdown so each term is defined
# inline on its first appearance in the email body; subsequent uses stay as-is.
NYC_ACRONYMS: dict[str, str] = {
    "J-51": "a property tax break that preserves affordable rents",
    "MIH": "Mandatory Inclusionary Housing — a zoning rule requiring affordable units",
    "PACT": "Permanent Affordability Commitment Together — a HUD program converting public housing",
    "ULURP": "Uniform Land Use Review Procedure — the city's land-use approval process",
    "CEQR": "City Environmental Quality Review",
    "421-a": "a tax exemption tied to affordable unit requirements",
    "485-x": "a post-2022 affordable housing tax deal",
    "SLA": "State Liquor Authority",
}

# Plain-English background blurbs keyed by canonical action_type — passed to
# render_markdown so each land-use item carries a brief explanation of what that
# category of application means and who it typically affects. Lets a reader decide
# whether the item is relevant to them without needing to research it separately.
NYC_LAND_USE_CONTEXT: dict[str, str] = {
    "rezoning": (
        "A rezoning changes what can legally be built on specific lots — it may allow"
        " taller or denser buildings, new commercial uses, or different uses than current"
        " rules permit. Property owners, renters in surrounding buildings, and nearby"
        " businesses are all typically affected."
    ),
    "special_permit": (
        "A special permit allows a use the current zoning doesn't automatically permit —"
        " such as a hotel, large parking facility, certain retail stores, or community"
        " facilities — if specific conditions are met. The City Planning Commission reviews"
        " these and may attach requirements meant to protect the surrounding neighborhood."
    ),
    "variance": (
        "A variance lets a specific property deviate from the standard zoning rules — for"
        " example, building taller than the allowed height limit or using land in a way the"
        " zoning doesn't normally allow. The Board of Standards and Appeals reviews these."
    ),
    "authorization": (
        "An authorization allows a use or design feature that zoning rules permit only under"
        " specific circumstances, reviewed by City Planning. Less formal than a full rezoning"
        " but can still affect what gets built on the affected lots."
    ),
    "certification": (
        "A certification confirms that a proposed project meets the zoning rules' technical"
        " requirements — primarily an administrative check. It affects what can be built but"
        " typically does not involve a public hearing."
    ),
    "urban_renewal": (
        "An urban renewal or UDAAP action involves the city acquiring or redeveloping"
        " property, which can displace existing residents or businesses in the affected area."
        " These actions go through the full ULURP public review process."
    ),
    "environmental_review": (
        "An environmental review (CEQR) assesses how a proposed development would affect the"
        " surrounding area — traffic, noise, air quality, shadows, and potential displacement"
        " of residents or businesses. This typically runs alongside a major rezoning or"
        " development application."
    ),
    "site_selection": (
        "A site selection determines where a city facility — a school, shelter, office, or"
        " other public use — will be located in the neighborhood. The decision affects nearby"
        " residents and businesses."
    ),
}

# Resident action prompts — keyed by canonical action_type, passed to render_markdown.
# Each value answers: "what can I actually do about this item?"
# NYC-SPECIFIC (knows CB11 phone, CM office, 311, HPD, SLA process). Lives here, not in
# the city-agnostic Deliver stage (Rule 4).
NYC_ACTION_CONTACTS: dict[str, str] = {
    "rezoning": (
        "CB11 must hold a public hearing — call 212-831-8929 to sign up to speak,"
        " or check manhattancb11.org for upcoming hearing dates and written-comment instructions."
    ),
    "special_permit": (
        "CB11 must hold a public hearing — call 212-831-8929 to sign up to speak before the vote."
    ),
    "variance": (
        "The Board of Standards and Appeals reviews this application."
        " Submit written comments to bsa@buildings.nyc.gov or appear at the BSA public hearing."
    ),
    "authorization": (
        "City Planning reviews this — call CB11 at 212-831-8929 for upcoming comment opportunities."
    ),
    "land_use_hearing": (
        "To testify at this hearing, contact the committee chair in advance."
        " Written testimony can be submitted to the NYC Council Clerk at"
        " council.nyc.gov/committees."
    ),
    "council_hearing": (
        "To submit testimony, contact the relevant City Council committee."
        " Written comments can be filed with the NYC Council Clerk at council.nyc.gov/committees."
    ),
    "sla_license": (
        "CB11's SLA committee reviews applications and can request conditions or file an objection."
        " Call 212-831-8929 to learn the next committee meeting date,"
        " or email the CB11 office to submit written comments within the 30-day window."
    ),
    "violation": (
        "Call 311 (or 311online.nyc.gov) to confirm the violation is on record."
        " HPD must inspect Class C (immediately hazardous) violations within 24 hours."
        " If a landlord ignores a repair order, tenants can call the HPD Emergency Repairs"
        " line: 212-863-7900."
    ),
    "permit": (
        "For construction safety concerns (blocked exits, falling debris, unsafe scaffold),"
        " call 311 immediately. For questions about what a permit allows, contact CB11's"
        " Land Use committee at 212-831-8929."
    ),
    "displacement_signal": (
        "If your building has hazardous violations and a major construction permit, contact"
        " a tenant organizer: CASA (Community Action for Safe Apartments) at 212-234-2098"
        " or Met Council on Housing at 212-979-0611."
    ),
}

# Plain-English "why this matters to you" lines, keyed by canonical action_type — passed to
# render_markdown so each item connects to the reader's own life, not just the record. This
# is the angle a database printout never gives. NYC-SPECIFIC phrasing, so it lives here, not
# in the city-agnostic Deliver stage.
NYC_WHY_MATTERS: dict[str, str] = {
    "violation": (
        "If your own apartment has similar problems, this shows the City does act on them —"
        " and a documented violation is leverage when you ask your landlord for repairs."
    ),
    "displacement_signal": (
        "Hazardous conditions plus major construction on the same building can be an early"
        " sign of pressure on rent-regulated tenants — worth watching if you live nearby."
    ),
    "habitability_complaints": (
        "A cluster of reports about heat, hot water, or plumbing on your block can mean a"
        " building-wide problem — useful context if you're dealing with the same issue."
    ),
    "permit": (
        "Major construction nearby can mean noise, sidewalk changes, or a building being"
        " repositioned — and on a building with violations, it can signal pressure on tenants."
    ),
    "rezoning": (
        "A rezoning can reshape what gets built around you for years — and these often include"
        " affordable apartments you may be eligible to apply for."
    ),
    "land_use_application": (
        "Land-use decisions made now set what your block looks like later; the public-comment"
        " window is the point where a resident's voice still counts."
    ),
    "building_energy_grade": (
        "A low energy grade often tracks with deferred maintenance — a useful signal about how"
        " a building is being kept up."
    ),
}

# Dev-mode fallback subscriber used only when no real subscriber has been registered.
# In production every row comes from add_subscriber() (signup -> geocode -> CSV).
SAMPLE_SUBSCRIBER = {
    "email": "neighbor@example.com",
    "address": "123 East 116th Street, New York, NY 10029",
    "bbl": "1016500030",
    "latitude": 40.7969,
    "longitude": -73.9410,
    "zip": "10029",
    "community_district": "111",
    "council_member": "Salaam",
}


# Primary Council Member by 3-digit community_district string. Manhattan CD11 (East Harlem)
# spans Council Districts 8 and 11; the majority of the district is represented by CD8.
_CD_TO_COUNCIL_MEMBER: dict[str, str] = {
    "111": "Salaam",  # Manhattan CD11 — primarily Council District 8 (Yusef Salaam)
}


def _get_subscriber() -> dict[str, Any]:
    """Return the first registered subscriber from CSV, or the dev-mode sample as fallback.

    A real subscriber is one whose row was written by add_subscriber() — geocoded address,
    real email. Falls back to SAMPLE_SUBSCRIBER only when the CSV is absent or empty, and
    logs loudly so it's obvious no real reader exists yet.
    """
    subs = load_subscribers()
    if subs:
        sub = subs[0]
        if "council_member" not in sub:
            cm = _CD_TO_COUNCIL_MEMBER.get(str(sub.get("community_district", "")))
            if cm:
                sub = {**sub, "council_member": cm}
        return sub
    log.warning(
        "No subscribers found in out/subscribers.csv — using built-in sample subscriber "
        "(dev mode). Run add_subscriber() to register a real reader."
    )
    return SAMPLE_SUBSCRIBER


_RECENT_PERMITS = (
    "job_type in ('A1','NB','DM') AND (issuance_date like '%2025' or issuance_date like '%2026')"
)


def _link_zap_to_legistar(events: list[CivicEvent]) -> list[CivicEvent]:
    """Enrich ZAP events that carry a ULURP number with their Legistar matter ID.

    When a match is found, appends the Legistar matter to project_thread_id so
    downstream stages can join the two sources on one project story. For CPC-stage
    projects, also queries GET /Matters/{id}/Histories for a scheduled hearing date
    and overrides the approximated deadline if one is found.

    Fails soft: a lookup failure never drops the ZAP event.
    """
    from ingest.sources.nyc.legistar import _get_all, _parse_event_date

    result: list[CivicEvent] = []
    for ev in events:
        if ev.source_id != "nyc_zap" or ev.ulurp_number is None:
            result.append(ev)
            continue
        try:
            matter = find_matter_by_ulurp(ev.ulurp_number)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ULURP->matter lookup failed for %s (%s); keeping original event",
                ev.ulurp_number,
                exc,
            )
            result.append(ev)
            continue
        if matter is None:
            result.append(ev)
            continue

        matter_id: int = matter["MatterId"]
        project_id: str = ev.extras.get("project_id") or ev.source_record_id
        new_thread_id = f"zap:{project_id}|legistar:matter:{matter_id}"
        updates: dict[str, Any] = {"project_thread_id": new_thread_id}

        # For CPC-stage projects, try to replace the approximated deadline with a
        # confirmed Legistar hearing date.
        if ev.extras.get("cpc_stage") == "cpc_review":
            try:
                histories = _get_all(f"/Matters/{matter_id}/Histories")
                for h in histories:
                    action_name = (h.get("MatterHistoryActionName") or "").lower()
                    if "hearing" in action_name:
                        hearing_date = _parse_event_date(h.get("MatterHistoryActionDate"))
                        if hearing_date is not None:
                            updates["deadline"] = hearing_date
                            updates["citations"] = [
                                *ev.citations,
                                Citation(
                                    kind="data_source",
                                    verifies="corroborating_record",
                                    label=f"NYC Council Matter #{matter_id} (Legistar)",
                                    url=(
                                        "https://legistar.council.nyc.gov/"
                                        f"LegislationDetail.aspx?ID={matter_id}"
                                    ),
                                    retrieved_at=datetime.now(UTC),
                                ),
                            ]
                            updates["extras"] = {
                                **ev.extras,
                                "cpc_stage": "cpc_hearing_scheduled",
                                "legistar_matter_id": matter_id,
                            }
                            break
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "CPC hearing date lookup failed for matter %s (%s); keeping approximation",
                    matter_id,
                    exc,
                )

        result.append(ev.model_copy(update=updates))
    return result


def gather_live_events(
    *,
    per_feed: int = 6,
    include_signal: bool = False,
    signals: int = 3,
    include_zap: bool = True,
    include_legistar: bool = True,
    legistar_days: int = 30,
    include_grades: bool = False,
    include_311: bool = False,
    include_cb_agenda: bool = False,
    include_ulurp_packet: bool = False,
    max_pages: int = 20,
    include_agenda_enrichment: bool = False,
    include_votes: bool = False,
    include_vote_history: bool = False,
    vote_history_since: date | None = None,
    include_dob_now: bool = True,
    include_permitted_events: bool = False,
    permitted_events_limit: int = 5,
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

    ``include_grades`` (enabled by the runner) enriches the buildings already surfaced
    above with their Local Law 33 energy letter grade, surfacing only the below-average
    (D/F) grades as low-key building context. It is bounded to the surfaced BBLs, so it
    never firehoses; coordinates are carried over from the surfaced event so the grade
    threads into the same proximity band as the building's permits/violations.

    ``include_311`` (enabled by the runner) attaches a per-building summary of recent
    severe 311 complaints (habitability/safety types only) to the surfaced buildings.
    Like the grades, it is bounded to the surfaced BBLs and is low-key context, never a
    headline -- and it is framed as resident reports, not confirmed violations.

    ``include_cb_agenda`` (off by default) fetches CB11 board meeting agenda PDFs and
    runs the full parse → extract chain to surface meeting items as CivicEvents.

    ``include_ulurp_packet`` (off by default) fetches primary ULURP packet PDFs for active
    East Harlem land-use applications and runs the full parse → extract chain. Because
    packets are typically hundreds of pages, only the first ``max_pages`` pages are sent
    to the extractor (the actionable summary, hearing dates, and affected addresses are
    concentrated in the opening pages).

    ``max_pages`` (default 20) caps the pages of each ULURP packet that reach the LLM
    extractor. Has no effect on the agenda leg (agendas are small documents).

    ``include_vote_history`` (off by default) does a matter-first historical scan of
    Legistar for housing/land-use votes since ``vote_history_since`` (default 90 days
    back). This is a full Legistar scan and is heavy — keep it off in the default path.
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
    events = _link_zap_to_legistar(events)
    if include_legistar:
        # Phase 1 gate: "upcoming hearings in CD X returns correct dates for next 30 days."
        legistar_events = discover_cd_hearings("MN11", days_ahead=legistar_days)
        if include_agenda_enrichment:
            enriched: list[CivicEvent] = []
            for ev in legistar_events:
                try:
                    enriched.append(enrich_with_agenda(ev))
                except Exception as exc:  # noqa: BLE001 — fail soft; keep original
                    log.warning("agenda enrichment skipped for %s (%s)", ev.source_record_id, exc)
                    enriched.append(ev)
            legistar_events = enriched
        if include_votes:
            from ingest.sources.nyc.legistar import discover_stated_meeting_votes

            vote_events: list[CivicEvent] = []
            for ev in legistar_events:
                body = ev.extras.get("body_name", "").lower()
                if "stated" in body or "city council" in body:
                    eid_str = ev.source_record_id
                    if ":" in eid_str:
                        try:
                            eid = int(eid_str.split(":")[1])
                            vote_events += discover_stated_meeting_votes(eid)
                        except Exception as exc:  # noqa: BLE001 — fail soft
                            log.warning(
                                "vote discovery skipped for event %s (%s)",
                                ev.source_record_id,
                                exc,
                            )
            events += vote_events
        events += legistar_events
    if include_vote_history:
        from ingest.sources.nyc.legistar import iter_cm_vote_history

        history_since = vote_history_since or (datetime.now(UTC).date() - timedelta(days=90))
        try:
            events += list(iter_cm_vote_history(history_since))
        except Exception as exc:  # noqa: BLE001
            log.warning("vote history scan skipped (%s)", exc)
    if include_dob_now:
        try:
            # DOB NOW covers most current permits that the legacy BIS dataset (ipu4-2q9a) misses.
            dob_now_events = list(
                iter_feed(DOB_NOW_PERMITS_FEED, limit=per_feed, order="approved_date DESC")
            )
            events += dob_now_events
            log.info("dob_now: fetched %d permit(s)", len(dob_now_events))
        except Exception as exc:  # fail soft — legacy DOB feed still runs
            log.warning("DOB NOW feed skipped (%s)", exc)
    if include_signal:
        events += list(islice(discover_displacement_signals(), signals))
    # Enrichments are additive context: a fetch failure on a supplementary feed must never
    # discard the core digest, so each fails soft (warn + skip) while the base feeds fail loud.
    if include_grades:
        try:
            events += _enrich_with_energy_grades(events)
        except Exception as exc:  # network/API hiccup on a supplementary feed -> skip it
            log.warning("energy-grade enrichment skipped (%s)", exc)
    if include_311:
        try:
            events += _enrich_with_service_requests(events)
        except Exception as exc:  # network/API hiccup on a supplementary feed -> skip it
            log.warning("311 enrichment skipped (%s)", exc)
    if include_cb_agenda:
        from ingest.extract.extractor import LLMUnavailableError as _LLMUnavailable

        try:
            llm_failures = 0
            for agenda_ref in cb_agenda.discover_agendas("MN11"):
                if llm_failures >= 2:
                    log.warning(
                        "cb_agenda: LLM unavailable on 2 consecutive agendas;"
                        " skipping remaining PDFs this run"
                    )
                    break
                try:
                    pdf_bytes = cb_agenda.fetch(agenda_ref.url)
                except Exception as exc:
                    log.warning("cb_agenda fetch failed for %s (%s)", agenda_ref.url, exc)
                    continue
                try:
                    doc = pdf_text.extract_text(pdf_bytes)
                    agenda_events = extractor.extract(doc, source_id=cb_agenda.SOURCE_ID)
                    llm_failures = 0  # reset on successful extraction
                    events += agenda_events
                    log.info(
                        "cb_agenda: extracted %d event(s) from %s (%s)",
                        len(agenda_events),
                        agenda_ref.url,
                        agenda_ref.meeting_date or "date unknown",
                    )
                except _LLMUnavailable as exc:
                    llm_failures += 1
                    log.warning(
                        "cb_agenda: LLM unavailable for %s (%s); consecutive failures: %d",
                        agenda_ref.meeting_date or agenda_ref.url,
                        exc,
                        llm_failures,
                    )
                except Exception as exc:
                    log.warning("cb_agenda parse/extract failed for %s (%s)", agenda_ref.url, exc)
        except Exception as exc:
            log.warning("cb_agenda discovery skipped (%s)", exc)
    if include_ulurp_packet:
        try:
            for packet_ref in ulurp_packet.discover_packets():
                try:
                    pdf_bytes = ulurp_packet.fetch(packet_ref.url)
                except Exception as exc:
                    log.warning("ulurp_packet fetch failed for %s (%s)", packet_ref.url, exc)
                    continue
                try:
                    doc = pdf_text.extract_text(pdf_bytes)
                    # Packets are hundreds of pages; only the opening pages contain
                    # the actionable summary, hearing dates, and affected addresses.
                    if max_pages and doc.layout and len(doc.layout) > max_pages:
                        char_limit = (
                            sum(p.char_count for p in doc.layout[:max_pages])
                            + max(0, max_pages - 1) * 2
                        )
                        doc = ParsedDoc(
                            text=doc.text[:char_limit],
                            layout=doc.layout[:max_pages],
                        )
                    packet_events = extractor.extract(doc, source_id=ulurp_packet.SOURCE_ID)
                    events += packet_events
                    log.info(
                        "ulurp_packet: extracted %d event(s) from %s (%s)",
                        len(packet_events),
                        packet_ref.url,
                        packet_ref.ulurp_number,
                    )
                except Exception as exc:
                    log.warning(
                        "ulurp_packet parse/extract failed for %s (%s)", packet_ref.url, exc
                    )
        except Exception as exc:
            log.warning("ulurp_packet discovery skipped (%s)", exc)
    if include_permitted_events:
        try:
            permitted = discover_permitted_events(limit=permitted_events_limit)
            events += permitted
            log.info("permitted_events: fetched %d event(s)", len(permitted))
        except Exception as exc:
            log.warning("permitted events feed skipped (%s)", exc)
    zap_events = [e for e in events if e.source_id == "nyc_zap"]
    # Reconcile dirty extractions against the authoritative ZAP records in three passes:
    # thread ULURP-less land-use extractions onto ZAP projects by street address
    # (conservative, exactly-one-candidate), corroborate identifiers, then drop dirty
    # duplicates of ZAP records — first transferring any hearing/comment date only the
    # dirty event carried onto the ZAP event as a flagged unverified-date note.
    events = corroborate.thread_dirty_by_address(events, zap_events)
    events = corroborate.corroborate_against_zap(events, zap_events)
    events = corroborate.dedup_dirty_against_zap(events)

    # Only surface major-work permits (new building or major alteration).  Sidewalk
    # sheds, scaffolding, and minor alterations are routine maintenance — not the
    # signal a civic digest should lead on.
    events = [
        ev
        for ev in events
        if ev.action_type != "permit" or ev.extras.get("job_type") in ("A1", "NB")
    ]

    # Drop Legistar hearings held at generic City Hall / 250 Broadway venues.  Those
    # are citywide proceedings with no East Harlem agenda — they clutter the digest
    # until agenda enrichment can confirm a local item is on the docket.
    _DOWNTOWN_VENUES = {"250 broadway", "city hall"}
    events = [
        ev
        for ev in events
        if not (
            ev.action_type in ("council_hearing", "land_use_hearing")
            and any(
                v in (ev.extras.get("location") or ev.address or "").lower()
                for v in _DOWNTOWN_VENUES
            )
        )
    ]

    return events


def _enrich_surfaced(
    events: list[CivicEvent],
    discover: Callable[..., Iterable[CivicEvent]],
) -> list[CivicEvent]:
    """Run a BBL-keyed enrichment over the buildings already surfaced, backfilling coords.

    The enrichment ``discover(bbls=...)`` is looked up only for the BBLs the base feeds
    already surfaced, so it is bounded by the building feed and cannot firehose. An enrichment
    event that arrives without coordinates (e.g. an energy grade) inherits the lat/lng of a
    co-located surfaced event on the same BBL — it is literally the same building — so it lands
    in the same proximity band and groups with that building's events. An enrichment that
    already carries coordinates (e.g. a 311 summary) is left untouched.
    """
    coords_by_bbl: dict[str, tuple[float, float]] = {
        ev.bbl: (ev.latitude, ev.longitude)
        for ev in events
        if ev.bbl and ev.latitude is not None and ev.longitude is not None
    }
    bbls = {ev.bbl for ev in events if ev.bbl}
    enriched: list[CivicEvent] = []
    for ev in discover(bbls=bbls):
        if (ev.latitude is None or ev.longitude is None) and (
            coords := coords_by_bbl.get(ev.bbl or "")
        ):
            ev = ev.model_copy(update={"latitude": coords[0], "longitude": coords[1]})
        enriched.append(ev)
    return enriched


def _enrich_with_energy_grades(events: list[CivicEvent]) -> list[CivicEvent]:
    """Pull D/F energy grades for the buildings already surfaced, threaded to their BBL."""
    return _enrich_surfaced(events, discover_energy_grades)


def _enrich_with_service_requests(events: list[CivicEvent]) -> list[CivicEvent]:
    """Summarize recent severe 311 complaints for the buildings already surfaced."""
    return _enrich_surfaced(events, discover_service_requests)


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
    # with the HPD/DOB events into one building group as low-key context.
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
    # A severe-311 summary on the subscriber's own building (same BBL), built from inline
    # tickets through the real aggregation — threads as low-key context, framed as reports.
    from ingest.sources.nyc.service_requests import _summarize_building, _ticket_to_event

    tickets = [
        _ticket_to_event(
            {
                "unique_key": "DEMO311001",
                "bbl": "1016500030",
                "complaint_type": "HEAT/HOT WATER",
                "descriptor": "ENTIRE BUILDING",
                "status": "Closed",
                "incident_address": "123 EAST 116 STREET",
                "created_date": "2026-05-20T08:00:00.000",
                "latitude": "40.7969",
                "longitude": "-73.9410",
            }
        ),
        _ticket_to_event(
            {
                "unique_key": "DEMO311002",
                "bbl": "1016500030",
                "complaint_type": "PLUMBING",
                "status": "Open",
                "incident_address": "123 EAST 116 STREET",
                "created_date": "2026-06-02T08:00:00.000",
            }
        ),
    ]
    complaints = _summarize_building("1016500030", tickets)
    return [v, p, nb, signal, z, grade, complaints]


def gather_events() -> tuple[list[CivicEvent], bool]:
    """Return (events, is_live). Falls back to sample data if the API is unreachable."""
    try:
        # Energy-grade and severe-311 enrichments are on: both are structured, row-cited,
        # ACCEPTED context bounded to the surfaced buildings, and fail soft, so they are safe
        # in the live path.
        events = gather_live_events(
            include_grades=True,
            include_311=False,
            # Agenda enrichment now uses a public HTML scrape (no token needed) when the
            # event carries a GUID from the web-calendar scraper. Fails soft per event.
            include_agenda_enrichment=True,
            # CB11 agendas: fetch + parse are live; extraction requires GOOGLE_API_KEY with
            # Gemini quota — fails soft (warns + skips) if quota is exhausted.
            include_cb_agenda=True,
            # ZAP Heroku API (zap-api-production.herokuapp.com) is publicly accessible;
            # discover_packets() queries it per project to find the LR-Item narrative PDF.
            include_ulurp_packet=True,
            include_dob_now=True,
            # Permitted events geocodes every Manhattan event to find CD-11 ones; slow
            # when few events are nearby but fails soft and adds resident-friendly context.
            include_permitted_events=True,
        )
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
    subscriber = _get_subscriber()
    matched = match_subscriber(subscriber, events)
    council_member: str | None = subscriber.get("council_member")  # type: ignore[assignment]
    digest = build_digest(
        subscriber,
        matched,
        asof=datetime.now(UTC).date(),
        subscriber_council_member=council_member,
    )

    # Attach the NYC plain-English help text to the digest itself so it travels with the
    # digest through the human-review round-trip (dump -> reviewer process -> send) and the
    # delivered email keeps its term definitions and "how to respond" prompts. The renderer
    # reads these from render_options; they are NYC-specific and so live here, not in the
    # city-agnostic Deliver stage.
    digest["render_options"] = {
        "glossary": NYC_ACRONYMS,
        "action_context": NYC_LAND_USE_CONTEXT,
        "action_contacts": NYC_ACTION_CONTACTS,
        "why_matters": NYC_WHY_MATTERS,
        "hearing_guidance": (
            "CB11 must hold a public hearing on this application."
            " To comment, call CB11 at 212-831-8929."
        ),
        "subscriber_council_member": council_member,
    }

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
            pending_path = dump_pending(digest, subscriber)
            print(f"\nHUMAN-REVIEW QUEUE: {len(digest['review_items'])} item(s) need review:")
            for title in digest["review_items"]:
                print(f"  - {title}")
            print(f"\nSaved pending digest to: {pending_path}")
            print("Run the reviewer to clear it and send:")
            print("  python -m ingest.deliver.review")
            return

    path = send_digest(digest, subscriber)
    print(f"\nDigest written to: {path}\n")
    print("----- rendered body -----")
    # render_options ride along on the digest, so the preview matches the delivered email.
    print(render_markdown(digest))


if __name__ == "__main__":
    run()
