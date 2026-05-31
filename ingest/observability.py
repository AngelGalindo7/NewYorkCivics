"""Logging + tracing seam (CITY-AGNOSTIC).

A thin seam, not a framework (Rule 16). :func:`get_logger` returns a stdlib logger
with a consistent format; :func:`observe` is a decorator that no-ops when LANGFUSE_*
is unset and wraps ``langfuse.observe`` once configured — supporting the EVAL.md
"log every trace, sample 5-10/day, promote failures into the golden set" loop
(Rule 5). Zero dependency and zero overhead until Langfuse is turned on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

_F = TypeVar("_F", bound=Callable[..., object])
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def get_logger(name: str) -> logging.Logger:
    """Return a configured stdlib logger (consistent format across the pipeline)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def observe(func: _F) -> _F:
    """Trace decorator: identity no-op unless LANGFUSE_* is configured.

    Contract: when ``settings.langfuse_public_key`` / ``langfuse_secret_key`` are set
    (read via :func:`ingest.config.get_settings`), delegate to ``langfuse.observe``;
    otherwise return ``func`` unchanged so call sites can be annotated now at zero
    cost (Rule 16 — a seam, not a framework).
    """
    # TODO Phase 3: when settings.langfuse_* is set, wrap with langfuse.observe();
    # until then this is an identity decorator so call sites can be annotated today.
    return func
