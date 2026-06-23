"""Stage 1 (Fetch) — community board agendas. NYC-SPECIFIC.

Single responsibility: discover and download NYC community board meeting agendas
(PDFs), and identify which board / meeting each belongs to. This is a DIRTY
source: agendas are PDFs across ~59 board websites with wildly inconsistent
layouts. This module only fetches bytes + identity metadata; it does NOT parse or
extract — those are the city-agnostic Parse and Extract stages.

Design notes
------------
- No LLM at this stage: fetch is deterministic; any model-assisted extraction
  fires later, in the Extract stage, on the parsed PDF.
- Raw bytes are preserved faithfully so every downstream fact can trace back to a
  verbatim line in the original agenda (source grounding).
- Board URLs and the ~59-board roster are NYC-specific knowledge and stay in
  this package (nyc/).

CB11-specific: as of 2026, CB11 publishes meeting documents via an Airtable
calendar embedded at cb11m.org/calendar/.  discovery uses the Airtable shared-
view CSV export (no session cookies required; signed attachment URLs in the CSV
are valid for ~1 year).

This is one of the two PDF connectors (with ``ulurp_packet``). The ~59 boards
cluster by website template; Phase 2 builds fetchers per cluster (likely 5-20),
not per board.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta
from urllib.parse import parse_qs

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from ingest.observability import get_logger

log = get_logger(__name__)

SOURCE_ID = "nyc_cb_mn11"

# CB11 moved from nyc.gov/site/manhattancb11 to cb11m.org in 2024-2025.
_LISTING_URL = "https://www.cb11m.org/calendar/"
_TIMEOUT = 20.0

# Airtable shared-view constants embedded in cb11m.org/calendar/.
_AT_APP_ID = "appedcOCWGdk7kppK"
_AT_VIEW_ID = "viw9Uu3M3qvVBKKTF"
_AT_EMBED_RE = re.compile(r'src=["\']([^"\']*airtable\.com/embed/[^"\']+)["\']')

# Regex to find the readSharedViewData query string in the embed page JS.
# Captures the full "?..." portion including requestId + accessPolicy.
_AT_POLICY_RE = re.compile(r'readSharedViewData(\?[^"\'<\s]+)')

# Regex to extract the page-load ID required by Airtable's internal API.
_AT_PAGE_LOAD_RE = re.compile(r'"x-airtable-page-load-id":"([^"]+)"')

# Regex to extract filename+URL pairs from an Airtable CSV attachment cell.
# Cell format: "Filename.pdf (https://v5.airtableusercontent.com/...)"
_AT_ATTACH_RE = re.compile(r"[^(]+\.pdf\s+\((https://[^)]+)\)", re.IGNORECASE)

# Days back from today to include when discovering agendas.
_LOOKBACK_DAYS = 90


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


def _extract_airtable_embed_url(html: str) -> str | None:
    """Return the Airtable embed iframe URL found in the CB11 calendar HTML."""
    m = _AT_EMBED_RE.search(html)
    return m.group(1) if m else None


def _parse_mm_dd_yyyy(s: str) -> str | None:
    """Convert MM/DD/YYYY → ISO date string, or None if unparseable."""
    parts = s.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        return _date(int(parts[2]), int(parts[0]), int(parts[1])).isoformat()
    except (ValueError, IndexError):
        return None


def _parse_csv_rows(csv_text: str, lookback_days: int = _LOOKBACK_DAYS) -> list[AgendaRef]:
    """Parse an Airtable CSV export into AgendaRefs.

    Filters to meetings within the past ``lookback_days`` days that have at
    least one PDF in the Agenda column.  Exported URLs are long-lived Airtable
    CDN tokens (~1 year), so they survive the discover→fetch round-trip.

    Exposed for offline testing without network.
    """
    cutoff = (_date.today() - timedelta(days=lookback_days)).isoformat()
    refs: list[AgendaRef] = []

    reader = csv.DictReader(io.StringIO(csv_text))
    cleaned: dict[str, str] = {}
    for row in reader:
        # Normalise keys: strip leading BOM/whitespace on first key
        cleaned = {k.lstrip("﻿").strip(): v for k, v in row.items()}
        date_iso = _parse_mm_dd_yyyy(cleaned.get("Date", ""))
        if not date_iso or date_iso < cutoff:
            continue
        meeting_name = cleaned.get("Name", "").strip()
        agenda_cell = cleaned.get("Agenda", "")
        for m in _AT_ATTACH_RE.finditer(agenda_cell):
            url = m.group(1)
            title = f"{meeting_name} — {date_iso}" if meeting_name else date_iso
            refs.append(AgendaRef(board="MN11", url=url, meeting_date=date_iso, title=title))

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
            # Step 1: CB11 calendar page → locate Airtable embed URL.
            cal_resp = client.get(_LISTING_URL, headers={"User-Agent": "nyc-civic-ingest/1.0"})
            cal_resp.raise_for_status()
            embed_url = _extract_airtable_embed_url(cal_resp.text)
            if not embed_url:
                log.warning("No Airtable embed iframe found on CB11 calendar page")
                return []

            # Step 2: Airtable embed page → extract access policy + page-load ID.
            embed_resp = client.get(embed_url, headers={"User-Agent": "nyc-civic-ingest/1.0"})
            embed_resp.raise_for_status()
            policy_m = _AT_POLICY_RE.search(embed_resp.text)
            if not policy_m:
                log.warning("Airtable access policy not found in embed page JS")
                return []
            qs_params = parse_qs(policy_m.group(1).lstrip("?"))
            request_id = qs_params.get("requestId", [""])[0]
            access_policy = qs_params.get("accessPolicy", [""])[0]
            if not request_id or not access_policy:
                log.warning("Airtable requestId/accessPolicy missing from embed page")
                return []
            pl_m = _AT_PAGE_LOAD_RE.search(embed_resp.text)
            page_load_id = pl_m.group(1) if pl_m else ""

            # Step 3: CSV export — no session cookies required; signed attachment
            # URLs in the response are valid for ~1 year.
            # The x-airtable-* headers are required by Airtable's internal API
            # even for shared-view endpoints.
            csv_resp = client.get(
                f"https://airtable.com/v0.3/view/{_AT_VIEW_ID}/downloadCsv",
                params={"requestId": request_id, "accessPolicy": access_policy},
                headers={
                    "User-Agent": "nyc-civic-ingest/1.0",
                    "x-time-zone": "America/New_York",
                    "x-airtable-page-load-id": page_load_id,
                    "x-airtable-inter-service-client": "webClient",
                    "x-airtable-application-id": _AT_APP_ID,
                    "x-user-locale": "en",
                },
            )
            csv_resp.raise_for_status()
            return _parse_csv_rows(csv_resp.text)

    except Exception as exc:
        log.warning("CB11 agenda discovery failed (%s); returning []", exc)
        return []


def fetch(url: str) -> bytes:
    """Download one agenda PDF.

    Args:
        url: Direct PDF link from an :class:`AgendaRef`.

    Returns:
        Raw PDF bytes, handed verbatim to Parse (preserved for source grounding).
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
#   references a known ULURP/ZAP item (cross-source correlation).
