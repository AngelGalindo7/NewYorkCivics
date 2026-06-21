"""Offline tests for ingest/sources/nyc/ulurp_packet.py.

All tests run fully offline — no network, no Socrata, no LLM. Live feeds and
httpx are either absent (CI) or monkeypatched out.
"""

from __future__ import annotations

import types

from ingest.sources.nyc.ulurp_packet import discover_packets


def _make_fake_httpx(status_code: int, json_body: dict | None = None):
    # SimpleNamespace doesn't support the context manager protocol through instance
    # attributes — Python resolves __enter__/__exit__ on the type, not the instance.
    # Use a minimal class so `with http.Client(...) as client:` works correctly.
    _json_body = json_body or {}
    _status = status_code

    class FakeResp:
        status_code = _status

        def json(self):
            return _json_body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

    class FakeClient:
        def get(self, url, **kw):
            return FakeResp()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeHttpx:
        @staticmethod
        def Client(**kw):
            return FakeClient()

    return FakeHttpx()


def _make_event(
    source_record_id: str = "2020M0383",
    ulurp_number: str | None = "C 240042 ZMM",
    project_thread_id: str = "zap:2020M0383",
):
    from ingest.extract.schemas import CivicEvent

    return CivicEvent.model_validate(
        {
            "source_id": "nyc_zap",
            "source_record_id": source_record_id,
            "ulurp_number": ulurp_number,
            "project_thread_id": project_thread_id,
            "title": "Zoning Map Amendment — In Public Review",
            "action_type": "rezoning",
        }
    )


# --------------------------------------------------------------------------- #
# Import safety                                                                #
# --------------------------------------------------------------------------- #


def test_module_imports_clean() -> None:
    """ulurp_packet imports cleanly with only pydantic installed."""
    import importlib

    importlib.import_module("ingest.sources.nyc.ulurp_packet")


# --------------------------------------------------------------------------- #
# discover_packets — graceful degradation                                      #
# --------------------------------------------------------------------------- #


def test_discover_packets_no_httpx(monkeypatch) -> None:
    """With httpx absent, discover_packets returns [] without raising."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    monkeypatch.setattr(up_mod, "_httpx", None)
    result = discover_packets()
    assert result == []


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
# discover_packets — ULURP filter                                              #
# --------------------------------------------------------------------------- #


def test_discover_packets_ulurp_filter_excludes_non_matching(monkeypatch) -> None:
    """discover_packets with a ULURP filter skips events that don't match."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    # Use a fake httpx that would succeed if called, so we'd know if the filter
    # was bypassed and actually hit the Heroku API.
    fake_httpx = _make_fake_httpx(200, {"included": []})
    monkeypatch.setattr(up_mod, "_httpx", fake_httpx)

    event = _make_event(ulurp_number="C 240042 ZMM")
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter([event]))

    result = discover_packets(ulurp_number="C 999999 ZMM")
    assert result == []


# --------------------------------------------------------------------------- #
# _fetch_project_packages                                                      #
# --------------------------------------------------------------------------- #


def test_fetch_project_packages_returns_public_packages_sorted(monkeypatch) -> None:
    """Returns only GENERAL_PUBLIC packages sorted by version descending."""
    from ingest.sources.nyc.ulurp_packet import _fetch_project_packages

    json_body = {
        "included": [
            {
                "type": "packages",
                "attributes": {
                    "dcp-name": "2020M0383_Filed LU Package_2",
                    "dcp-packageversion": 2,
                    "dcp-visibility": 717170003,  # GENERAL_PUBLIC
                    "documents": [],
                },
            },
            {
                "type": "packages",
                "attributes": {
                    "dcp-name": "2020M0383_Internal Package_3",
                    "dcp-packageversion": 3,
                    "dcp-visibility": 717170001,  # not public
                    "documents": [],
                },
            },
        ]
    }
    fake_httpx = _make_fake_httpx(200, json_body)
    result = _fetch_project_packages("2020M0383", http=fake_httpx)

    assert len(result) == 1
    assert result[0]["dcp-packageversion"] == 2


def test_fetch_project_packages_404_returns_empty(monkeypatch) -> None:
    """404 from Heroku returns [] without raising."""
    from ingest.sources.nyc.ulurp_packet import _fetch_project_packages

    fake_httpx = _make_fake_httpx(404)
    result = _fetch_project_packages("old_project", http=fake_httpx)
    assert result == []


# --------------------------------------------------------------------------- #
# _pick_document                                                               #
# --------------------------------------------------------------------------- #


def test_pick_document_skips_signature_form() -> None:
    """First eligible doc is returned; DCP signature form (name starts with "0.") is skipped."""
    from ingest.sources.nyc.ulurp_packet import _pick_document

    package = {
        "documents": [
            {"name": "0. DCP Signature Form.pdf", "serverRelativeUrl": "/skip"},
            {"name": "1. Project Description.pdf", "serverRelativeUrl": "/pick"},
        ]
    }
    result = _pick_document(package)
    assert result is not None
    assert result["serverRelativeUrl"] == "/pick"


def test_pick_document_returns_none_for_empty_package() -> None:
    """Package with no eligible documents returns None."""
    from ingest.sources.nyc.ulurp_packet import _pick_document

    package = {
        "documents": [
            {"name": "0. DCP Signature Form.pdf", "serverRelativeUrl": "/skip"},
        ]
    }
    assert _pick_document(package) is None


# --------------------------------------------------------------------------- #
# discover_packets — end-to-end offline                                        #
# --------------------------------------------------------------------------- #


def test_discover_packets_returns_ref_for_project(monkeypatch) -> None:
    """discover_packets returns a PacketRef with Heroku URL for a project with public packages."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    json_body = {
        "included": [
            {
                "type": "packages",
                "attributes": {
                    "dcp-name": "2020M0383_Filed LU Package_1",
                    "dcp-packageversion": 1,
                    "dcp-visibility": 717170003,
                    "documents": [
                        {
                            "name": "1. Project Description.pdf",
                            "serverRelativeUrl": "/01ABCDEF",
                            "timeCreated": "2024-01-01T00:00:00Z",
                        }
                    ],
                },
            }
        ]
    }
    fake_httpx = _make_fake_httpx(200, json_body)
    monkeypatch.setattr(up_mod, "_httpx", fake_httpx)

    event = _make_event(
        source_record_id="2020M0383",
        ulurp_number="C 240042 ZMM",
        project_thread_id="zap:2020M0383",
    )
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter([event]))

    result = discover_packets()

    assert len(result) == 1
    assert result[0].ulurp_number == "C 240042 ZMM"
    assert result[0].url.startswith("https://zap-api-production.herokuapp.com/document/package/")
    assert "/01ABCDEF" in result[0].url
    assert result[0].title == "1. Project Description.pdf"


def test_discover_packets_skips_project_not_in_heroku(monkeypatch) -> None:
    """Project that returns 404 from Heroku is skipped; result is []."""
    import ingest.sources.nyc.ulurp_packet as up_mod

    fake_httpx = _make_fake_httpx(404)
    monkeypatch.setattr(up_mod, "_httpx", fake_httpx)

    event = _make_event(source_record_id="old_project")
    monkeypatch.setattr(up_mod, "_iter_zap_events", lambda *a, **k: iter([event]))

    assert discover_packets() == []
