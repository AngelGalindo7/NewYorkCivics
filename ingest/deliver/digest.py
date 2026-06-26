"""Digest assembly — group, order, human-review, then render (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: turn one subscriber's matched,
ranked events into a plain-English, forward-looking digest, gate it through human
review, and render the verifiable email body. Sending is send.py's job.

The render is a short weekly briefing built for scannability and trust. Top to
bottom: a personalization line scoped to the subscriber's address; a one-line stats
hook of whole-number counts; a "Right next to you" lead of buildings carrying a
confirmed serious violation (a safety fact outranks procedure); an "Act on this" lead
of items a reader can still act on (a future deadline or event); the proximity-banded
building feed; the honest verifiability footer. A confirmed hazard near the reader
leads because it is the most consequential thing in the email; a still-open deadline
leads the actionable items because it is the most useful.

Rules honored here:
  - Rule 9  (Human-review-then-send): :func:`build_digest` returns a review-ready
            object with ``review_required`` set whenever a non-high-confidence item
            is in the top-N. A wrong extraction never auto-publishes as fact.
  - Rule 10 (Confidence routing): every item is tagged verified vs needs-verification
            and the render visibly separates them with a footnote — the biggest
            trust lever. A needs-verification item never leads the email.
  - Rule 8  (linear ranker): rank.score() breaks ties, but the digest orders for
            ACTIONABILITY first (soonest still-open deadlines lead).
  - Rule 3  (quote the source) + citations: each item renders its source links so a
            reader can verify the claim against the authoritative record.

CITY-AGNOSTIC: renders canonical CivicEvents; no NYC specifics. Summaries are reused
from Extract / the structured connectors (cached), never regenerated here.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from ingest.deliver import rank
from ingest.deliver.match import (
    BAND_IN_YOUR_AREA,
    BAND_IN_YOUR_NEIGHBORHOOD,
    BAND_ON_YOUR_BLOCK,
)
from ingest.extract.schemas import RecordStatus

if TYPE_CHECKING:
    from ingest.extract.schemas import CivicEvent

# Soonest-deadline window (days) that counts as "needs your attention".
ATTENTION_DEADLINE_DAYS = 14

# Maximum days ahead for the "Act on this" lead; items with open windows beyond
# this threshold move to a "Later" subsection instead of cluttering the urgent lead.
LEAD_MAX_DAYS = 60

# How far back a lapsed deadline can be and still appear in "Deadline passed".
_OVERDUE_LOOKBACK_DAYS = 90

# How many building threads the "Near you" feed renders in full before the rest are
# summarized in a compact "More nearby" list. Keeps a busy week scannable (the lead
# sections are never capped — the most consequential items always show in full).
FEED_NEAR_YOU_CAP = 5

# Reader-facing labels + display order for the three proximity bands.
_BAND_LABELS = (
    (BAND_ON_YOUR_BLOCK, "On your block"),
    (BAND_IN_YOUR_NEIGHBORHOOD, "In your neighborhood"),
    (BAND_IN_YOUR_AREA, "In your area"),
)

# Permit types that represent outdoor or temporary street-level work (sidewalk sheds,
# cranes, equipment placements). These affect the streetscape, not the building itself,
# so they are informational context rather than action items for a resident.
_STREET_EVENT_PERMIT_TYPES = frozenset({"EW"})

# Public-review action types — formal land-use proposals that open a public-comment
# window a resident can weigh in on (alongside the hearing family matched below).
# These are canonical taxonomy values, not source- or city-specific strings.
_PUBLIC_REVIEW_ACTIONS = frozenset(
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


def _is_speakable(action_type: str | None) -> bool:
    """True when the action type is one a reader can testify at / comment on.

    Drives the "hearing you can still speak at" count in the stats line. We match the
    whole hearing family by name (so any ``*hearing`` taxonomy value — a plain hearing,
    a land-use hearing, a council hearing — counts, and a new hearing source can't
    silently drop out) plus the formal public-review proposals that open a comment
    window. A permit or a violation is never speakable: there is nothing to testify at.
    """
    if not action_type:
        return False
    return "hearing" in action_type or action_type in _PUBLIC_REVIEW_ACTIONS


def _category_weight(action_type: str | None) -> float:
    """Per-action-type importance for the ranker, keyed off the canonical taxonomy.

    Matches the hearing family by name (like ``_is_speakable``) so a land-use or
    council hearing is weighted as a hearing, not dropped to the neutral default. A
    displacement correlation carries the most weight; a routine permit the least.
    """
    if action_type == "displacement_signal":
        return 1.0
    if action_type == "violation":
        return 0.6
    if action_type == "permit":
        return 0.5
    if action_type == "sla_license":
        return 0.65  # 30-day action window + CB notification requirement makes this high-value
    if action_type == "building_energy_grade":
        return 0.3  # building context, not an action a reader takes — ranks below a permit
    if action_type == "habitability_complaints":
        return 0.35  # a cluster of 311 reports is context (and a report, not a confirmed fact)
    if action_type == "permitted_event":
        # Time-bound neighborhood event: more urgent than a static grade, not a civic action.
        return 0.32
    if action_type and "hearing" in action_type:
        return 0.7
    if action_type in _PUBLIC_REVIEW_ACTIONS:  # rezoning, special permit, variance, ...
        return 0.9
    return 0.4  # unknown / other action type: neutral-low prior


def _actionable_date(event_date: date | None, deadline: date | None, asof: date) -> date | None:
    """The date the reader can still act on, or None if nothing is open.

      - A deadline governs when present: a future deadline IS the actionable date; a
        lapsed one closes the window (return None — a recent observation date must not
        reopen a missed deadline).
      - With no deadline, fall back to a still-future event_date.

    We never tell a reader to act on a deadline that already lapsed.
    """
    if deadline is not None:
        return deadline if deadline >= asof else None
    if event_date is not None and event_date >= asof:
        return event_date
    return None


def _when_phrase(when: date, asof: date) -> str:
    """Plain-words phrasing for a future actionable date ('today' / 'in 5 days')."""
    days = (when - asof).days
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _deadline_note(deadline: date | None, asof: date) -> str | None:
    """Human phrasing for a deadline relative to ``asof`` (drives actionability)."""
    if deadline is None:
        return None
    days = (deadline - asof).days
    if days < 0:
        return f"{deadline.isoformat()} ({-days} days overdue)"
    if days == 0:
        return f"{deadline.isoformat()} (today)"
    return f"{deadline.isoformat()} (in {days} days)"


def _signals(event: CivicEvent, band: str, asof: date) -> dict[str, float]:
    """Normalized [0,1] ranker signals for one (subscriber, event) pair (Rule 8)."""
    proximity = {
        BAND_ON_YOUR_BLOCK: 1.0,
        BAND_IN_YOUR_NEIGHBORHOOD: 0.6,
        BAND_IN_YOUR_AREA: 0.3,
    }.get(band, 0.3)

    recency = 0.0
    if event.event_date:
        age = (asof - event.event_date).days
        recency = max(0.0, 1.0 - age / 180.0)  # linear decay over ~6 months

    deadline_urgency = 0.0
    if event.deadline:
        days = (event.deadline - asof).days
        deadline_urgency = 1.0 if days <= 0 else max(0.0, 1.0 - days / 30.0)

    magnitude = 0.0
    if event.action_type == "displacement_signal":
        n = (event.extras.get("violation_count") or 0) + (event.extras.get("permit_count") or 0)
        magnitude = min(1.0, n / 5.0)
    elif event.action_type in ("permit", "rezoning"):
        magnitude = 0.5

    return {
        "proximity": proximity,
        "recency": recency,
        "deadline_urgency": deadline_urgency,
        "magnitude": magnitude,
        "novelty": 0.5,  # no thread history in the prototype; neutral prior
        "category_weight": _category_weight(event.action_type),
    }


# Order citations strongest-first so the reader sees the best proof first.
_VERIFY_RANK = {"exact_record": 0, "exact_building": 1, "search": 2}


def _citation_label(citation: dict[str, Any]) -> str:
    """Append a strength badge so readers can tell exact records from search fallbacks."""
    label = citation["label"]
    if citation.get("verifies") in ("exact_record", "exact_building"):
        return label + " ✓"
    return label + " (search)"


def _first_link(items: list[dict[str, Any]]) -> str | None:
    """A single strongest-proof markdown link for a compact one-line building summary.

    Citations are pre-sorted strongest-first, so the first item's first citation is the
    best available proof. Returns None when no item carries a link.
    """
    for item in items:
        for citation in item["citations"]:
            return f"[{_citation_label(citation)}]({citation['url']})"
    return None


def _to_item(event: CivicEvent, band: str, asof: date) -> dict[str, Any]:
    """Project one CivicEvent into a render-ready digest item (with verify links)."""
    cites = sorted(event.citations, key=lambda c: _VERIFY_RANK.get(c.verifies, 9))
    # A record the reader cannot check is never shown as a confirmed fact: an item with no
    # source link at all is treated as needs-verification regardless of its status, so an
    # AI reading of a public document can't read as authoritative as a linked City record.
    verified = event.status == RecordStatus.ACCEPTED and bool(cites)
    action_on = _actionable_date(event.event_date, event.deadline, asof)
    return {
        "title": event.title or "(untitled event)",
        "summary": event.summary or "",
        "action_type": event.action_type,
        "band": band,
        "bbl": event.bbl,
        "address": (
            event.address
            or event.extras.get("primary_address")
            or (f"BBL {event.bbl}" if event.bbl else None)
        ),
        "status": event.status.value,
        "confidence": event.confidence,
        "needs_verification": not verified,
        # Strongest proof this item carries: the exact record/building, or just a search.
        "verifies": cites[0].verifies if cites else None,
        "deadline": event.deadline,
        "deadline_note": _deadline_note(event.deadline, asof),
        "event_date": event.event_date,
        # Soonest still-open date the reader can act on (None = nothing open).
        "actionable_date": action_on,
        "score": rank.score(_signals(event, band, asof)),
        "citations": [
            {"kind": c.kind, "verifies": c.verifies, "label": c.label, "url": c.url} for c in cites
        ],
        "source_record_id": event.source_record_id,
        "extras": dict(event.extras),
    }


def _is_actionable(item: dict[str, Any]) -> bool:
    """Lead-section gate: a still-open date AND a confirmed (verified) item.

    A needs-verification item is never promoted to the lead — an unconfirmed
    correlation must not read as a headline fact. It still appears in its building
    thread below, flagged.

    Procedural council/committee hearings with no specific building or address are
    citywide meetings — they belong in the Near you feed as area context, not in the
    actionable lead over genuinely local items.
    """
    if item["actionable_date"] is None or item["needs_verification"]:
        return False
    return not (
        item.get("action_type") in ("council_hearing", "land_use_hearing")
        and not item.get("bbl")
        and not item.get("address")
    )


def _is_hazardous_violation(item: dict[str, Any]) -> bool:
    """The city-agnostic trigger to lead the digest with a building.

    A confirmed, serious housing violation is a safety fact that outranks procedure, so the
    building it sits on leads. Keys off a connector-set severity flag
    (``extras['hazardous']``) — never a city-specific class code — and never a
    needs-verification item: a headline must be a confirmed fact. A complaint or a permit
    alone never qualifies; they only corroborate a confirmed hazard (see _corroboration_note).
    """
    return (
        item["action_type"] == "violation"
        and not item["needs_verification"]
        and bool(item.get("extras", {}).get("hazardous"))
    )


def _is_street_event(item: dict[str, Any]) -> bool:
    """True when the permit is for outdoor/temporary street-level work (EW permit type).

    Street-level equipment work affects the sidewalk, not the building — it is
    informational context for a reader, not an action item that warrants leading the digest.
    """
    return item.get("extras", {}).get("permit_type") in _STREET_EVENT_PERMIT_TYPES


def _actionability_key(item: dict[str, Any]) -> tuple:
    """Sort key: soonest/overdue deadlines first, then highest rank score."""
    has_deadline = item["deadline"] is not None
    return (not has_deadline, item["deadline"] or date.max, -item["score"])


def _lead_key(item: dict[str, Any]) -> tuple:
    """Sort key for the lead section: soonest still-open date first, then rank."""
    return (item["actionable_date"] or date.max, -item["score"])


def _building_key(item: dict[str, Any]) -> str:
    """Group key collapsing all events on one building (Rule 7 thread, by BBL)."""
    if item["bbl"]:
        return f"bbl:{item['bbl']}"
    if item["address"]:
        return f"addr:{item['address'].lower()}"
    return f"item:{item['title']}"  # ungroupable -> stands alone


def _group_buildings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse items sharing a building into one group, most-actionable first."""
    groups: dict[str, dict[str, Any]] = {}
    for it in items:
        key = _building_key(it)
        if key not in groups:
            # ``key`` is persisted so the at-risk lead can reference a building by key
            # (round-trip-safe) instead of duplicating its item dicts in the digest.
            groups[key] = {"key": key, "label": None, "items": []}
        groups[key]["items"].append(it)
    out = list(groups.values())
    for g in out:
        g["items"].sort(key=_actionability_key)
        # Prefer a real street address for the header: a reader can't place "BBL 1016170120".
        # Any record on the building may carry the address, so scan the whole group rather
        # than only the lead item; _to_item uses a "BBL ..." string as its own address
        # fallback, so skip those — a bare BBL is the last resort.
        first = g["items"][0]
        address = next(
            (
                it["address"]
                for it in g["items"]
                if it["address"] and not it["address"].startswith("BBL ")
            ),
            None,
        )
        g["label"] = address or (f"BBL {first['bbl']}" if first["bbl"] else first["title"])
    out.sort(key=lambda g: _actionability_key(g["items"][0]))
    return out


