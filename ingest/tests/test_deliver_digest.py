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
    hpd = next(
        it
        for it in _all_items(digest)
        if it["title"] == "Immediately hazardous violation (Class C) — HPD"
    )
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
    from ingest.deliver.digest import _to_item, build_digest
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


def _ai_item_no_citation(rid, bbl, *, addr):
    # An AI reading of a public document: ACCEPTED status but NO source link. It must never
    # render as a confirmed City record.
    from ingest.extract.schemas import CivicEvent, RecordStatus

    return CivicEvent(
        source_id="nyc_cb_agenda",
        source_record_id=rid,
        bbl=bbl,
        action_type="land_use_application",
        title=f"Proposed 40-unit development at {addr}",
        summary="Read from the community board agenda PDF.",
        address=addr,
        confidence=0.9,
        status=RecordStatus.ACCEPTED,  # high confidence, but unverifiable without a link
        citations=[],
    )


def test_item_without_source_link_is_flagged_needs_verification():
    # An item with no citation cannot be checked, so it must be flagged regardless of its
    # status — it can never read as authoritative as a linked City record.
    digest = build_digest(
        SAMPLE_SUBSCRIBER,
        {BAND_ON_YOUR_BLOCK: [_ai_item_no_citation("AG1", "1000000099", addr="9 AGENDA ST")]},
        asof=ASOF,
    )
    item = _all_items(digest)[0]
    assert item["needs_verification"] is True
    # It enters the human-review queue (the gate must see an unverifiable claim).
    assert digest["review_required"] is True

    body = render_markdown(digest)
    assert "[needs verification]" in body
    assert "no City record links this yet" in body  # explicit inline marker


def test_footer_does_not_claim_a_link_for_unlinked_items():
    # The footer must describe link-less items truthfully, never claiming they "link to a
    # search tool" (the false-promise the fact-checker flagged).
    events = [
        _violation("HAZ", "1000000001", hazardous=True, addr="1 HAZARD ST"),  # exact link
        _ai_item_no_citation("AG1", "1000000002", addr="2 AGENDA ST"),  # no link
    ]
    digest = build_digest(SAMPLE_SUBSCRIBER, {BAND_ON_YOUR_BLOCK: events}, asof=ASOF)
    body = render_markdown(digest)
    assert digest["linked_count"] == 1 and digest["item_count"] == 2
    # The footer names the unlinked item honestly instead of promising a search link for it.
    assert "no City record to link yet" in body
    assert "the rest link to an official search tool" not in body


def test_building_label_uses_a_real_address_over_a_bare_bbl():
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    # A reader can't place "BBL 1000000050". When any record on the building carries a street
    # address, the building header uses it — even if the most-actionable record does not.
    def _ev(rid: str, addr: str | None) -> CivicEvent:
        return CivicEvent(
            source_id="s",
            source_record_id=rid,
            bbl="1000000050",
            action_type="building_energy_grade",
            title=f"Record {rid}",
            summary="Context record.",
            address=addr,
            confidence=1.0,
            status=RecordStatus.ACCEPTED,
            citations=[
                Citation(
                    kind="data_source", verifies="exact_record", label="r", url=f"https://x/{rid}"
                )
            ],
        )

    # Same BBL -> one group: one record has no street address, the other does.
    digest = build_digest(
        SAMPLE_SUBSCRIBER,
        {BAND_ON_YOUR_BLOCK: [_ev("A", None), _ev("B", "50 REAL STREET")]},
        asof=ASOF,
    )
    body = render_markdown(digest)
    assert "50 REAL STREET" in body
    assert "BBL 1000000050" not in body


def test_why_this_matters_line_renders_from_render_options_and_kwarg():
    # The plain-English "why this matters to you" line connects an item to the reader's life.
    events = [_violation("V", "1000000060", hazardous=True, addr="60 HAZARD ST")]

    # Embedded on the digest (the production path).
    d1 = build_digest(SAMPLE_SUBSCRIBER, {BAND_ON_YOUR_BLOCK: events}, asof=ASOF)
    d1["render_options"] = {"why_matters": {"violation": "This is leverage with your landlord."}}
    assert "**Why this matters:** This is leverage with your landlord." in render_markdown(d1)

    # Explicit kwarg overrides / works on its own.
    d2 = build_digest(SAMPLE_SUBSCRIBER, {BAND_ON_YOUR_BLOCK: events}, asof=ASOF)
    body2 = render_markdown(d2, why_matters={"violation": "Direct kwarg line."})
    assert "**Why this matters:** Direct kwarg line." in body2


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
    # The sample's only building carries a confirmed Class C violation, so it leads the
    # digest under "Right next to you" rather than sitting in the proximity feed.
    assert "## Right next to you" in body


