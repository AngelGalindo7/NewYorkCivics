"""Contract tests for the Deliver path (rank -> match -> digest -> send).

Runs fully offline on the sample East Harlem events (no Socrata, no DB). Locks the
trust-critical behavior: confidence routing (Rule 10), the human-review gate
(Rule 9), actionability ordering, and that every claim carries verifiable links.
"""

from __future__ import annotations

from datetime import date
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
    # not appear as two separate top-level entries.
    from ingest.deliver.digest import _group_buildings, _to_item

    matched = match_subscriber(SAMPLE_SUBSCRIBER, _sample_events())
    block = [_to_item(ev, BAND_ON_YOUR_BLOCK, ASOF) for ev in matched[BAND_ON_YOUR_BLOCK]]
    groups = _group_buildings(block)
    # displacement signal + the A1 permit are both BBL 1016500030 -> one group.
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

    # Ensure BYPASS_HUMAN_REVIEW is off regardless of the developer's local env.
    monkeypatch.delenv("BYPASS_HUMAN_REVIEW", raising=False)

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