def _stats_line(all_items: list[dict[str, Any]], lead: list[dict[str, Any]]) -> str | None:
    """One scannable line of small whole-number counts scoped near the subscriber.

    Numeracy guardrail: only natural frequencies (whole counts) over the items near
    the subscriber — never a percentage, rate, or relative-risk figure. A zero count
    is omitted (we don't print "0 hearings"). The "hearing you can still speak at"
    clause counts only the still-open windows in the lead section, so the number
    matches what a reader can actually act on. A needs-verification signal is never
    counted here — it is not a headline fact.
    """
    permits = sum(1 for it in all_items if it["action_type"] == "permit")
    violations = sum(1 for it in all_items if it["action_type"] == "violation")
    speakable = sum(1 for it in lead if _is_speakable(it["action_type"]))

    clauses: list[str] = []
    if permits:
        clauses.append(f"{permits} new permit{'s' if permits != 1 else ''}")
    if violations:
        clauses.append(f"{violations} hazardous violation{'s' if violations != 1 else ''}")
    if speakable:
        # "upcoming" keeps the count honest: the window is still open, but the hearing
        # itself may be weeks out — it is not necessarily happening "this week".
        noun = (
            "upcoming hearing you can still speak at"
            if speakable == 1
            else "upcoming hearings you can still speak at"
        )
        clauses.append(f"{speakable} {noun}")
    if not clauses:
        return None
    return "This week within a 5-minute walk: " + " · ".join(clauses) + "."


