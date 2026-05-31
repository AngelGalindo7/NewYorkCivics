"""Eval stage package.

Import path is ``ingest.eval`` (NOT a top-level ``eval`` — that would shadow the
Python builtin). The eval harness is CITY-AGNOSTIC and is built FIRST, per
Rule 5 (Evals before agents): the eval is more important than the agent.

This package holds the permanent harness that sits BESIDE the assembly line
(Fetch -> Parse -> Extract -> Normalize & validate -> Store -> Deliver), not
inside it, measuring each stage against hand-labeled ground truth.

Public surface:
    golden/        -- hand-labeled ground-truth set (append-only).
    promptfoo.yaml -- CI assertions (promptfoo, node/npx CLI).
    tasks.py       -- offline trajectory evals (Inspect AI).

Thresholds are NOT defined in code; they live in ``docs/EVAL.md``.
"""
