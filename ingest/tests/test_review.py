"""Contract tests for the human-review gate (rank -> match -> digest -> review -> send).

Runs fully offline on the sample East Harlem events (no Socrata, no DB). Locks the
trust-critical behavior the CLI exists to enforce (human-review-then-send): a
digest carrying a needs-verification item cannot be sent until a human clears it; an
approval clears it and a rejection removes the item while keeping the rendered counts
honest; and the whole thing survives a JSON round-trip between processes.
"""

from __future__ import annotations

from datetime import date

import pytest

from ingest.deliver.digest import build_digest, render_markdown
from ingest.deliver.match import match_subscriber
from ingest.deliver.review import (
    dump_pending,
    load_pending,
    main,
    review_digest,
)
from ingest.deliver.send import send_digest
from ingest.extract.schemas import Citation, CivicEvent, RecordStatus
from ingest.sources.nyc.harlem_digest import SAMPLE_SUBSCRIBER, _sample_events

ASOF = date(2026, 5, 31)

# The subscriber's own block id, as a plain str so synthetic events thread onto it.
SUBSCRIBER_BBL = str(SAMPLE_SUBSCRIBER["bbl"])


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


def _gate_on(monkeypatch):
    # Force the human-review gate ON regardless of the developer's local .env. Set the
    # var to a false value rather than deleting it: get_settings() reloads .env on every
    # call, which would re-inject a deleted flag; an already-present env var wins over the
    # file, so this keeps the gate deterministic locally and in CI alike.
    monkeypatch.setenv("BYPASS_HUMAN_REVIEW", "false")


def test_approve_all_clears_gate_and_keeps_items_then_sends(tmp_path, digest, monkeypatch):
    _gate_on(monkeypatch)
    before = digest["item_count"]
    assert digest["review_required"] is True

    reviewed = review_digest(digest, decide=lambda item: True)

    # Gate cleared, every item retained.
    assert reviewed["review_required"] is False
    assert reviewed["review_items"] == []
    assert reviewed["item_count"] == before
    assert len(_all_items(reviewed)) == before
    # The approved flagged item is still present and still flagged (shown as-is).
    assert any(it["needs_verification"] for it in _all_items(reviewed))

    path = send_digest(reviewed, SAMPLE_SUBSCRIBER, sink_dir=tmp_path)
    assert path.exists()


def test_reject_displacement_removes_it_and_clears_gate(tmp_path, digest, monkeypatch):
    _gate_on(monkeypatch)
    before = digest["item_count"]

    # Reject exactly the needs-verification item (the displacement signal).
    reviewed = review_digest(digest, decide=lambda item: not item["needs_verification"])

    assert reviewed["review_required"] is False
    assert reviewed["item_count"] == before - 1
    titles = [it["title"].lower() for it in _all_items(reviewed)]
    assert not any("displacement" in t for t in titles)

    # The subject (the email's H1) is itself a derived count and must track the surviving
    # items — a stale headline would over-count updates and re-assert the rejected signal.
    assert f"{before - 1} update" in reviewed["subject"]
    assert f"{before} update" not in reviewed["subject"]
    # The rendered H1 must agree with the recomputed body counts, not the pre-rejection ones.
    body = render_markdown(reviewed)
    h1 = body.splitlines()[0]
    assert h1 == f"# {reviewed['subject']}"
    assert f"{before - 1} update" in h1
    assert f"{before} update" not in h1

    # No surviving flagged item -> the needs-verification footnote is gone.
    assert not any("needs verification" in note.lower() for note in reviewed["footnotes"])

    assert "displacement" not in body.lower()
    assert "[needs verification]" not in body

    path = send_digest(reviewed, SAMPLE_SUBSCRIBER, sink_dir=tmp_path)
    assert path.exists()


