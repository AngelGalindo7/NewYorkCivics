"""Offline tests for the ZAP connector mapper.

Covers CPC deadline approximation, milestone stage enrichment, address/title from
project_name, and core mapper field contracts.  No network calls; no DB.
"""

from __future__ import annotations

from datetime import date

import pytest

from ingest.extract.schemas import RecordStatus
from ingest.sources.nyc.zap_api import (
    _address_from_project_name,
    _ulurp_milestone_stage,
    _zap_project_to_event,
)

# ── CPC deadline approximation ────────────────────────────────────────────────


def test_cpc_deadline_set_when_in_public_review_and_no_hearing_date():
    rec = {
        "project_id": "P2024M0042",
        "ulurp_numbers": "C 240042 ZMM",
        "public_status": "In Public Review",
        "certified_referred": "2026-05-01T00:00:00.000",
    }
    ev = _zap_project_to_event(rec)
    assert ev.deadline == date(2026, 6, 30)  # 2026-05-01 + 60 days
    assert ev.extras.get("cpc_stage") == "cpc_review"


def test_cpc_deadline_not_set_when_explicit_hearing_date_present():
    rec = {
        "project_id": "P2024M0042",
        "ulurp_numbers": "C 240042 ZMM",
        "public_status": "In Public Review",
        "certified_referred": "2026-05-01T00:00:00.000",
        "hearing_date_1": "2026-06-15T00:00:00.000",
    }
    ev = _zap_project_to_event(rec)
    assert ev.deadline == date(2026, 6, 15)
    assert ev.extras.get("cpc_stage") is None


def test_cpc_deadline_not_set_for_other_statuses():
    rec = {
        "project_id": "P2024M0099",
        "public_status": "Filed",
        "certified_referred": "2026-05-01T00:00:00.000",
    }
    ev = _zap_project_to_event(rec)
    assert ev.deadline is None
    assert ev.extras.get("cpc_stage") is None


def test_cpc_deadline_not_set_when_no_certified_referred():
    rec = {
        "project_id": "P2024M0100",
        "public_status": "In Public Review",
    }
    ev = _zap_project_to_event(rec)
    assert ev.deadline is None


# ── core mapper field contracts ───────────────────────────────────────────────


def test_zap_mapper_source_identity():
    rec = {"project_id": "P2024M0042", "ulurp_numbers": "C 240042 ZMM"}
    ev = _zap_project_to_event(rec)
    assert ev.source_id == "nyc_zap"
    assert ev.source_record_id == "P2024M0042"
    assert ev.project_thread_id == "zap:P2024M0042"


def test_zap_mapper_status_accepted():
    rec = {"project_id": "P2024M0042"}
    ev = _zap_project_to_event(rec)
    assert ev.status == RecordStatus.ACCEPTED
    assert ev.confidence == 1.0


def test_zap_mapper_missing_project_id_raises():
    with pytest.raises(ValueError, match="project_id"):
        _zap_project_to_event({})


# ── _address_from_project_name ────────────────────────────────────────────────


def test_address_from_project_name_strips_rezoning():
    result = _address_from_project_name("58-62 East 125th Street Rezoning")
    assert result == "58-62 East 125th Street"


def test_address_from_project_name_strips_special_permit():
    result = _address_from_project_name("200 East 110th Street Special Permit")
    assert result == "200 East 110th Street"


def test_address_from_project_name_no_known_suffix_returns_none():
    # Can't reliably split if there's no recognisable action keyword at the end.
    assert _address_from_project_name("Some Unknown Project") is None


# ── _ulurp_milestone_stage ────────────────────────────────────────────────────


def test_milestone_dcp_filing_review():
    stage, is_cb = _ulurp_milestone_stage("ZM - Review Filed Land Use Application")
    assert stage == "DCP Filing Review"
    assert is_cb is False


def test_milestone_community_board_review():
    stage, is_cb = _ulurp_milestone_stage("CB - Community Board Review")
    assert stage == "Community Board Review"
    assert is_cb is True


