"""Stage 1 (Fetch) — community board agendas. NYC-SPECIFIC.

Single responsibility: discover and download NYC community board meeting agendas
(PDFs), and identify which board / meeting each belongs to. This is a DIRTY
source: agendas are PDFs with inconsistent layouts. This module only fetches
bytes + identity metadata; it does NOT parse or extract — those are the
city-agnostic Parse and Extract stages.

CB11 (Manhattan Community Board 11) maintains its meeting calendar in an
Airtable base. The connector hits the Airtable REST API when ``AIRTABLE_TOKEN``
is set; in offline mode (CI, tests, no token) it reads from a local fixture
file at ``ingest/tests/fixtures/cb11_meetings_airtable.json``.

Design notes
------------
- No LLM at this stage: fetch is deterministic.
- Raw PDF bytes are preserved faithfully so every downstream fact can trace
  back to a verbatim line in the original agenda (source grounding).
- Board-specific knowledge (base id, table id) stays in this package (nyc/).
- The ~59-board roster is Phase 2; this connector handles MN11 only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from ingest.observability import get_logger

log = get_logger(__name__)

SOURCE_ID = "nyc_cb_mn11"

_AIRTABLE_BASE_ID = "apphZWGKrurmBYkuh"
_AIRTABLE_TABLE_ID = "tbldWVutSlb06he2b"
_AIRTABLE_API_URL = f"https://api.airtable.com/v0/{_AIRTABLE_BASE_ID}/{_AIRTABLE_TABLE_ID}"
_FIXTURE_PATH = (
    Path(__file__).parent.parent.parent / "tests" / "fixtures" / "cb11_meetings_airtable.json"
)
_TIMEOUT = 20.0


@dataclass(frozen=True)
class AgendaRef:
    """A discovered community board agenda, before download.

    Attributes:
        board: Community board id (e.g. ``"MN11"``). NYC-SPECIFIC.
        url: Direct link to the agenda PDF (pre-signed Airtable CDN URL).
        meeting_date: ISO date of the meeting (``YYYY-MM-DD``), if available.
        title: Meeting name as it appears in the Airtable calendar.
        meeting_type: Committee or meeting type (e.g. ``"Full Board"``).
        location: Meeting venue, if recorded.
        register_url: Public registration link for attendees, if available.
    """

    board: str
    url: str
    meeting_date: str | None = None
    title: str | None = None
    meeting_type: str | None = None
    location: str | None = None
    register_url: str | None = None


def _parse_airtable_records(data: dict) -> list[AgendaRef]:
    """Map a raw Airtable API response body into AgendaRefs.

    Records without an uploaded agenda PDF are skipped.
    """
    refs: list[AgendaRef] = []
    for record in data.get("records", []):
        fields = record.get("fields", {})
        attachments = fields.get("Agenda", [])
        if not attachments:
            continue
        agenda_url = attachments[0].get("url", "")
        if not agenda_url:
            continue

        raw_date = fields.get("Date")
        meeting_date = raw_date[:10] if raw_date else None

        refs.append(
            AgendaRef(
                board="MN11",
                url=agenda_url,
                meeting_date=meeting_date,
                title=fields.get("Name"),
                meeting_type=fields.get("Type"),
                location=fields.get("Location"),
                register_url=fields.get("Register to Attend"),
            )
        )
    return refs


def _fetch_from_airtable(token: str) -> list[AgendaRef]:
    """Page through the Airtable API and return all records with an agenda PDF."""
    if httpx is None:
        log.warning("httpx not installed; Airtable CB11 fetch skipped")
        return []

    headers = {"Authorization": f"Bearer {token}"}
    refs: list[AgendaRef] = []
    params: dict[str, str] = {}

    while True:
        try:
            resp = httpx.get(_AIRTABLE_API_URL, headers=headers, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Airtable CB11 fetch failed (%s); returning partial results", exc)
            return refs

        data = resp.json()
        refs.extend(_parse_airtable_records(data))

        offset = data.get("offset")
        if not offset:
            break
        params = {"offset": offset}

    return refs


def _fetch_from_fixture(path: Path | None = None) -> list[AgendaRef]:
    """Load AgendaRefs from the local fixture file (offline / CI mode)."""
    fixture = path or _FIXTURE_PATH
    if not fixture.exists():
        log.warning("CB11 fixture not found at %s; returning []", fixture)
        return []
    data = json.loads(fixture.read_text(encoding="utf-8"))
    return _parse_airtable_records(data)


def discover_agendas(board: str | None = None) -> list[AgendaRef]:
    """Find community board meeting agendas to fetch.

    Uses the Airtable API when ``AIRTABLE_TOKEN`` is set in the environment;
    falls back to the local fixture file for offline development and CI.

    Args:
        board: Restrict discovery to a single board id (e.g. ``"MN11"``).
            ``None`` returns all configured boards (currently only MN11).

    Returns:
        References to agendas with uploaded PDFs ready to fetch.
    """
    if board is not None and board != "MN11":
        return []

    # Lazy import to avoid circular dependency at module load time.
    from ingest.config import get_settings

    token = get_settings().airtable_token or os.environ.get("AIRTABLE_TOKEN", "")
    if token:
        return _fetch_from_airtable(token)

    log.info("AIRTABLE_TOKEN not set; loading CB11 agendas from fixture")
    return _fetch_from_fixture()


def fetch(url: str) -> bytes:
    """Download one agenda PDF.

    Airtable attachment URLs are pre-signed CDN links — no auth header required.

    Args:
        url: Direct PDF URL from an :class:`AgendaRef`.

    Returns:
        Raw PDF bytes, handed verbatim to Parse (preserved for source grounding).
    """
    if httpx is None:
        raise ImportError("httpx is required for cb_agenda.fetch()")
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": "nyc-civic-ingest/1.0"})
        resp.raise_for_status()
        return resp.content
