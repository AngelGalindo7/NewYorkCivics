"""Offline tests for digest.py render_markdown — action_context blurbs."""

from __future__ import annotations

from datetime import date

from ingest.deliver.digest import build_digest, render_markdown
from ingest.extract.schemas import Citation, CivicEvent, RecordStatus


def _cite(url: str) -> Citation:
    # A verified record always carries a source link; without one an item is flagged
    # needs-verification and cannot lead the digest.
    return Citation(kind="data_source", verifies="exact_record", label="record", url=url)


def _rezoning_event() -> CivicEvent:
    return CivicEvent(
        source_id="nyc_zap",
        source_record_id="P2024M0042",
        project_thread_id="zap:P2024M0042",
        action_type="rezoning",
        title="Zoning Map Amendment (C 240042 ZMM) — In Public Review",
        summary="Proposed rezoning of lots on East 116th Street.",
        deadline=date(2026, 7, 15),
        status=RecordStatus.ACCEPTED,
        confidence=1.0,
        citations=[_cite("https://example.com/zap/P2024M0042")],
    )


def _violation_event() -> CivicEvent:
    return CivicEvent(
        source_id="nyc_hpd",
        source_record_id="V-001",
        project_thread_id="hpd:V-001",
        action_type="violation",
        title="Class C violation — HPD",
        summary="Immediately hazardous condition.",
        status=RecordStatus.ACCEPTED,
        confidence=1.0,
        citations=[_cite("https://example.com/hpd/V-001")],
    )


_CONTEXT = {"rezoning": "A rezoning changes what can be built on specific lots."}
_SUBSCRIBER = {
    "email": "test@example.com",
    "address": "123 E 116th St",
    "bbl": "1016500030",
    "latitude": 40.7969,
    "longitude": -73.9410,
    "zip": "10029",
    "community_district": "111",
}


def _render(events: list[CivicEvent], action_context=None) -> str:
    from ingest.deliver.match import BAND_IN_YOUR_AREA

    matched = {BAND_IN_YOUR_AREA: events}
    digest = build_digest(_SUBSCRIBER, matched, asof=date(2026, 6, 23))
    return render_markdown(digest, action_context=action_context)


def test_rezoning_blurb_appears_when_context_supplied():
    md = _render([_rezoning_event()], action_context=_CONTEXT)
    assert "A rezoning changes what can be built on specific lots." in md


def test_rezoning_blurb_absent_when_no_context():
    md = _render([_rezoning_event()], action_context=None)
    assert "A rezoning changes what can be built on specific lots." not in md


def test_violation_gets_no_blurb_from_rezoning_context():
    # The context dict has no "violation" key — no blurb should appear.
    md = _render([_violation_event()], action_context=_CONTEXT)
    assert "A rezoning changes what can be built on specific lots." not in md


def test_blurb_appears_in_lead_section():
    # Land-use items with an open deadline go to the "Act on this" lead, where the
    # blurb is equally important for a reader deciding whether the item is relevant.
    md = _render([_rezoning_event()], action_context=_CONTEXT)
    lead_section = md.split("## Near you")[0] if "## Near you" in md else md
    assert "A rezoning changes what can be built on specific lots." in lead_section


def test_stale_deadline_item_excluded():
    """Item with deadline older than the lookback window must not appear anywhere in the digest."""
    from datetime import timedelta

    from ingest.deliver.digest import _OVERDUE_LOOKBACK_DAYS

    stale_deadline = date(2026, 6, 23) - timedelta(days=_OVERDUE_LOOKBACK_DAYS + 1)
    stale_event = CivicEvent(
        source_id="nyc_zap",
        source_record_id="STALE-001",
        project_thread_id="zap:STALE-001",
        action_type="rezoning",
        title="Old Rezoning Application",
        summary="A very old rezoning.",
        deadline=stale_deadline,
        event_date=date(2019, 1, 1),
        status=RecordStatus.ACCEPTED,
        confidence=1.0,
        citations=[_cite("https://example.com/old")],
    )
    md = _render([stale_event])
    assert "Old Rezoning Application" not in md


def test_recent_overdue_item_appears():
    """Item with deadline lapsed within the lookback window must still appear (Deadline passed)."""
    from datetime import timedelta

    from ingest.deliver.digest import _OVERDUE_LOOKBACK_DAYS

    recent_overdue = date(2026, 6, 23) - timedelta(days=_OVERDUE_LOOKBACK_DAYS - 1)
    event = CivicEvent(
        source_id="nyc_zap",
        source_record_id="RECENT-001",
        project_thread_id="zap:RECENT-001",
        action_type="rezoning",
        title="Recently Closed Rezoning",
        summary="A recently closed rezoning.",
        deadline=recent_overdue,
        event_date=recent_overdue - timedelta(days=5),
        status=RecordStatus.ACCEPTED,
        confidence=1.0,
        citations=[_cite("https://example.com/recent")],
    )
    md = _render([event])
    assert "Recently Closed Rezoning" in md


def test_no_empty_section_headers_in_near_you():
    """Every '####' heading inside 'Near you' must have body content before the next heading."""
    import re

    # rezoning_event has a future deadline → goes to "Act on this" (removed from near-you visible)
    # violation_event has no deadline → stays in near-you
    md = _render([_rezoning_event(), _violation_event()])
    near_you = md.split("## Near you", 1)[1] if "## Near you" in md else ""
    heading_pat = re.compile(r"^####\s+.+$", re.MULTILINE)
    headings = list(heading_pat.finditer(near_you))
    for i, m in enumerate(headings):
        start = m.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(near_you)
        between = near_you[start:end].strip()
        assert between, f"Empty heading in Near you: {m.group()!r}"


def test_citywide_council_hearing_not_in_lead():
    """council_hearing with no BBL and no address must not appear in 'Act on this'."""
    hearing = CivicEvent(
        source_id="nyc_legistar",
        source_record_id="event:12345",
        project_thread_id="legistar:event:12345",
        action_type="council_hearing",
        title="City Council Stated Meeting — 2026-07-16",
        summary="City Council session.",
        event_date=date(2026, 7, 16),
        status=RecordStatus.ACCEPTED,
        confidence=1.0,
        citations=[_cite("https://legistar.council.nyc.gov/MeetingDetail.aspx?ID=12345")],
    )
    md = _render([hearing])
    act_section = ""
    if "## Act on this" in md:
        after_act = md.split("## Act on this", 1)[1]
        act_section = after_act.split("##")[0] if "##" in after_act else after_act
    assert "City Council Stated Meeting" not in act_section


def test_local_hearing_with_address_in_lead():
    """land_use_hearing WITH a specific address must appear in 'Act on this'."""
    from ingest.deliver.match import BAND_IN_YOUR_AREA

    hearing = CivicEvent(
        source_id="nyc_zap",
        source_record_id="zap-hearing-001",
        project_thread_id="zap:P2024M0001",
        action_type="land_use_hearing",
        title="Land Use Hearing — 123 East 116th Street",
        summary="Public hearing on rezoning application.",
        address="123 East 116th Street",
        event_date=date(2026, 7, 16),
        status=RecordStatus.ACCEPTED,
        confidence=1.0,
        citations=[_cite("https://example.com/hearing")],
    )
    matched = {BAND_IN_YOUR_AREA: [hearing]}
    digest = build_digest(_SUBSCRIBER, matched, asof=date(2026, 6, 23))
    md = render_markdown(digest)
    act_section = ""
    if "## Act on this" in md:
        after_act = md.split("## Act on this", 1)[1]
        act_section = after_act.split("##")[0] if "##" in after_act else after_act
    assert "Land Use Hearing" in act_section
