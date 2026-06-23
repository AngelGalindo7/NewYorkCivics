"""Contract tests for the CB11 agenda fetcher.

Runs fully offline against the cb11_agenda.html fixture and a synthetic CSV
snippet.  No network calls, no DB.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from ingest.sources.nyc.cb_agenda import (
    SOURCE_ID,
    _extract_airtable_embed_url,
    _parse_csv_rows,
    _parse_mm_dd_yyyy,
    discover_agendas,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cb11_agenda.html"


# ── import-safety (runs in CI with only pydantic) ─────────────────────────────


def test_module_imports_clean():
    # Confirmed by the import at the top of this file; no assertion needed.
    assert SOURCE_ID == "nyc_cb_mn11"


def test_discover_agendas_returns_list():
    # discover_agendas() must return a list (possibly empty) and never raise.
    result = discover_agendas(None)
    assert isinstance(result, list)


def test_discover_agendas_unknown_board_returns_empty():
    assert discover_agendas("BK01") == []


# ── fixture HTML tests ────────────────────────────────────────────────────────


@pytest.fixture
def fixture_html():
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_fixture_has_airtable_embed(fixture_html):
    # The cb11m.org calendar page delegates document listing to an Airtable embed.
    embed_url = _extract_airtable_embed_url(fixture_html)
    assert embed_url is not None
    assert "airtable.com/embed" in embed_url


def test_fixture_embed_contains_known_share_id(fixture_html):
    embed_url = _extract_airtable_embed_url(fixture_html)
    assert embed_url is not None
    assert "shrEZxc5vi8McZNFb" in embed_url


# ── date-parsing helper ───────────────────────────────────────────────────────


def test_parse_mm_dd_yyyy_valid():
    assert _parse_mm_dd_yyyy("6/16/2026") == "2026-06-16"
    assert _parse_mm_dd_yyyy("1/1/2025") == "2025-01-01"


def test_parse_mm_dd_yyyy_invalid():
    assert _parse_mm_dd_yyyy("") is None
    assert _parse_mm_dd_yyyy("not-a-date") is None
    assert _parse_mm_dd_yyyy("2026-06-16") is None  # wrong format


# ── CSV parser tests ──────────────────────────────────────────────────────────

_SAMPLE_CSV = textwrap.dedent("""\
    Name,Date,Location,Register to Attend,Agenda,Minutes,Recording,Presentations
    Full Board,6/16/2026,via Video Conference,https://zoom.example/,Full Board Agenda 6-16-26.pdf (https://v5.airtableusercontent.com/fake/agenda1.pdf),,https://youtu.be/fake,
    Full Board,3/17/2026,via Video Conference,https://zoom.example/,Full Board Agenda 3-17-26.pdf (https://v5.airtableusercontent.com/fake/agenda2.pdf),,https://youtu.be/fake,
    Land Use,6/11/2026,via Video Conference,https://zoom.example/,Land Use Cmte Agenda 6-11-26.pdf (https://v5.airtableusercontent.com/fake/agenda3.pdf),,https://youtu.be/fake,
    Full Board,7/18/2019,1664 Park Avenue,,,,,
    New Year's Day - CLOSED,1/1/2026,,,,,,
""")


def test_parse_csv_rows_returns_recent_agendarefs():
    refs = _parse_csv_rows(_SAMPLE_CSV, lookback_days=180)
    assert len(refs) >= 1
    assert all(r.board == "MN11" for r in refs)


def test_parse_csv_rows_filters_old_rows():
    # 2019 row must be excluded regardless of lookback window
    refs = _parse_csv_rows(_SAMPLE_CSV, lookback_days=9999)
    assert not any("2019" in (r.meeting_date or "") for r in refs)


def test_parse_csv_rows_urls_are_absolute():
    refs = _parse_csv_rows(_SAMPLE_CSV, lookback_days=180)
    assert all(r.url.startswith("https://") for r in refs)


def test_parse_csv_rows_meeting_date_is_iso():
    iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    refs = _parse_csv_rows(_SAMPLE_CSV, lookback_days=180)
    for r in refs:
        assert r.meeting_date is not None
        assert iso_pattern.match(r.meeting_date), f"Bad date: {r.meeting_date!r}"


def test_parse_csv_rows_skips_rows_without_agenda_pdf():
    # "New Year's Day - CLOSED" row has no Agenda PDF — must be excluded.
    refs = _parse_csv_rows(_SAMPLE_CSV, lookback_days=9999)
    assert not any("CLOSED" in (r.title or "") for r in refs)


def test_parse_csv_rows_strict_lookback():
    # With lookback_days=1, only today's meetings would qualify — sample has none.
    refs = _parse_csv_rows(_SAMPLE_CSV, lookback_days=1)
    assert refs == []
