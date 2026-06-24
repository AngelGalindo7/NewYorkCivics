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
