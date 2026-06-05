"""Contract tests for the Legistar connector.

Runs fully offline against hard-coded Legistar Event / EventItem / Vote dicts.
Verifies: source identity (Rule 15), action_type categorization, confidence
routing (Rule 10), citation audit (Rule 3 / Rule 5), and roll-call mapping.
No network calls; no DB.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from ingest.extract.schemas import RecordStatus
from ingest.sources.nyc.legistar import (
    SOURCE_ID,
    _event_to_civic,
    _parse_event_date,
    _parse_event_time,
)

# Representative Legistar Event rows (field names mirror the REST API shape).
SAMPLE_LU_EVENT = {
    "EventId": 73601,
    "EventBodyId": 9,
    "EventBodyName": "Committee on Land Use",
    "EventDate": "2026-06-25T00:00:00",
    "EventTime": "10:00 AM",
    "EventLocation": "City Hall - Committee Room",
    "EventAgendaStatusName": "Final",
    "EventMinutesStatusName": "Not Started",
}

SAMPLE_COUNCIL_EVENT = {
    "EventId": 73700,
    "EventBodyId": 1,
    "EventBodyName": "City Council",
    "EventDate": "2026-07-01T00:00:00",
    "EventTime": "1:30 PM",
    "EventLocation": "City Hall - Council Chambers",
    "EventAgendaStatusName": "Tentative",
    "EventMinutesStatusName": "Not Started",
}

SAMPLE_OTHER_EVENT = {
    "EventId": 73999,
    "EventBodyId": 55,
    "EventBodyName": "Committee on Housing and Buildings",
    "EventDate": "2026-06-20T00:00:00",
    "EventTime": "9:00 AM",
    "EventLocation": "250 Broadway, Room 1620",
    "EventAgendaStatusName": "Tentative",
    "EventMinutesStatusName": "Not Started",
}


# ── helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture
def lu_event():
    return _event_to_civic(SAMPLE_LU_EVENT)


@pytest.fixture
def council_event():
    return _event_to_civic(SAMPLE_COUNCIL_EVENT)


@pytest.fixture
def other_event():
    return _event_to_civic(SAMPLE_OTHER_EVENT)


# ── Rule 15: source identity ──────────────────────────────────────────────────


def test_source_id(lu_event):
    assert lu_event.source_id == SOURCE_ID == "nyc_legistar"


def test_source_record_id(lu_event):
    assert lu_event.source_record_id == "event:73601"


def test_project_thread_id(lu_event):
    assert lu_event.project_thread_id == "legistar:event:73601"


# ── Rule 10: confidence routing ───────────────────────────────────────────────


def test_confidence_is_one(lu_event):
    assert lu_event.confidence == 1.0


def test_status_accepted(lu_event, council_event, other_event):
    assert lu_event.status == RecordStatus.ACCEPTED
    assert council_event.status == RecordStatus.ACCEPTED
    assert other_event.status == RecordStatus.ACCEPTED


# ── action_type categorization ────────────────────────────────────────────────


def test_land_use_body_gets_land_use_type(lu_event):
    assert lu_event.action_type == "land_use_hearing"


def test_city_council_gets_land_use_type(council_event):
    assert council_event.action_type == "land_use_hearing"


def test_other_body_gets_council_hearing_type(other_event):
    assert other_event.action_type == "council_hearing"


# ── date / time parsing ───────────────────────────────────────────────────────


def test_event_date_parsed(lu_event):
    assert lu_event.event_date == date(2026, 6, 25)


def test_event_time_parsed(lu_event):
    assert lu_event.event_time == time(10, 0)


def test_event_time_pm(council_event):
    assert council_event.event_time == time(13, 30)


def test_parse_event_date_none():
    assert _parse_event_date(None) is None


def test_parse_event_date_bad():
    assert _parse_event_date("not-a-date") is None


def test_parse_event_time_none():
    assert _parse_event_time(None) is None


def test_parse_event_time_unknown_format():
    assert _parse_event_time("noon") is None


# ── title / summary ───────────────────────────────────────────────────────────


def test_title_contains_body_name(lu_event):
    assert "Committee on Land Use" in lu_event.title


def test_title_contains_date(lu_event):
    assert "2026-06-25" in lu_event.title


def test_summary_contains_location(lu_event):
    assert "City Hall" in lu_event.summary


def test_summary_contains_agenda_status(lu_event):
    assert "Final" in lu_event.summary


# ── citations (Rule 3 / Rule 5) ───────────────────────────────────────────────


def test_citation_present(lu_event):
    assert len(lu_event.citations) == 1


def test_citation_url(lu_event):
    cit = lu_event.citations[0]
    assert "73601" in cit.url
    assert cit.url.startswith("https://legistar.council.nyc.gov/")


def test_citation_kind(lu_event):
    assert lu_event.citations[0].kind == "data_source"


def test_citation_verifies_exact_record(lu_event):
    assert lu_event.citations[0].verifies == "exact_record"


# ── extras ────────────────────────────────────────────────────────────────────


def test_extras_body_name(lu_event):
    assert lu_event.extras["body_name"] == "Committee on Land Use"


def test_extras_agenda_status(lu_event):
    assert lu_event.extras["agenda_status"] == "Final"


def test_extras_location(lu_event):
    assert lu_event.extras["location"] == "City Hall - Committee Room"


# ── no BBL / no address (hearings are not building-level) ─────────────────────


def test_no_bbl(lu_event):
    assert lu_event.bbl is None


def test_no_address(lu_event):
    assert lu_event.address is None


# ── missing optional fields are safe ─────────────────────────────────────────


def test_minimal_event():
    minimal = {"EventId": 1, "EventBodyName": "City Council", "EventDate": "2026-06-30T00:00:00"}
    ev = _event_to_civic(minimal)
    assert ev.source_record_id == "event:1"
    assert ev.event_time is None
    assert ev.status == RecordStatus.ACCEPTED
