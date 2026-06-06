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

import io

from ingest.parse import PageLayout, ParsedDoc


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
    if not pdf_bytes:
        raise ValueError("pdf_bytes is empty; caller must validate input before parsing")
    try:
        import pdfplumber as _pdfplumber

        page_texts: list[str] = []
        layout: list[PageLayout] = []

        with _pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                raw = page.extract_text() or ""
                char_count = len(raw)
                page_texts.append(raw)
                layout.append(
                    PageLayout(
                        page_number=i,
                        char_count=char_count,
                        is_scanned=(char_count == 0),
                        extractor="text",
                    )
                )

        return ParsedDoc(
            text="\n\n".join(page_texts),
            page_images=[],  # Phase 2 will populate this via pdf_route / pdf_vision
            layout=layout,
        )

    except ImportError:
        pass

    try:
        import fitz as _fitz

        page_texts = []
        layout = []

        doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            for i in range(doc.page_count):
                page = doc.load_page(i)
                raw = page.get_text("text") or ""
                char_count = len(raw)
                page_texts.append(raw)
                layout.append(
                    PageLayout(
                        page_number=i + 1,
                        char_count=char_count,
                        is_scanned=(char_count == 0),
                        extractor="text",
                    )
                )
        finally:
            doc.close()

        return ParsedDoc(
            text="\n\n".join(page_texts),
            page_images=[],  # Phase 2 will populate this via pdf_route / pdf_vision
            layout=layout,
        )

    except ImportError:
        pass

    raise RuntimeError(
        "Neither pdfplumber nor PyMuPDF (fitz) is installed. "
        "Install at least one: `pip install pdfplumber` or `pip install pymupdf`."
    )