def test_milestone_borough_president():
    stage, is_cb = _ulurp_milestone_stage("BP - Borough President Review")
    assert stage == "Borough President Review"
    assert is_cb is False


def test_milestone_cpc():
    stage, is_cb = _ulurp_milestone_stage("CPC - City Planning Commission Review")
    assert stage == "City Planning Commission Review"
    assert is_cb is False


def test_milestone_empty_string():
    stage, is_cb = _ulurp_milestone_stage("")
    assert stage == ""
    assert is_cb is False


# ── project_name → title and address ─────────────────────────────────────────


def test_title_uses_project_name_when_present():
    rec = {
        "project_id": "2020M0383",
        "project_name": "58-62 East 125th Street Rezoning",
        "public_status": "Filed",
        "current_milestone": "ZM - Review Filed Land Use Application",
    }
    ev = _zap_project_to_event(rec)
    assert ev.title == "58-62 East 125th Street Rezoning"


def test_address_derived_from_project_name_when_no_primary_address():
    rec = {
        "project_id": "2020M0383",
        "project_name": "58-62 East 125th Street Rezoning",
        "public_status": "Filed",
    }
    ev = _zap_project_to_event(rec)
    assert ev.address == "58-62 East 125th Street"


def test_primary_address_takes_precedence_over_project_name():
    rec = {
        "project_id": "2020M0383",
        "project_name": "58-62 East 125th Street Rezoning",
        "primary_address": "58 East 125th Street",
        "public_status": "Filed",
    }
    ev = _zap_project_to_event(rec)
    assert ev.address == "58 East 125th Street"


# ── milestone context in summary ──────────────────────────────────────────────


def test_summary_contains_dcp_review_guidance_for_filed_status():
    rec = {
        "project_id": "2020M0383",
        "project_name": "58-62 East 125th Street Rezoning",
        "public_status": "Filed",
        "current_milestone": "ZM - Review Filed Land Use Application",
        "current_milestone_date": "2026-04-20T00:00:00.000",
    }
    ev = _zap_project_to_event(rec)
    assert "DCP is reviewing" in (ev.summary or "")
    assert "since 2026-04-20" in (ev.summary or "")
    assert "manhattancb11.org" in (ev.summary or "")


def test_summary_contains_cb_hearing_guidance_for_cb_stage():
    rec = {
        "project_id": "2020M0383",
        "project_name": "58-62 East 125th Street Rezoning",
        "public_status": "In Public Review",
        "current_milestone": "CB - Community Board Review",
        "certified_referred": "2026-05-01T00:00:00.000",
    }
    ev = _zap_project_to_event(rec)
    assert "CB11 now has 60 days" in (ev.summary or "")
    assert "manhattancb11.org" in (ev.summary or "")


# ── CB-stage deadline ─────────────────────────────────────────────────────────


def test_cb_stage_sets_deadline_60_days_from_certified_referred():
    rec = {
        "project_id": "2020M0383",
        "public_status": "In Public Review",
        "current_milestone": "CB - Community Board Review",
        "certified_referred": "2026-05-01T00:00:00.000",
    }
    ev = _zap_project_to_event(rec)
    assert ev.deadline == date(2026, 6, 30)  # 2026-05-01 + 60 days


def test_cb_stage_no_deadline_when_no_certified_date():
    rec = {
        "project_id": "2020M0383",
        "public_status": "In Public Review",
        "current_milestone": "CB - Community Board Review",
    }
    ev = _zap_project_to_event(rec)
    assert ev.deadline is None


# ── milestone fields in extras ────────────────────────────────────────────────


def test_current_milestone_stored_in_extras():
    rec = {
        "project_id": "2020M0383",
        "current_milestone": "ZM - Review Filed Land Use Application",
        "current_milestone_date": "2026-04-20T00:00:00.000",
    }
    ev = _zap_project_to_event(rec)
    assert ev.extras.get("current_milestone") == "ZM - Review Filed Land Use Application"
    assert ev.extras.get("current_milestone_date") == "2026-04-20T00:00:00.000"
    assert ev.extras.get("milestone_stage") == "DCP Filing Review"