def test_at_risk_building_leads_then_act_on_this(digest):
    # A confirmed serious violation near the reader is the most consequential item, so its
    # building leads the digest; the still-actionable hearing follows in "Act on this". Both
    # sit below the personalization line / stats hook at the very top.
    body = render_markdown(digest)
    at_risk_idx = body.index("## Right next to you")
    act_idx = body.index("## Act on this")
    assert body.index("For the address you gave us") < at_risk_idx < act_idx


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


def _violation(rid, bbl, *, hazardous, addr, status=None):
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    return CivicEvent(
        source_id="test_hpd",
        source_record_id=rid,
        bbl=bbl,
        action_type="violation",
        title="Housing-maintenance violation",
        summary=f"HPD cited {addr}.",
        address=addr,
        event_date=ASOF - timedelta(days=3),
        deadline=ASOF - timedelta(days=1),  # overdue -> not actionable, stays in the thread
        confidence=1.0,
        status=status or RecordStatus.ACCEPTED,
        citations=[
            Citation(kind="data_source", verifies="exact_record", label="r", url=f"https://x/{rid}")
        ],
        extras={"hazardous": hazardous},
    )


def _simple_event(rid, bbl, action_type, *, addr):
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    return CivicEvent(
        source_id="test_src",
        source_record_id=rid,
        bbl=bbl,
        action_type=action_type,
        title=f"{action_type} at {addr}",
        summary=f"{action_type} record for {addr}.",
        address=addr,
        event_date=ASOF - timedelta(days=2),
        deadline=ASOF - timedelta(days=1),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(kind="data_source", verifies="exact_record", label="r", url=f"https://x/{rid}")
        ],
        extras={},
    )


def test_only_confirmed_hazardous_violation_building_leads():
    # The lead trigger is a confirmed serious violation. A building with only a permit, or
    # only a complaint, must NOT lead — a permit alone isn't alarming and a complaint is an
    # unconfirmed report. Only the building with the hazardous violation is at-risk.
    events = [
        _violation("HAZ1", "1000000001", hazardous=True, addr="1 HAZARD ST"),
        _simple_event("PERM1", "1000000002", "permit", addr="2 PERMIT AVE"),
        _simple_event("CMPL1", "1000000003", "habitability_complaints", addr="3 COMPLAINT RD"),
    ]
    digest = build_digest(SAMPLE_SUBSCRIBER, {BAND_ON_YOUR_BLOCK: events}, asof=ASOF)
    assert digest["at_risk_building_keys"] == ["bbl:1000000001"]

    body = render_markdown(digest)
    assert "## Right next to you" in body
    # Isolate just the "Right next to you" section (up to the next ## heading).
    at_risk_block = body.split("## Right next to you", 1)[1].split("\n## ", 1)[0]
    assert "1 HAZARD ST" in at_risk_block
    assert "2 PERMIT AVE" not in at_risk_block
    assert "3 COMPLAINT RD" not in at_risk_block


def test_non_hazardous_or_unconfirmed_violation_does_not_lead():
    from ingest.extract.schemas import RecordStatus

    # A verified-but-routine violation is not lead-worthy; neither is an unconfirmed
    # (needs-verification) violation, even if it would be hazardous — a headline must be a
    # confirmed fact.
    events = [
        _violation("ROUTINE", "1000000010", hazardous=False, addr="10 ROUTINE ST"),
        _violation(
            "UNCONF", "1000000011", hazardous=True, addr="11 UNCONF ST", status=RecordStatus.REVIEW
        ),
    ]
    digest = build_digest(SAMPLE_SUBSCRIBER, {BAND_ON_YOUR_BLOCK: events}, asof=ASOF)
    assert digest["at_risk_building_keys"] == []
    assert "## Right next to you" not in render_markdown(digest)