def build_digest(
    subscriber: dict[str, Any],
    matched: dict[str, list[CivicEvent]],
    *,
    asof: date | None = None,
    subscriber_council_member: str | None = None,
) -> dict[str, Any]:
    """Assemble a review-ready digest for one subscriber.

    Args:
        subscriber: the subscriber row (email/address used for the header).
        matched: band -> events, as returned by match.match_subscriber (ranked or not).
        asof: reference date for deadline/recency phrasing (default today).

    Returns a structured digest: the forward-looking "Act on this" lead (still-open
    items only, soonest first), the proximity-banded building feed (block ->
    neighborhood -> area), each item carrying its source citations and a
    verified/needs-verification tag (Rule 10), plus ``review_required`` and the
    list of items a human must clear before send (Rule 9). Does NOT send.
    """
    asof = asof or date.today()

    raw_items: list[dict[str, Any]] = []
    _band_map: dict[str, str] = {}
    for band, label in _BAND_LABELS:
        for ev in matched.get(band, []):
            it = _to_item(ev, band, asof)
            raw_items.append(it)
            _band_map[band] = label

    # Drop pure past events: an event that already happened with no open deadline gives
    # a reader nothing to act on and clutters the feed. Events with a lapsed deadline
    # are kept because they belong in "Deadline passed". Events with no event_date
    # (e.g. a displacement signal) are always kept.
    all_items = [
        it
        for it in raw_items
        if not (it["event_date"] is not None and it["event_date"] < asof and it["deadline"] is None)
        and not (
            it["deadline"] is not None
            and it["deadline"] < asof - timedelta(days=_OVERDUE_LOOKBACK_DAYS)
        )
    ]

    # "Happened this week": past events that fell within the last 7 days — context only,
    # no open action window, surfaced so the reader knows what just occurred nearby.
    recent_cutoff = asof - timedelta(days=7)
    recent_items = [
        it
        for it in all_items
        if it["event_date"] is not None and recent_cutoff <= it["event_date"] < asof
    ]

    sections: list[dict[str, Any]] = []
    for band, label in _BAND_LABELS:
        band_items = [it for it in all_items if it["band"] == band]
        if band_items:
            buildings = _group_buildings(band_items)
            sections.append({"band": band, "label": label, "buildings": buildings})

    # "Right next to you": buildings with a confirmed serious (hazardous) violation lead the
    # whole digest — a safety fact a reader can act on outranks any procedural hearing. The
    # keys are listed in proximity order (sections run block -> neighborhood -> area); the
    # renderer resolves each key back to its building thread, so a building is never
    # duplicated and the items keep their single source of truth in ``sections``.
    at_risk_building_keys = [
        building["key"]
        for section in sections
        for building in section["buildings"]
        if any(_is_hazardous_violation(it) for it in building["items"])
    ]

    # "Act on this": only items with a still-open action window, soonest first. The
    # same item may also appear in its building thread below for context — the digest
    # orders for actionability first. Needs-verification items are excluded from the lead.
    # Items with open windows more than LEAD_MAX_DAYS out move to "Later" so the urgent
    # lead stays focused on what a reader should act on this week or month.
    lead_cutoff = asof + timedelta(days=LEAD_MAX_DAYS)
    actionable = sorted(
        (it for it in all_items if _is_actionable(it) and not _is_street_event(it)),
        key=_lead_key,
    )
    lead_items = [it for it in actionable if it["actionable_date"] <= lead_cutoff]
    later_items = [it for it in actionable if it["actionable_date"] > lead_cutoff]

    # "Deadline passed": lapsed deadlines from the past 90 days — listed so the reader
    # knows what recently closed, without implying there is still an open action window.
    overdue_cutoff = asof - timedelta(days=_OVERDUE_LOOKBACK_DAYS)
    overdue_items = [
        it
        for it in all_items
        if it["deadline"] is not None
        and it["deadline"] < asof
        and it["event_date"] is not None
        and it["event_date"] >= overdue_cutoff
    ]

    stats_line = _stats_line(all_items, lead_items)

    def _needs_attention(item: dict[str, Any]) -> bool:
        if item["needs_verification"]:
            return True
        if item["deadline"] is not None:
            return (item["deadline"] - asof).days <= ATTENTION_DEADLINE_DAYS
        return False

    attention = [it for it in all_items if _needs_attention(it)]
    # The send gate must cover EVERY needs-verification item the reader would see, not a
    # bounded slice — otherwise a flagged item that sorts to the back would ship
    # unreviewed. The human's queue stays short because confidence routing only flags the
    # genuinely uncertain middle; here we just order those flagged items most-actionable
    # first so a human clears them in priority order.
    review_items = sorted(
        (it for it in all_items if it["needs_verification"]), key=_actionability_key
    )

    footnotes: list[str] = []
    if review_items:
        footnotes.append(
            "Items marked [needs verification] are lower-confidence AI extractions or "
            "cross-source correlations that haven't cleared the acceptance threshold. "
            "A person verified these items for basic factual sense against the source links "
            "before sending. If a source link doesn't match what the item says, "
            "reply to this email — we log every correction."
        )

    n = len(all_items)
    attn = len(attention)
    building_count = sum(len(s["buildings"]) for s in sections)
    # Honesty metric: how many items link to the exact record/building vs only a search vs
    # carry no source link at all (an AI reading of a public document). The footer must
    # describe all three truthfully — it may never claim a link an item does not have.
    _exact = ("exact_record", "exact_building")
    exact_verifiable = sum(1 for it in all_items if it["verifies"] in _exact)
    linked_count = sum(1 for it in all_items if it["citations"])
    subject = f"Your neighborhood this week: {n} update{'s' if n != 1 else ''}" + (
        f" ({attn} need{'s' if attn == 1 else ''} attention)" if attn else ""
    )

    return {
        "subject": subject,
        "subscriber_email": subscriber.get("email"),
        "area": subscriber.get("address"),
        "asof": asof.isoformat(),
        "subscriber_council_member": subscriber_council_member,
        "stats_line": stats_line,
        "lead_items": lead_items,
        "lead_ids": [it["source_record_id"] for it in lead_items],
        "at_risk_building_keys": at_risk_building_keys,
        "later_items": later_items,
        "overdue_items": overdue_items,
        "recent_items": recent_items,
        "sections": sections,
        "item_count": n,
        "building_count": building_count,
        "exact_verifiable_count": exact_verifiable,
        "linked_count": linked_count,
        "needs_attention_count": attn,
        "review_required": bool(review_items),
        "review_items": [it["title"] for it in review_items],
        "footnotes": footnotes,
    }


