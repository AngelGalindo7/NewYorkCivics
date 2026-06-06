"""Offline trajectory evals — Inspect AI task skeletons (CITY-AGNOSTIC).

Stage: Eval (sits beside the assembly line, not inside it).
Single responsibility: define offline, reproducible Inspect AI tasks that score a
stage's output against the hand-labeled golden set in ``golden/``.

Rules honored here:
  - Rule 5  (Evals before agents): these tasks exist before a working extractor.
  - Rule 13 (Per-field accuracy targets): score per field, never one blended F1.
  - Rule 11 (Binary checks beat 1-5 scales): scorers are atomic pass/fail.
  - Rule 3  (Quote the source): hallucination sub-check verifies every extracted
            value appears in the provenance source_quote, and that source_quote
            appears in the source text.
  - Rule 12 (Control judge bias): the extractor (EXTRACT_MODEL=gemini-2.5-flash)
            is judged by a separate scorer; no LLM-as-judge here — pure exact-match
            for identifier fields avoids the cross-family bias issue entirely.

CITY-AGNOSTIC: no NYC specifics. The golden labels happen to be NYC docs today,
but this harness scores any extractor against any golden set.

Thresholds are NOT defined here — they are owned by the eval framework (extractor
field-level F1 >= 0.80; block on >3pp regression). See docs/EVAL.md.

Run with: ``inspect eval ingest/eval/tasks.py``
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

# --- Import guard: inspect_ai is a dev dep; the module must import in CI without it ---
# TYPE_CHECKING branch: mypy sees the real types for type-checking purposes.
# Runtime branch: try the real import; fall back to no-op stubs so the smoke
# test (import-safety) passes even when inspect_ai is not installed.
if TYPE_CHECKING:
    from inspect_ai import Task, task
    from inspect_ai.dataset import MemoryDataset, Sample
    from inspect_ai.model import ModelOutput
    from inspect_ai.scorer import Score, Target, mean, scorer
    from inspect_ai.solver import Generate, TaskState, solver
else:  # pragma: no cover — exercised only when inspect_ai is installed
    try:
        from inspect_ai import Task, task
        from inspect_ai.dataset import MemoryDataset, Sample
        from inspect_ai.model import ModelOutput
        from inspect_ai.scorer import Score, Target, mean, scorer
        from inspect_ai.solver import Generate, TaskState, solver

        _INSPECT_AVAILABLE = True
    except ImportError:
        _INSPECT_AVAILABLE = False

        Task = Any  # type: ignore[assignment,misc]
        MemoryDataset = Any
        Sample = Any
        ModelOutput = Any
        Score = Any
        Target = Any
        Generate = Any
        TaskState = Any

        def mean():  # type: ignore[no-redef]
            return None

        def task(func):  # type: ignore[no-redef]
            """No-op @task: module imports without inspect_ai (smoke test)."""
            return func

        def solver(func):  # type: ignore[no-redef]
            """No-op @solver stub."""
            return func

        def scorer(*args: Any, **kwargs: Any):  # type: ignore[no-redef]
            """No-op @scorer stub."""

            def decorator(func: Any) -> Any:
                return func

            return decorator


# Resolved at runtime; relative to the project root where `inspect eval` is invoked.
GOLDEN_DIR = Path(__file__).parent / "golden"

# High-bar identifier fields measured separately (Rule 13).
_ID_FIELDS = ("ulurp_number", "zoning_from", "zoning_to", "action_type", "event_date")


def _load_golden_set() -> list[dict[str, Any]]:
    """Load hand-labeled golden records; skip ``_``-prefixed sample/fixture files."""
    records: list[dict[str, Any]] = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("Skipping malformed golden label %s: %s", path.name, exc)
    return records


# ---------------------------------------------------------------------------
# Custom solver: call ingest.extract.extractor.extract() (not a raw LLM call)
# ---------------------------------------------------------------------------


@solver
def _extract_solver():  # type: ignore[misc]
    """Inspect AI solver that delegates to the Python extractor (not a raw model call)."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:  # type: ignore[misc]
        from ingest.extract.extractor import extract
        from ingest.parse import ParsedDoc

        # state.input is a list[ChatMessage]; the first user message holds the source text.
        input_text: str = ""
        if hasattr(state, "input_text"):
            input_text = state.input_text  # type: ignore[attr-defined]
        elif hasattr(state, "input") and isinstance(state.input, str):
            input_text = state.input
        elif hasattr(state, "messages") and state.messages:
            first = state.messages[0]
            content = getattr(first, "content", "")
            input_text = content if isinstance(content, str) else str(content)

        doc = ParsedDoc(text=input_text)
        events = extract(doc, source_id="nyc_cb_agenda_eval")

        output_content = json.dumps(
            [ev.model_dump(mode="json", exclude_none=False) for ev in events],
            ensure_ascii=False,
            default=str,
        )

        from ingest.config import get_settings

        state.output = ModelOutput.from_content(  # type: ignore[attr-defined]
            model=get_settings().extract_model,
            content=output_content,
        )
        return state

    return solve