def test_future_deadline_hazard_no_empty_header_no_duplicate():
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    # A hazardous violation with a FUTURE correct-by date is actionable, so it leads
    # "Act on this". The "Right next to you" header must not print empty, and the item must
    # not also re-render in "Later"/"Happened this week".
    ev = CivicEvent(
        source_id="test_hpd",
        source_record_id="FUT1",
        bbl="1000000020",
        action_type="violation",
        title="Future hazard violation",
        summary="Cited, correct-by ahead.",
        address="20 FUTURE ST",
        event_date=ASOF - timedelta(days=1),
        deadline=ASOF + timedelta(days=5),  # future -> actionable -> leads "Act on this"
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(kind="data_source", verifies="exact_record", label="r", url="https://x/FUT1")
        ],
        extras={"hazardous": True},
    )
    digest = build_digest(SAMPLE_SUBSCRIBER, {BAND_ON_YOUR_BLOCK: [ev]}, asof=ASOF)
    body = render_markdown(digest)
    assert "## Right next to you" not in body  # nothing to show there -> no empty header
    assert "## Act on this" in body
    assert body.count("Future hazard violation") == 1  # rendered exactly once


def test_at_risk_item_is_not_shown_twice(digest):
    # An overdue hazardous violation that leads "Right next to you" must not also appear in
    # "Deadline passed" or "Near you" — each event is shown exactly once. The violation item's
    # title is its fingerprint (the displacement signal cites the same record but reads
    # differently), so the title must appear exactly once in the whole body.
    body = render_markdown(digest)
    assert body.count("Immediately hazardous violation (Class C)") == 1


def test_near_you_feed_caps_buildings_and_links_the_remainder():
    from ingest.deliver.digest import FEED_NEAR_YOU_CAP
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    # A busy week with more buildings than the cap: only the closest FEED_NEAR_YOU_CAP
    # render in full; the rest are summarized in a compact "More nearby" list that still
    # links each building's record (nothing hidden behind a dead link).
    def _bldg(i: int) -> CivicEvent:
        # Date-less context records (no deadline, no event_date) live only in the feed —
        # not the lead, not "Deadline passed", not "Happened this week".
        return CivicEvent(
            source_id="test_src",
            source_record_id=f"B{i}",
            bbl=f"100000{i:04d}",
            action_type="building_energy_grade",
            title=f"Building grade {i}",
            summary=f"Grade for building {i}.",
            address=f"{i} FEED ST",
            confidence=1.0,
            status=RecordStatus.ACCEPTED,
            citations=[
                Citation(
                    kind="data_source",
                    verifies="exact_record",
                    label=f"r{i}",
                    url=f"https://x/B{i}",
                )
            ],
        )

    events = [_bldg(i) for i in range(FEED_NEAR_YOU_CAP + 2)]  # two over the cap
    digest = build_digest(SAMPLE_SUBSCRIBER, {BAND_ON_YOUR_BLOCK: events}, asof=ASOF)
    body = render_markdown(digest)

    assert "## Near you" in body
    near = body.split("## Near you", 1)[1]
    full_part, _, remainder_part = near.partition("### More nearby")
    # Exactly the cap renders in full (one "####" building header each).
    assert full_part.count("#### ") == FEED_NEAR_YOU_CAP
    # The two over the cap are summarized as compact linked one-liners.
    assert "### More nearby" in body
    assert remainder_part.count("https://x/B") == 2


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


