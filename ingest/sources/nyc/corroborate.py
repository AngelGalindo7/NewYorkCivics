"""Post-extract reconciliation against ZAP structured data. NYC-SPECIFIC.

After the LLM extracts a CivicEvent from a dirty source (ULURP packet, CB agenda),
its ulurp_number and project_thread_id are unverified claims. ZAP already provides
the authoritative structured version of the same project. This module upgrades a
dirty event to ACCEPTED when ZAP confirms the key identifiers, or keeps it at
REVIEW with a discrepancy note when they conflict.

This is the reliability moat: a dirty event corroborated by the ZAP structured
record is independently verifiable to the reader.

Boundary: pure function — no I/O, no network, no LLM. Safe to call unconditionally;
a missing or empty ZAP feed means dirty events pass through unchanged.
"""

from __future__ import annotations

from ingest.extract.schemas import CivicEvent, RecordStatus

# Dirty-source identifiers whose extracted fields should be checked against ZAP.
_DIRTY_SOURCE_IDS = {"nyc_ulurp_packet", "nyc_cb_mn11"}


def _normalize_ulurp(num: str) -> str:
    """Strip spaces and uppercase for case/whitespace-insensitive comparison."""
    return num.replace(" ", "").upper()


def corroborate_against_zap(
    events: list[CivicEvent],
    zap_events: list[CivicEvent],
) -> list[CivicEvent]:
    """Reconcile dirty-source events against ZAP authoritative structured records.

    For each event from a dirty source (ULURP packet or CB agenda) that carries a
    project_thread_id, look up the matching ZAP record by that id. When a match is
    found, compare the ULURP numbers:

    - Both agree (or the dirty event has no ulurp_number to compare): upgrade to
      ACCEPTED — the ZAP record independently confirms the application exists.
    - They disagree: keep REVIEW, add extras["corroboration_discrepancy"] describing
      the conflict so a human reviewer can spot the contradiction.
    - No ZAP match found: leave the event unchanged (ZAP may simply not have it yet).

    ZAP events and all other source events pass through untouched.

    Args:
        events: Full list of CivicEvents from gather_live_events (mixed sources).
        zap_events: Subset of events already known to come from nyc_zap, passed
            separately so callers don't re-filter. Must be a subset of ``events``
            or an independently-built slice — not a requirement, but the typical use.

    Returns:
        A new list; dirty-source events may be replaced with model_copy()
        equivalents. Incoming objects are never mutated.
    """
    # Build lookup from project_thread_id -> ZAP CivicEvent.
    zap_by_thread: dict[str, CivicEvent] = {
        e.project_thread_id: e for e in zap_events if e.project_thread_id is not None
    }

    result: list[CivicEvent] = []
    for event in events:
        if event.source_id not in _DIRTY_SOURCE_IDS or event.project_thread_id is None:
            result.append(event)
            continue

        zap_record = zap_by_thread.get(event.project_thread_id)
        if zap_record is None:
            # ZAP doesn't have this thread yet — pass through unchanged.
            result.append(event)
            continue

        # ZAP has a matching record. Compare ULURP numbers if both sides have one.
        dirty_num = event.ulurp_number
        zap_num = zap_record.ulurp_number

        if (
            dirty_num is not None
            and zap_num is not None
            and _normalize_ulurp(dirty_num) != _normalize_ulurp(zap_num)
        ):
            # Conflict: keep at REVIEW with a note for the human reviewer.
            discrepancy = f"extracted ULURP '{dirty_num}' differs from ZAP record '{zap_num}'"
            result.append(
                event.model_copy(
                    update={
                        "status": RecordStatus.REVIEW,
                        "extras": {
                            **event.extras,
                            "corroboration_discrepancy": discrepancy,
                        },
                    }
                )
            )
            continue

        # ULURP numbers agree (or at least one side has none) — ZAP confirms the thread.
        result.append(event.model_copy(update={"status": RecordStatus.ACCEPTED}))

    return result
