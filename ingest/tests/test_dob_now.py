"""Offline contract tests for the DOB NOW: Build connector (qnmk-7xra).

Verifies the mapper, feed registration, and citation correctness without any
live network calls.  A separate integration test (not in CI) exercises the real
Socrata endpoint.
"""

from __future__ import annotations

from ingest.sources.nyc.citations import audit_citation
from ingest.sources.nyc.dob_hpd import (
    DATASET_DOB_NOW,
    DOB_NOW_PERMITS_FEED,
    SOURCE_ID_DOB_NOW_BUILD,
    _dob_now_permit_to_event,
)

_SAMPLE_REC = {
    "job_filing_number": "M00123456789",
    "work_type": "Alteration Type 1",
    "house_no": "308",
    "street_name": "E 116TH ST",
    "borough": "MANHATTAN",
    "bin": "1060000",
    "block": "1650",
    "lot": "30",
    "c_b_no": "111",
    "issued_date": "2026-03-15T00:00:00",
    "approved_date": "2026-03-14T00:00:00",
    "owner_business_name": "308 EAST HOLDINGS LLC",
    "applicant_first_name": "Jane",
    "applicant_last_name": "Smith",
    "job_description": "Interior renovation",
}


def test_mapper_source_id():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    assert ev.source_id == SOURCE_ID_DOB_NOW_BUILD


def test_mapper_record_id_uses_filing_number():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    assert ev.source_record_id == "M00123456789"


def test_mapper_action_type_is_permit():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    assert ev.action_type == "permit"


def test_mapper_job_type_normalized_to_a1():
    # "Alteration Type 1" → job_type code "A1" in extras (needed by displacement signal).
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    assert ev.extras["job_type"] == "A1"


def test_mapper_title_uses_plain_english():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    assert ev.title is not None
    assert "major alteration" in ev.title.lower()


def test_mapper_address_assembled():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    assert ev.address == "308 E 116TH ST"


def test_mapper_event_date_parsed():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    from datetime import date

    assert ev.event_date == date(2026, 3, 15)


def test_mapper_bbl_derived_from_block_lot():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    # Manhattan boro digit 1 + block 01650 + lot 0030
    assert ev.bbl == "1016500030"


def test_mapper_citations_include_socrata_row():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    row_cites = [c for c in ev.citations if c.verifies == "exact_record"]
    assert len(row_cites) == 1
    assert DATASET_DOB_NOW in row_cites[0].url
    assert "M00123456789" in row_cites[0].url


def test_mapper_citations_include_bis_building_link():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    building_cites = [c for c in ev.citations if c.verifies == "exact_building"]
    assert len(building_cites) >= 1


def test_mapper_all_citations_pass_audit():
    ev = _dob_now_permit_to_event(_SAMPLE_REC)
    for c in ev.citations:
        problem = audit_citation(c)
        assert problem is None, f"Citation audit failed: {problem} — {c}"


def test_mapper_demolition_work_type():
    rec = {**_SAMPLE_REC, "work_type": "Full Demolition", "job_filing_number": "M-DM-001"}
    ev = _dob_now_permit_to_event(rec)
    assert ev.extras["job_type"] == "DM"
    assert "demolition" in ev.title.lower()


def test_mapper_new_building_work_type():
    rec = {**_SAMPLE_REC, "work_type": "New Building", "job_filing_number": "M-NB-001"}
    ev = _dob_now_permit_to_event(rec)
    assert ev.extras["job_type"] == "NB"
    assert "new building" in ev.title.lower()


def test_mapper_unknown_work_type_preserved():
    rec = {**_SAMPLE_REC, "work_type": "PLUMBING", "job_filing_number": "M-PLB-001"}
    ev = _dob_now_permit_to_event(rec)
    # Unknown work types are uppercased and preserved rather than silently dropped.
    assert ev.extras["job_type"] == "PLUMBING"


def test_mapper_missing_filing_number_falls_back_to_composite():
    rec = {**_SAMPLE_REC, "job_filing_number": ""}
    ev = _dob_now_permit_to_event(rec)
    # No exact_record citation when there is no primary key to link on.
    assert not any(c.verifies == "exact_record" for c in ev.citations)


def test_feed_registration_dataset_id():
    assert DOB_NOW_PERMITS_FEED.dataset_id == DATASET_DOB_NOW


def test_feed_registration_primary_key():
    assert "job_filing_number" in DOB_NOW_PERMITS_FEED.primary_key


def test_feed_scope_references_cb_field():
    assert "c_b_no" in DOB_NOW_PERMITS_FEED.scope_where


def test_feed_incremental_cursor():
    assert DOB_NOW_PERMITS_FEED.incremental_cursor == "approved_date"


def test_mapper_empty_work_type_does_not_double_permit():
    rec = {**_SAMPLE_REC, "work_type": "", "job_filing_number": "M-EMPTY-001"}
    ev = _dob_now_permit_to_event(rec)
    assert ev.title == "DOB NOW permit"
    assert "permit permit" not in ev.summary


def test_mapper_a2_work_type():
    rec = {**_SAMPLE_REC, "work_type": "Alteration Type 2", "job_filing_number": "M-A2-001"}
    ev = _dob_now_permit_to_event(rec)
    assert ev.extras["job_type"] == "A2"