def test_hearing_guidance_appended_for_liquor_item():
    # hearing_guidance must appear after a liquor-license hearing item; it must NOT
    # appear for a non-liquor hearing (we don't want unrelated CB guidance polluting
    # items that aren't about liquor licenses).
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    def _hearing_ev(record_id: str, title: str, summary: str = "") -> CivicEvent:
        return CivicEvent(
            source_id="test_src",
            source_record_id=record_id,
            bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
            action_type="council_hearing",
            title=title,
            summary=summary,
            address=str(SAMPLE_SUBSCRIBER["address"]),
            event_date=ASOF + timedelta(days=5),
            confidence=1.0,
            status=RecordStatus.ACCEPTED,
            citations=[
                Citation(
                    kind="data_source",
                    verifies="exact_record",
                    label="hearing",
                    url=f"https://example.com/{record_id}",
                )
            ],
        )

    liquor = _hearing_ev("LQ-1", "SLA liquor license application", "Application for beer garden.")
    non_liquor = _hearing_ev("NL-1", "Zoning variance hearing", "Height variance request.")

    guidance = "CB11 must hold a public hearing. Call 212-831-8929."

    matched_lq = match_subscriber(SAMPLE_SUBSCRIBER, [liquor])
    digest_lq = build_digest(SAMPLE_SUBSCRIBER, matched_lq, asof=ASOF)
    body_lq = render_markdown(digest_lq, hearing_guidance=guidance)
    assert guidance in body_lq

    matched_nl = match_subscriber(SAMPLE_SUBSCRIBER, [non_liquor])
    digest_nl = build_digest(SAMPLE_SUBSCRIBER, matched_nl, asof=ASOF)
    body_nl = render_markdown(digest_nl, hearing_guidance=guidance)
    assert guidance not in body_nl


def test_glossary_expands_on_first_use_only():
    # Acronyms defined in the glossary must be expanded inline on their FIRST appearance
    # in the rendered body; subsequent uses of the same acronym must remain unexpanded.
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    ev = CivicEvent(
        source_id="test_src",
        source_record_id="GLOSSARY-1",
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="land_use_hearing",
        title="ULURP hearing: ULURP application review",
        summary="Community board review under ULURP procedures.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=ASOF + timedelta(days=7),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="ULURP record",
                url="https://example.com/ulurp/GLOSSARY-1",
            )
        ],
    )
    glossary = {"ULURP": "Uniform Land Use Review Procedure — the city's land-use approval process"}
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    body = render_markdown(digest, glossary=glossary)

    # Exactly one expansion should appear; subsequent occurrences stay as bare "ULURP".
    expansion = "ULURP (Uniform Land Use Review Procedure — the city's land-use approval process)"
    assert expansion in body
    assert body.count(expansion) == 1

    # Without glossary, no expansion.
    body_no_gloss = render_markdown(digest)
    assert "(Uniform Land Use Review Procedure" not in body_no_gloss


def test_render_options_embedded_in_digest_reach_the_body():
    # Rendering options carried on the digest under render_options must be honored, so the
    # plain-English help text reaches a digest rendered in a separate process (the reviewer).
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    ev = CivicEvent(
        source_id="test_src",
        source_record_id="EMBED-1",
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="land_use_hearing",
        title="ULURP hearing: application review",
        summary="Community board review under ULURP procedures.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=ASOF + timedelta(days=7),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="ULURP record",
                url="https://example.com/ulurp/EMBED-1",
            )
        ],
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    digest["render_options"] = {
        "glossary": {"ULURP": "Uniform Land Use Review Procedure"},
    }

    # No explicit kwarg: the embedded glossary must still expand the acronym on first use.
    body = render_markdown(digest)
    assert "ULURP (Uniform Land Use Review Procedure)" in body

    # An explicit kwarg overrides the embedded value for that option.
    body_override = render_markdown(digest, glossary={"ULURP": "overridden definition"})
    assert "ULURP (overridden definition)" in body_override
    assert "ULURP (Uniform Land Use Review Procedure)" not in body_override


def test_corroboration_note_when_permit_and_violation():
    from ingest.deliver.digest import _corroboration_note

    permit_item = {"action_type": "permit"}
    violation_item = {"action_type": "violation"}
    complaint_item = {"action_type": "habitability_complaints"}

    # Permit + violation -> note mentions both "permit" and "violation".
    note = _corroboration_note([permit_item, violation_item])
    assert note is not None
    assert "permit" in note
    assert "violation" in note

    # Permit + complaint -> note mentions "permit" and "complaint".
    note2 = _corroboration_note([permit_item, complaint_item])
    assert note2 is not None
    assert "permit" in note2
    assert "complaint" in note2

    # Permit only -> None.
    assert _corroboration_note([permit_item]) is None

    # Violation only (no permit) -> None.
    assert _corroboration_note([violation_item]) is None


