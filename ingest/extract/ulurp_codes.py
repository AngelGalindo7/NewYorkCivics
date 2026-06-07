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
# Canonical zoning codes (2024 NYC Zoning Resolution)
# ---------------------------------------------------------------------------
# The authoritative, complete set of zoning districts from the NYC Zoning
# Resolution (DCP). `ingest/normalize/zoning.py` validates against this list
# and quarantines rejects (Rule 2).
CANONICAL_ZONING_CODES: frozenset[str] = frozenset(
    {
        # Residence districts
        "R1-1",
        "R1-2",
        "R2",
        "R2A",
        "R2X",
        "R3-1",
        "R3-2",
        "R3A",
        "R3X",
        "R4",
        "R4-1",
        "R4A",
        "R4B",
        "R5",
        "R5A",
        "R5B",
        "R5D",
        "R6",
        "R6A",
        "R6B",
        "R7-1",
        "R7-2",
        "R7A",
        "R7B",
        "R7D",
        "R7X",
        "R8",
        "R8A",
        "R8B",
        "R8X",
        "R9",
        "R9A",
        "R9D",
        "R9X",
        "R10",
        "R10A",
        "R10H",
        "R10X",
        # Commercial overlays
        "C1-1",
        "C1-2",
        "C1-3",
        "C1-4",
        "C1-5",
        "C1-6",
        "C1-7",
        "C1-8",
        "C1-9",
        "C2-1",
        "C2-2",
        "C2-3",
        "C2-4",
        "C2-5",
        "C2-6",
        "C2-7",
        "C2-8",
        # Commercial districts
        "C3",
        "C3A",
        "C4-1",
        "C4-2",
        "C4-2A",
        "C4-3",
        "C4-3A",
        "C4-4",
        "C4-4A",
        "C4-5",
        "C4-5A",
        "C4-5D",
        "C4-5X",
        "C4-6",
        "C4-6A",
        "C4-7",
        "C5-1",
        "C5-2",
        "C5-2A",
        "C5-3",
        "C5-4",
        "C5-5",
        "C6-1",
        "C6-1A",
        "C6-2",
        "C6-2A",
        "C6-3",
        "C6-3A",
        "C6-3D",
        "C6-3X",
        "C6-4",
        "C6-4A",
        "C6-4X",
        "C6-5",
        "C6-6",
        "C6-7",
        "C6-9",
        "C7",
        "C8-1",
        "C8-2",
        "C8-3",
        "C8-4",
        # Manufacturing districts
        "M1-1",
        "M1-1D",
        "M1-2",
        "M1-2D",
        "M1-3",
        "M1-4",
        "M1-4D",
        "M1-5",
        "M1-5A",
        "M1-5B",
        "M1-5M",
        "M1-6",
        "M1-6D",
        "M1-6M",
        "M2-1",
        "M2-2",
        "M2-3",
        "M2-4",
        "M3-1",
        "M3-2",
        # Special/mixed-use districts
        "MX-1",
        "MX-6",
        "MX-7",
        "MX-8",
        "MX-16",
        "MX-17",
        "MX-23",
        "MX-25",
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
