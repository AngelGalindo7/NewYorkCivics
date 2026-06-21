"""Digest assembly — group, order, human-review, then render (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: turn one subscriber's matched,
ranked events into a plain-English, forward-looking digest, gate it through human
review, and render the verifiable email body. Sending is send.py's job.

The render is a short weekly briefing built for scannability and trust. Top to
bottom: a personalization line scoped to the subscriber's address; a one-line stats
hook of whole-number counts; an "Act on this" lead of items a reader can still act
on (a future deadline or event); the proximity-banded building feed; the honest
verifiability footer. Forward-looking items lead because a deadline you can still
meet is the most useful thing in the email.

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

# Reader-facing labels + display order for the three proximity bands.
_BAND_LABELS = (
    (BAND_ON_YOUR_BLOCK, "On your block"),
    (BAND_IN_YOUR_NEIGHBORHOOD, "In your neighborhood"),
    (BAND_IN_YOUR_AREA, "In your area"),
)

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
    if action_type == "building_energy_grade":
        return 0.3  # building context, not an action a reader takes — ranks below a permit
    if action_type == "habitability_complaints":
        return 0.35  # a cluster of 311 reports is context (and a report, not a confirmed fact)
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


def _to_item(event: CivicEvent, band: str, asof: date) -> dict[str, Any]:
    """Project one CivicEvent into a render-ready digest item (with verify links)."""
    verified = event.status == RecordStatus.ACCEPTED
    cites = sorted(event.citations, key=lambda c: _VERIFY_RANK.get(c.verifies, 9))
    action_on = _actionable_date(event.event_date, event.deadline, asof)
    return {
        "title": event.title or "(untitled event)",
        "summary": event.summary or "",
        "action_type": event.action_type,
        "band": band,
        "bbl": event.bbl,
        "address": event.address,
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
    """
    return item["actionable_date"] is not None and not item["needs_verification"]


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
            label = it["address"] or (f"BBL {it['bbl']}" if it["bbl"] else it["title"])
            groups[key] = {"label": label, "items": []}
        groups[key]["items"].append(it)
    out = list(groups.values())
    for g in out:
        g["items"].sort(key=_actionability_key)
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
    return "This week near your address: " + " · ".join(clauses) + "."


def build_digest(
    subscriber: dict[str, Any],
    matched: dict[str, list[CivicEvent]],
    *,
    asof: date | None = None,
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

    # "Act on this": only items with a still-open action window, soonest first. The
    # same item may also appear in its building thread below for context — the digest
    # orders for actionability first. Needs-verification items are excluded from the lead.
    # Items with open windows more than LEAD_MAX_DAYS out move to "Later" so the urgent
    # lead stays focused on what a reader should act on this week or month.
    lead_cutoff = asof + timedelta(days=LEAD_MAX_DAYS)
    actionable = sorted((it for it in all_items if _is_actionable(it)), key=_lead_key)
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
            "Items marked [needs verification] are AI-surfaced correlations or "
            "lower-confidence records, not confirmed facts - a human reviews them "
            "before this digest is sent, and the source links let you check them yourself."
        )

    n = len(all_items)
    attn = len(attention)
    building_count = sum(len(s["buildings"]) for s in sections)
    # Honesty metric: how many items link to the exact record/building vs only a search.
    _exact = ("exact_record", "exact_building")
    exact_verifiable = sum(1 for it in all_items if it["verifies"] in _exact)
    subject = f"Your neighborhood this week: {n} update{'s' if n != 1 else ''}" + (
        f" ({attn} need{'s' if attn == 1 else ''} attention)" if attn else ""
    )

    return {
        "subject": subject,
        "subscriber_email": subscriber.get("email"),
        "area": subscriber.get("address"),
        "asof": asof.isoformat(),
        "stats_line": stats_line,
        "lead_items": lead_items,
        "later_items": later_items,
        "overdue_items": overdue_items,
        "recent_items": recent_items,
        "sections": sections,
        "item_count": n,
        "building_count": building_count,
        "exact_verifiable_count": exact_verifiable,
        "needs_attention_count": attn,
        "review_required": bool(review_items),
        "review_items": [it["title"] for it in review_items],
        "footnotes": footnotes,
    }


