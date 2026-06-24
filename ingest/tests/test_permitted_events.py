"""Offline contract tests for the NYC permitted events connector (tvpp-9vvx).

All tests run without live network calls.  The mapper is tested against fixture
records; geocoding and the full discovery path are tested with monkeypatching.
"""

from __future__ import annotations

from datetime import date

from ingest.sources.nyc.citations import audit_citation
from ingest.sources.nyc.permitted_events import (
    SOURCE_ID,
    _parse_event_dt,
    _permitted_event_to_event,
)

_SAMPLE_REC = {
    "event_id": "EVT-20260701-001",
    "event_name": "East Harlem Block Party",
    "event_type": "Block Party",
    "event_location": "116th St between Lex and Park",
    "start_date_time": "2026-07-04T12:00:00",
    "end_date_time": "2026-07-04T18:00:00",
    "event_borough": "Manhattan",
    "event_contact_phone": "212-555-1234",
    "event_contact_e_mail": "organizer@example.com",
}


def test_mapper_source_id():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    assert ev.source_id == SOURCE_ID


def test_mapper_action_type():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    assert ev.action_type == "permitted_event"


def test_mapper_title_uses_event_name():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    assert ev.title == "East Harlem Block Party"


def test_mapper_record_id_uses_event_id():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    assert ev.source_record_id == "EVT-20260701-001"


def test_mapper_event_date_parsed():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    assert ev.event_date == date(2026, 7, 4)


def test_mapper_address_from_event_location():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    assert ev.address == "116th St between Lex and Park"


def test_mapper_summary_includes_type_and_location():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    assert "Block Party" in ev.summary
    assert "116th St" in ev.summary


def test_mapper_extras_preserved():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    assert ev.extras["event_type"] == "Block Party"
    assert ev.extras["end_date"] == "2026-07-04"
    assert ev.extras["contact_phone"] == "212-555-1234"
    assert ev.extras["contact_email"] == "organizer@example.com"
    assert ev.extras["event_borough"] == "Manhattan"


def test_mapper_citation_is_exact_record():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    exact = [c for c in ev.citations if c.verifies == "exact_record"]
    assert len(exact) == 1
    assert "tvpp-9vvx" in exact[0].url
    assert "EVT-20260701-001" in exact[0].url


def test_mapper_citations_pass_audit():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    for c in ev.citations:
        problem = audit_citation(c)
        assert problem is None, f"Citation audit failed: {problem} — {c}"


def test_mapper_status_accepted():
    ev = _permitted_event_to_event(_SAMPLE_REC)
    from ingest.extract.schemas import RecordStatus

    assert ev.status == RecordStatus.ACCEPTED
    assert ev.confidence == 1.0


def test_mapper_missing_event_name_defaults():
    rec = {**_SAMPLE_REC, "event_name": "", "event_id": "EVT-NONAME"}
    ev = _permitted_event_to_event(rec)
    assert ev.title == "Permitted event"


def test_mapper_missing_event_id_no_citation():
    rec = {**_SAMPLE_REC, "event_id": "", "event_name": "No ID event"}
    ev = _permitted_event_to_event(rec)
    # No citation when there is no primary key to link on.
    assert len(ev.citations) == 0


def test_parse_event_dt_iso_format():
    dt = _parse_event_dt("2026-07-04T12:00:00")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 7
    assert dt.day == 4


def test_parse_event_dt_none_returns_none():
    assert _parse_event_dt(None) is None
    assert _parse_event_dt("") is None


def test_geosearch_cd_returns_none_on_network_error(monkeypatch):
    from ingest.sources.nyc import permitted_events

    def _fail(address):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(permitted_events, "_geosearch_cd", _fail)
    # Confirm the mapper itself never calls _geosearch_cd (geocoding is in iter_permitted_events)
    ev = _permitted_event_to_event(_SAMPLE_REC)
    assert ev.source_id == SOURCE_ID


# ══════════════════════════════════════════════════════════════════════════════
# _geosearch_cd — HTTP layer
# ══════════════════════════════════════════════════════════════════════════════


def test_geosearch_cd_returns_cd_string(monkeypatch):
    # GeoSearch returns addendum.pad.cd as a 3-char string; zfill(3) leaves it unchanged.
    import json
    import urllib.request

    from ingest.sources.nyc.permitted_events import _geosearch_cd

    fixture = json.dumps(
        {"features": [{"properties": {"addendum": {"pad": {"cd": "111"}}}}]}
    ).encode()

    class _Resp:
        def read(self):
            return fixture

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert _geosearch_cd("308 E 116th St") == "111"


