"""Offline tests for ingest/sources/nyc/ulurp_packet.py.

All tests run fully offline — no network, no Socrata, no LLM. Live feeds and
httpx are either absent (CI) or monkeypatched out.
"""

from __future__ import annotations

import json
from pathlib import Path

from ingest.sources.nyc.ulurp_packet import (
    PacketRef,
    _build_doc_url,
    _pick_primary_doc,
    discover_packets,
)

_FIXTURES = Path(__file__).parent / "fixtures"

# Minimal ZAP API project response with one land-use package containing two docs.
_FAKE_PROJECT_JSON = {
    "included": [
        {
            "type": "packages",
            "id": "pkg-abc",
            "attributes": {
                "dcp-packagetype": "717170011",
                "dcp-packagesubmissiondate": "2024-01-15T10:00:00.000Z",
                "documents": [
                    {
                        "name": "0.-DCP-Signature-Form.pdf",
                        "serverRelativeUrl": "/01ABCDEFSIGNFORM",
                    },
                    {
                        "name": "1.-123-Main-St---LR-Item-1-15-24.pdf",
                        "serverRelativeUrl": "/01ABCDEFLRITEM",
                    },
                ],
            },
        }
    ]
}


def _load_fixture_events() -> list:
    """Load the ZAP-event-shaped fixture and return as CivicEvent objects."""
    from ingest.extract.schemas import CivicEvent

    rows = json.loads((_FIXTURES / "ulurp_packet_zap_events.json").read_text())
    return [CivicEvent.model_validate(row) for row in rows]


# --------------------------------------------------------------------------- #
# Import safety                                                                #
# --------------------------------------------------------------------------- #


def test_module_imports_clean() -> None:
    """ulurp_packet imports cleanly with only pydantic installed."""
    import importlib

    importlib.import_module("ingest.sources.nyc.ulurp_packet")


# --------------------------------------------------------------------------- #
# _pick_primary_doc                                                            #
# --------------------------------------------------------------------------- #


def test_pick_primary_doc_prefers_lr_item() -> None:
    """Picks the doc labelled 'LR Item' when present."""
    docs = [
        {"name": "0.-DCP-Signature-Form.pdf", "serverRelativeUrl": "/01SIG"},
        {"name": "1.-Address---LR-Item-3-30-26.pdf", "serverRelativeUrl": "/01LR"},
        {"name": "2.-Zoning-Map.pdf", "serverRelativeUrl": "/01MAP"},
    ]
    result = _pick_primary_doc(docs)
    assert result is not None
    assert result["serverRelativeUrl"] == "/01LR"


def test_pick_primary_doc_fallback_to_second() -> None:
    """Falls back to index 1 when no 'LR Item' label is present (index 0 is signature form)."""
    docs = [
        {"name": "0.-DCP-Signature-Form.pdf", "serverRelativeUrl": "/01SIG"},
        {"name": "E-125th-Street_EAS_11.4.2025.pdf", "serverRelativeUrl": "/01EAS"},
    ]
    result = _pick_primary_doc(docs)
    assert result is not None
    assert result["serverRelativeUrl"] == "/01EAS"


def test_pick_primary_doc_single_item() -> None:
    """Returns the only doc when there is just one."""
    docs = [{"name": "only.pdf", "serverRelativeUrl": "/01ONLY"}]
    result = _pick_primary_doc(docs)
    assert result is not None
    assert result["serverRelativeUrl"] == "/01ONLY"


def test_pick_primary_doc_empty() -> None:
    """Returns None for an empty document list."""
    assert _pick_primary_doc([]) is None


# --------------------------------------------------------------------------- #
# _build_doc_url                                                               #
# --------------------------------------------------------------------------- #


def test_build_doc_url_package() -> None:
    """Package doc URL uses the /document/package prefix."""
    url = _build_doc_url("/01ABCDEF12345")
    assert "zap-api-production.herokuapp.com" in url
    assert "/document/package/01ABCDEF12345" in url


def test_build_doc_url_artifact() -> None:
    """Artifact doc URL uses the /document/artifact prefix."""
    url = _build_doc_url("/01ABCDEF12345", doc_type="artifact")
    assert "/document/artifact/01ABCDEF12345" in url


# --------------------------------------------------------------------------- #
# discover_packets — graceful degradation                                      #
# --------------------------------------------------------------------------- #


def test_discover_packets_iter_failure_returns_empty(monkeypatch) -> None:
    """If iter_zap_events raises, discover_packets returns [] without propagating."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(up_mod, "_iter_zap_events", _boom)
    assert discover_packets() == []


def test_discover_packets_unknown_ulurp(monkeypatch) -> None:
    """A ULURP number not present in the ZAP feed produces an empty list."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    fixture_events = _load_fixture_events()
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter(fixture_events))

    result = discover_packets(ulurp_number="C 999999 ZMM")  # not in fixture
    assert result == []


def test_discover_packets_returns_refs(monkeypatch) -> None:
    """discover_packets returns one PacketRef per project with a land-use package."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    fixture_events = _load_fixture_events()
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter(fixture_events))
    monkeypatch.setattr(up_mod, "_fetch_project_json", lambda pid: _FAKE_PROJECT_JSON)

    result = discover_packets()
    assert len(result) == len(fixture_events)
    for ref in result:
        assert isinstance(ref, PacketRef)
        assert ref.url.startswith("https://")
        assert "zap-api-production.herokuapp.com" in ref.url
        assert ref.ulurp_number
        assert ref.project_thread_id is not None


def test_discover_packets_lr_item_url_selected(monkeypatch) -> None:
    """The URL in the returned ref points to the LR Item doc, not the signature form."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    fixture_events = _load_fixture_events()[:1]
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter(fixture_events))
    monkeypatch.setattr(up_mod, "_fetch_project_json", lambda pid: _FAKE_PROJECT_JSON)

    result = discover_packets()
    assert len(result) == 1
    assert "01ABCDEFLRITEM" in result[0].url


def test_discover_packets_no_packages_skipped(monkeypatch) -> None:
    """Projects with no land-use packages produce no PacketRef."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    fixture_events = _load_fixture_events()
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter(fixture_events))
    no_pkg_json = {"included": [{"type": "milestones", "id": "x", "attributes": {}}]}
    monkeypatch.setattr(up_mod, "_fetch_project_json", lambda pid: no_pkg_json)

    result = discover_packets()
    assert result == []


def test_discover_packets_api_failure_skipped(monkeypatch) -> None:
    """Projects whose API call fails produce no PacketRef (fail-soft)."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    fixture_events = _load_fixture_events()
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter(fixture_events))
    monkeypatch.setattr(up_mod, "_fetch_project_json", lambda pid: {})

    result = discover_packets()
    assert result == []


def test_fetch_raises_without_httpx(monkeypatch) -> None:
    """fetch() raises ImportError when httpx is not installed."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    monkeypatch.setattr(up_mod, "_httpx", None)

    try:
        up_mod.fetch("https://example.com/fake.pdf")
        raise AssertionError("expected ImportError")
    except ImportError:
        pass