def _render_item(item: dict[str, Any], out: list[str]) -> None:
    tag = " **[needs verification]**" if item["needs_verification"] else ""
    out.append(f"**{item['title']}**{tag}")
    if item["summary"]:
        out.append(item["summary"])
    if item["deadline_note"]:
        out.append(f"- **Action deadline:** {item['deadline_note']}")
    # Sources, strongest proof first, on one line so it reads like a citation.
    if item["citations"]:
        links = " · ".join(f"[{c['label']}]({c['url']})" for c in item["citations"])
        prefix = "Verify" if not item["needs_verification"] else "Check the sources"
        out.append(f"- {prefix}: {links}")
    out.append("")


def _render_lead_item(item: dict[str, Any], asof: date, out: list[str]) -> None:
    """One compact entry in the 'Act on this' lead: title, when, and the verify line."""
    out.append(f"**{item['title']}**")
    when = item["actionable_date"]
    if when is not None:
        out.append(f"- **When:** {_when_phrase(when, asof)} ({when.isoformat()})")
    if item["citations"]:
        links = " · ".join(f"[{c['label']}]({c['url']})" for c in item["citations"])
        out.append(f"- Verify: {links}")
    out.append("")


def render_markdown(digest: dict[str, Any]) -> str:
    """Render the digest into the plain-English, verifiable email body (Markdown)."""
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

    # "Act on this": the forward-looking lead — still-open items only, soonest first.
    asof = date.fromisoformat(digest["asof"])
    lead = digest.get("lead_items") or []
    if lead:
        out.append("## Act on this")
        out.append("")
        for item in lead:
            _render_lead_item(item, asof, out)

    # "Deadline passed": recently lapsed items — listed so the reader knows what
    # closed, without implying an open action window.
    overdue = digest.get("overdue_items") or []
    if overdue:
        out.append("## Deadline passed")
        out.append(
            "_The comment window for these items closed recently — they are listed so you know._"
        )
        out.append("")
        for item in overdue:
            _render_item(item, out)

    # "Near you": the proximity-banded, building-threaded feed.
    out.append("## Near you")
    out.append("")
    for section in digest["sections"]:
        out.append(f"### {section['label']}")
        out.append("")
        for building in section["buildings"]:
            items = building["items"]
            out.append(f"#### {building['label']}")
            if len(items) > 1:
                out.append(f"_{len(items)} updates on this building_")
            out.append("")
            for item in items:
                _render_item(item, out)

    # "Later": items with a still-open action window more than LEAD_MAX_DAYS out —
    # they matter but aren't urgent enough to headline this week's digest.
    later = digest.get("later_items") or []
    if later:
        out.append("### Later")
        out.append(f"_These items have open action windows more than {LEAD_MAX_DAYS} days out._")
        out.append("")
        for item in later:
            _render_lead_item(item, asof, out)

    # "Happened this week": events that occurred in the past 7 days with no open
    # action window — context only, not an invitation to act.
    recent = digest.get("recent_items") or []
    if recent:
        out.append("### Happened this week")
        out.append(
            "_These events occurred in the past 7 days"
            " — they are context, not an open action window._"
        )
        out.append("")
        for item in recent:
            _render_item(item, out)

    if digest["footnotes"]:
        out.append("---")
        for note in digest["footnotes"]:
            out.append(f"> {note}")
        out.append("")

    # Honest footer: only claim row-exact verifiability for the items that have it.
    n, exact = digest["item_count"], digest["exact_verifiable_count"]
    out.append("---")
    if exact == n:
        out.append(
            "_Every update above links to the City's own record for that exact "
            "building or filing, so you can verify it yourself._"
        )
    else:
        out.append(
            f"_{exact} of {n} updates link to the exact City record; the rest link to an "
            "official search tool where you can look the building up._"
        )
    return "\n".join(out)
