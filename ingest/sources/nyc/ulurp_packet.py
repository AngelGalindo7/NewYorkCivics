"""Stage 1 (Fetch) — ULURP land-use packets via the ZAP public API. NYC-SPECIFIC.

Single responsibility: discover and download NYC ULURP (Uniform Land Use Review
Procedure) application packets from the ZAP public API. Each ZAP project exposes
package documents — the formal ULURP land-use application narratives — which are
the dirty PDF source for later Parse and Extract stages.

Design
------
ULURP project IDs come from the ZAP structured connector (``zap_api``), which
already pulls active MN11 applications from Socrata. For each project, this
connector queries the ZAP Heroku API (``zap-api-production.herokuapp.com``) to
find the most recent land-use application package and selects the primary narrative
document (the "LR Item" application report). The document proxy URL is served by
the same Heroku API host.

Discovery does not require httpx — it uses only stdlib ``urllib.request``.
httpx is required only for the final PDF download (``fetch()``).

Rules honored
-------------
- LLM only on dirty inputs: no LLM here; the LLM fires later, in Extract.
- Quote the source: raw PDF bytes are passed verbatim to Parse so every extracted
  fact traces back to a verbatim line in the packet.
- NYC-specific code in nyc/: ZAP API patterns stay in this package.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

try:
    import httpx as _httpx
except ImportError:
    _httpx = None  # type: ignore[assignment]

from ingest.observability import get_logger

# Bound at module level so tests can monkeypatch _iter_zap_events without patching
# the zap_api module directly.
from ingest.sources.nyc.zap_api import iter_zap_events as _iter_zap_events

log = get_logger(__name__)

SOURCE_ID = "nyc_ulurp_packet"
_TIMEOUT = 60.0

# The ZAP portal is an Ember SPA whose backend is this public Heroku app.
# Confirmed 2026-06-29 by decoding the <meta name="labs-zap-search/config/environment">
# tag embedded in zap.planning.nyc.gov — the "host" key points here. The old
# a836-zap.nyc.gov hostname in the original URL template fails public DNS.
_PROJECT_API = "https://zap-api-production.herokuapp.com"

# Package type code for land-use applications, confirmed from the API response for
# project 2020M0383. Other types include 717170012 (Environmental Assessment Statement).
_LAND_USE_PKG_TYPE = "717170011"

_UA = "nyc-civic-ingest/1.0"


@dataclass(frozen=True)
class PacketRef:
    """A discovered ULURP packet, before download.

    Attributes:
        ulurp_number: The ULURP application number (e.g. ``"C 240123 ZMM"``).
        url: Direct PDF proxy link via the ZAP API.
        project_thread_id: Optional cross-source story id, if already known.
        title: Document name as it appears in ZAP.
    """

    ulurp_number: str
    url: str
    project_thread_id: str | None = None
    title: str | None = None


def _fetch_project_json(project_id: str) -> dict:
    """Fetch ZAP project JSON from the public Heroku API.

    Returns an empty dict on any failure so callers can fail-soft.
    """
    url = f"{_PROJECT_API}/projects/{project_id}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        log.warning("ulurp_packet: API %s -> HTTP %d", url, exc.code)
        return {}
    except Exception as exc:
        log.warning("ulurp_packet: API %s failed: %s", url, exc)
        return {}


def _pick_primary_doc(documents: list[dict]) -> dict | None:
    """Select the main narrative document from a package's document list.

    DCP packages always lead with a "DCP Signature Form" at index 0. The primary
    application narrative is typically labelled "LR Item" (Land Review item).
    Falls back to the second document when no "LR Item" label is found, since
    the first is invariably the signature form.
    """
    if not documents:
        return None
    for doc in documents:
        name_lower = (doc.get("name") or "").lower()
        if "lr item" in name_lower or "lr-item" in name_lower:
            return doc
    return documents[1] if len(documents) > 1 else documents[0]


def _build_doc_url(server_relative_url: str, doc_type: str = "package") -> str:
    """Construct the ZAP API proxy URL for one document.

    The proxy serves documents at ``/document/{type}{serverRelativeUrl}`` where
    serverRelativeUrl is a OneDrive item ID path (e.g. ``/01QY2C5K...``).
    """
    return f"{_PROJECT_API}/document/{doc_type}{server_relative_url}"


def discover_packets(ulurp_number: str | None = None) -> list[PacketRef]:
    """Find ULURP packets to fetch for active MN11 applications.

    For each active East Harlem project returned by the ZAP connector, queries
    the ZAP API to find the most recent land-use application package and its
    primary narrative document. Returns one :class:`PacketRef` per project with
    a discoverable packet.

    Args:
        ulurp_number: Restrict discovery to a single ULURP number;
            ``None`` scans all active MN11 applications.

    Returns:
        :class:`PacketRef` list. Returns ``[]`` on any discovery failure — never raises.
    """
    try:
        events = list(_iter_zap_events())
    except Exception as exc:
        log.warning("ulurp_packet: ZAP event pull failed (%s); returning []", exc)
        return []

    refs: list[PacketRef] = []
    seen_project_ids: set[str] = set()

    for event in events:
        if ulurp_number is not None and event.ulurp_number != ulurp_number:
            continue

        project_id = event.source_record_id
        if not project_id or project_id in seen_project_ids:
            continue
        seen_project_ids.add(project_id)

        proj_json = _fetch_project_json(project_id)
        if not proj_json:
            continue

        # Find the most recent land-use application package.
        included = proj_json.get("included") or []
        packages = [
            item
            for item in included
            if item.get("type") == "packages"
            and str(item.get("attributes", {}).get("dcp-packagetype", "")) == _LAND_USE_PKG_TYPE
        ]
        if not packages:
            log.debug("ulurp_packet: project %s has no land-use packages", project_id)
            continue

        packages.sort(
            key=lambda p: p.get("attributes", {}).get("dcp-packagesubmissiondate") or "",
            reverse=True,
        )
        documents = packages[0].get("attributes", {}).get("documents") or []

        primary = _pick_primary_doc(documents)
        if primary is None:
            log.debug("ulurp_packet: project %s latest package has no documents", project_id)
            continue

        server_relative_url = primary.get("serverRelativeUrl")
        if not server_relative_url:
            log.debug("ulurp_packet: project %s doc missing serverRelativeUrl", project_id)
            continue

        refs.append(
            PacketRef(
                ulurp_number=event.ulurp_number or project_id,
                url=_build_doc_url(server_relative_url),
                project_thread_id=event.project_thread_id,
                title=primary.get("name") or f"ULURP packet {project_id}",
            )
        )

    return refs


def fetch(url: str) -> bytes:
    """Download one ULURP packet PDF.

    Args:
        url: Direct PDF proxy link from a :class:`PacketRef`.

    Returns:
        Raw PDF bytes (possibly hundreds of pages), handed verbatim to Parse.

    Raises:
        ImportError: if httpx is not installed.
        httpx.HTTPStatusError: on a non-2xx HTTP response.
    """
    if _httpx is None:
        raise ImportError("httpx is required for ulurp_packet.fetch()")
    with _httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": _UA})
        resp.raise_for_status()
        return resp.content