def _corroboration_note(items: list[dict[str, Any]]) -> str | None:
    """A one-line cross-source alert when a building has both a new permit and open violations.

    Lets a reader see the reliability signal at a glance: a permit filed on a building that
    already has active violations or resident complaints is a stronger story than either alone.
    Returns None when the building does not have both a permit and a violation/complaint.
    """
    has_permit = any(it["action_type"] == "permit" for it in items)
    if not has_permit:
        return None
    violation_count = sum(1 for it in items if it["action_type"] == "violation")
    complaint_count = sum(1 for it in items if it["action_type"] == "habitability_complaints")
    if violation_count == 0 and complaint_count == 0:
        return None
    n = violation_count + complaint_count
    if violation_count > 0 and complaint_count == 0:
        kind = f"{n} active violation{'s' if n != 1 else ''}"
    elif complaint_count > 0 and violation_count == 0:
        kind = f"{n} active complaint{'s' if n != 1 else ''}"
    else:
        kind = (
            f"{violation_count} active violation{'s' if violation_count != 1 else ''}"
            f" and {complaint_count} complaint{'s' if complaint_count != 1 else ''}"
        )
    return f"This building received a new permit and has {kind}."


@dataclasses.dataclass(frozen=True, slots=True)
class _RenderCtx:
    """Stateless render options threaded as one object instead of five separate params.

    Adding a new option requires editing only _RenderCtx plus the one site that builds it
    in render_markdown — not every helper signature and every call site.
    """

    hearing_guidance: str | None = None
    action_context: dict[str, str] | None = None
    action_contacts: dict[str, str] | None = None
    why_matters: dict[str, str] | None = None
    subscriber_council_member: str | None = None


