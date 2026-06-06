"""Stage 3 (Extract) — LLM-powered extraction from parsed CB agendas. CITY-AGNOSTIC.

Single responsibility: take a ParsedDoc (Stage 2 output) and return a list of
validated CivicEvent objects by calling the configured EXTRACT_MODEL.

Rules honored:
- Rule 1 (LLM only on dirty inputs): fires on ParsedDoc only, never on clean JSON feeds.
- Rule 2 (Fail fast, don't guess): LLM / parse failures -> empty list; callers quarantine.
- Rule 3 (Quote the source): every extracted field must carry a provenance entry.
- Rule 6 (model behind config): reads EXTRACT_MODEL from config, never hard-coded.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ingest.extract.schemas import CivicEvent, RecordStatus
    from ingest.parse import ParsedDoc

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str = "cb_agenda.v1.md") -> str:
    path = _PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Extraction prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def extract(
    doc: "ParsedDoc",
    *,
    source_id: str,
    prompt_name: str = "cb_agenda.v1.md",
) -> "list[CivicEvent]":
    """Extract CivicEvent objects from a ParsedDoc using EXTRACT_MODEL.

    Returns an empty list on any LLM or parsing failure — callers quarantine the
    source record (Rule 2). Never raises on model or parsing failures.
    """
    prompt_template = _load_prompt(prompt_name)
    full_prompt = f"{prompt_template}\n\n---\n\n## Agenda content\n\n{doc.text}"

    try:
        raw = _call_llm(full_prompt)
    except Exception as exc:
        logger.warning("LLM call failed during extraction: %s", exc)
        return []

    return _parse_response(raw, source_id=source_id)


def _call_llm(prompt: str) -> str:
    """Call EXTRACT_MODEL and return the raw response text (a JSON array string)."""
    from ingest.config import get_settings

    settings = get_settings()

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as exc:
        raise RuntimeError(
            "google-genai is not installed. Run: pip install google-genai>=1.0"
        ) from exc

    if not settings.google_api_key:
        raise ValueError("GOOGLE_API_KEY is not set — add it to .env (see .env.example).")

    client = genai.Client(api_key=settings.google_api_key)
    response = client.models.generate_content(
        model=settings.extract_model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    return response.text


def _parse_response(raw: str, *, source_id: str) -> "list[CivicEvent]":
    """Validate and coerce the raw LLM JSON into CivicEvent objects (Rule 2)."""
    from ingest.extract.schemas import CivicEvent, RecordStatus
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Extractor returned invalid JSON (%s): %.120s", exc, raw)
        return []

    if not isinstance(data, list):
        logger.warning("Extractor returned %s, expected a JSON array.", type(data).__name__)
        return []

    events: list[CivicEvent] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict item at index %d.", i)
            continue

        # Inject system-of-record identity (Rule 15) — the model doesn't know source_id.
        item.setdefault("source_id", source_id)
        item.setdefault("source_record_id", f"{source_id}-item-{i:04d}")

        # Route by confidence (Rule 10).
        conf = item.get("confidence")
        if conf is not None:
            if conf >= 0.6:
                item.setdefault("status", RecordStatus.ACCEPTED)
            elif conf >= 0.4:
                item.setdefault("status", RecordStatus.REVIEW)
            else:
                item.setdefault("status", RecordStatus.UNVERIFIED)

        try:
            events.append(CivicEvent.model_validate(item))
        except Exception as exc:
            logger.warning("Skipping invalid CivicEvent at index %d: %s", i, exc)

    return events
