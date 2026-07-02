"""Post-extract reconciliation against ZAP structured data. NYC-SPECIFIC.

After the LLM extracts a CivicEvent from a dirty source (ULURP packet, CB agenda),
its ulurp_number and project_thread_id are unverified claims. ZAP already provides
the authoritative structured version of the same project. This module upgrades a
dirty event to ACCEPTED when ZAP confirms the key identifiers, or keeps it at
REVIEW with a discrepancy note when they conflict.

Cross-source corroboration makes a dirty event independently verifiable: a
ZAP structured record confirms the application exists and carries the same
identifier, so the extracted claim can be checked against a public authoritative
source.

Boundary: pure function — no I/O, no network, no LLM. Safe to call unconditionally;
a missing or empty ZAP feed means dirty events pass through unchanged.
"""

from __future__ import annotations

import re
from datetime import date

from ingest.extract.schemas import CivicEvent, RecordStatus

# Dirty-source identifiers whose extracted fields should be checked against ZAP.
# Public: the digest runner also uses this to scope its ZAP-authoritative dedup so a
# structured source carrying a ULURP number can never be silently dropped.
DIRTY_SOURCE_IDS = frozenset({"nyc_ulurp_packet", "nyc_cb_mn11"})

# Land-use action types eligible for address-based threading. Mirrors the deliver-side
# public-review taxonomy; kept as an independent copy because the NYC layer must never
# import from the city-agnostic Deliver stage.
_LAND_USE_ACTIONS = frozenset(
    {
        "rezoning",
        "special_permit",
        "variance",
        "authorization",
        "certification",
        "urban_renewal",
        "environmental_review",
        "site_selection",
        "land_use_application",
    }
)

# Street-type suffixes collapsed to one canonical short form for address comparison.
_STREET_TYPES = {
    "street": "st",
    "avenue": "ave",
    "boulevard": "blvd",
    "place": "pl",
    "road": "rd",
    "drive": "dr",
    "lane": "ln",
}

_ORDINAL_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b")


def normalize_ulurp(num: str) -> str:
    """Strip spaces and uppercase for case/whitespace-insensitive comparison."""
    return num.replace(" ", "").upper()


def normalize_address(addr: str) -> str:
    """Case/punctuation/street-suffix-insensitive form of an address for equality checks.

    "58-62 East 125th Street" and "58-62 east 125 St." normalize identically. Compass
    words are NOT aliased ("East" != "E") and house-number ranges stay verbatim —
    comparison must stay conservative because a false address match merges two projects.
    """
    text = addr.lower()
    text = re.sub(r"[.,;:#]+", " ", text)
    text = _ORDINAL_RE.sub(r"\1", text)
    tokens = [_STREET_TYPES.get(tok, tok) for tok in text.split()]
    return " ".join(tokens)


def thread_dirty_by_address(
    events: list[CivicEvent],
    zap_events: list[CivicEvent],
) -> list[CivicEvent]:
    """Thread ULURP-less dirty land-use extractions onto ZAP projects by street address.

    A CB-agenda extraction often names the project ("58-62 East 125th Street Rezoning")
    without capturing its ULURP number, so the ULURP-based reconciliation can't see that
    ZAP already covers it and the digest shows the same project twice. This pass joins on
    the normalized street address instead — conservatively: the dirty event must come
    from a dirty source, carry no ULURP number, have an address, and be a land-use
    action; and exactly ONE ZAP project may match the address. Zero or several
    candidates, and the event passes through unchanged — a false merge is worse than a
    duplicate.

    Threaded copies are marked ``extras["address_threaded"]`` so the dedup step treats
    them as duplicates of the ZAP record. A packet detail threaded at discovery time is
    not marked and is never dropped. Incoming objects are never mutated.
    """
    by_addr: dict[str, set[str]] = {}
    for z in zap_events:
        if not z.address or not z.project_thread_id:
            continue
        norm = normalize_address(z.address)
        if norm:
            by_addr.setdefault(norm, set()).add(z.project_thread_id)

    out: list[CivicEvent] = []
    for ev in events:
        if (
            ev.source_id not in DIRTY_SOURCE_IDS
            or ev.ulurp_number is not None
            or not ev.address
            or ev.action_type not in _LAND_USE_ACTIONS
        ):
            out.append(ev)
            continue
        threads = by_addr.get(normalize_address(ev.address), set())
        if len(threads) != 1:
            out.append(ev)
            continue
        out.append(
            ev.model_copy(
                update={
                    "project_thread_id": next(iter(threads)),
                    "extras": {**ev.extras, "address_threaded": True},
                }
            )
        )
    return out


def dedup_dirty_against_zap(events: list[CivicEvent]) -> list[CivicEvent]:
    """Drop dirty-source duplicates of ZAP records, transferring what they add first.

    A dirty event duplicates a ZAP record when its normalized ULURP number matches one
    ZAP already carries, or when it was address-threaded onto a ZAP project
    (``extras["address_threaded"]``). ZAP is the authoritative source and carries the
    verified City record link; keeping both duplicates the entry in the digest.

    Merge-before-drop: when the dropped event carries a hearing/comment date the ZAP
    record lacks — no deadline at all, or only the approximated 60-day review window
    (``extras["cpc_stage"] == "cpc_review"``) — the date is preserved on the surviving
    ZAP event as ``extras["unverified_date_note"]``, a pre-composed sentence the
    renderer shows as a flagged sub-line. The ZAP ``deadline`` field itself is never
    overwritten with an unverified date. Several duplicates contribute at most one note
    (soonest date wins). Structured sources are never dropped. No mutation.
    """
    zap_events = [e for e in events if e.source_id == "nyc_zap"]
    zap_by_ulurp = {normalize_ulurp(e.ulurp_number): e for e in zap_events if e.ulurp_number}
    zap_by_thread = {e.project_thread_id: e for e in zap_events if e.project_thread_id}

    contributed: dict[str, date] = {}
    kept: list[CivicEvent] = []
    for ev in events:
        if ev.source_id not in DIRTY_SOURCE_IDS:
            kept.append(ev)
            continue
        target: CivicEvent | None = None
        if ev.ulurp_number and normalize_ulurp(ev.ulurp_number) in zap_by_ulurp:
            target = zap_by_ulurp[normalize_ulurp(ev.ulurp_number)]
        elif ev.extras.get("address_threaded") and ev.project_thread_id in zap_by_thread:
            target = zap_by_thread[ev.project_thread_id]
        if target is None:
            kept.append(ev)
            continue
        dirty_date = ev.deadline or ev.event_date
        zap_needs_date = target.deadline is None or target.extras.get("cpc_stage") == "cpc_review"
        if dirty_date and zap_needs_date and dirty_date != target.deadline:
            prev = contributed.get(target.source_record_id)
            if prev is None or dirty_date < prev:
                contributed[target.source_record_id] = dirty_date

    if not contributed:
        return kept
    out: list[CivicEvent] = []
    for ev in kept:
        if ev.source_id == "nyc_zap" and ev.source_record_id in contributed:
            d = contributed[ev.source_record_id]
            note = (
                f"A community-board agenda lists {d.isoformat()} as a hearing/comment date "
                "for this application — read from a public document, not yet confirmed by a "
                "City record; check with the community board office before relying on it."
            )
            out.append(
                ev.model_copy(update={"extras": {**ev.extras, "unverified_date_note": note}})
            )
        else:
            out.append(ev)
    return out


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
        if event.source_id not in DIRTY_SOURCE_IDS or event.project_thread_id is None:
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
            and normalize_ulurp(dirty_num) != normalize_ulurp(zap_num)
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
