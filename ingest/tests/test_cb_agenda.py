"""Contract tests for the CB11 agenda fetcher (Airtable connector).

Runs fully offline against the cb11_meetings_airtable.json fixture.
No network calls, no DB.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ingest.sources.nyc.cb_agenda import (
    SOURCE_ID,
    AgendaRef,
    _fetch_from_fixture,
    _parse_airtable_records,
    discover_agendas,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cb11_meetings_airtable.json"


# ── import-safety (runs in CI with only pydantic) ─────────────────────────────


def test_module_imports_clean():
    assert SOURCE_ID == "nyc_cb_mn11"


def test_discover_agendas_offline_returns_fixture_count(monkeypatch):
    # No token → fixture fallback; fixture has 3 records with agendas.
    # Patch get_settings so load_dotenv() inside it can't restore the token
    # from .env, and clear the env var so the or-fallback also returns empty.
    import ingest.config as cfg
    from ingest.config import Settings

    monkeypatch.setattr(cfg, "get_settings", lambda: Settings())
    monkeypatch.delenv("AIRTABLE_TOKEN", raising=False)
    result = discover_agendas(None)
    assert len(result) == 3


def test_discover_agendas_unknown_board_returns_empty():
    assert discover_agendas("BK01") == []


# ── _parse_airtable_records ────────────────────────────────────────────────────


@pytest.fixture
def fixture_data():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_parse_skips_records_without_agenda(fixture_data):
    # The fixture has 4 records; 1 has no Agenda attachment.
    refs = _parse_airtable_records(fixture_data)
    assert len(refs) == 3


def test_parse_all_board_mn11(fixture_data):
    refs = _parse_airtable_records(fixture_data)
    assert all(r.board == "MN11" for r in refs)


def test_parse_urls_are_absolute(fixture_data):
    refs = _parse_airtable_records(fixture_data)
    assert all(r.url.startswith("https://") for r in refs)


def test_parse_meeting_date_iso(fixture_data):
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    refs = _parse_airtable_records(fixture_data)
    for r in refs:
        if r.meeting_date is not None:
            assert iso.match(r.meeting_date), f"Bad date: {r.meeting_date!r}"


def test_parse_extended_fields_populated(fixture_data):
    refs = _parse_airtable_records(fixture_data)
    # Full Board record carries all optional fields.
    full_board = next(r for r in refs if r.title == "Full Board Meeting")
    assert full_board.meeting_type == "Full Board"
    assert full_board.location is not None
    assert full_board.register_url is not None


def test_parse_record_missing_optional_fields():
    # Health & Human Services in fixture has no register_url — must not crash.
    data = {
        "records": [
            {
                "id": "recTest",
                "createdTime": "2026-06-01T00:00:00.000Z",
                "fields": {
                    "Name": "Some Committee",
                    "Date": "2026-07-01",
                    "Type": "Some Type",
                    "Agenda": [
                        {
                            "id": "attTest",
                            "url": "https://v5.airtableusercontent.com/test.pdf",
                            "filename": "test.pdf",
                            "size": 1024,
                            "type": "application/pdf",
                        }
                    ],
                },
            }
        ]
    }
    refs = _parse_airtable_records(data)
    assert len(refs) == 1
    assert refs[0].register_url is None
    assert refs[0].location is None


def test_parse_returns_agendaref_instances(fixture_data):
    refs = _parse_airtable_records(fixture_data)
    assert all(isinstance(r, AgendaRef) for r in refs)


# ── _fetch_from_fixture ────────────────────────────────────────────────────────


def test_fetch_from_fixture_loads_real_file():
    refs = _fetch_from_fixture(FIXTURE_PATH)
    assert len(refs) == 3


def test_fetch_from_fixture_missing_path(tmp_path):
    refs = _fetch_from_fixture(tmp_path / "nonexistent.json")
    assert refs == []
