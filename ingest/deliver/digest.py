"""Digest assembly — group, order, human-review, then send (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: turn one subscriber's ranked,
matched events into a plain-English digest, gate it through human review, then
hand it to the send adapter.

Rules honored here:
  - Rule 9  (Human-review-then-send): AI assembles the candidate list; a human
            clears a short queue (top-N) before anything is sent. A wrong
            extraction never auto-publishes as fact.
  - Rule 10 (Confidence routing): items below the high-confidence band are marked
            "unverified" with a footnote; the digest visibly distinguishes
            high-confidence from needs-verification items (the biggest trust lever).
  - Rule 8  (linear ranker): ordering uses rank.score(), but the digest reorders
            for ACTIONABILITY — deadlines first.

CITY-AGNOSTIC: renders generic events; no NYC specifics. Summaries are reused from
Extract (cached), not regenerated here.
"""

from __future__ import annotations

from typing import Any

# Default cap on items surfaced for human review per subscriber per run (Rule 9).
# TODO Phase 2: tune against review-queue clearing time (pivot threshold: if
# clearing exceeds 30 min/day the validation is too strict — tighten upstream).
DEFAULT_REVIEW_TOP_N = 10


def build_digest(
    subscriber: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    review_top_n: int = DEFAULT_REVIEW_TOP_N,
) -> dict[str, Any]:
    """Assemble a review-ready digest for one subscriber.

    Contract: group ``events`` (already matched + ranked) for ``subscriber``, order
    by ACTIONABILITY (soonest deadlines first), keep the top-N for human review
    (Rule 9), and mark any non-high-confidence item as "unverified" with a footnote
    (Rule 10). Returns a structured digest (subject, ordered sections, review flags)
    ready for the human queue, then send.py. Does NOT send directly — review gates.
    """
    raise NotImplementedError(
        "Phase 2: group by actionability (deadlines first), take top-N for review "
        "(Rule 9), mark unverified items with a footnote (Rule 10), return a "
        "review-ready digest object. Sending happens after the human clears it."
    )
