"""Stage 1 (Fetch) — ULURP land-use packets. NYC-SPECIFIC.

Single responsibility: discover and download NYC ULURP (Uniform Land Use Review
Procedure) packets and identify which application each belongs to. This is a
DIRTY source: packets are PDFs, often *hundreds of pages* of dense legal prose.
This module only fetches bytes + identity metadata; parsing and extraction happen
in the city-agnostic Parse and Extract stages.

Design
------
ULURP numbers are sourced from the ZAP structured connector (``zap_api``), which
already pulls active MN11 applications from Socrata. Each ZAP ``CivicEvent`` carries
a ``source_record_id`` that is the raw Socrata project ID (e.g. ``"2020M0383"``).

Discovery is a two-step Heroku JSON:API call:

1. ``GET /projects/{project_id}`` — returns a JSON:API envelope whose ``included``
   array contains ``"packages"`` resources. We take the highest-version
   GENERAL_PUBLIC package.
2. ``GET /document/package{serverRelativeUrl}`` — returns the PDF bytes directly.

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
from typing import Any

try:
    import httpx as _httpx
except ImportError:
    _httpx = None  # type: ignore[assignment]

from ingest.observability import get_logger

# Bound at module level so tests can monkeypatch _iter_zap_events without patching
# the zap_api module directly (sodapy is still only needed at call time, not import time).
from ingest.sources.nyc.zap_api import iter_zap_events as _iter_zap_events

log = get_logger(__name__)

SOURCE_ID = "nyc_ulurp_packet"
_TIMEOUT = 60.0

_HEROKU_BASE = "https://zap-api-production.herokuapp.com"
# Unofficial endpoint: NYC Planning's own NestJS backend, reverse-engineered from
# github.com/NYCPlanning/labs-zap-search. No SLA; 404 = project not in this instance.
_VISIBILITY_PUBLIC = 717170003


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


def _fetch_project_packages(project_id: str, *, http: Any) -> list[dict]:
    """Return GENERAL_PUBLIC packages for a ZAP project, sorted by version descending.

    Args:
        project_id: Raw Socrata project ID (e.g. ``"2020M0383"``).
        http: httpx-like module with a ``Client`` context manager.

    Returns:
        List of package ``attributes`` dicts, highest version first.
        Returns ``[]`` on 404 (project not in this Heroku instance) or any error.
    """
    url = f"{_HEROKU_BASE}/projects/{project_id}"
    try:
        with http.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code == 404:
                log.debug(
                    "ulurp_packet: project %s not in Heroku ZAP instance; skipping", project_id
                )
                return []
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        log.warning("ulurp_packet: failed to fetch packages for project %s: %s", project_id, exc)
        return []

    included = body.get("included", [])
    packages = [
        r["attributes"]
        for r in included
        if r.get("type") == "packages"
        and r.get("attributes", {}).get("dcp-visibility") == _VISIBILITY_PUBLIC
    ]
    packages.sort(key=lambda p: p.get("dcp-packageversion", 0), reverse=True)
    return packages


def _pick_document(package: dict) -> dict | None:
    """Return the first eligible document from a package, or ``None``.

    Skips the DCP signature form (name starts with ``"0."``) and any document
    whose name contains ``"signature"`` (case-insensitive).
    """
    for doc in package.get("documents", []):
        name = doc.get("name", "")
        if name.startswith("0.") or "signature" in name.lower():
            continue
        return doc
    return None


def discover_packets(ulurp_number: str | None = None) -> list[PacketRef]:
    """Find ULURP packets to fetch for active MN11 applications.

    Pulls active East Harlem (MN11) ULURP numbers from the ZAP structured connector,
    looks up the highest-version public package for each project via the Heroku ZAP
    API, and constructs a :class:`PacketRef` for each eligible document found.
    Callers deduplicate against what is already stored.

    Args:
        ulurp_number: Restrict discovery to a single application number;
            ``None`` scans all active MN11 applications returned by ZAP.

    Returns:
        :class:`PacketRef` list, one per eligible packet document.
        Returns ``[]`` on any discovery failure — never raises.
    """
    if _httpx is None:
        log.warning("ulurp_packet: httpx not installed; skipping")
        return []

    try:
        events = list(_iter_zap_events())
    except Exception as exc:
        log.warning("ulurp_packet: ZAP event pull failed (%s); returning []", exc)
        return []

    refs: list[PacketRef] = []
    seen_urls: set[str] = set()

    for event in events:
        if not event.ulurp_number:
            continue
        if ulurp_number is not None and event.ulurp_number != ulurp_number:
            continue

        packages = _fetch_project_packages(event.source_record_id, http=_httpx)
        if not packages:
            continue

        doc = _pick_document(packages[0])
        if doc is None:
            log.debug(
                "ulurp_packet: no eligible document in highest-version package for %s",
                event.source_record_id,
            )
            continue

        url = f"{_HEROKU_BASE}/document/package{doc['serverRelativeUrl']}"
        if url in seen_urls:
            continue
        seen_urls.add(url)

        refs.append(
            PacketRef(
                ulurp_number=event.ulurp_number,
                url=url,
                project_thread_id=event.project_thread_id,
                title=doc["name"],
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