def _render_item(
    item: dict[str, Any],
    out: list[str],
    *,
    expand: Callable[[str], str] | None = None,
    ctx: _RenderCtx | None = None,
) -> None:
    _e = expand or (lambda t: t)
    _ctx = ctx or _RenderCtx()
    tag = " **[needs verification]**" if item["needs_verification"] else ""
    out.append(f"**{_e(item['title'])}**{tag}")
    if item["summary"]:
        out.append(_e(item["summary"]))
    # Plain-English "why this matters to you" line — connects the item to the reader's life.
    if _ctx.why_matters and (why := _ctx.why_matters.get(item.get("action_type") or "")):
        out.append(f"- **Why this matters:** {why}")
    # Caller-supplied background blurb for this action type — visually separated as a
    # blockquote so it reads as general context, not a claim about this specific filing.
    if _ctx.action_context and (blurb := _ctx.action_context.get(item.get("action_type") or "")):
        out.append(f"> {blurb}")
    # Resident action prompt — who to call / what to do for this specific action type.
    if _ctx.action_contacts and (
        contact := _ctx.action_contacts.get(item.get("action_type") or "")
    ):
        out.append(f"- **How to respond:** {contact}")
    if item["deadline_note"]:
        out.append(f"- **Action deadline:** {item['deadline_note']}")
    # Sources, strongest proof first, on one line so it reads like a citation. An item with
    # no source link says so plainly — it must never read as a confirmed City record.
    if item["citations"]:
        links = " · ".join(f"[{_citation_label(c)}]({c['url']})" for c in item["citations"])
        prefix = "Verify" if not item["needs_verification"] else "Check the sources"
        out.append(f"- {prefix}: {links}")
    else:
        out.append(
            "- _Read from a public document — no City record links this yet; "
            "confirm before relying on it._"
        )
    if item.get("action_type") == "council_vote" and item.get("extras", {}).get("roll_call"):
        roll_call: dict[str, str] = item["extras"]["roll_call"]
        member = _ctx.subscriber_council_member
        lines: list[str] = []
        if member and member in roll_call:
            lines.append(f"- **Council Member {member} voted {roll_call[member]}**")
        for name, vote in roll_call.items():
            if member and name == member:
                continue
            lines.append(f"- Council Member {name} voted {vote}")
        out.extend(lines)
    if (
        _ctx.hearing_guidance
        and "hearing" in (item.get("action_type") or "")
        and re.search(
            r"liquor|sla",
            (item.get("title") or "") + " " + (item.get("summary") or ""),
            re.IGNORECASE,
        )
    ):
        out.append(_ctx.hearing_guidance)
    out.append("")


