"""ULURP-number + zoning-code reference data.

==============================================================================
NYC-SPECIFIC MODULE. This is the ONLY NYC-specific file in `ingest/extract/`.
Everything else in this package is CITY-AGNOSTIC (Rule 4 — NYC-specific code
lives only in `nyc/` and labeled lookup modules). To port to another city, do
NOT edit this file's machinery — copy it to a new lookup module and swap the
patterns/reference list (Rule 14 — port by copy-and-swap).
==============================================================================

Stage: Extract (stage 3) — reference/lookup, no LLM.
Single responsibility: recognize and decode NYC ULURP numbers and validate NYC
zoning codes against a canonical list.

Rules honored:
- Rule 2 (fail fast, don't guess): parse/validate stubs return a clear failure
  rather than guessing a malformed code into a valid one.
- Rule 4 (NYC-specific lives in labeled modules): this file is that label.
- Rule 13 (per-field accuracy targets): ULURP/zoning are high-bar identifier
  fields the extractor must hit, so they get explicit validation here.

These are contract stubs / first-draft reference data, not implementations.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# ---------------------------------------------------------------------------
# ULURP number format
# ---------------------------------------------------------------------------
# Example decoded: "C 240123 ZMM"
#   C       -> application type prefix (C = ULURP application requiring City Planning
#              Commission + City Council action; N = CPC action without Council review;
#              M = modification, etc.)
#   240123  -> 6-digit application number; first two digits are the calendar filing
#              year (24 = 2024), the remaining four a sequence
#   ZM      -> action code (ZM = Zoning Map amendment; ZR = Zoning text;
#              ZS = Special permit; etc.)
#   M       -> borough suffix (M=Manhattan, X=Bronx, K=Brooklyn, Q=Queens,
#              R=Staten Island, Y=citywide/multi)
#
# TODO Phase 2: confirm the full prefix/action/borough enumerations against the
# DCP ULURP application-number spec before relying on group meanings.
ULURP_PATTERN = re.compile(
    r"""
    ^\s*
    (?P<prefix>[A-Z]{1,2})      # application type prefix, e.g. C, N, M
    \s*
    (?P<number>\d{6})           # 6-digit application number (first 2 = calendar filing year)
    \s*
    (?P<action>[A-Z]{2})        # action code, e.g. ZM, ZR, ZS
    (?P<borough>[MXKQRY])       # borough suffix
    \s*$
    """,
    re.VERBOSE,
)


class UlurpNumber(NamedTuple):
    """Decoded parts of a ULURP number."""

    raw: str
    prefix: str
    number: str
    filing_year: str  # calendar filing year, from the first two digits of `number`
    action: str
    borough: str


# ---------------------------------------------------------------------------
# Canonical zoning codes (STUB — NOT the full list)
# ---------------------------------------------------------------------------
# The authoritative, complete set of zoning districts comes from the NYC Zoning
# Resolution (DCP). This is a small illustrative stub only, enough to exercise
# the validation contract. `ingest/normalize/zoning.py` validates against the
# canonical list and quarantines rejects (Rule 2).
#
# TODO Phase 1: replace with the full canonical district list sourced from the
# Zoning Resolution / DCP zoning data; consider loading from a data file rather
# than a hard-coded set so it can be refreshed without code changes.
CANONICAL_ZONING_CODES_STUB: frozenset[str] = frozenset(
    {
        # Residence districts (sample)
        "R6",
        "R6A",
        "R6B",
        "R7-2",
        "R7A",
        "R7B",
        "R8",
        "R8A",
        # Commercial overlays / districts (sample)
        "C1-2",
        "C2-4",
        "C4-2",
        # Manufacturing (sample)
        "M1-1",
        "M1-4",
    }
)


def parse_ulurp(raw: str) -> UlurpNumber | None:
    """Decode a ULURP number string into its parts, or ``None`` if malformed.

    Contract: returns ``None`` (does not raise, does not guess) on any input
    that doesn't match ``ULURP_PATTERN`` — fail fast (Rule 2).
    """
    raise NotImplementedError("Phase 2: implement ULURP decode via ULURP_PATTERN.")


def validate_ulurp(raw: str) -> bool:
    """Return True iff ``raw`` is a well-formed ULURP number.

    Contract: format check only (shape, filing-year sanity, known suffix).
    Existence in ZAP is a separate Normalize concern, not checked here.
    """
    raise NotImplementedError("Phase 2: implement ULURP format validation.")
