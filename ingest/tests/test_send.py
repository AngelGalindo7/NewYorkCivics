"""Contract tests for the Deliver send adapter (file sink + Resend provider).

Runs fully offline on the sample East Harlem digest (no Socrata, no DB, no network).
Locks the trust-critical send behavior: the file-sink fallback when no provider is set;
the Resend HTTP path (endpoint, payload, auth header) with ``urlopen`` monkeypatched; and
that every failure mode — missing config, an unsupported provider, a non-2xx response, and
an unreviewed digest — raises loudly and never leaks the API key.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import date
from typing import Any

import pytest

from ingest.deliver import send as send_mod
from ingest.deliver.digest import build_digest, render_markdown
from ingest.deliver.match import match_subscriber
from ingest.deliver.send import send_digest
from ingest.sources.nyc.harlem_digest import SAMPLE_SUBSCRIBER, _sample_events

ASOF = date(2026, 5, 31)


@pytest.fixture
def reviewed_digest() -> dict[str, Any]:
    """A built digest with the human-review queue cleared (sendable)."""
    matched = match_subscriber(SAMPLE_SUBSCRIBER, _sample_events())
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    # Simulate a human clearing the queue so the send gate opens.
    digest["review_required"] = False
    digest["review_items"] = []
    return digest


class _FakeResponse:
    """Minimal context-manager stand-in for an http.client.HTTPResponse."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status


def _provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a fully-wired Resend provider with a deterministic gate state."""
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "test_key")
    monkeypatch.setenv("EMAIL_FROM", "digest@example.org")
    # Keep the human-review gate deterministic regardless of a developer's local .env.
    monkeypatch.setenv("BYPASS_HUMAN_REVIEW", "false")


def test_file_sink_fallback_when_provider_unset(tmp_path, reviewed_digest, monkeypatch):
    # No EMAIL_PROVIDER -> the file sink writes the rendered digest to disk.
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.setenv("BYPASS_HUMAN_REVIEW", "false")

    path = send_digest(reviewed_digest, SAMPLE_SUBSCRIBER, sink_dir=tmp_path)

    assert path.exists()
    assert path.suffix == ".md"
    body = path.read_text(encoding="utf-8")
    assert body == render_markdown(reviewed_digest)
    assert body.startswith("# ")


def test_resend_happy_path_posts_expected_request(tmp_path, reviewed_digest, monkeypatch):
    _provider_env(monkeypatch)

    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        captured["calls"] = captured.get("calls", 0) + 1
        return _FakeResponse(200)

    monkeypatch.setattr(send_mod.urllib.request, "urlopen", fake_urlopen)

    result = send_digest(reviewed_digest, SAMPLE_SUBSCRIBER)

    # Provider path returns the sentinel path, not a file.
    assert str(result) == "<sent via resend>"
    assert captured["calls"] == 1

    req = captured["req"]
    assert req.full_url == send_mod.RESEND_ENDPOINT
    assert req.get_method() == "POST"
    # Authorization carries the bearer key; never the file sink.
    assert req.get_header("Authorization") == "Bearer test_key"
    assert req.get_header("Content-type") == "application/json"

    payload = json.loads(req.data.decode("utf-8"))
    assert payload["from"] == "digest@example.org"
    assert payload["to"] == [SAMPLE_SUBSCRIBER["email"]]
    assert payload["subject"] == reviewed_digest["subject"]
    assert payload["text"] == render_markdown(reviewed_digest)


def test_resend_raises_when_api_key_missing(reviewed_digest, monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("EMAIL_FROM", "digest@example.org")
    monkeypatch.setenv("BYPASS_HUMAN_REVIEW", "false")

    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        send_digest(reviewed_digest, SAMPLE_SUBSCRIBER)


def test_resend_raises_when_email_from_missing(reviewed_digest, monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "test_key")
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    monkeypatch.setenv("BYPASS_HUMAN_REVIEW", "false")

    with pytest.raises(ValueError, match="EMAIL_FROM"):
        send_digest(reviewed_digest, SAMPLE_SUBSCRIBER)


def test_resend_transport_error_raises_without_leaking_key(reviewed_digest, monkeypatch):
    _provider_env(monkeypatch)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            url=send_mod.RESEND_ENDPOINT, code=422, msg="Unprocessable", hdrs=None, fp=None
        )

    monkeypatch.setattr(send_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        send_digest(reviewed_digest, SAMPLE_SUBSCRIBER)

    message = str(excinfo.value)
    assert "422" in message
    assert "test_key" not in message  # the API key is never surfaced in the error


def test_resend_non_2xx_status_raises(reviewed_digest, monkeypatch):
    _provider_env(monkeypatch)

    monkeypatch.setattr(
        send_mod.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(422)
    )

    with pytest.raises(RuntimeError, match="422"):
        send_digest(reviewed_digest, SAMPLE_SUBSCRIBER)


def test_unsupported_provider_raises_naming_resend(reviewed_digest, monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "postmark")
    monkeypatch.setenv("BYPASS_HUMAN_REVIEW", "false")

    with pytest.raises(NotImplementedError, match="resend"):
        send_digest(reviewed_digest, SAMPLE_SUBSCRIBER)


def test_send_gate_blocks_unreviewed_digest_before_any_send(monkeypatch):
    _provider_env(monkeypatch)

    # An UNreviewed digest must be refused before the adapter is ever reached.
    matched = match_subscriber(SAMPLE_SUBSCRIBER, _sample_events())
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    assert digest["review_required"] is True

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _FakeResponse(200)

    monkeypatch.setattr(send_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ValueError):
        send_digest(digest, SAMPLE_SUBSCRIBER)
    assert calls["n"] == 0  # the gate fired before any network call


def test_bypass_cannot_send_unreviewed_via_provider(monkeypatch):
    # The dev bypass clears the queue only for the file sink. Combined with a real provider
    # it must refuse — an unreviewed digest can never be emailed to a real subscriber.
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "test_key")
    monkeypatch.setenv("EMAIL_FROM", "digest@example.org")
    monkeypatch.setenv("BYPASS_HUMAN_REVIEW", "true")

    matched = match_subscriber(SAMPLE_SUBSCRIBER, _sample_events())
    digest = build_digest(SAMPLE_SUBSCRIBER, matched, asof=ASOF)
    assert digest["review_required"] is True

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _FakeResponse(200)

    monkeypatch.setattr(send_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ValueError, match="EMAIL_PROVIDER"):
        send_digest(digest, SAMPLE_SUBSCRIBER)
    assert calls["n"] == 0  # refused before any network call


def test_empty_recipient_raises_before_network(reviewed_digest, monkeypatch):
    _provider_env(monkeypatch)

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _FakeResponse(200)

    monkeypatch.setattr(send_mod.urllib.request, "urlopen", fake_urlopen)

    subscriber = {**SAMPLE_SUBSCRIBER, "email": ""}
    with pytest.raises(ValueError, match="recipient email is empty"):
        send_digest(reviewed_digest, subscriber)
    assert calls["n"] == 0  # a bad recipient fails fast, never round-trips to Resend
