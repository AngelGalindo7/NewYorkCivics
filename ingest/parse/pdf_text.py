"""Stage 2 (Parse) — digital PDF text extraction. CITY-AGNOSTIC.

Single responsibility: extract text and layout from *digital* (non-scanned) PDFs
using pdfplumber / PyMuPDF, and return the uniform ``ParsedDoc``. This is the
fast, free path — no LLM. The routing layer (``pdf_route``) decides which pages
come here vs. the vision fallback.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): NO LLM here — this is deterministic text
  extraction; the LLM only enters via ``pdf_vision`` for pages this path can't read.
- Rule 4 (NYC-specific code in nyc/): city-agnostic; nothing NYC here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingest.parse import ParsedDoc

if TYPE_CHECKING:
    # Heavy optional libs are imported lazily inside the implementation so the
    # stub imports cleanly before deps are installed.
    import pdfplumber  # noqa: F401


def extract_text(pdf_bytes: bytes) -> ParsedDoc:
    """Extract digital text + layout from a PDF into the uniform ParsedDoc.

    Args:
        pdf_bytes: Raw PDF bytes from a Fetch connector.

    Returns:
        A :class:`~ingest.parse.ParsedDoc` whose ``text`` and per-page ``layout``
        come from digital extraction; pages with no extractable text are left for
        ``pdf_route`` to send to the vision fallback. No ``page_images`` rendered
        here.
    """
    raise NotImplementedError("Phase 0: pdfplumber/PyMuPDF digital text extraction -> ParsedDoc.")


# TODO Phase 0: implement on the 10 hand-labeled CB agendas; gate is all 10 parse
#   without crashing.
# TODO Phase 2: render page_images here (or in pdf_route) so the vision fallback and
#   Extract have images for scanned/messy pages.