def test_address_populated_from_extras_primary_address():
    # When the top-level address field is absent, the item's address must be filled from
    # extras["primary_address"] so building thread labels show a street address, not a raw BBL.
    from ingest.deliver.digest import _to_item
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    ev = CivicEvent(
        source_id="test_src",
        source_record_id="ADDR-1",
        bbl="1016500030",
        action_type="permit",
        title="Permit without address field",
        summary="Test event.",
        address=None,
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        extras={"primary_address": "123 MAIN ST"},
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="permit",
                url="https://example.com/permit/ADDR-1",
            )
        ],
    )
    item = _to_item(ev, BAND_ON_YOUR_BLOCK, ASOF)
    assert item["address"] == "123 MAIN ST"


def test_street_event_permit_stays_out_of_lead():
    # An EW (Equipment Work) permit is outdoor/temporary street-level work — it belongs
    # in the building thread as context but must never lead the digest as an action item.
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    street = CivicEvent(
        source_id="test_src",
        source_record_id="STREET-1",
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="permit",
        title="Sidewalk shed permit",
        summary="Equipment Work permit for sidewalk shed installation.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=ASOF + timedelta(days=5),
        deadline=ASOF + timedelta(days=30),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        extras={"permit_type": "EW"},
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="DOB permit",
                url="https://example.com/permit/STREET-1",
            )
        ],
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [street])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)

    lead_titles = [it["title"] for it in digest["lead_items"]]
    assert "Sidewalk shed permit" not in lead_titles

    # Must still appear in sections (building threads).
    all_section_titles = [
        it["title"]
        for section in digest["sections"]
        for building in section["buildings"]
        for it in building["items"]
    ]
    assert "Sidewalk shed permit" in all_section_titles


def test_lead_item_not_duplicated_in_near_you():
    # An item that appears in "Act on this" must not also render in "Near you" — seeing
    # the same building update twice in one email is confusing and wastes the reader's attention.
    upcoming = _accepted_event(
        "DEDUP-1",
        "Permit with upcoming expiry",
        event_date=ASOF + timedelta(days=10),
        deadline=ASOF + timedelta(days=10),
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [upcoming])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    assert any(it["title"] == "Permit with upcoming expiry" for it in digest["lead_items"])

    body = render_markdown(digest)
    # The title should appear exactly once: in "Act on this", not again under "Near you".
    assert body.count("Permit with upcoming expiry") == 1
    act_section = body.split("## Act on this")[1].split("## Near you")[0]
    assert "Permit with upcoming expiry" in act_section


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


# ══════════════════════════════════════════════════════════════════════════════
# B3 — council vote roll call rendering
# ══════════════════════════════════════════════════════════════════════════════


def _council_vote_event(record_id: str, roll_call: dict[str, str]):
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    return CivicEvent(
        source_id="test_src",
        source_record_id=record_id,
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="council_vote",
        title="Council vote on affordable housing bill",
        summary="Roll-call vote recorded.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        extras={"roll_call": roll_call},
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="NYC Council Roll Call",
                url="https://example.com/vote/1",
            )
        ],
    )


def test_council_vote_renders_roll_call_in_item():
    roll = {"Rivera": "Affirmative", "Powers": "Affirmative", "Salaam": "Negative"}
    ev = _council_vote_event("VOTE-1", roll)
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF, subscriber_council_member="Salaam")
    body = render_markdown(digest, subscriber_council_member="Salaam")
    assert "Council Member Rivera voted Affirmative" in body
    assert "Council Member Salaam voted Negative" in body


def test_subscriber_council_member_vote_rendered_first_and_bold():
    roll = {"Rivera": "Affirmative", "Salaam": "Negative"}
    ev = _council_vote_event("VOTE-2", roll)
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF, subscriber_council_member="Salaam")
    body = render_markdown(digest, subscriber_council_member="Salaam")
    salaam_pos = body.index("Salaam")
    rivera_pos = body.index("Rivera")
    assert salaam_pos < rivera_pos  # subscriber's member appears first
    assert "**Council Member Salaam voted Negative**" in body  # bolded


def test_hpd_title_uses_plain_english(digest):
    body = render_markdown(digest)
    assert "immediately hazardous" in body.lower()
    assert "Class C" in body
    assert "HPD Class C violation" not in body


