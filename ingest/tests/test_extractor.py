"""Offline tests for the LLM extractor's failure routing.

No live model calls: ``_call_llm`` and the google-genai client are monkeypatched.
Covers the one deliberate exception to fail-soft — a temporary model outage
(HTTP 503 / UNAVAILABLE) must raise LLMUnavailableError so callers can
circuit-break — while every other failure keeps returning an empty list.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ingest.extract import extractor
from ingest.extract.extractor import LLMUnavailableError, extract
from ingest.parse import ParsedDoc

_DOC = ParsedDoc(text="MN11 agenda body")


def test_extract_propagates_llm_unavailable(monkeypatch):
    def _unavailable(prompt):
        raise LLMUnavailableError("503 UNAVAILABLE: model overloaded")

    monkeypatch.setattr(extractor, "_call_llm", _unavailable)
    with pytest.raises(LLMUnavailableError):
        extract(_DOC, source_id="nyc_cb_mn11")


def test_extract_returns_empty_on_other_llm_failures(monkeypatch):
    def _boom(prompt):
        raise RuntimeError("some other API error")

    monkeypatch.setattr(extractor, "_call_llm", _boom)
    assert extract(_DOC, source_id="nyc_cb_mn11") == []


def _fake_settings():
    return SimpleNamespace(google_api_key="test-key", extract_model="test-model")


def _patch_genai_client(monkeypatch, exc: Exception) -> None:
    """Replace genai.Client with a fake whose generate_content raises ``exc``."""
    from google import genai

    class _Models:
        def generate_content(self, **kwargs):
            raise exc

    class _Client:
        def __init__(self, api_key=None):
            self.models = _Models()

    monkeypatch.setattr("ingest.config.get_settings", _fake_settings)
    monkeypatch.setattr(genai, "Client", _Client)


@pytest.mark.parametrize(
    "message",
    ["503 Service Unavailable", "the model is UNAVAILABLE, please retry"],
)
def test_call_llm_classifies_outage_as_unavailable(monkeypatch, message):
    pytest.importorskip("google.genai")
    _patch_genai_client(monkeypatch, RuntimeError(message))

    with pytest.raises(LLMUnavailableError):
        extractor._call_llm("prompt")


def test_call_llm_reraises_non_outage_errors_unchanged(monkeypatch):
    pytest.importorskip("google.genai")
    _patch_genai_client(monkeypatch, RuntimeError("429 rate limited"))

    with pytest.raises(RuntimeError) as excinfo:
        extractor._call_llm("prompt")
    assert not isinstance(excinfo.value, LLMUnavailableError)


def test_extract_swallows_non_outage_client_errors_end_to_end(monkeypatch):
    pytest.importorskip("google.genai")
    _patch_genai_client(monkeypatch, RuntimeError("400 bad request"))

    assert extract(_DOC, source_id="nyc_cb_mn11") == []
