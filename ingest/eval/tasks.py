"""Offline trajectory evals — Inspect AI task skeletons (CITY-AGNOSTIC).

Stage: Eval (sits beside the assembly line, not inside it).
Single responsibility: define offline, reproducible Inspect AI tasks that score a
stage's output against the hand-labeled golden set in ``golden/``.

Rules honored here:
  - Rule 5  (Evals before agents): these tasks exist before a working extractor.
  - Rule 13 (Per-field accuracy targets): score per field, never one blended F1.
  - Rule 11 (Binary checks beat 1-5 scales): scorers are atomic pass/fail.
  - Rule 3  (Quote the source): a hallucination sub-check verifies every extracted
            value appears verbatim/plausible-paraphrase in the source text.
  - Rule 12 (Control judge bias): any LLM-judge scorer uses a cross-family judge
            (JUDGE_MODEL=claude-haiku-4-5 vs the Gemini Flash extractor).

CITY-AGNOSTIC: no NYC specifics. The golden labels happen to be NYC docs today,
but this harness scores any extractor against any golden set.

Thresholds are NOT defined here — they are owned by the eval framework (extractor
field-level F1 >= 0.80, block deploy on >3pp regression).

Run with: ``inspect eval ingest/eval/tasks.py``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# Inspect AI is an optional dev/eval dependency (requirements-dev.txt). Guard the
# import so this module is importable in environments where it is not installed
# (e.g. a runtime container that never runs evals). See Rule 16 (no premature
# abstraction) — we keep the guard minimal.
if TYPE_CHECKING:
    from inspect_ai import Task, task
else:  # pragma: no cover - import guard, exercised only when inspect_ai present
    try:
        from inspect_ai import Task, task
    except ImportError:  # inspect_ai not installed
        Task = Any  # type: ignore[assignment,misc]

        def task(func):  # type: ignore[no-redef]
            """No-op @task fallback so the module imports without inspect_ai."""
            return func


# Resolved at runtime from the EXTRACT_MODEL / JUDGE_MODEL config flags (Rule 6).
# TODO Phase 0: read these via ingest.config.get_settings() (Rule 6), not os.environ directly.
GOLDEN_DIR = "ingest/eval/golden"


@task
def extractor_field_f1() -> Task:
    """Extractor field-level F1 vs the golden set (the first of the two evals).

    Contract:
      - Load each labeled record from ``golden/`` (ignore ``_``-prefixed samples).
      - Run the configured extractor (EXTRACT_MODEL) on the same source doc.
      - Score PER FIELD (Rule 13): identifiers (ULURP/zoning/BBL/date) on a high
        bar; fuzzy fields (applicant names) lower and human-reviewed.
      - Add a schema-conformance sub-check (validates against the Pydantic model)
        and a hallucination sub-check (Rule 3): each extracted value must appear
        verbatim/plausible-paraphrase in the source.
      - Report per-field F1, NOT one blended number.

    Threshold: field-level F1 >= 0.80; block on >3pp regression.
    """
    raise NotImplementedError(
        "Phase 0: load golden set, run EXTRACT_MODEL, score per-field F1 + "
        "schema-conformance + hallucination sub-checks. Thresholds owned by the eval framework."
    )


# TODO Phase 1: add a second @task for the geocoding error histogram (meters of
# error vs OSM/Nominatim over 100 addresses; median < 50m, p95 < 500m). It is the
# cheapest, highest-signal eval and should be built before the extractor is good.
