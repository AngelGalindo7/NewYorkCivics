"""Email send — provider adapter behind a config flag (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: the thin adapter that hands a
finished, human-cleared digest to an email provider. The provider is chosen via
the ``EMAIL_PROVIDER`` config flag (one of ses | postmark | resend | mailchimp).

Rules honored here:
  - Rule 6  (Model/provider behind a config flag, never hard-coded): the provider
            is selected by EMAIL_PROVIDER; no provider is wired in by default.
  - Rule 9  (Human-review-then-send): this is the LAST step — only a digest that a
            human has cleared reaches here.
  - Rule 16 (No premature abstraction): one simple adapter, not a pluggable
            messaging framework.

Phase 2: provider is intentionally unset in v1 (.env.example leaves EMAIL_PROVIDER
empty with a TODO). v1 output is the email digest + RSS.

CITY-AGNOSTIC: no NYC specifics.
"""

from __future__ import annotations

# TODO Phase 2: read EMAIL_PROVIDER via ingest.config.get_settings() (Rule 6) and
# dispatch to the matching adapter. Left unset in v1 — see .env.example.
EMAIL_PROVIDER_ENV = "EMAIL_PROVIDER"
SUPPORTED_PROVIDERS = ("ses", "postmark", "resend", "mailchimp")


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
