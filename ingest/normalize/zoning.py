"""Validate zoning codes against a caller-supplied canonical list.

Stage: Normalize & validate (stage 4).
Single responsibility: check that an extracted zoning code (e.g. ``R7A``,
``C1-2``) is a known zoning district before it reaches ``events`` — reject
unknown codes into quarantine.

Boundary: CITY-AGNOSTIC. The canonical code set is injected by the caller so
this module never imports NYC-specific data directly. To port, pass a different
city's canonical set — no changes here (Rule 4 / Rule 14).

Rules honored:
- Rule 2 (fail fast, don't guess): a code not on the canonical list is rejected
  into quarantine with a reason — never silently kept or "corrected".
- Rule 13 (per-field accuracy targets): zoning is a high-bar identifier field,
  validated independently of other fields.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ZoningValidation:
    """Outcome of validating one zoning code.

    ``ok is False`` -> caller quarantines the record (Rule 2).
    """

    ok: bool
    code: str
    normalized: str | None = None  # canonical form if it could be normalized
    reason: str | None = None  # quarantine reason when ok is False


def validate_zoning_code(code: str, canonical_codes: frozenset[str]) -> ZoningValidation:
    """Return whether ``code`` is in ``canonical_codes``.

    Contract: looks ``code`` up against the caller-supplied canonical set. On a
    miss returns ``ZoningValidation(ok=False, reason=...)`` so the record is
    quarantined (Rule 2) — does not guess or auto-correct.

    The NYC canonical set lives in ``ingest/extract/ulurp_codes.CANONICAL_ZONING_CODES``;
    pass it in from the NYC connector, not from here.
    """
    normalized = code.strip().upper()
    if normalized in canonical_codes:
        return ZoningValidation(ok=True, code=normalized, normalized=normalized)
    return ZoningValidation(
        ok=False,
        code=code,
        reason=f"Unknown zoning district: {normalized!r}",
    )
