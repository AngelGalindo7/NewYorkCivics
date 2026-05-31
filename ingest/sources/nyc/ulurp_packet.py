"""Stage 1 (Fetch) — ULURP land-use packets. NYC-SPECIFIC.

Single responsibility: discover and download NYC ULURP (Uniform Land Use Review
Procedure) packets and identify which application each belongs to. This is a
DIRTY source: packets are PDFs, often *hundreds of pages* of dense legal prose.
This module only fetches bytes + identity metadata; parsing and extraction happen
in the city-agnostic Parse and Extract stages.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): no LLM here; the LLM fires later, in Extract.
- Rule 3 (quote the source): preserve raw bytes so every extracted fact traces
  back to a verbatim line in the packet.
- Rule 4 (NYC-specific code in nyc/): the ULURP-number shape and packet locations
  are NYC knowledge and stay in this package.

Hundred-page packets are the single most expensive PDFs in the system; Phase 2
must route most pages to free digital text and reserve vision-LLM for the few
scanned/messy pages (see Parse / ``pdf_route``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PacketRef:
    """A discovered ULURP packet, before download.

    Attributes:
        ulurp_number: The ULURP application number (e.g. ``"C 240123 ZMM"``).
            Format validation lives in ``ingest/extract/ulurp_codes.py``.
        url: Direct link to the packet PDF.
        project_thread_id: Optional cross-source story id, if already known (Rule 7).
        title: Human label as it appears on the source site, if any.
    """

    ulurp_number: str
    url: str
    project_thread_id: str | None = None
    title: str | None = None


def discover_packets(ulurp_number: str | None = None) -> list[PacketRef]:
    """Find new ULURP packets to fetch.

    Args:
        ulurp_number: Restrict discovery to a single application; ``None`` scans
            all configured listings.

    Returns:
        References to packets not yet ingested (caller dedups against the store).
    """
    raise NotImplementedError("Phase 2: discover ULURP packets; identify by ULURP number.")


def fetch(url: str) -> bytes:
    """Download one ULURP packet PDF.

    Args:
        url: Direct PDF link from a :class:`PacketRef`.

    Returns:
        Raw PDF bytes (possibly hundreds of pages), handed verbatim to Parse and
        preserved for source grounding (Rule 3).
    """
    raise NotImplementedError("Phase 2: fetch packet bytes; no parsing here.")


# TODO Phase 2: validate ULURP numbers via ingest/extract/ulurp_codes.py before
#   ingest; fail fast into quarantine on malformed numbers (Rule 2).
# TODO Phase 2 (or v2): packets are a Phase 2 / v2 source per the source order —
#   confirm scope before building the long-tail fetchers.