def test_geosearch_cd_integer_cd(monkeypatch):
    # GeoSearch v2 returns cd as a string in practice, but json.loads yields a Python int
    # when the JSON value has no quotes (e.g. "cd": 111).  str() + zfill(3) guards this.
    import json
    import urllib.request

    from ingest.sources.nyc.permitted_events import _geosearch_cd

    fixture = json.dumps(
        {"features": [{"properties": {"addendum": {"pad": {"cd": 111}}}}]}
    ).encode()

    class _Resp:
        def read(self):
            return fixture

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert _geosearch_cd("308 E 116th St") == "111"


def test_geosearch_cd_no_features_returns_none(monkeypatch):
    import json
    import urllib.request

    from ingest.sources.nyc.permitted_events import _geosearch_cd

    fixture = json.dumps({"features": []}).encode()

    class _Resp:
        def read(self):
            return fixture

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert _geosearch_cd("308 E 116th St") is None


def test_geosearch_cd_network_error_returns_none(monkeypatch):
    import urllib.request

    from ingest.sources.nyc.permitted_events import _geosearch_cd

    def _raise(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert _geosearch_cd("308 E 116th St") is None


# ══════════════════════════════════════════════════════════════════════════════
# iter_permitted_events — CD filter paths
# ══════════════════════════════════════════════════════════════════════════════

_ITER_SAMPLE_REC = {
    "event_id": "EVT-ITER-001",
    "event_name": "Harlem Block Party",
    "event_type": "Block Party",
    "event_location": "116th St between Lex and Park",
    "start_date_time": "2026-07-04T12:00:00",
    "end_date_time": "2026-07-04T18:00:00",
    "event_borough": "Manhattan",
    "event_contact_phone": None,
    "event_contact_e_mail": None,
}


def _make_fake_get_page(records):
    """Return a _get_page replacement that yields records on the first call, then stops."""
    seen = {"called": False}

    def _fake(client, *, where, limit, offset):
        if not seen["called"]:
            seen["called"] = True
            return records
        return []

    return _fake


class _FakeSocrata:
    def __init__(self, *a, **k):
        pass

    def close(self):
        pass


def test_iter_passes_matching_cd_event(monkeypatch):
    # An event whose geocode returns cd == TARGET_COMMUNITY_DISTRICT must be emitted.
    from ingest.sources.nyc import permitted_events

    # raising=False: Socrata may not exist in the namespace when sodapy is absent.
    monkeypatch.setattr(permitted_events, "Socrata", _FakeSocrata, raising=False)
    monkeypatch.setattr(permitted_events, "_get_page", _make_fake_get_page([_ITER_SAMPLE_REC]))
    monkeypatch.setattr(permitted_events, "_geosearch_cd", lambda addr: "111")

    events = list(permitted_events.iter_permitted_events("111"))
    assert len(events) == 1
    assert events[0].source_record_id == "EVT-ITER-001"


def test_iter_filters_wrong_cd_event(monkeypatch):
    # An event whose geocode returns a different CD must be filtered out.
    from ingest.sources.nyc import permitted_events

    # raising=False: Socrata may not exist in the namespace when sodapy is absent.
    monkeypatch.setattr(permitted_events, "Socrata", _FakeSocrata, raising=False)
    monkeypatch.setattr(permitted_events, "_get_page", _make_fake_get_page([_ITER_SAMPLE_REC]))
    monkeypatch.setattr(permitted_events, "_geosearch_cd", lambda addr: "112")

    events = list(permitted_events.iter_permitted_events("111"))
    assert events == []


def test_iter_skips_blank_location_without_raising(monkeypatch):
    # A record with a blank event_location must hit the continue guard and not raise.
    from ingest.sources.nyc import permitted_events

    blank_rec = {**_ITER_SAMPLE_REC, "event_location": ""}
    # raising=False: Socrata may not exist in the namespace when sodapy is absent.
    monkeypatch.setattr(permitted_events, "Socrata", _FakeSocrata, raising=False)
    monkeypatch.setattr(permitted_events, "_get_page", _make_fake_get_page([blank_rec]))

    events = list(permitted_events.iter_permitted_events("111"))
    assert events == []
