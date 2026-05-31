"""Stage 2 (Parse) — per-page routing: digital vs. vision. CITY-AGNOSTIC.

Single responsibility: decide, per page, whether to read it with the free digital
path (``pdf_text``) or the expensive vision-LLM fallback (``pdf_vision``), run the
chosen extractors, and assemble the single uniform ``ParsedDoc`` that Extract
consumes. This is the orchestration entrypoint for Parse.

The shared output shape (``ParsedDoc``) is defined once in
:mod:`ingest.parse` (``__init__``) and re-exported here for convenience, so
``pdf_text``, ``pdf_vision``, ``pdf_route``, and Extract all speak the same contract.

Routing rule (the cost lever)
-----------------------------
- Digital pages (extractable text layer)        -> ``pdf_text`` (free, no LLM).
- Scanned / empty / messy pages (no/garbled text) -> ``pdf_vision`` (LLM).

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): routing confines the LLM to the few pages
  that actually need it — the single biggest cost lever in the system.
- Rule 4 (NYC-specific code in nyc/): city-agnostic; nothing NYC here.
- Rule 6 (model behind a config flag): the vision path reads ``EXTRACT_MODEL``.
"""

from __future__ import annotations

from ingest.parse import PageLayout, ParsedDoc

# Re-export the shared Parse contract so callers can import it from the router too.
__all__ = ["ParsedDoc", "PageLayout", "route", "needs_vision"]


def needs_vision(page: PageLayout, *, min_chars: int = 1) -> bool:
    """Decide whether a single page must go to the vision-LLM fallback.

    Args:
        page: Per-page layout hints from the digital pass.
        min_chars: Minimum digital chars to treat a page as readable text;
            below this the page is considered scanned/empty -> vision.

    Returns:
        ``True`` if the page should be routed to ``pdf_vision`` (scanned/empty/
        messy), ``False`` if the digital ``pdf_text`` result is sufficient.
    """
    raise NotImplementedError(
        "Phase 2: per-page heuristic (char_count < min_chars or is_scanned) -> vision."
    )


def route(pdf_bytes: bytes, *, model: str | None = None) -> ParsedDoc:
    """Parse a PDF into the uniform ParsedDoc, routing each page to text or vision.

    Args:
        pdf_bytes: Raw PDF bytes from a Fetch connector.
        model: Vision model id passed through to ``pdf_vision``; defaults to
            ``EXTRACT_MODEL`` from config when ``None`` (Rule 6).

    Returns:
        One assembled :class:`~ingest.parse.ParsedDoc` — digital pages from
        ``pdf_text``, scanned/messy pages from ``pdf_vision`` — ready for Extract.
    """
    raise NotImplementedError(
        "Phase 2: digital pass via pdf_text, route weak pages to pdf_vision, merge -> ParsedDoc."
    )


# TODO Phase 0: route every page to pdf_text only (vision off) so the 10 hand-labeled
#   agendas parse without crashing (Phase 0 gate); add vision routing in Phase 2.
# TODO Phase 2: tune the needs_vision heuristic on the clustered golden set; record
#   the per-page extractor choice in PageLayout.extractor for eval + cost tracking.
