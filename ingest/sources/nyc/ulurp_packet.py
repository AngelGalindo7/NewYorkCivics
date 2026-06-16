"""Stage 1 (Fetch) — ULURP land-use packets. NYC-SPECIFIC.

Single responsibility: discover and download NYC ULURP (Uniform Land Use Review
Procedure) packets and identify which application each belongs to. This is a
DIRTY source: packets are PDFs, often *hundreds of pages* of dense legal prose.
This module only fetches bytes + identity metadata; parsing and extraction happen
in the city-agnostic Parse and Extract stages.

Design
------
ULURP numbers are sourced from the ZAP structured connector (``zap_api``), which
already pulls active MN11 applications from Socrata. Each validated ULURP number
maps to a packet PDF URL via a deterministic URL template derived from the DCP ZAP
portal (``a836-zap.nyc.gov``). Only the primary packet document is fetched; the
hundreds of boilerplate attachments are skipped.

Rules honored
-------------
- LLM only on dirty inputs: no LLM here; the LLM fires later, in Extract.
- Quote the source: preserve raw bytes so every extracted fact traces back to a
  verbatim line in the packet.
- NYC-specific code in nyc/: the ULURP-number shape and packet locations are NYC
  knowledge and stay in this package.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import httpx as _httpx
except ImportError:
    _httpx = None  # type: ignore[assignment]

from ingest.extract.ulurp_codes import ULURP_PATTERN as _ULURP_RE
from ingest.observability import get_logger

# Bound at module level so tests can monkeypatch _iter_zap_events without patching
# the zap_api module directly (sodapy is still only needed at call time, not import time).
from ingest.sources.nyc.zap_api import iter_zap_events as _iter_zap_events

log = get_logger(__name__)

SOURCE_ID = "nyc_ulurp_packet"
_TIMEOUT = 60.0

# URL pattern for ULURP packet PDFs on the DCP ZAP portal backend.
# Confirmed from the "Documents" tab at https://zap.planning.nyc.gov/projects/{project_id}:
# the primary packet PDF is served at this path, keyed by the normalized ULURP number
# (prefix + 6-digit sequence + 2-letter action code + borough suffix, no spaces, uppercase).
# Example: "C 240042 ZMM" -> "C240042ZMM" -> https://a836-zap.nyc.gov/document/ulurp/C240042ZMM
_ZAP_PACKET_URL_TEMPLATE = "https://a836-zap.nyc.gov/document/ulurp/{normalized}"


@dataclass(frozen=True)
class PacketRef:
    """A discovered ULURP packet, before download.

    Attributes:
        ulurp_number: The ULURP application number (e.g. ``"C 240123 ZMM"``).
            Format validation lives in ``ingest/extract/ulurp_codes.py``.
        url: Direct link to the packet PDF.
        project_thread_id: Optional cross-source story id, if already known.
        title: Human label as it appears on the source site, if any.
    """

    ulurp_number: str
    url: str
    project_thread_id: str | None = None
    title: str | None = None


def _build_packet_url(ulurp_number: str) -> str | None:
    """Construct the packet PDF URL for a ULURP number; ``None`` if malformed.

    Pure, network-free: validates the ULURP number against the canonical pattern
    and assembles the URL deterministically. Callers should treat a ``None``
    return as fail-fast — don't guess at a URL for a malformed number.
    """
    m = _ULURP_RE.match(ulurp_number)
    if not m:
        return None
    normalized = m.group("prefix") + m.group("number") + m.group("action") + m.group("borough")
    return _ZAP_PACKET_URL_TEMPLATE.format(normalized=normalized)


def discover_packets(ulurp_number: str | None = None) -> list[PacketRef]:
    """Find ULURP packets to fetch for active MN11 applications.

    Pulls active East Harlem (MN11) ULURP numbers from the ZAP structured connector,
    validates each against the canonical ULURP format, and constructs a
    :class:`PacketRef` for every valid application. Callers deduplicate against
    what is already stored.

    Args:
        ulurp_number: Restrict discovery to a single application number;
            ``None`` scans all active MN11 applications returned by ZAP.

    Returns:
        :class:`PacketRef` list, one per valid ULURP application.
        Returns ``[]`` on any discovery failure — never raises.
    """
    if _httpx is None:
        log.warning("ulurp_packet: httpx not installed; discover_packets skipped")
        return []

    try:
        events = list(_iter_zap_events())
    except Exception as exc:
        log.warning("ulurp_packet: ZAP event pull failed (%s); returning []", exc)
        return []

    refs: list[PacketRef] = []
    seen_urls: set[str] = set()

    for event in events:
        num = event.ulurp_number
        if not num:
            continue
        if ulurp_number is not None and num != ulurp_number:
            continue
        # Reject malformed numbers — don't construct a guess URL.
        url = _build_packet_url(num)
        if url is None:
            log.warning("ulurp_packet: malformed ULURP number %r; skipping", num)
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        refs.append(
            PacketRef(
                ulurp_number=num,
                url=url,
                project_thread_id=event.project_thread_id,
                title=f"ULURP packet {num}",
            )
        )

    return refs


def fetch(url: str) -> bytes:
    """Download one ULURP packet PDF.

    Args:
        url: Direct PDF link from a :class:`PacketRef`.

    Returns:
        Raw PDF bytes (possibly hundreds of pages), handed verbatim to Parse.
        Callers must handle network failures; this function does not suppress them.

    Raises:
        ImportError: if httpx is not installed.
        httpx.HTTPStatusError: on a non-2xx HTTP response.
    """
    if _httpx is None:
        raise ImportError("httpx is required for ulurp_packet.fetch()")
    with _httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": "nyc-civic-ingest/1.0"})
        resp.raise_for_status()
        return resp.content