# ---------------------------------------------------------------------------
# Custom scorer: per-field exact-match accuracy (Rule 11 + Rule 13)
# ---------------------------------------------------------------------------


@scorer(metrics=[mean()])  # type: ignore[call-arg]
def _field_accuracy_scorer():  # type: ignore[misc]
    """Score per identifier field; report overall mean and per-field breakdown."""

    async def score(state: TaskState, target: Target) -> Score:  # type: ignore[misc]
        expected: dict[str, Any] = json.loads(target.text)

        completion = ""
        if hasattr(state, "output") and state.output is not None:
            completion = getattr(state.output, "completion", "") or ""

        try:
            actual_list: list[dict[str, Any]] = json.loads(completion)
        except (json.JSONDecodeError, TypeError):
            actual_list = []

        # Compare against the first extracted event (single-item agenda items).
        actual = actual_list[0] if isinstance(actual_list, list) and actual_list else {}

        field_scores: dict[str, float] = {}
        for field in _ID_FIELDS:
            exp_val = expected.get(field)
            if exp_val is None:
                continue  # Field not labeled in this golden doc — skip.
            act_val = actual.get(field)
            field_scores[field] = 1.0 if _values_match(exp_val, act_val) else 0.0

        overall = sum(field_scores.values()) / len(field_scores) if field_scores else 0.0

        # Hallucination sub-check (Rule 3): provenance source_quotes must appear in
        # the source text. Count misses — 0 is ideal, any >0 flags the eval run.
        hallucinations = _count_hallucinations(actual, _get_input_text(state))

        return Score(  # type: ignore[call-arg]
            value=overall,
            explanation=json.dumps(
                {"field_scores": field_scores, "hallucination_count": hallucinations}
            ),
        )

    return score


def _get_input_text(state: TaskState) -> str:  # type: ignore[misc]
    if hasattr(state, "input_text"):
        return state.input_text  # type: ignore[attr-defined]
    if hasattr(state, "input") and isinstance(state.input, str):
        return state.input
    if hasattr(state, "messages") and state.messages:
        first = state.messages[0]
        c = getattr(first, "content", "")
        return c if isinstance(c, str) else str(c)
    return ""


def _values_match(expected: object, actual: object) -> bool:
    """Case-insensitive, whitespace-normalized match for identifier fields."""
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    return str(expected).strip().upper() == str(actual).strip().upper()


def _count_hallucinations(event: dict[str, Any], source_text: str) -> int:
    """Count provenance source_quotes that don't appear verbatim in source_text (Rule 3)."""
    src_lower = source_text.lower()
    count = 0
    for _field, prov in event.get("provenance", {}).items():
        if not isinstance(prov, dict):
            continue
        quote = prov.get("source_quote", "")
        if quote and quote.lower() not in src_lower:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Inspect AI @task
# ---------------------------------------------------------------------------


@task
def extractor_field_f1() -> Task:  # type: ignore[misc]
    """Extractor field-level accuracy vs the golden set (the first of the two evals).

    Contract:
      - Load each labeled record from ``golden/`` (ignore ``_``-prefixed samples).
      - Run the configured extractor (EXTRACT_MODEL) on the embedded source text.
      - Score PER FIELD (Rule 13): ULURP, zoning, action_type, event_date on exact match.
      - Hallucination sub-check (Rule 3): each provenance source_quote must appear in
        the source text.
      - Report per-field accuracy, NOT one blended number.

    Threshold: identifier fields >= 0.70 (Phase 0 gate); >= 0.80 for deploy block.
    Run: ``inspect eval ingest/eval/tasks.py``
    """
    golden = _load_golden_set()
    if not golden:
        raise RuntimeError(
            f"No golden labels found in {GOLDEN_DIR}. "
            "Add labels via the add-golden-doc skill before running evals."
        )

    samples = [
        Sample(  # type: ignore[call-arg]
            input=record.get("_source_text", ""),
            target=json.dumps(
                {k: v for k, v in record.items() if not k.startswith("_")},
                ensure_ascii=False,
                default=str,
            ),
            id=record.get("source_record_id", f"golden-{i}"),
        )
        for i, record in enumerate(golden)
    ]

    return Task(  # type: ignore[call-arg]
        dataset=MemoryDataset(samples),
        solver=[_extract_solver()],
        scorer=[_field_accuracy_scorer()],
    )
