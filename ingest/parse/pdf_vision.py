"""Stage 2 (Parse) — vision-LLM fallback for scanned/messy pages. CITY-AGNOSTIC.

Single responsibility: read page IMAGES with a vision-capable LLM to recover text
from pages that digital extraction (``pdf_text``) can't read — scanned scans, image
overlays, broken text layers. Returns the uniform ``ParsedDoc`` (or per-page text
merged by ``pdf_route``). This is the expensive path and runs ONLY on pages the
router flags.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): the LLM fires here, and ONLY here within
  Parse, and ONLY on scanned/messy pages — never on clean digital text or
  structured JSON. This routing is the single biggest cost lever.
- Rule 6 (model behind a config flag): the model is read from ``EXTRACT_MODEL``
  via config (default ``gemini-2.5-flash``), NEVER hard-coded. Swap models without
  code changes.
- Rule 4 (NYC-specific code in nyc/): city-agnostic; nothing NYC here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingest.parse import ParsedDoc

if TYPE_CHECKING:
    # Vision SDK is optional/lazy so the stub imports before deps are installed.
    pass


def extract_page_image(image: bytes, *, model: str | None = None) -> str:
    """Read one page image with the vision LLM and return its text.

    Args:
        image: Rendered image bytes for a single PDF page.
        model: Vision model id; defaults to ``EXTRACT_MODEL`` from config when
            ``None`` (Rule 6) — never hard-code a model name.

    Returns:
        Best-effort text for the page, to be merged into the document ``ParsedDoc``.
    """
    raise NotImplementedError(
        "Phase 2: vision-LLM read of a page image; model from EXTRACT_MODEL config (Rule 6)."
    )


def extract_doc(parsed: ParsedDoc, *, model: str | None = None) -> ParsedDoc:
    """Fill in text for the vision-flagged pages of a partially-parsed doc.

    Args:
        parsed: A :class:`~ingest.parse.ParsedDoc` whose layout marks which pages
            need the vision fallback (populated by ``pdf_route``).
        model: Vision model id; defaults to ``EXTRACT_MODEL`` from config (Rule 6).

    Returns:
        The same ``ParsedDoc`` with vision-extracted text merged in for flagged pages.
    """
    raise NotImplementedError(
        "Phase 2: run extract_page_image on flagged pages; merge into ParsedDoc."
    )


# TODO Phase 2: read EXTRACT_MODEL via ingest.config.get_settings() (Rule 6); default
#   gemini-2.5-flash.
# TODO Phase 3: per-source daily token budget + circuit-breaker to deterministic
#   fallback / review queue when vision spend spikes.