def _render_lead_item(
    item: dict[str, Any],
    asof: date,
    out: list[str],
    *,
    expand: Callable[[str], str] | None = None,
    ctx: _RenderCtx | None = None,
) -> None:
    """One compact entry in the 'Act on this' lead: title, when, and the verify line."""
    _e = expand or (lambda t: t)
    _ctx = ctx or _RenderCtx()
    out.append(f"**{_e(item['title'])}**")
    when = item["actionable_date"]
    if when is not None:
        out.append(f"- **When:** {_when_phrase(when, asof)} ({when.isoformat()})")
    if item["citations"]:
        links = " · ".join(f"[{_citation_label(c)}]({c['url']})" for c in item["citations"])
        out.append(f"- Verify: {links}")
    if _ctx.why_matters and (why := _ctx.why_matters.get(item.get("action_type") or "")):
        out.append(f"- **Why this matters:** {why}")
    if _ctx.action_context and (blurb := _ctx.action_context.get(item.get("action_type") or "")):
        out.append(f"> {blurb}")
    if _ctx.action_contacts and (
        contact := _ctx.action_contacts.get(item.get("action_type") or "")
    ):
        out.append(f"- **How to respond:** {contact}")
    if (
        _ctx.hearing_guidance
        and "hearing" in (item.get("action_type") or "")
        and re.search(
            r"liquor|sla",
            (item.get("title") or "") + " " + (item.get("summary") or ""),
            re.IGNORECASE,
        )
    ):
        out.append(_ctx.hearing_guidance)
    out.append("")


