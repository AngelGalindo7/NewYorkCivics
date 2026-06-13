"""Digest send — file sink by default, Resend adapter behind a config flag (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: hand a finished, human-cleared
digest to its destination. With no ``EMAIL_PROVIDER`` configured, the v1 sink writes
the rendered digest to disk (the file/RSS fallback); when ``EMAIL_PROVIDER=resend`` is
set, the digest is sent as a real email through Resend's HTTP API.

The provider is chosen by EMAIL_PROVIDER, never hard-coded; none is wired by default.
As the last step before a digest reaches a real subscriber, this stage refuses any digest
with unreviewed items. The adapter is one function over one provider via stdlib HTTP —
deliberately not a pluggable messaging framework.

CITY-AGNOSTIC: no NYC specifics.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ingest.config import get_settings
from ingest.deliver.digest import render_markdown
from ingest.observability import get_logger

log = get_logger(__name__)

# Documented provider names. Only "resend" is built; selecting any other fails loud
# rather than silently dropping a send.
SUPPORTED_PROVIDERS = ("ses", "postmark", "resend", "mailchimp")
RESEND_PROVIDER = "resend"
RESEND_ENDPOINT = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 30

# v1 sink: write the rendered digest to disk so the pipeline runs end-to-end with no
# email provider wired. A real provider is selected via EMAIL_PROVIDER.
DEFAULT_SINK_DIR = Path("out") / "digests"


def _slug(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "subscriber").lower()).strip("-") or "subscriber"


def send_digest(
    digest: dict[str, Any],
    subscriber: dict[str, Any],
    *,
    sink_dir: Path | None = None,
) -> Path:
    """Send one digest to one subscriber; returns the path written by the v1 file sink.

    Contract: never sends unless the digest passed human review — callers must clear
    ``digest['review_required']`` first (human-review-then-send). With no
    ``EMAIL_PROVIDER`` set, render to Markdown and write it to ``sink_dir`` (the
    file/RSS fallback). When a provider is configured, hand off to :func:`send`. Raises
    on a transport failure or an unreviewed digest — fail loud, never silently drop.
    """
    if digest.get("review_required"):
        settings = get_settings()
        if settings.bypass_human_review:
            if settings.email_provider:
                # The dev bypass exists to exercise the pipeline to the file sink without a
                # human in the loop. It must never push an unreviewed digest to a real
                # subscriber: refuse the combination rather than email flagged items out.
                raise ValueError(
                    "Refusing to send: BYPASS_HUMAN_REVIEW cannot clear an unreviewed digest "
                    "for a real provider send (EMAIL_PROVIDER is set). Clear the queue through "
                    "the reviewer (python -m ingest.deliver.review), or unset EMAIL_PROVIDER to "
                    "write to the file sink."
                )
            # Dev override for the file sink only: run the pipeline end-to-end without a
            # human clearing the queue. The data is still factually correct (structured
            # public sources); this skips only the send gate, not data validation.
            log.warning(
                "BYPASS_HUMAN_REVIEW is set — skipping the human-review gate "
                "(dev/CI file sink only; never use in production)"
            )
            digest = {**digest, "review_required": False, "review_items": []}
        else:
            raise ValueError(
                "Digest has unreviewed items (human-review-then-send). "
                "Clear review_items and set review_required=False before sending."
            )

    settings = get_settings()  # ok to re-call — idempotent cached load
    if settings.email_provider:
        send(subscriber.get("email", ""), digest["subject"], render_markdown(digest))
        return Path(f"<sent via {settings.email_provider}>")

    sink_dir = sink_dir or DEFAULT_SINK_DIR
    sink_dir.mkdir(parents=True, exist_ok=True)
    path = sink_dir / f"{_slug(subscriber.get('email'))}-{digest['asof']}.md"
    path.write_text(render_markdown(digest), encoding="utf-8")
    log.info("digest written to %s (provider unset -> file sink)", path)
    return path


def send(to_email: str, subject: str, body: str) -> None:
    """Send one rendered digest to one subscriber via the configured provider.

    Dispatches by ``EMAIL_PROVIDER``: ``resend`` is the implemented v1 provider and posts
    the email through Resend's HTTP API. Any other provider name is documented-but-unbuilt
    and fails loud rather than silently dropping the send. This is only called on digests a
    human has already cleared.
    """
    settings = get_settings()
    provider = settings.email_provider

    if not provider:
        # send_digest only calls this when a provider is set; guard anyway so a direct
        # caller gets a clear error instead of a silent no-op.
        raise ValueError(
            "send() called with no EMAIL_PROVIDER configured. Set EMAIL_PROVIDER=resend "
            "(and EMAIL_FROM / RESEND_API_KEY), or leave it unset to use the file sink."
        )

    if provider != RESEND_PROVIDER:
        raise NotImplementedError(
            f"EMAIL_PROVIDER={provider!r} is not implemented. Resend is the v1 provider — "
            f"set EMAIL_PROVIDER={RESEND_PROVIDER!r}. "
            f"(Documented-but-unbuilt providers: "
            f"{', '.join(p for p in SUPPORTED_PROVIDERS if p != RESEND_PROVIDER)}.)"
        )

    if not settings.resend_api_key:
        raise ValueError("RESEND_API_KEY is not set — set it to send email via Resend.")
    if not settings.email_from:
        raise ValueError("EMAIL_FROM is not set — set it to a Resend-verified sender address.")
    if not to_email:
        raise ValueError("recipient email is empty — cannot send.")

    payload = {
        "from": settings.email_from,
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    _post_resend(payload, api_key=settings.resend_api_key)
    # Log the recipient and subject only — never the API key or the body.
    log.info("digest emailed to %s via resend (subject: %s)", to_email, subject)


def _post_resend(payload: dict[str, Any], *, api_key: str) -> None:
    """POST one email payload to Resend over stdlib HTTP; raise on any non-2xx response.

    Uses only urllib (no third-party HTTP client), so ``urllib.request.urlopen`` is the
    single network call and tests can monkeypatch it to run offline. Both an HTTP error and
    a non-2xx status become a RuntimeError that names the status but never the API key.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        RESEND_ENDPOINT,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=RESEND_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", None) or resp.getcode() or 0
    except urllib.error.HTTPError as exc:
        # Do not include the API key; the status code is enough to diagnose.
        raise RuntimeError(f"Resend send failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Resend send failed: could not reach the API ({exc.reason})") from exc

    if not (200 <= int(status) < 300):
        raise RuntimeError(f"Resend send failed: HTTP {status}")
