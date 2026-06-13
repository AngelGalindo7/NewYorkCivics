"""Shared configuration loader (CITY-AGNOSTIC).

Cross-cutting. Single responsibility: read every config-flagged setting (Rule 6 —
model name behind a config flag, never hard-coded) from the environment / .env in
ONE place, so no other module reaches into ``os.environ`` directly. ``python-dotenv``
loads ``.env`` once; every stage that needs a flag imports :func:`get_settings`.

Rules honored:
- Rule 6 (config flag): EXTRACT_MODEL / JUDGE_MODEL / provider / keys live here.
- Rule 16 (no premature abstraction): a thin typed accessor over env vars — NOT a
  settings framework. ``python-dotenv`` is the chosen dep; do not add pydantic-settings.

This is a contract stub. The field set mirrors ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:  # import-safety (test_smoke): real env vars work without python-dotenv

    def load_dotenv(*args: object, **kwargs: object) -> bool:
        """No-op fallback when python-dotenv isn't installed; .env is simply not loaded."""
        return False


@dataclass(frozen=True)
class Settings:
    """Typed snapshot of the environment config (mirrors .env.example)."""

    # --- LLM (Rule 6) ---
    extract_model: str = "gemini-2.5-flash"  # EXTRACT_MODEL — default extractor
    judge_model: str = "claude-haiku-4-5"  # JUDGE_MODEL — cross-family judge (Rule 12)
    google_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None  # optional alternate judge

    # --- Storage / sources ---
    database_url: str | None = None  # DATABASE_URL (Postgres+PostGIS)
    socrata_app_token: str | None = None  # ~1000 req/hr on NYC Open Data

    # --- Geocoding (NYC GeoSupport; setup caveat) ---
    geosupport_geofiles: str | None = None
    geosupport_gs_library_path: str | None = None

    # --- Delivery (Phase 2; unset in v1) ---
    email_provider: str | None = None  # ses | postmark | resend | mailchimp (resend is built)
    email_from: str | None = None  # EMAIL_FROM — verified sender address the provider sends from
    resend_api_key: str | None = None  # RESEND_API_KEY — Resend send API key (never logged)

    # --- External APIs ---
    legistar_token: str | None = None  # LEGISTAR_TOKEN — optional (public API is keyless)

    # --- Dev overrides (NEVER set in production) ---
    # Bypass the Rule 9 human-review-then-send gate so the full digest pipeline
    # can be exercised end-to-end before an organizer has cleared the queue.
    # Value: the displacement signal + Legistar feeds produce genuine, unique
    # civic intelligence (cross-feed BBL correlation + upcoming hearing dates)
    # whose correctness can be verified from public sources; the human gate
    # guards *send*, not the data's factual correctness. Set to "true" only in
    # local dev / CI — never in production (Rule 9 applies to production send).
    bypass_human_review: bool = False  # BYPASS_HUMAN_REVIEW=true

    # --- Eval / tracing (Langfuse Hobby) ---
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"


def get_settings() -> Settings:
    """Return the process :class:`Settings`, reading ``.env`` once via python-dotenv.

    Contract: call ``dotenv.load_dotenv()`` once (idempotent), then build a frozen
    ``Settings`` from ``os.environ`` with the documented defaults. Never hard-code a
    model name elsewhere — read it from here (Rule 6). No secret is ever logged.
    """
    load_dotenv()  # idempotent; no-op if .env is absent

    def _opt(key: str) -> str | None:
        # Treat empty strings as unset so blank .env lines don't mask defaults.
        value = os.environ.get(key)
        return value or None

    return Settings(
        extract_model=os.environ.get("EXTRACT_MODEL", "gemini-2.5-flash"),
        judge_model=os.environ.get("JUDGE_MODEL", "claude-haiku-4-5"),
        google_api_key=_opt("GOOGLE_API_KEY"),
        anthropic_api_key=_opt("ANTHROPIC_API_KEY"),
        openai_api_key=_opt("OPENAI_API_KEY"),
        database_url=_opt("DATABASE_URL"),
        socrata_app_token=_opt("SOCRATA_APP_TOKEN"),
        geosupport_geofiles=_opt("GEOSUPPORT_GEOFILES"),
        geosupport_gs_library_path=_opt("GEOSUPPORT_GS_LIBRARY_PATH"),
        email_provider=_opt("EMAIL_PROVIDER"),
        email_from=_opt("EMAIL_FROM"),
        resend_api_key=_opt("RESEND_API_KEY"),
        legistar_token=_opt("LEGISTAR_TOKEN"),
        bypass_human_review=os.environ.get("BYPASS_HUMAN_REVIEW", "").lower()
        in ("1", "true", "yes"),
        langfuse_public_key=_opt("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=_opt("LANGFUSE_SECRET_KEY"),
        langfuse_host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
    )
