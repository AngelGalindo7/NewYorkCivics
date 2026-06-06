"""Validate zoning codes against the canonical NYC list.

Stage: Normalize & validate (stage 4).
Single responsibility: check that an extracted zoning code (e.g. ``R7A``,
``C1-2``) is a real NYC zoning district before it reaches ``events`` — reject
unknown codes into quarantine.

Boundary: this module CALLS NYC-SPECIFIC reference data (the canonical zoning
list lives in ``ingest/extract/ulurp_codes.py``, the labeled NYC lookup
module). The validate-or-quarantine orchestration is city-agnostic. To port,
swap the canonical list behind the same contract (Rule 14).

Rules honored:
- Rule 2 (fail fast, don't guess): a code not on the canonical list is rejected
  into quarantine with a reason — never silently kept or "corrected".
- Rule 13 (per-field accuracy targets): zoning is a high-bar identifier field,
  validated independently of other fields.
"""

from __future__ import annotations

from dataclasses import dataclass

from ingest.extract.ulurp_codes import CANONICAL_ZONING_CODES


@dataclass(frozen=True)
class ZoningValidation:
    """Outcome of validating one zoning code.

    ``ok is False`` -> caller quarantines the record (Rule 2).
    """

    ok: bool
    code: str
    normalized: str | None = None  # canonical form if it could be normalized
    reason: str | None = None  # quarantine reason when ok is False


def validate_zoning_code(code: str) -> ZoningValidation:
    """Return whether ``code`` is a canonical NYC zoning district.

    Contract: looks ``code`` up against the canonical list (from the NYC lookup
    module). On a miss returns ``ZoningValidation(ok=False, reason=...)`` so the
    record is quarantined (Rule 2) — does not guess or auto-correct.
    """
    normalized = code.strip().upper()
    if normalized in CANONICAL_ZONING_CODES:
        return ZoningValidation(ok=True, code=normalized, normalized=normalized)
    return ZoningValidation(
        ok=False,
        code=code,
        reason=f"Unknown zoning district: {normalized!r}",
    )
