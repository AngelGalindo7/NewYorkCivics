"""Contract tests for the ZAP ULURP connector.

Runs fully offline against hardcoded ZAP project rows (no Socrata, no DB). Locks the
trust-critical properties: source identity (Rule 15), project threading (Rule 7), ULURP
field extraction, confidence routing (Rule 10), citation audit (Rule 5), and that a ZAP
event drops straight into the existing city-agnostic deliver pipeline (Rule 4 seam check).
"""

from __future__ import annotations

from datetime import date

import pytest

from ingest.extract.schemas import RecordStatus
from ingest.sources.nyc import citations as cit_mod
from ingest.sources.nyc.zap_api import SOURCE_ID, _zap_project_to_event

# Representative East Harlem ZAP project row; field names mirror the hgx4-8ukb schema
# as confirmed in ADR 0007. Ids are illustrative (pattern-valid, not live).
SAMPLE_ZAP_REC = {
    "project_id": "P2024M0042",
    "ulurp_numbers": "C 240042 ZMM, N 240042 ZRM",
    "project_brief": (
        "Proposed rezoning from R7-2 to R8A to facilitate construction of a "
        "12-story mixed-use building with 80 affordable units."
    ),
    "public_status": "In Public Review",
    "applicant_name": "East Harlem Realty LLC",
    "lead_action": "Zoning Map Amendment",
    "community_district": "MN11",
    "borough": "Manhattan",
    "primary_address": "123 East 116th Street",
    "certified_referred": "2024-03-15T00:00:00.000",
    "hearing_date_1": "2024-05-20T00:00:00.000",
}

SAMPLE_BBL = "1016500030"


@pytest.fixture
def zap_event():
    return _zap_project_to_event(SAMPLE_ZAP_REC, bbl_value=SAMPLE_BBL)


# --------------------------------------------------------------------------- #
# Rule 15: source identity                                                     #
# --------------------------------------------------------------------------- #

def test_source_id(zap_event):
    assert zap_event.source_id == SOURCE_ID == "nyc_zap"


def test_source_record_id(zap_event):
    assert zap_event.source_record_id == "P2024M0042"


def test_bbl_set(zap_event):
    assert zap_event.bbl == SAMPLE_BBL


def test_bbl_none_is_allowed():
    """Fail-soft: no BBL match emits bbl=None, not a quarantine error (Rule 2)."""
    ev = _zap_project_to_event(SAMPLE_ZAP_REC, bbl_value=None)
    assert ev.bbl is None
    assert ev.source_record_id == "P2024M0042"  # identity still intact


# --------------------------------------------------------------------------- #
# Rule 7: project threading                                                    #
# --------------------------------------------------------------------------- #

def test_project_thread_id(zap_event):
    assert zap_event.project_thread_id == "zap:P2024M0042"


# --------------------------------------------------------------------------- #
# ULURP field extraction                                                       #
# --------------------------------------------------------------------------- #

def test_ulurp_number_is_first_value(zap_event):
    """First ULURP promoted to the canonical ulurp_number field."""
    assert zap_event.ulurp_number == "C 240042 ZMM"


def test_all_ulurp_numbers_preserved_in_extras(zap_event):
    """Full multi-value ULURP string kept verbatim in extras for downstream use."""
    raw = zap_event.extras.get("ulurp_numbers") or ""
    assert "C 240042 ZMM" in raw
    assert "N 240042 ZRM" in raw


def test_applicant_in_extras(zap_event):
    assert zap_event.extras.get("applicant_name") == "East Harlem Realty LLC"


def test_public_status_in_extras(zap_event):
    assert zap_event.extras.get("public_status") == "In Public Review"


# --------------------------------------------------------------------------- #
# Rule 10: confidence routing                                                  #
# --------------------------------------------------------------------------- #

def test_confidence_is_1(zap_event):
    assert zap_event.confidence == 1.0


def test_status_is_accepted(zap_event):
    assert zap_event.status is RecordStatus.ACCEPTED


# --------------------------------------------------------------------------- #
# Dates                                                                        #
# --------------------------------------------------------------------------- #

def test_event_date_from_certified_referred(zap_event):
    assert zap_event.event_date == date(2024, 3, 15)


def test_deadline_from_hearing_date(zap_event):
    """Hearing date is surfaced as the actionable deadline (drives urgency ranking)."""
    assert zap_event.deadline == date(2024, 5, 20)


# --------------------------------------------------------------------------- #
# Rule 3 + citations                                                           #
# --------------------------------------------------------------------------- #

