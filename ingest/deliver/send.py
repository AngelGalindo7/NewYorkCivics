"""Digest send — file sink now, provider adapter behind a config flag (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: hand a finished, human-cleared
digest to its destination. With no ``EMAIL_PROVIDER`` configured, the v1 sink writes
the rendered digest to disk (the file/RSS fallback); when a provider is set, dispatch
to it (Phase 2).

Rules honored here:
  - Rule 6  (provider behind a config flag, never hard-coded): destination is
            selected by EMAIL_PROVIDER; no provider is wired in by default.
  - Rule 9  (Human-review-then-send): this is the LAST step — a digest with
            unreviewed items is refused, never sent.
  - Rule 16 (No premature abstraction): one simple adapter + a file sink, not a
            pluggable messaging framework.

CITY-AGNOSTIC: no NYC specifics.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ingest.config import get_settings
from ingest.deliver.digest import render_markdown
from ingest.observability import get_logger

log = get_logger(__name__)

EMAIL_PROVIDER_ENV = "EMAIL_PROVIDER"
SUPPORTED_PROVIDERS = ("ses", "postmark", "resend", "mailchimp")

# v1 sink: write the rendered digest to disk so the pipeline runs end-to-end with no
# email provider wired. A real provider is selected in Phase 2 via EMAIL_PROVIDER.
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

    Contract: never sends unless the digest passed human review (Rule 9) — callers
    must clear ``digest['review_required']`` first. With no ``EMAIL_PROVIDER`` set,
    render to Markdown and write it to ``sink_dir`` (the file/RSS fallback). When a
    provider is configured, hand off to :func:`send` (Phase 2). Raises on a transport
    failure or an unreviewed digest — fail loud, never silently drop (Rule 2).
    """
    if digest.get("review_required"):
        raise ValueError(
            "Digest has unreviewed items (Rule 9: human-review-then-send). "
            "Clear review_items and set review_required=False before sending."
        )

    settings = get_settings()
    if settings.email_provider:
        send(subscriber.get("email", ""), render_markdown(digest))
        return Path(f"<sent via {settings.email_provider}>")

    sink_dir = sink_dir or DEFAULT_SINK_DIR
    sink_dir.mkdir(parents=True, exist_ok=True)
    path = sink_dir / f"{_slug(subscriber.get('email'))}-{digest['asof']}.md"
    path.write_text(render_markdown(digest), encoding="utf-8")
    log.info("digest written to %s (provider unset -> file sink)", path)
    return path


def send(email: str, html: str) -> None:
    """Send one rendered digest to one subscriber via the configured provider.

    Contract: dispatch ``html`` to ``email`` using the adapter named by
    ``EMAIL_PROVIDER`` (Rule 6). Only called on digests a human has already cleared
    (Rule 9). Raises if EMAIL_PROVIDER is unset/unsupported — fail loud, never
    silently drop a send (Rule 2 (Fail fast, don't guess)).
    """
    raise NotImplementedError(
        "Phase 2: select adapter by EMAIL_PROVIDER (ses|postmark|resend|mailchimp) "
        "and send. Provider is unset in v1 — see .env.example."
    )
