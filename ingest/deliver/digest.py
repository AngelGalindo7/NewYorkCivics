"""Digest assembly — group, order, human-review, then render (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: turn one subscriber's matched,
ranked events into a plain-English digest, gate it through human review, and
render the verifiable email body. Sending is send.py's job.

Rules honored here:
  - Rule 9  (Human-review-then-send): :func:`build_digest` returns a review-ready
            object with ``review_required`` set whenever a non-high-confidence item
            is in the top-N. A wrong extraction never auto-publishes as fact.
  - Rule 10 (Confidence routing): every item is tagged verified vs needs-verification
            and the render visibly separates them with a footnote — the biggest
            trust lever.
  - Rule 8  (linear ranker): rank.score() breaks ties, but the digest orders for
            ACTIONABILITY first (soonest/overdue deadlines lead).
  - Rule 3  (quote the source) + citations: each item renders its source links so a
            reader can verify the claim against the authoritative record.

CITY-AGNOSTIC: renders canonical CivicEvents; no NYC specifics. Summaries are reused
from Extract / the structured connectors (cached), never regenerated here.
"""

from __future__ import annotations

from datetime import date
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

# Default cap on items surfaced for human review per subscriber per run (Rule 9).
# TODO Phase 2: tune against review-queue clearing time (pivot threshold: if
# clearing exceeds 30 min/day the validation is too strict — tighten upstream).
DEFAULT_REVIEW_TOP_N = 10

# Soonest-deadline window (days) that counts as "needs your attention".
ATTENTION_DEADLINE_DAYS = 14

# Reader-facing labels + display order for the three proximity bands.
_BAND_LABELS = (
    (BAND_ON_YOUR_BLOCK, "On your block"),
    (BAND_IN_YOUR_NEIGHBORHOOD, "In your neighborhood"),
    (BAND_IN_YOUR_AREA, "In your area"),
)

# Per-action-type category weight (Rule 8 w_cat) and a coarse magnitude prior.
_CATEGORY_WEIGHT = {
    "displacement_signal": 1.0,
    "rezoning": 0.9,
    "violation": 0.6,
    "permit": 0.5,
    "hearing": 0.7,
}


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
        "category_weight": _CATEGORY_WEIGHT.get(event.action_type or "", 0.4),
    }


# Order citations strongest-first so the reader sees the best proof first.
_VERIFY_RANK = {"exact_record": 0, "exact_building": 1, "search": 2}


def _to_item(event: CivicEvent, band: str, asof: date) -> dict[str, Any]:
    """Project one CivicEvent into a render-ready digest item (with verify links)."""
    verified = event.status == RecordStatus.ACCEPTED
    cites = sorted(event.citations, key=lambda c: _VERIFY_RANK.get(c.verifies, 9))
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
        "score": rank.score(_signals(event, band, asof)),
        "citations": [
            {"kind": c.kind, "verifies": c.verifies, "label": c.label, "url": c.url}
            for c in cites
        ],
    }


def _actionability_key(item: dict[str, Any]) -> tuple:
    """Sort key: soonest/overdue deadlines first, then highest rank score."""
    has_deadline = item["deadline"] is not None
    return (not has_deadline, item["deadline"] or date.max, -item["score"])


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


def build_digest(
    subscriber: dict[str, Any],
    matched: dict[str, list[CivicEvent]],
    *,
    asof: date | None = None,
    review_top_n: int = DEFAULT_REVIEW_TOP_N,
) -> dict[str, Any]:
    """Assemble a review-ready digest for one subscriber.

    Args:
        subscriber: the subscriber row (email/address used for the header).
        matched: band -> events, as returned by match.match_subscriber (ranked or not).
        asof: reference date for deadline/recency phrasing (default today).
        review_top_n: cap on items a human reviews before send (Rule 9).

    Returns a structured digest: ordered, non-empty sections (block ->
    neighborhood -> area), each item carrying its source citations and a
    verified/needs-verification tag (Rule 10), plus ``review_required`` and the
    list of items a human must clear before send (Rule 9). Does NOT send.
    """
    asof = asof or date.today()

    sections: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    for band, label in _BAND_LABELS:
        items = [_to_item(ev, band, asof) for ev in matched.get(band, [])]
        if items:
            buildings = _group_buildings(items)
            sections.append({"band": band, "label": label, "buildings": buildings})
            all_items.extend(items)

    def _needs_attention(item: dict[str, Any]) -> bool:
        if item["needs_verification"]:
            return True
        if item["deadline"] is not None:
            return (item["deadline"] - asof).days <= ATTENTION_DEADLINE_DAYS
        return False

    attention = [it for it in all_items if _needs_attention(it)]
    # Top-N most actionable items are what the human actually clears (Rule 9).
    top_n = sorted(all_items, key=_actionability_key)[:review_top_n]
    review_items = [it for it in top_n if it["needs_verification"]]

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
    subject = (
        f"Your neighborhood this week: {n} update{'s' if n != 1 else ''}"
        + (f" ({attn} need{'s' if attn == 1 else ''} attention)" if attn else "")
    )

    return {
        "subject": subject,
        "subscriber_email": subscriber.get("email"),
        "area": subscriber.get("address"),
        "asof": asof.isoformat(),
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


def render_markdown(digest: dict[str, Any]) -> str:
    """Render the digest into the plain-English, verifiable email body (Markdown)."""
    out: list[str] = []
    out.append(f"# {digest['subject']}")
    if digest.get("area"):
        out.append(f"_For {digest['area']} — as of {digest['asof']}_")
    out.append("")

    if digest["item_count"] == 0:
        out.append("No new civic activity near you this week.")
        return "\n".join(out)

    for section in digest["sections"]:
        out.append(f"## {section['label']}")
        out.append("")
        for building in section["buildings"]:
            items = building["items"]
            out.append(f"### {building['label']}")
            if len(items) > 1:
                out.append(f"_{len(items)} updates on this building_")
            out.append("")
            for item in items:
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
