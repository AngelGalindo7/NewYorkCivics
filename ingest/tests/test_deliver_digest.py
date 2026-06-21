"""Contract tests for the Deliver path (rank -> match -> digest -> send).

Runs fully offline on the sample East Harlem events (no Socrata, no DB). Locks the
trust-critical behavior: confidence routing (Rule 10), the human-review gate
(Rule 9), actionability ordering, and that every claim carries verifiable links.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from ingest.deliver import rank
from ingest.deliver.digest import build_digest, render_markdown
from ingest.deliver.match import BAND_ON_YOUR_BLOCK, match_subscriber
from ingest.sources.nyc.harlem_digest import SAMPLE_SUBSCRIBER, _sample_events

ASOF = date(2026, 5, 31)


@pytest.fixture
def digest():
    matched = match_subscriber(SAMPLE_SUBSCRIBER, _sample_events())
    return build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)


def _all_items(digest):
    return [
        it
        for section in digest["sections"]
        for building in section["buildings"]
        for it in building["items"]
    ]


def test_rank_score_is_weighted_sum():
    signals = {"proximity": 1.0, "deadline_urgency": 1.0}  # others default to 0
    expected = rank.DEFAULT_WEIGHTS["w_d"] + rank.DEFAULT_WEIGHTS["w_dl"]
    assert rank.score(signals) == pytest.approx(expected)


def test_confidence_routing_marks_only_review_items(digest):
    # Rule 10: only the REVIEW-status displacement signal needs verification;
    # ACCEPTED structured records (HPD/DOB) are shown as verified facts.
    review = [it["title"] for it in _all_items(digest) if it["needs_verification"]]
    assert len(review) == 1
    assert "displacement" in review[0].lower()


def test_every_item_carries_verifiable_citations(digest):
    for it in _all_items(digest):
        assert it["citations"], f"{it['title']} has no source links"
        kinds = {c["kind"] for c in it["citations"]}
        assert "data_source" in kinds  # the exact, machine-verifiable row


def test_overdue_hpd_deadline_surfaces_as_attention(digest):
    hpd = next(it for it in _all_items(digest) if it["title"] == "HPD Class C violation")
    assert hpd["deadline_note"] is not None
    assert "overdue" in hpd["deadline_note"]
    assert digest["needs_attention_count"] >= 1


def test_same_bbl_event_lands_on_your_block():
    matched = match_subscriber(SAMPLE_SUBSCRIBER, _sample_events())
    titles = [e.title or "" for e in matched[BAND_ON_YOUR_BLOCK]]
    assert any("displacement" in t.lower() for t in titles)


def test_events_on_one_building_are_threaded_into_one_group():
    # Two events sharing a BBL must collapse into a single building group (Rule 7),
    # not appear as two separate top-level entries. The DOB permit events (issuance
    # dates in the past, no deadline) are filtered out by the pure-past-event filter,
    # so the surviving on-block events are the HPD violation and the displacement signal,
    # both on BBL 1016500030 -> they still form one group.
    from ingest.deliver.digest import _group_buildings, _to_item, build_digest
    from ingest.deliver.match import match_subscriber as ms

    matched = ms(SAMPLE_SUBSCRIBER, _sample_events())
    block = [_to_item(ev, BAND_ON_YOUR_BLOCK, ASOF) for ev in matched[BAND_ON_YOUR_BLOCK]]
    # build_digest applies the filter; check the groups from the actual digest sections.
    dig = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    block_section = next((s for s in dig["sections"] if s["band"] == BAND_ON_YOUR_BLOCK), None)
    assert block_section is not None
    groups = block_section["buildings"]
    # HPD violation + displacement signal are both BBL 1016500030 -> one group.
    assert len(groups) < len(block)
    assert max(len(g["items"]) for g in groups) >= 2


def test_citation_audit_passes_for_every_sample_link():
    # Rule 5: prove "verifiable" is structural, not just a label.
    from ingest.sources.nyc.citations import audit_citation

    problems = [
        (ev.title, audit_citation(c))
        for ev in _sample_events()
        for c in ev.citations
        if audit_citation(c) is not None
    ]
    assert problems == [], f"malformed citations: {problems}"


def test_honest_footer_does_not_overclaim(digest):
    body = render_markdown(digest)
    n = digest["item_count"]
    exact = digest["exact_verifiable_count"]
    if exact < n:
        assert "link to the exact City record" in body
        assert "Every update above links" not in body


def test_review_gate_blocks_send_until_cleared(tmp_path: Path, digest, monkeypatch):
    from ingest.deliver.send import send_digest

    # Force the human-review gate ON regardless of the developer's local .env. Set the
    # var to a false value rather than deleting it: get_settings() reloads .env via
    # load_dotenv on every call, which would re-inject a *deleted* BYPASS_HUMAN_REVIEW
    # from a developer's .env; an already-present env var wins over the file, so this
    # keeps the gate test deterministic locally and in CI alike.
    monkeypatch.setenv("BYPASS_HUMAN_REVIEW", "false")

    # Rule 9: a digest with unreviewed items must not send.
    assert digest["review_required"] is True
    with pytest.raises(ValueError):
        send_digest(digest, SAMPLE_SUBSCRIBER, sink_dir=tmp_path)

    digest["review_required"] = False  # human cleared the queue
    path = send_digest(digest, SAMPLE_SUBSCRIBER, sink_dir=tmp_path)
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert "[needs verification]" in body  # the review item is flagged in the body
    assert "data.cityofnewyork.us" in body  # verifiable links rendered


def test_render_is_nonempty_markdown(digest):
    body = render_markdown(digest)
    assert body.startswith("# ")
    assert "On your block" in body


def test_act_on_this_leads_before_near_you(digest):
    # The forward-looking lead must sit above the proximity feed so the most useful
    # thing (what you can still act on) is the first thing a reader sees.
    body = render_markdown(digest)
    act_idx = body.index("## Act on this")
    near_idx = body.index("## Near you")
    assert act_idx < near_idx
    # And both sit below the personalization line / stats hook at the very top.
    assert body.index("For the address you gave us") < act_idx


def test_act_on_this_holds_future_hearing_not_overdue(digest):
    # The future rezoning hearing (deadline 2026-06-30) is the still-actionable item;
    # the overdue-only HPD violation (deadline 2026-05-10) must not lead.
    lead_titles = [it["title"] for it in digest["lead_items"]]
    assert any("Zoning Map Amendment" in t for t in lead_titles)
    assert not any(t == "HPD Class C violation" for t in lead_titles)
    # Sorted soonest-first by the still-open action date.
    dates = [it["actionable_date"] for it in digest["lead_items"]]
    assert dates == sorted(dates)


def test_displacement_never_leads_and_is_flagged(digest):
    # The displacement signal must never read as a forward-looking headline: it stays
    # in its building thread, flagged needs-verification, never in "Act on this".
    lead_titles = [it["title"].lower() for it in digest["lead_items"]]
    assert not any("displacement" in t for t in lead_titles)

    displacement = [it for it in _all_items(digest) if "displacement" in it["title"].lower()]
    assert len(displacement) == 1
    assert displacement[0]["needs_verification"] is True


def _review_event_with_future_deadline(asof=ASOF):
    # A needs-verification record (REVIEW status) that DOES carry a future, still-open
    # deadline — so its actionable_date is non-None and only the verification guard
    # keeps it out of the lead. This is the case the displacement fixture cannot
    # exercise (that signal has no date), so it isolates the verification guard.
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    return CivicEvent(
        source_id="test_src",
        source_record_id="REVIEW-FUTURE-1",
        bbl=SAMPLE_SUBSCRIBER["bbl"],
        action_type="hearing",
        title="Tentative rezoning hearing (unconfirmed)",
        summary="Low-confidence record awaiting human review.",
        address=SAMPLE_SUBSCRIBER["address"],
        deadline=asof + timedelta(days=10),
        confidence=0.5,
        status=RecordStatus.REVIEW,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="source row",
                url="https://example.com/row/REVIEW-FUTURE-1",
            )
        ],
    )


def test_needs_verification_item_with_future_date_is_kept_out_of_lead():
    # Guards the confidence-routing lever independently of the no-date path: an item
    # that is needs-verification AND has a future actionable date must NOT lead, even
    # though its date alone would qualify it. Deleting the verification clause from
    # _is_actionable must fail here.
    from ingest.deliver.digest import _is_actionable, _to_item

    item = _to_item(_review_event_with_future_deadline(), BAND_ON_YOUR_BLOCK, ASOF)
    # Date guard alone is satisfied — so this isolates the verification guard.
    assert item["actionable_date"] is not None
    assert item["needs_verification"] is True
    assert _is_actionable(item) is False

    # End-to-end: it is excluded from the lead but still threaded into its building.
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [_review_event_with_future_deadline()])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    lead_titles = [it["title"] for it in digest["lead_items"]]
    assert not any("unconfirmed" in t.lower() for t in lead_titles)
    threaded = [
        it["title"]
        for section in digest["sections"]
        for building in section["buildings"]
        for it in building["items"]
    ]
    assert any("unconfirmed" in t.lower() for t in threaded)


def test_speakable_stat_counts_a_hearing_in_the_lead():
    # The "hearing you can still speak at" stat must reflect a real, verified hearing
    # that lands in the lead — not silently miss it. A future, ACCEPTED hearing on the
    # subscriber's building is speakable and actionable, so the stat clause must count it.
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    hearing = CivicEvent(
        source_id="test_src",
        source_record_id="HEARING-1",
        bbl=SAMPLE_SUBSCRIBER["bbl"],
        action_type="land_use_hearing",
        title="Land Use Committee hearing",
        summary="Upcoming hearing a resident can testify at.",
        address=SAMPLE_SUBSCRIBER["address"],
        event_date=ASOF + timedelta(days=7),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="hearing record",
                url="https://example.com/hearing/HEARING-1",
            )
        ],
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [hearing])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    lead_types = [it["action_type"] for it in digest["lead_items"]]
    assert "land_use_hearing" in lead_types  # it reached the lead
    line = digest["stats_line"]
    assert line is not None
    assert "1 upcoming hearing you can still speak at" in line


def test_stats_top_line_is_whole_numbers_no_percent(digest):
    # Numeracy guardrail: the scannable hook uses natural frequencies (whole counts),
    # never a percentage or rate.
    line = digest["stats_line"]
    assert line is not None
    assert "%" not in line
    body = render_markdown(digest)
    assert line in body
    # Every count in the line is a bare integer (no decimals, no "%").
    import re

    for token in re.findall(r"\d+(?:\.\d+)?", line):
        assert "." not in token


def test_actionable_date_rules():
    # Direct coverage of the load-bearing forward-looking predicate, including the
    # "a lapsed deadline closes the window even if something happened recently" branch
    # that the end-to-end fixture only exercises indirectly.
    from ingest.deliver.digest import _actionable_date

    future = ASOF + timedelta(days=5)
    sooner = ASOF + timedelta(days=2)
    past = ASOF - timedelta(days=5)

    assert _actionable_date(None, future, ASOF) == future  # future deadline is the date
    assert _actionable_date(future, past, ASOF) is None  # lapsed deadline closes the window
    assert _actionable_date(future, None, ASOF) == future  # no deadline -> future event_date
    assert _actionable_date(past, None, ASOF) is None  # no deadline, past event -> nothing open
    assert _actionable_date(sooner, future, ASOF) == future  # a present deadline governs
    assert _actionable_date(None, ASOF, ASOF) == ASOF  # today still counts as open


def test_when_phrase_plain_words():
    from ingest.deliver.digest import _when_phrase

    assert _when_phrase(ASOF, ASOF) == "today"
    assert _when_phrase(ASOF + timedelta(days=1), ASOF) == "tomorrow"
    assert _when_phrase(ASOF + timedelta(days=5), ASOF) == "in 5 days"
    # The lead only feeds futures, but a non-future date must degrade gracefully.
    assert _when_phrase(ASOF - timedelta(days=3), ASOF) == "today"


def test_category_weight_covers_the_hearing_family():
    # A real Land-Use / Council hearing must be weighted as a hearing, not silently
    # dropped to the neutral default just because its taxonomy value is not "hearing".
    from ingest.deliver.digest import _category_weight

    assert _category_weight("hearing") == 0.7
    assert _category_weight("land_use_hearing") == 0.7
    assert _category_weight("council_hearing") == 0.7
    assert _category_weight("rezoning") == 0.9
    assert _category_weight("special_permit") == 0.9
    assert _category_weight("violation") == 0.6
    assert _category_weight("permit") == 0.5
    assert _category_weight("displacement_signal") == 1.0
    assert _category_weight(None) == 0.4
    assert _category_weight("brand_new_action_type") == 0.4


def _accepted_hearing(record_id: str, title: str, deadline: date):
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    return CivicEvent(
        source_id="test_src",
        source_record_id=record_id,
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="land_use_hearing",
        title=title,
        summary="Upcoming hearing a resident can testify at.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        deadline=deadline,
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="hearing record",
                url=f"https://example.com/hearing/{record_id}",
            )
        ],
    )


def test_lead_is_sorted_soonest_first():
    # Two still-open items with different future dates exercise real ordering (the
    # single-item sample makes `sorted(dates) == dates` vacuously true).
    soon = _accepted_hearing("SOON", "Sooner hearing", ASOF + timedelta(days=3))
    later = _accepted_hearing("LATER", "Later hearing", ASOF + timedelta(days=20))

    matched = match_subscriber(SAMPLE_SUBSCRIBER, [later, soon])  # deliberately out of order
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)

    lead_titles = [it["title"] for it in digest["lead_items"]]
    assert lead_titles == ["Sooner hearing", "Later hearing"]
    dates = [it["actionable_date"] for it in digest["lead_items"]]
    assert dates == sorted(dates) and dates[0] != dates[-1]  # genuinely ascending, not equal


def _accepted_event(record_id: str, title: str, *, event_date: date, deadline: date | None = None):
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    return CivicEvent(
        source_id="test_src",
        source_record_id=record_id,
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="permit",
        title=title,
        summary="A civic event for testing.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=event_date,
        deadline=deadline,
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="source row",
                url=f"https://example.com/row/{record_id}",
            )
        ],
    )


def test_pure_past_event_is_dropped():
    # An event that happened in the past with no deadline gives the reader nothing to
    # act on and should be filtered out of all digest sections.
    past = _accepted_event(
        "PAST-1",
        "Already happened permit",
        event_date=ASOF - timedelta(days=10),
        deadline=None,
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [past])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    assert digest["item_count"] == 0

    # An event with a lapsed deadline IS kept (appears in overdue/sections, not lead).
    overdue = _accepted_event(
        "OVERDUE-KEEP-1",
        "Lapsed deadline permit",
        event_date=ASOF - timedelta(days=5),
        deadline=ASOF - timedelta(days=3),
    )
    matched2 = match_subscriber(SAMPLE_SUBSCRIBER, [overdue])
    digest2 = build_digest(SAMPLE_SUBSCRIBER, matched2, asof=ASOF)
    assert digest2["item_count"] == 1
    assert len(digest2["overdue_items"]) == 1


def test_happened_this_week_section():
    # An event from 3 days ago with no deadline is a pure past event — but it was
    # filtered in as "Happened this week" only if the filter logic keeps it.
    # Actually per A2: pure past events (event_date < asof, deadline=None) ARE dropped
    # from all_items. The "Happened this week" subsection draws from the surviving
    # all_items (those with lapsed deadlines or no event_date). A past permit WITH a
    # lapsed deadline falls in "Deadline passed", not "Happened this week", because its
    # deadline condition triggers the overdue filter first.
    # What actually goes to "Happened this week": items with a lapsed deadline AND
    # event_date within the last 7 days (they survive the filter because deadline is not None).
    recent_with_deadline = _accepted_event(
        "RECENT-DL-1",
        "Recent application with closed window",
        event_date=ASOF - timedelta(days=3),
        deadline=ASOF - timedelta(days=1),
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [recent_with_deadline])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    assert len(digest["recent_items"]) == 1
    body = render_markdown(digest)
    assert "Happened this week" in body


def test_far_future_item_goes_to_later_not_lead():
    # An item with an open action window more than 60 days out must move to "Later"
    # rather than cluttering the urgent "Act on this" lead.
    far = _accepted_event(
        "FAR-1",
        "Far-future zoning hearing",
        event_date=ASOF + timedelta(days=90),
        deadline=ASOF + timedelta(days=90),
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [far])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)

    lead_titles = [it["title"] for it in digest["lead_items"]]
    assert "Far-future zoning hearing" not in lead_titles

    later_titles = [it["title"] for it in digest["later_items"]]
    assert "Far-future zoning hearing" in later_titles

    body = render_markdown(digest)
    assert "### Later" in body
    later_section = body.split("### Later")[1]
    assert "Far-future zoning hearing" in later_section


def test_deadline_passed_section_appears_and_lead_excludes_overdue():
    # An ACCEPTED event with a recently lapsed deadline must NOT appear in "Act on this"
    # (actionable_date is None for lapsed deadlines) but MUST appear in "Deadline passed".
    overdue = _accepted_event(
        "OVERDUE-1",
        "Recently expired permit application",
        event_date=ASOF - timedelta(days=10),
        deadline=ASOF - timedelta(days=5),
    )
    # A very old lapsed deadline (beyond 90-day lookback) must NOT appear in the section.
    ancient = _accepted_event(
        "ANCIENT-1",
        "Old expired application",
        event_date=ASOF - timedelta(days=92),
        deadline=ASOF - timedelta(days=91),
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [overdue, ancient])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)

    lead_titles = [it["title"] for it in digest["lead_items"]]
    assert "Recently expired permit application" not in lead_titles

    overdue_titles = [it["title"] for it in digest["overdue_items"]]
    assert "Recently expired permit application" in overdue_titles
    assert "Old expired application" not in overdue_titles

    body = render_markdown(digest)
    assert "## Deadline passed" in body
    assert "Recently expired permit application" in body.split("## Deadline passed")[1]
    assert "Act on this" not in body  # no open action windows -> no lead section
