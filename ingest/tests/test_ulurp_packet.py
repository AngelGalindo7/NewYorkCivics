"""Offline tests for ingest/sources/nyc/ulurp_packet.py.

All tests run fully offline — no network, no Socrata, no LLM. Live feeds and
httpx are either absent (CI) or monkeypatched out.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

from ingest.sources.nyc.ulurp_packet import PacketRef, _build_packet_url, discover_packets

_FIXTURES = Path(__file__).parent / "fixtures"


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
# _build_packet_url                                                            #
# --------------------------------------------------------------------------- #


def test_build_packet_url_valid() -> None:
    """A well-formed ULURP number produces a non-empty HTTPS URL."""
    url = _build_packet_url("C 240042 ZMM")
    assert url is not None
    assert url.startswith("https://")
    assert "C240042ZMM" in url


def test_build_packet_url_valid_variants() -> None:
    """Other valid ULURP formats (different prefix / action) also produce URLs."""
    assert _build_packet_url("N 230117 ZRM") is not None
    assert _build_packet_url("C 220088 ZSM") is not None


def test_build_packet_url_invalid() -> None:
    """Malformed ULURP numbers return None (fail fast, don't construct a guess URL)."""
    assert _build_packet_url("NOT_A_ULURP") is None
    assert _build_packet_url("") is None
    assert _build_packet_url("12345") is None
    assert _build_packet_url("C 240042") is None  # missing action+borough


# --------------------------------------------------------------------------- #
# discover_packets — graceful degradation                                      #
# --------------------------------------------------------------------------- #


def test_discover_packets_no_httpx(monkeypatch) -> None:
    """With httpx absent, discover_packets returns [] without raising."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    monkeypatch.setattr(up_mod, "_httpx", None)
    result = discover_packets()
    assert result == []


def test_discover_packets_unknown_ulurp(monkeypatch) -> None:
    """A ULURP number not present in the ZAP feed produces an empty list."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    # Provide a non-None httpx sentinel so the httpx guard passes.
    monkeypatch.setattr(up_mod, "_httpx", types.ModuleType("httpx"))
    # Stub iter_zap_events to return the fixture events (no network).
    fixture_events = _load_fixture_events()
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter(fixture_events))

    result = discover_packets(ulurp_number="C 999999 ZMM")  # not in fixture
    assert result == []


def test_discover_packets_returns_refs_for_known_ulurp(monkeypatch) -> None:
    """discover_packets returns a PacketRef for each valid ULURP in the ZAP feed."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    monkeypatch.setattr(up_mod, "_httpx", types.ModuleType("httpx"))
    fixture_events = _load_fixture_events()
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter(fixture_events))

    result = discover_packets()
    assert len(result) == len(fixture_events)
    for ref in result:
        assert isinstance(ref, PacketRef)
        assert ref.url.startswith("https://")
        assert ref.ulurp_number
        assert ref.project_thread_id is not None


def test_discover_packets_iter_failure_returns_empty(monkeypatch) -> None:
    """If iter_zap_events raises, discover_packets returns [] without propagating."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    monkeypatch.setattr(up_mod, "_httpx", types.ModuleType("httpx"))

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(up_mod, "_iter_zap_events", _boom)

    result = discover_packets()
    assert result == []


# --------------------------------------------------------------------------- #
# Compound ULURP strings — _first_ulurp handles semicolons at the ZAP boundary#
# --------------------------------------------------------------------------- #


def test_first_ulurp_semicolon_separated() -> None:
    """_first_ulurp extracts the first candidate from a semicolon-separated compound string.

    The fix belongs in zap_api._first_ulurp (where CivicEvents are built), not
    in discover_packets. By the time events reach discover_packets their
    ulurp_number field is already a single clean value.
    """
    from ingest.sources.nyc.zap_api import _first_ulurp

    assert _first_ulurp("C 240042 ZMM; N 230117 ZRM") == "C 240042 ZMM"
    assert _first_ulurp("230285PQM; 230286PPM") == "230285PQM"
    assert _first_ulurp("C 240042 ZMM") == "C 240042 ZMM"  # single — unchanged
    assert _first_ulurp(None) is None
    assert _first_ulurp("") is None


def test_build_packet_url_uses_first_ulurp_after_split() -> None:
    """The URL built from the first split candidate contains the normalized number."""
    url = _build_packet_url("C 240042 ZMM")
    assert url is not None
    assert "C240042ZMM" in url
    assert url.startswith("https://a836-zap.nyc.gov/document/ulurp/")