def test_dob_permit_title_plain_english_leads():
    # Past permits without a deadline are filtered from the rendered body; check the
    # title directly on the source event before the filter runs.
    from ingest.sources.nyc.harlem_digest import _sample_events

    permit = next(
        e
        for e in _sample_events()
        if e.action_type == "permit" and e.extras.get("job_type") == "A1"
    )
    lower = (permit.title or "").lower()
    assert "major alteration" in lower
    assert lower.index("major alteration") < lower.index("(dob a1)")


def test_zap_summary_does_not_contain_bare_ulurp_label(digest):
    body = render_markdown(digest)
    assert "ULURP:" not in body


def test_stats_line_uses_walkable_area_framing(digest):
    # Violations and permits within block/neighbourhood band carry the "5-minute walk"
    # prefix; the claim is scoped to items we actually know are that close.
    line = digest["stats_line"]
    assert line is not None
    assert "within a 5-minute walk" in line.lower()


# ══════════════════════════════════════════════════════════════════════════════
# Citation strength badges (CHANGE 1)
# ══════════════════════════════════════════════════════════════════════════════


def test_citation_badge_exact_record_renders_checkmark():
    # An exact_record citation must append " ✓" so a reader can immediately see
    # this link goes to the definitive city record, not a search fallback.
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    ev = CivicEvent(
        source_id="test_src",
        source_record_id="BADGE-EXACT-1",
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="permit",
        title="Permit with exact record citation",
        summary="A permit event.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=ASOF + timedelta(days=5),
        deadline=ASOF + timedelta(days=30),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="DOB permit row",
                url="https://data.cityofnewyork.us/permit/1",
            )
        ],
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    body = render_markdown(digest)
    assert "DOB permit row ✓" in body
    assert "DOB permit row (search)" not in body


def test_citation_badge_search_renders_search_label():
    # A search-strength citation must append " (search)" so the reader knows this
    # link queries a search tool, not a direct record link.
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    ev = CivicEvent(
        source_id="test_src",
        source_record_id="BADGE-SEARCH-1",
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="permit",
        title="Permit with search citation",
        summary="A permit event.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=ASOF + timedelta(days=5),
        deadline=ASOF + timedelta(days=30),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="search",
                label="DOB building search",
                url="https://data.cityofnewyork.us/search/1",
            )
        ],
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    body = render_markdown(digest)
    assert "DOB building search (search)" in body
    assert "DOB building search ✓" not in body


def test_citation_badge_in_lead_item():
    # _render_lead_item must also apply the strength badge (not just _render_item).
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    ev = CivicEvent(
        source_id="test_src",
        source_record_id="BADGE-LEAD-1",
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="land_use_hearing",
        title="Hearing with exact citation",
        summary="Upcoming hearing.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=ASOF + timedelta(days=7),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="Legistar matter",
                url="https://legistar.council.nyc.gov/matter/1",
            )
        ],
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    body = render_markdown(digest)
    # The item lands in the "Act on this" lead section.
    assert any("Hearing with exact citation" in it["title"] for it in digest["lead_items"])
    assert "Legistar matter ✓" in body


# ══════════════════════════════════════════════════════════════════════════════
# [needs verification] footnote content (CHANGE 2)
# ══════════════════════════════════════════════════════════════════════════════


def test_needs_verification_footnote_content(digest):
    # The footnote must name the confidence threshold, describe what human review
    # checks, and offer a concrete feedback path — so a reader knows what the flag
    # means and how to report a mismatch.
    assert digest["footnotes"], "expected at least one footnote for the needs-verification item"
    fn = digest["footnotes"][0]
    assert "acceptance threshold" in fn
    assert "A person verified" in fn
    assert "reply to this email" in fn


# ══════════════════════════════════════════════════════════════════════════════
# action_contacts — "How to respond" per action type
# ══════════════════════════════════════════════════════════════════════════════


