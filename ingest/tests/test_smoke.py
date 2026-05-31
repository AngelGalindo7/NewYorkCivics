"""Smoke tests: the stub package must import cleanly and the contract must hold.

These pass against pure stubs WITHOUT the optional/heavy deps (psycopg, inspect_ai,
pdfplumber, geosupport) installed — that import-safety is exactly the invariant the
TYPE_CHECKING / try-except guards exist to guarantee (Rule 16: keep the guards, and
prove they work). They must not hit any NotImplementedError path. pydantic is the one
genuine import-time dependency (the canonical schema).
"""

from __future__ import annotations

import importlib
import pkgutil

import ingest


def test_all_submodules_import_without_optional_deps() -> None:
    """Every ingest submodule imports cleanly (the guards keep heavy deps optional)."""
    failed: dict[str, str] = {}
    for mod in pkgutil.walk_packages(ingest.__path__, prefix="ingest."):
        if mod.name == "ingest.tests" or mod.name.startswith("ingest.tests."):
            continue
        try:
            importlib.import_module(mod.name)
        except Exception as exc:  # noqa: BLE001 - report ANY import failure as a test failure
            failed[mod.name] = repr(exc)
    assert not failed, f"submodules failed to import without optional deps: {failed}"


def test_civic_event_constructs_and_emits_schema() -> None:
    """The canonical record builds and emits a JSON schema (the contract is real)."""
    from ingest.extract.schemas import CivicEvent, RecordStatus

    ev = CivicEvent(source_id="nyc_cb_agenda", source_record_id="smoke-1")
    assert ev.source_id == "nyc_cb_agenda"
    assert ev.status is RecordStatus.REVIEW  # default routing status (Rule 10)
    schema = CivicEvent.model_json_schema()
    assert schema["type"] == "object"
    assert "source_record_id" in schema["properties"]