def test_has_data_source_citation(zap_event):
    """Exact-record Socrata link must be present for machine verification."""
    kinds = {c.kind for c in zap_event.citations}
    assert "data_source" in kinds


def test_has_official_lookup_citation(zap_event):
    """Official ZAP portal link must be present for human verification."""
    kinds = {c.kind for c in zap_event.citations}
    assert "official_lookup" in kinds


def test_data_source_url_contains_project_id(zap_event):
    data_src = next(c for c in zap_event.citations if c.kind == "data_source")
    assert "P2024M0042" in data_src.url


def test_official_lookup_url_contains_project_id(zap_event):
    lookup = next(c for c in zap_event.citations if c.kind == "official_lookup")
    assert "P2024M0042" in lookup.url


# --------------------------------------------------------------------------- #
# Rule 5: citation audit                                                       #
# --------------------------------------------------------------------------- #

def test_citation_audit_passes(zap_event):
    """Every citation must pass the structural audit (Rule 5, offline check)."""
    problems = [
        (c.url, cit_mod.audit_citation(c))
        for c in zap_event.citations
        if cit_mod.audit_citation(c) is not None
    ]
    assert problems == [], f"malformed citations: {problems}"


# --------------------------------------------------------------------------- #
# Rule 2: fail-fast on bad input                                               #
# --------------------------------------------------------------------------- #

def test_missing_project_id_raises():
    with pytest.raises(ValueError, match="missing project_id"):
        _zap_project_to_event({})


def test_empty_project_id_raises():
    with pytest.raises(ValueError, match="missing project_id"):
        _zap_project_to_event({"project_id": "   "})


# --------------------------------------------------------------------------- #
# Content sanity                                                               #
# --------------------------------------------------------------------------- #

def test_title_contains_lead_action(zap_event):
    assert "Zoning Map Amendment" in (zap_event.title or "")


def test_summary_contains_project_brief(zap_event):
    assert "R7-2" in (zap_event.summary or "")


def test_action_type_is_rezoning(zap_event):
    assert zap_event.action_type == "rezoning"


# --------------------------------------------------------------------------- #
# Rule 4 seam check: ZAP event drops into the city-agnostic deliver pipeline  #
# --------------------------------------------------------------------------- #

def test_zap_event_matches_on_same_bbl():
    """A ZAP event on the subscriber's BBL lands in the on-your-block band."""
    from ingest.deliver.match import BAND_ON_YOUR_BLOCK, match_subscriber
    from ingest.sources.nyc.harlem_digest import SAMPLE_SUBSCRIBER

    ev = _zap_project_to_event(SAMPLE_ZAP_REC, bbl_value=SAMPLE_SUBSCRIBER["bbl"])
    matched = match_subscriber(SAMPLE_SUBSCRIBER, [ev])
    assert ev in matched[BAND_ON_YOUR_BLOCK]


def test_zap_event_threads_with_hpd_dob_on_same_bbl():
    """Same-BBL ZAP + HPD + DOB events collapse to one building group (Rule 7)."""
    from ingest.deliver.digest import _group_buildings, _to_item
    from ingest.deliver.match import BAND_ON_YOUR_BLOCK, match_subscriber
    from ingest.sources.nyc.harlem_digest import SAMPLE_SUBSCRIBER, _sample_events

    asof = date(2026, 5, 31)
    matched = match_subscriber(SAMPLE_SUBSCRIBER, _sample_events())
    block_items = [
        _to_item(ev, BAND_ON_YOUR_BLOCK, asof)
        for ev in matched[BAND_ON_YOUR_BLOCK]
    ]
    groups = _group_buildings(block_items)
    # HPD violation + DOB permit + displacement signal + ZAP event all share
    # BBL 1016500030, so they collapse to one building group (not four entries).
    assert len(groups) < len(block_items)
    rezoning_items = [
        it
        for g in groups
        for it in g["items"]
        if it["action_type"] == "rezoning"
    ]
    assert rezoning_items, "ZAP event (action_type='rezoning') must appear in the digest"


def test_zap_item_is_accepted_not_review():
    """ZAP structured record is ACCEPTED — it does not inflate the review queue."""
    from ingest.deliver.digest import _to_item

    ev = _zap_project_to_event(SAMPLE_ZAP_REC, bbl_value=SAMPLE_BBL)
    item = _to_item(ev, "on_your_block", date(2026, 5, 31))
    assert item["needs_verification"] is False