def test_action_contacts_rendered_in_body():
    # When action_contacts is supplied, render_markdown must emit a "How to respond:"
    # line for items whose action_type has a contact entry.
    from ingest.extract.schemas import CivicEvent, RecordStatus

    ev = CivicEvent(
        source_id="test_src",
        source_record_id="contact-violation-1",
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="violation",
        title="Class C violation",
        summary="HPD cited hazardous conditions.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=ASOF,
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    body = render_markdown(
        digest,
        action_contacts={"violation": "Call 311 to confirm the violation is on record."},
    )
    assert "**How to respond:**" in body
    assert "Call 311 to confirm" in body


def test_action_contacts_not_rendered_when_no_mapping():
    # If action_contacts has no entry for an action type, no "How to respond:" line appears.
    from ingest.extract.schemas import CivicEvent, RecordStatus

    ev = CivicEvent(
        source_id="test_src",
        source_record_id="contact-permit-noop",
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="permit",
        title="Minor alteration permit",
        summary="A permit was issued.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=ASOF,
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    # Pass contacts dict with no entry for "permit"
    body = render_markdown(digest, action_contacts={"violation": "Call 311."})
    assert "**How to respond:**" not in body


def test_action_contacts_in_lead_item():
    # _render_lead_item (the "Act on this" section) must also render "How to respond:"
    from ingest.extract.schemas import Citation, CivicEvent, RecordStatus

    ev = CivicEvent(
        source_id="test_src",
        source_record_id="contact-lead-hearing",
        bbl=str(SAMPLE_SUBSCRIBER["bbl"]),
        action_type="land_use_hearing",
        title="Land use hearing",
        summary="Upcoming hearing for a rezoning.",
        address=str(SAMPLE_SUBSCRIBER["address"]),
        event_date=ASOF + timedelta(days=5),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label="Legistar matter",
                url="https://legistar.council.nyc.gov/LegislationDetail.aspx?ID=1",
            )
        ],
    )
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    body = render_markdown(
        digest,
        action_contacts={"land_use_hearing": "Register to speak at council.nyc.gov/committees."},
    )
    assert "**How to respond:**" in body
    assert "Register to speak" in body


def test_action_contacts_none_produces_no_contact_line():
    # Omitting action_contacts entirely must not produce any "How to respond:" output.
    matched = match_subscriber(SAMPLE_SUBSCRIBER, _sample_events())
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    body = render_markdown(digest)
    assert "**How to respond:**" not in body


# ══════════════════════════════════════════════════════════════════════════════
# permitted_event category weight — must rank below permit and above grade
# ══════════════════════════════════════════════════════════════════════════════


def test_permitted_event_category_weight_below_permit():
    from ingest.deliver.digest import _category_weight

    assert _category_weight("permitted_event") < _category_weight("permit")


def test_permitted_event_category_weight_above_energy_grade():
    from ingest.deliver.digest import _category_weight

    assert _category_weight("permitted_event") > _category_weight("building_energy_grade")


def test_permitted_event_category_weight_exact_value():
    # 0.32 sits between habitability_complaints (0.35) and building_energy_grade (0.30):
    # more urgent than a static label, less than a cluster of 311 reports.
    from ingest.deliver.digest import _category_weight

    assert _category_weight("permitted_event") == 0.32


# ══════════════════════════════════════════════════════════════════════════════
# Overdue items must not surface "How to respond" contact prompts
# ══════════════════════════════════════════════════════════════════════════════


def test_deadline_passed_items_have_no_contact_line():
    # Items whose deadline has already passed must NOT show a "How to respond" prompt —
    # the action window is closed, so surfacing a contact would mislead the reader.
    from datetime import date

    from ingest.deliver.digest import render_markdown

    overdue_item = {
        "title": "Test zoning filing",
        "summary": "A zoning change was filed.",
        "action_type": "special_permit",
        "source_record_id": "TEST-001",
        "needs_verification": False,
        "deadline_note": "overdue",
        "citations": [
            {
                "kind": "data_source",
                "url": "https://example.com/row/1",
                "label": "Test row",
                "verifies": "exact_record",
            }
        ],
        "extras": {},
    }
    digest = {
        "subject": "Test digest",
        "area": "East Harlem",
        "asof": date.today().isoformat(),
        "item_count": 1,
        "exact_verifiable_count": 1,
        "stats_line": None,
        "lead_items": [],
        "lead_ids": [],
        "overdue_items": [overdue_item],
        "sections": [],
        "later_items": [],
        "needs_attention_count": 0,
        "footnotes": [],
    }
    action_contacts = {"special_permit": "Call 311 to comment."}
    md = render_markdown(digest, action_contacts=action_contacts)
    assert "How to respond" not in md
