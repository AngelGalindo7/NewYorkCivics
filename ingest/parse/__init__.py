"""Stage 2 (Parse) — PDF/HTML -> uniform ParsedDoc. CITY-AGNOSTIC.

Single responsibility: turn a fetched document into one uniform shape that the
Extract stage consumes, regardless of source or city. Parse knows nothing about
NYC. Structured JSON feeds (Legistar, DOB/HPD, ZAP) SKIP this stage entirely —
only dirty inputs (PDFs) reach Parse.

The uniform output contract
---------------------------
Parse always returns a :class:`ParsedDoc`:

- ``text``        — best-effort extracted text for the whole document.
- ``page_images`` — per-page rendered images (for the vision-LLM fallback / Extract).
- ``layout``      — per-page layout/structure hints (page count, char counts,
                    routing decisions) so Extract and routing can reason about it.

This shape is defined ONCE here and imported by ``pdf_text``, ``pdf_vision``,
``pdf_route``, and the Extract stage — keep it the single shared contract.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): only PDFs reach Parse; vision-LLM fires only
  on scanned/messy pages (see ``pdf_vision`` / ``pdf_route``), never on clean text.
- Rule 4 (NYC-specific code in nyc/): nothing here mentions NYC.
- Rule 6 (model behind a config flag): the vision fallback reads ``EXTRACT_MODEL``.

"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PageLayout:
    """Per-page layout/structure hints used for routing and downstream reasoning.

    Attributes:
        page_number: 1-based page index.
        char_count: Characters of digital text extracted from this page (0 = likely
            scanned/empty -> route to vision; see ``pdf_route``).
        is_scanned: Best-effort guess that the page is an image scan, not digital text.
        extractor: Which extractor produced this page's text (``"text"`` or ``"vision"``).
    """

    page_number: int
    char_count: int = 0
    is_scanned: bool = False
    extractor: str | None = None


@dataclass
class ParsedDoc:
    """The uniform Parse output consumed by Extract (CITY-AGNOSTIC contract).

    Parse ALWAYS returns this shape, whatever the source or routing path. It is the
    single seam between dirty-input fetching and city-agnostic extraction.

    Attributes:
        text: Best-effort full-document text (digital + vision pages merged).
        page_images: Per-page rendered images, indexed by page order; the
            vision-LLM and Extract read these. Bytes kept opaque at the contract level.
        layout: Per-page :class:`PageLayout` hints (page count, char counts, routing).
        source_id: Connector id of the originating source (for provenance / SoR key).
        source_record_id: Per-source record id (for the SoR key, Rule 15).
    """

    text: str = ""
    page_images: list[bytes] = field(default_factory=list)
    layout: list[PageLayout] = field(default_factory=list)
    source_id: str | None = None
    source_record_id: str | None = None


__all__ = ["ParsedDoc", "PageLayout"]

# TODO Phase 0: confirm ParsedDoc covers what Extract needs on the 10 hand-labeled
#   CB agendas; widen `layout` only if a real extraction need appears (no premature
#   abstraction, Rule 16).
