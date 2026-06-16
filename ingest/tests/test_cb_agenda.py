"""Contract tests for the CB11 agenda fetcher.

Runs fully offline against the cb11_agenda.html fixture.
No network calls, no DB.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingest.sources.nyc.cb_agenda import SOURCE_ID, _parse_agenda_html, discover_agendas

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


# ── parser tests (require bs4, skipped in CI) ─────────────────────────────────


@pytest.fixture
def fixture_html():
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_parse_returns_agendarefs(fixture_html):
    pytest.importorskip("bs4")  # skip in CI (pydantic-only)
    refs = _parse_agenda_html(fixture_html)
    assert len(refs) >= 1


def test_parse_all_board_mn11(fixture_html):
    pytest.importorskip("bs4")
    refs = _parse_agenda_html(fixture_html)
    assert all(r.board == "MN11" for r in refs)


def test_parse_urls_are_absolute(fixture_html):
    pytest.importorskip("bs4")
    refs = _parse_agenda_html(fixture_html)
    assert all(r.url.startswith("https://") for r in refs)


def test_parse_skips_non_pdf_links(fixture_html):
    pytest.importorskip("bs4")
    refs = _parse_agenda_html(fixture_html)
    # The fixture has one non-PDF link ("Back to home") — it must be excluded.
    assert all(r.url.endswith(".pdf") for r in refs)


def test_parse_meeting_date_iso_or_none(fixture_html):
    pytest.importorskip("bs4")
    import re

    refs = _parse_agenda_html(fixture_html)
    iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for r in refs:
        if r.meeting_date is not None:
            assert iso_pattern.match(r.meeting_date), f"Bad date: {r.meeting_date!r}"


def test_parse_titles_nonempty(fixture_html):
    pytest.importorskip("bs4")
    refs = _parse_agenda_html(fixture_html)
    assert all(r.title for r in refs)


def test_parse_exactly_five_refs(fixture_html):
    pytest.importorskip("bs4")
    refs = _parse_agenda_html(fixture_html)
    assert len(refs) == 5  # 5 PDF links, 1 non-PDF link excluded