def test_no_op_when_nothing_needs_verification(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    # Drop the displacement signal from the fixture: the remaining records are all
    # ACCEPTED, so nothing needs verification and the digest is already sendable.
    events = [ev for ev in _sample_events() if "displacement" not in (ev.title or "").lower()]
    matched = match_subscriber(SAMPLE_SUBSCRIBER, events)
    accepted = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    assert accepted["review_required"] is False
    assert not any(it["needs_verification"] for it in _all_items(accepted))

    # decide should never be called; returning False would prove a mistaken walk.
    reviewed = review_digest(accepted, decide=lambda item: pytest.fail("decided a no-op item"))

    assert reviewed is accepted  # unchanged, not even copied
    # Already sendable as-is.
    send_digest(reviewed, SAMPLE_SUBSCRIBER, sink_dir=tmp_path)


def test_gate_is_real_blocks_before_review_and_opens_after(tmp_path, digest, monkeypatch):
    _gate_on(monkeypatch)

    # Before review: the unreviewed digest must not send.
    assert digest["review_required"] is True
    with pytest.raises(ValueError):
        send_digest(digest, SAMPLE_SUBSCRIBER, sink_dir=tmp_path)

    # After review approves: it sends.
    reviewed = review_digest(digest, decide=lambda item: True)
    path = send_digest(reviewed, SAMPLE_SUBSCRIBER, sink_dir=tmp_path)
    assert path.exists()


def test_dump_then_load_round_trips_render(tmp_path, digest):
    path = dump_pending(digest, SAMPLE_SUBSCRIBER, review_dir=tmp_path)
    assert path.exists()

    loaded_digest, loaded_subscriber = load_pending(path)

    # Date revival is correct iff the loaded digest renders byte-for-byte the same.
    assert render_markdown(loaded_digest) == render_markdown(digest)
    assert loaded_subscriber == SAMPLE_SUBSCRIBER
    # The revived item date fields are real date objects again, not strings.
    for it in _all_items(loaded_digest):
        for field in ("deadline", "event_date", "actionable_date"):
            value = it[field]
            assert value is None or isinstance(value, date)


def test_main_reviews_pending_and_writes_digest(tmp_path, digest, monkeypatch):
    _gate_on(monkeypatch)
    sink = tmp_path / "digests"
    monkeypatch.setattr("ingest.deliver.send.DEFAULT_SINK_DIR", sink)

    path = dump_pending(digest, SAMPLE_SUBSCRIBER, review_dir=tmp_path)

    # Approve every prompt.
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "y")

    rc = main(["--digest", str(path)])
    assert rc == 0

    written = list(sink.glob("*.md"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8")
    assert body.startswith("# ")


def test_main_no_pending_returns_zero(tmp_path, monkeypatch):
    # Point the default review dir at an empty temp dir; nothing pending -> clean exit.
    monkeypatch.setattr("ingest.deliver.review.DEFAULT_REVIEW_DIR", tmp_path)
    assert main([]) == 0


def _accepted_permit(i: int) -> CivicEvent:
    """A dated, high-confidence permit on the subscriber's block (sorts to the front)."""
    return CivicEvent(
        source_id="test_permit",
        source_record_id=f"P{i}",
        bbl=SUBSCRIBER_BBL,
        action_type="permit",
        title=f"Permit {i}",
        summary="Routine permit.",
        # A future deadline puts dated, actionable items ahead of the undated signal.
        deadline=date(2026, 6, 10),
        confidence=0.95,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label=f"Permit {i}",
                url=f"https://example.test/permit/{i}",
            )
        ],
    )


def _flagged_undated_signal() -> CivicEvent:
    """An undated, lower-confidence flagged item (sorts to the BACK of the queue)."""
    return CivicEvent(
        source_id="test_signal",
        source_record_id="S1",
        bbl=SUBSCRIBER_BBL,
        action_type="displacement_signal",
        title="Possible displacement pressure",
        summary="Correlated records suggest tenant risk.",
        confidence=0.4,
        status=RecordStatus.UNVERIFIED,
        citations=[
            Citation(
                kind="official_lookup",
                verifies="search",
                label="Look it up",
                url="https://example.test/search",
            )
        ],
    )


def test_review_required_covers_every_flagged_item_not_just_top_n():
    # Builder contract (regression): the send gate must flag EVERY needs-verification item
    # in the body, even an undated, low-confidence one sitting behind many dated accepted
    # items — so it can never ship unreviewed. (Earlier the gate keyed off a top-N slice
    # and this item fell out of it.)
    events = [_accepted_permit(i) for i in range(12)] + [_flagged_undated_signal()]
    matched = match_subscriber(SAMPLE_SUBSCRIBER, events)
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)

    assert digest["review_required"] is True
    assert any("displacement" in title.lower() for title in digest["review_items"])
    assert "[needs verification]" in render_markdown(digest)


def _digest_with_understated_review_flag():
    """A digest that still renders a flagged item but whose review_required flag is False.

    This is the failure mode the review layer must defend against regardless of how the
    upstream builder set the flag (a future builder regression, a hand-edited digest). We
    force the under-set state directly so the test does not depend on any builder bug.
    """
    events = [_accepted_permit(i) for i in range(3)] + [_flagged_undated_signal()]
    matched = match_subscriber(SAMPLE_SUBSCRIBER, events)
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    # Simulate an under-set gate flag: the flagged item is still in the body, but the flag
    # claims nothing needs review.
    digest["review_required"] = False
    digest["review_items"] = []
    return digest


def test_review_catches_flagged_item_even_when_flag_understated():
    digest = _digest_with_understated_review_flag()

    # Precondition: the flag is False and the title list empty, yet the flagged item is
    # still in the rendered body.
    assert digest["review_required"] is False
    assert digest["review_items"] == []
    assert "[needs verification]" in render_markdown(digest)

    # The review core keys off the actual body content, so it still finds the flagged
    # item and a rejection removes it.
    reviewed = review_digest(digest, decide=lambda item: not item["needs_verification"])
    assert "[needs verification]" not in render_markdown(reviewed)
    assert not any(it["needs_verification"] for it in _all_items(reviewed))


def test_main_reviews_flagged_item_even_when_flag_understated(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    sink = tmp_path / "digests"
    monkeypatch.setattr("ingest.deliver.send.DEFAULT_SINK_DIR", sink)

    digest = _digest_with_understated_review_flag()
    assert digest["review_required"] is False  # the gate flag is under-set
    path = dump_pending(digest, SAMPLE_SUBSCRIBER, review_dir=tmp_path)

    # Reject the flagged item at the prompt; the human is still asked despite the flag.
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "n")

    rc = main(["--digest", str(path)])
    assert rc == 0

    # It sent the surviving accepted items, but the rejected flagged item is gone.
    written = list(sink.glob("*.md"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8")
    assert "[needs verification]" not in body
