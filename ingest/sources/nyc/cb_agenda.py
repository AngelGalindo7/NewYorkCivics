"""Stage 1 (Fetch) — community board agendas. NYC-SPECIFIC.

Single responsibility: discover and download NYC community board meeting agendas
(PDFs), and identify which board / meeting each belongs to. This is a DIRTY
source: agendas are PDFs across ~59 board websites with wildly inconsistent
layouts. This module only fetches bytes + identity metadata; it does NOT parse or
extract — those are the city-agnostic Parse and Extract stages.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): no LLM here. Fetch is deterministic; the LLM
  fires later, in Extract, on the parsed PDF.
- Rule 3 (quote the source): preserve raw bytes faithfully so every downstream
  fact can trace back to a verbatim line in the original agenda.
- Rule 4 (NYC-specific code in nyc/): board URLs and the ~59-board roster are NYC
  knowledge and stay in this package.

This is one of the two PDF connectors (with ``ulurp_packet``). The ~59 boards
cluster by website template; Phase 2 builds fetchers per cluster (likely 5-20),
not per board.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

try:
    import httpx
except ImportError:
    httpx = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

from ingest.observability import get_logger

log = get_logger(__name__)

SOURCE_ID = "nyc_cb_mn11"
_LISTING_URL = (
    "https://www.nyc.gov/site/manhattancb11/meetings/meeting-notices.page"
    # The nyc.gov/site/manhattancb11 subdomain was retired ~2026.
    # Update this URL when CB11 migrates to its new home.
)
_TIMEOUT = 20.0

# Patterns to extract a meeting date from link text.
# Tries long month name, abbreviated month name, then numeric slashes/dashes.
_DATE_PATTERNS = [
    (
        re.compile(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+(\d{1,2}),?\s+(\d{4})\b"
        ),
        "long",
    ),
    (
        re.compile(
            r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(\d{4})\b"
        ),
        "short",
    ),
    (
        re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b"),
        "numeric",
    ),
]

_MONTH_LONG = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}
_MONTH_SHORT = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


@dataclass(frozen=True)
class AgendaRef:
    """A discovered community board agenda, before download.

    Attributes:
        board: Community board id (e.g. ``"MN07"`` for Manhattan CB7). NYC-SPECIFIC.
        url: Direct link to the agenda PDF.
        meeting_date: ISO date of the meeting, if discoverable from the listing.
        title: Human label as it appears on the source site, if any.
    """

    board: str
    url: str
    meeting_date: str | None = None
    title: str | None = None


def _extract_date(text: str) -> str | None:
    """Try to parse an ISO date string from free-form link text."""
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.search(text)
        if m is None:
            continue
        if kind == "long":
            month = _MONTH_LONG.get(m.group(1))
            if month is None:
                continue
            day = int(m.group(2))
            year = int(m.group(3))
        elif kind == "short":
            month = _MONTH_SHORT.get(m.group(1))
            if month is None:
                continue
            day = int(m.group(2))
            year = int(m.group(3))
        else:  # numeric: MM/DD/YYYY or MM-DD-YYYY
            month = int(m.group(1))
            day = int(m.group(2))
            year = int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _parse_agenda_html(html: str) -> list[AgendaRef]:
    """Parse CB11 meeting-notices HTML into a list of AgendaRefs.

    Internal helper — not in the public API, but extracted so tests can call it
    without network.
    """
    if BeautifulSoup is None:
        return []

    soup = BeautifulSoup(html, "html.parser")
    refs: list[AgendaRef] = []
    seen: set[str] = set()

    for tag in soup.find_all("a", href=True):
        href = str(tag["href"])
        if not href or not href.lower().endswith(".pdf"):
            continue
        url = urljoin("https://www.nyc.gov", href)
        if url in seen:
            continue
        seen.add(url)
        title = tag.get_text(strip=True)
        meeting_date = _extract_date(title)
        refs.append(AgendaRef(board="MN11", url=url, meeting_date=meeting_date, title=title))

    return refs


def discover_agendas(board: str | None = None) -> list[AgendaRef]:
    """Find new community board agendas to fetch.

    Args:
        board: Restrict discovery to a single board id; ``None`` scans all
            configured boards.

    Returns:
        References to agendas not yet ingested (caller dedups against the store).
    """
    if board is not None and board != "MN11":
        return []  # Phase 2 handles the full 59-board roster
    if httpx is None:
        log.warning("httpx not installed; cb_agenda.discover_agendas skipped")
        return []
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(_LISTING_URL, headers={"User-Agent": "nyc-civic-ingest/1.0"})
            resp.raise_for_status()
    except Exception as exc:
        log.warning("CB11 agenda listing fetch failed (%s); returning []", exc)
        return []
    return _parse_agenda_html(resp.text)


def fetch(url: str) -> bytes:
    """Download one agenda PDF.

    Args:
        url: Direct PDF link from an :class:`AgendaRef`.

    Returns:
        Raw PDF bytes, handed verbatim to Parse (preserved for source grounding,
        Rule 3).
    """
    if httpx is None:
        raise ImportError("httpx is required for cb_agenda.fetch()")
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": "nyc-civic-ingest/1.0"})
        resp.raise_for_status()
        return resp.content


# TODO Phase 2: build the board roster + per-cluster fetchers; verify real cluster
#   count in week 4 (may be 15-20; the long tail is where time goes).
# TODO Phase 2: link discovered agendas to a project_thread_id where the meeting
#   references a known ULURP/ZAP item (Rule 7).