def render_markdown(
    digest: dict[str, Any],
    *,
    glossary: dict[str, str] | None = None,
    hearing_guidance: str | None = None,
    action_context: dict[str, str] | None = None,
    action_contacts: dict[str, str] | None = None,
    why_matters: dict[str, str] | None = None,
    subscriber_council_member: str | None = None,
) -> str:
    """Render the digest into the plain-English, verifiable email body (Markdown).

    Rendering options (the glossary, hearing guidance, per-category context blurbs and
    action contacts, and the subscriber's council member) may also ride along inside the
    digest under ``render_options``. Because they live on the digest dict, they survive
    whatever the caller does with it — so a digest dumped for human review and sent from a
    *separate* process still carries its plain-English help text. An explicit keyword
    argument always overrides the embedded value for that option.
    """
    embedded = digest.get("render_options") or {}
    if glossary is None:
        glossary = embedded.get("glossary")
    if hearing_guidance is None:
        hearing_guidance = embedded.get("hearing_guidance")
    if action_context is None:
        action_context = embedded.get("action_context")
    if action_contacts is None:
        action_contacts = embedded.get("action_contacts")
    if why_matters is None:
        why_matters = embedded.get("why_matters")
    if subscriber_council_member is None:
        subscriber_council_member = embedded.get("subscriber_council_member")

    ctx = _RenderCtx(
        hearing_guidance=hearing_guidance,
        action_context=action_context,
        action_contacts=action_contacts,
        why_matters=why_matters,
        subscriber_council_member=subscriber_council_member,
    )

    # Acronym expansion: track which keys have already been expanded (first-use only)
    # so the same acronym is defined on first appearance, never repeated.
    _expanded: set[str] = set()

    def _expand(text: str) -> str:
        if not glossary:
            return text
        result = text
        for key, expansion in glossary.items():
            if key in _expanded:
                continue
            if re.search(r"\b" + re.escape(key) + r"\b", result):
                result = re.sub(
                    r"\b" + re.escape(key) + r"\b",
                    f"{key} ({expansion})",
                    result,
                    count=1,
                )
                _expanded.add(key)
        return result

    out: list[str] = []
    out.append(f"# {digest['subject']}")
    # Personalization framing: scoped to the address the subscriber gave us.
    if digest.get("area"):
        out.append(f"_For the address you gave us — {digest['area']} — as of {digest['asof']}._")
    out.append("")

    if digest["item_count"] == 0:
        out.append("No new civic activity near your address this week.")
        return "\n".join(out)

    # Stats top-line: the scannable hook (whole-number counts, no percentages).
    if digest.get("stats_line"):
        out.append(digest["stats_line"])
        out.append("")

    asof = date.fromisoformat(digest["asof"])
    lead = digest.get("lead_items") or []
    lead_ids = set(digest.get("lead_ids") or [])

    # "Right next to you": buildings with a confirmed serious violation lead the whole
    # digest — a safety fact outranks procedure. A building thread shown here is not repeated
    # in "Near you" below; items already in the "Act on this" lead are left to that section so
    # they keep their When/deadline framing. The displacement-style corroboration note (a new
    # permit on a building that also has violations/complaints) is the headline.
    building_by_key = {b["key"]: b for section in digest["sections"] for b in section["buildings"]}
    at_risk = [
        building_by_key[k]
        for k in (digest.get("at_risk_building_keys") or [])
        if k in building_by_key
    ]
    # Resolve each at-risk building to the items it would actually show here (its thread
    # minus anything already in the "Act on this" lead). Compute this before emitting the
    # header so a building whose only hazard item leads "Act on this" (a future correct-by
    # deadline) doesn't leave a stray empty "Right next to you" header.
    at_risk_visible = [
        (building, [it for it in building["items"] if it.get("source_record_id") not in lead_ids])
        for building in at_risk
    ]
    at_risk_visible = [(b, vis) for b, vis in at_risk_visible if vis]
    at_risk_rendered_ids: set[str] = set()
    if at_risk_visible:
        out.append("## Right next to you")
        out.append("")
        for building, visible in at_risk_visible:
            out.append(f"#### {building['label']}")
            out.append("")
            note = _corroboration_note(building["items"])
            if note:
                out.append(note)
                out.append("")
            for item in visible:
                _render_item(item, out, expand=_expand, ctx=ctx)
                at_risk_rendered_ids.add(item.get("source_record_id"))

    # Anything already rendered in a lead section above is suppressed in the feed sections
    # below, so no event is shown twice.
    shown_ids = lead_ids | at_risk_rendered_ids

    # "Act on this": the forward-looking lead — still-open items only, soonest first.
    if lead:
        out.append("## Act on this")
        out.append("")
        for item in lead:
            _render_lead_item(item, asof, out, expand=_expand, ctx=ctx)

    # "Deadline passed": recently lapsed items — listed so the reader knows what
    # closed, without implying an open action window. An item already shown in a lead
    # section above (e.g. an overdue hazardous violation that led "Right next to you") is
    # skipped here so it is never shown twice.
    overdue = [
        it
        for it in (digest.get("overdue_items") or [])
        if it.get("source_record_id") not in shown_ids
    ]
    if overdue:
        out.append("## Deadline passed")
        out.append(
            "_The comment window for these items closed recently — they are listed so you know._"
        )
        out.append("")
        for item in overdue:
            # action_contacts omitted — a contact prompt for a closed deadline would
            # mislead the reader into thinking action is still possible.
            _render_item(
                item,
                out,
                expand=_expand,
                ctx=dataclasses.replace(ctx, action_contacts=None),
            )

    # "Near you": the proximity-banded, building-threaded feed. Items already shown in the
    # "Act on this" lead or the "Right next to you" section are skipped here to avoid showing
    # the same event twice; a building whose every item already appeared above is omitted.
    # To keep a busy week scannable, only the closest FEED_NEAR_YOU_CAP building threads
    # render in full; the rest are summarized in a compact "More nearby" list that still
    # links each building's record (nothing is hidden behind a link we can't honor — there
    # is no web archive yet, so the remainder links straight to the City record).
    feed = [
        (section["label"], building, visible)
        for section in digest["sections"]
        for building in section["buildings"]
        if (
            visible := [
                it for it in building["items"] if it.get("source_record_id") not in shown_ids
            ]
        )
    ]
    if feed:
        out.append("## Near you")
        out.append("")
        current_label: str | None = None
        for label, building, visible in feed[:FEED_NEAR_YOU_CAP]:
            if not visible:
                continue
            if label != current_label:
                out.append(f"### {label}")
                out.append("")
                current_label = label
            out.append(f"#### {building['label']}")
            if len(visible) > 1:
                out.append(f"_{len(visible)} updates on this building_")
            out.append("")
            note = _corroboration_note(building["items"])
            if note:
                out.append(note)
                out.append("")
            for item in visible:
                _render_item(item, out, expand=_expand, ctx=ctx)

        remainder = feed[FEED_NEAR_YOU_CAP:]
        if remainder:
            out.append("### More nearby")
            out.append(
                f"_{len(remainder)} more building{'s' if len(remainder) != 1 else ''} nearby with"
                " activity this week — each links to its City record._"
            )
            out.append("")
            for label, building, visible in remainder:
                count = f"{len(visible)} update{'s' if len(visible) != 1 else ''}"
                link = _first_link(visible)
                suffix = f" · {link}" if link else ""
                out.append(f"- **{building['label']}** ({label}) — {count}{suffix}")
            out.append("")

    # "Later": items with a still-open action window more than LEAD_MAX_DAYS out —
    # they matter but aren't urgent enough to headline this week's digest. Suppress any
    # already shown in a lead section above so nothing is rendered twice.
    later = [
        it
        for it in (digest.get("later_items") or [])
        if it.get("source_record_id") not in shown_ids
    ]
    if later:
        out.append("### Later")
        out.append(f"_These items have open action windows more than {LEAD_MAX_DAYS} days out._")
        out.append("")
        for item in later:
            _render_lead_item(item, asof, out, expand=_expand, ctx=ctx)

    # "Happened this week": events that occurred in the past 7 days with no open
    # action window — context only, not an invitation to act. Suppress any already shown
    # in a lead section above so nothing is rendered twice.
    recent = [
        it
        for it in (digest.get("recent_items") or [])
        if it.get("source_record_id") not in shown_ids
    ]
    if recent:
        out.append("### Happened this week")
        out.append(
            "_These events occurred in the past 7 days"
            " — they are context, not an open action window._"
        )
        out.append("")
        for item in recent:
            _render_item(item, out, expand=_expand, ctx=ctx)

    if digest["footnotes"]:
        out.append("---")
        for note in digest["footnotes"]:
            out.append(f"> {note}")
        out.append("")

    # Honest footer: describe every tier truthfully — exact City record, official search
    # tool, or no link at all (read from a public document, flagged for verification). It
    # must never claim a link an item does not have.
    n = digest["item_count"]
    exact = digest["exact_verifiable_count"]
    linked = digest.get("linked_count", n)
    search_only = linked - exact
    unlinked = n - linked
    out.append("---")
    if exact == n:
        out.append(
            "_Every update above links to the City's own record for that exact "
            "building or filing, so you can verify it yourself._"
        )
    else:
        parts = [f"{exact} of {n} updates link to the exact City record"]
        if search_only:
            parts.append(f"{search_only} link to an official search tool where you can look it up")
        if unlinked:
            parts.append(
                f"{unlinked} {'is' if unlinked == 1 else 'are'} read from a public document and "
                "marked [needs verification] — there is no City record to link yet, so confirm "
                "before relying on it"
            )
        out.append("_" + "; ".join(parts) + "._")
    return "\n".join(out)
