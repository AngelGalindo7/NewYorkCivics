"""Linear-combo ranker — score per (subscriber, event) (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: assign each candidate event a
relevance score for a given subscriber, using a transparent linear combination of
six named signals.

Rule 8 (Linear-combo ranker, not ML) — the formula is deliberately linear and
per-weight overridable so it is debuggable; instrument for A/B but DO NOT build an
ML ranker in v1 (Rule 16 (No premature abstraction)).

    score = w_d  * proximity         (closer events score higher)
          + w_t  * recency           (more recent events score higher)
          + w_dl * deadline_urgency  (sooner actionable deadlines score higher)
          + w_m  * magnitude         (bigger projects/impact score higher)
          + w_n  * novelty           (first time this thread appears scores higher)
          + w_cat* category_weight   (per action-type importance)

Each signal is expected normalized to [0, 1]; weights are tunable defaults that a
caller may override. Tune by hand and A/B — never fit (Rule 8).

CITY-AGNOSTIC: signals are generic; no NYC specifics.
"""

from __future__ import annotations

# Six named weights (Rule 8). Overridable per call. These are conservative
# starting values — TODO Phase 2: tune against the ranking eval
# (NDCG@10 >= 0.70, list diversity >= 0.40; see docs/EVAL.md).
DEFAULT_WEIGHTS: dict[str, float] = {
    "w_d": 0.30,  # proximity
    "w_t": 0.15,  # recency
    "w_dl": 0.25,  # deadline_urgency
    "w_m": 0.10,  # magnitude
    "w_n": 0.10,  # novelty
    "w_cat": 0.10,  # category_weight
}


def score(
    signals: dict[str, float],
    weights: dict[str, float] | None = None,
) -> float:
    """Linear-combination relevance score for one (subscriber, event) pair.

    Contract: ``signals`` provides the six normalized [0,1] components
    (``proximity``, ``recency``, ``deadline_urgency``, ``magnitude``, ``novelty``,
    ``category_weight``). ``weights`` overrides ``DEFAULT_WEIGHTS`` per Rule 8.
    Returns the weighted sum. Keep it linear and debuggable — no ML.
    """
    weights = weights or DEFAULT_WEIGHTS
    # Map each weight key (w_d, w_t, ...) to its signal name; missing signals read as 0
    # so a caller may supply only the signals it can compute (Rule 2 — never guess a value).
    signal_for = {
        "w_d": "proximity",
        "w_t": "recency",
        "w_dl": "deadline_urgency",
        "w_m": "magnitude",
        "w_n": "novelty",
        "w_cat": "category_weight",
    }
    return sum(
        weight * signals.get(signal_for[key], 0.0)
        for key, weight in weights.items()
        if key in signal_for
    )
