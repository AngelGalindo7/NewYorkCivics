"""Human-review gate — clear the needs-verification queue before send (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: stand between a freshly built digest
and :func:`ingest.deliver.send.send_digest`. A digest that carries any AI-surfaced or
lower-confidence ("needs verification") item must not reach a real subscriber until a
human has looked at each flagged item and decided to keep it (approve, shown as-is with
its footnote) or drop it. This is the real gate that replaces the dev-only bypass flag.

The machinery here operates only on the canonical digest dict and subscriber dict — it
knows nothing about any particular city, source, or taxonomy. It walks the flagged
items, asks a decision callable (interactive by default) about each, removes the
rejected ones, and recomputes the digest's derived counts so the email stays honest.
Cleared, it sets ``review_required=False`` and the send gate in send.py opens.

A small JSON file on disk is the whole store: a digest built in one process can be
dumped, reviewed in another process, and then sent. No database, no queue framework.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ingest.deliver.digest import ATTENTION_DEADLINE_DAYS, _is_hazardous_violation

# Where pending (built-but-unreviewed) digests are parked between processes.
DEFAULT_REVIEW_DIR = Path("out") / "review"

# Item date fields that must survive the JSON round-trip as real date objects so the
# renderer and the send gate behave identically before and after persistence. (asof is
# deliberately NOT in this list — the renderer expects it as an ISO string.)
_ITEM_DATE_FIELDS = ("deadline", "event_date", "actionable_date")

# Citations strong enough to claim a row-exact verification (drives the honest footer).
_EXACT_VERIFIES = ("exact_record", "exact_building")


# --------------------------------------------------------------------------------------
# (a) review_digest — the pure, testable core
# --------------------------------------------------------------------------------------


def _iter_items(digest: dict[str, Any]) -> list[dict[str, Any]]:
    """Every item across the digest's sections, in render order."""
    return [
        item
        for section in digest.get("sections", [])
        for building in section.get("buildings", [])
        for item in building.get("items", [])
    ]


def _flagged_items(digest: dict[str, Any]) -> list[dict[str, Any]]:
    """Every needs-verification item actually present in the digest's body.

    Keys off what the renderer will show, not the upstream ``review_required`` flag or the
    ``review_items`` title list — either can under-report a flagged item the body still
    renders (a builder regression, a hand-edited digest), letting it slip past unreviewed.
    The gate must judge what the reader sees.
    """
    return [item for item in _iter_items(digest) if item.get("needs_verification")]


def _strongest_citation_url(item: dict[str, Any]) -> str | None:
    """The first (strongest, since citations are pre-sorted) source URL, if any."""
    for cite in item.get("citations") or []:
        url = cite.get("url")
        if url:
            return url
    return None


def _echo_item(item: dict[str, Any], echo: Callable[[str], Any]) -> None:
    """Print a readable summary of one flagged item so a human can judge it."""
    echo("")
    echo(f"  Title:      {item.get('title', '(untitled)')}")
    summary = item.get("summary")
    if summary:
        echo(f"  Summary:    {summary}")
    # When the item is relevant — a deadline phrase if it has one, else its event date.
    when = item.get("deadline_note")
    if not when and item.get("event_date") is not None:
        when = str(item["event_date"])
    if when:
        echo(f"  When:       {when}")
    confidence = item.get("confidence")
    if confidence is not None:
        echo(f"  Confidence: {confidence}")
    url = _strongest_citation_url(item)
    if url:
        echo(f"  Source:     {url}")
    echo(
        "  Why flagged: surfaced by a correlation or a lower-confidence record, "
        "not a confirmed fact — keep it only if the source above checks out."
    )


def _prompt_decision(item: dict[str, Any]) -> bool:
    """Default interactive decision: prompt on stdin, treat y/yes as approve."""
    answer = input("Approve this item? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _needs_attention(item: dict[str, Any], asof: date) -> bool:
    """The same notion the builder used: flagged, or a deadline inside the window."""
    if item.get("needs_verification"):
        return True
    deadline = item.get("deadline")
    if deadline is not None:
        return (deadline - asof).days <= ATTENTION_DEADLINE_DAYS
    return False


def _subject_line(item_count: int, attention_count: int) -> str:
    """Build the subject/H1 string from the two counts it summarizes.

    The renderer prints ``digest['subject']`` verbatim as the email's H1, so it is itself
    a derived count. This mirrors the formula the builder uses when it first assembles the
    digest; recomputing it here keeps the headline honest after items are removed.
    """
    plural = "s" if item_count != 1 else ""
    subject = f"Your neighborhood this week: {item_count} update{plural}"
    if attention_count:
        attn_plural = "s" if attention_count == 1 else ""
        subject += f" ({attention_count} need{attn_plural} attention)"
    return subject


def _recompute_derived(digest: dict[str, Any]) -> None:
    """Refresh the rendered-facing counts/footnotes after items were removed.

    Mutates the passed digest in place (the caller owns it — it is already a copy). The
    derived fields must track the surviving items so the email never claims a count that
    no longer matches what it shows.
    """
    items = _iter_items(digest)
    asof = date.fromisoformat(digest["asof"])

    digest["item_count"] = len(items)
    digest["building_count"] = sum(
        len(section.get("buildings", [])) for section in digest.get("sections", [])
    )
    digest["exact_verifiable_count"] = sum(
        1 for item in items if item.get("verifies") in _EXACT_VERIFIES
    )
    digest["needs_attention_count"] = sum(1 for item in items if _needs_attention(item, asof))
    # The at-risk lead is a derived field too: re-derive it from the surviving buildings so
    # the "Right next to you" lead can't point at a building review just emptied. (Today a
    # hazardous violation is always ACCEPTED and never rejected, so this is belt-and-braces —
    # but it keeps the field honest if that ever changes.)
    digest["at_risk_building_keys"] = [
        building["key"]
        for section in digest.get("sections", [])
        for building in section.get("buildings", [])
        if any(_is_hazardous_violation(item) for item in building["items"])
    ]
    # The subject is the email's H1 and a derived count — refresh it too, or the headline
    # keeps reporting pre-rejection totals over a recomputed body.
    digest["subject"] = _subject_line(digest["item_count"], digest["needs_attention_count"])

    # Keep the needs-verification footnote only while a flagged item still survives.
    any_flagged = any(item.get("needs_verification") for item in items)
    footnotes = [
        note for note in digest.get("footnotes", []) if "needs verification" not in note.lower()
    ]
    if any_flagged:
        footnotes.append(
            "Items marked [needs verification] are AI-surfaced correlations or "
            "lower-confidence records, not confirmed facts - a human reviews them "
            "before this digest is sent, and the source links let you check them yourself."
        )
    digest["footnotes"] = footnotes


def _drop_items(digest: dict[str, Any], rejected: list[int]) -> None:
    """Remove the rejected items (matched by object identity) and prune empties.

    Drops each rejected item from its building; drops a building left with no items;
    drops a section left with no buildings; and drops the item from ``lead_items`` too.
    """
    rejected_ids = set(rejected)

    surviving_sections: list[dict[str, Any]] = []
    for section in digest.get("sections", []):
        surviving_buildings: list[dict[str, Any]] = []
        for building in section.get("buildings", []):
            kept = [item for item in building.get("items", []) if id(item) not in rejected_ids]
            if kept:
                building["items"] = kept
                surviving_buildings.append(building)
        if surviving_buildings:
            section["buildings"] = surviving_buildings
            surviving_sections.append(section)
    digest["sections"] = surviving_sections

    digest["lead_items"] = [
        item for item in digest.get("lead_items", []) if id(item) not in rejected_ids
    ]


def review_digest(
    digest: dict[str, Any],
    *,
    decide: Callable[[dict[str, Any]], bool] | None = None,
    echo: Callable[[str], Any] = print,
) -> dict[str, Any]:
    """Clear the human-review queue on a copy of ``digest`` and return the cleared copy.

    Walks every item flagged ``needs_verification`` across the digest's sections. For
    each it echoes a readable summary then asks ``decide(item)``: ``True`` keeps the item
    (approved as-shown, still flagged, footnote intact), ``False`` removes it from its
    building / section / lead. With ``decide`` left as ``None`` the decision is made
    interactively on stdin (``y``/``yes`` approves).

    After any rejections the derived fields (item/building/exact/attention counts and the
    needs-verification footnote) are recomputed so the rendered digest stays truthful.
    Finally ``review_required`` is set False and ``review_items`` emptied — the human has
    cleared the queue and the send gate may open.

    With no flagged items this is a no-op: the input digest is returned unchanged (it was
    already sendable).
    """
    flagged = _flagged_items(digest)
    if not flagged:
        # Nothing to clear: already sendable, return as-is without copying or mutating.
        return digest

    decide = decide or _prompt_decision
    working = _deep_copy_digest(digest)

    # Decide against the COPY's items so identity-based removal targets the copy.
    flagged_copy = _flagged_items(working)
    rejected: list[int] = []
    for item in flagged_copy:
        _echo_item(item, echo)
        if not decide(item):
            rejected.append(id(item))

    if rejected:
        _drop_items(working, rejected)
        _recompute_derived(working)

    working["review_required"] = False
    working["review_items"] = []
    return working


def _deep_copy_digest(digest: dict[str, Any]) -> dict[str, Any]:
    """A deep copy we can mutate freely without touching the caller's digest.

    Two invariants matter. Removal keys off object identity, so the copy needs fresh item
    dicts — a shallow copy would mutate the caller's items. And an item shared between a
    section and ``lead_items`` must stay one object in the copy, so removing it by identity
    drops it from both; ``copy.deepcopy`` preserves that sharing through its memo.
    """
    import copy

    return copy.deepcopy(digest)


# --------------------------------------------------------------------------------------
# (b) dump_pending / load_pending — minimal cross-process persistence
# --------------------------------------------------------------------------------------


def _json_default(obj: Any) -> str:
    """JSON encoder hook: render date/datetime as ISO strings; reject anything else."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _slug(value: str | None) -> str:
    """Filesystem-safe stem from an arbitrary string (e.g. a subscriber email)."""
    import re

    return re.sub(r"[^a-z0-9]+", "-", (value or "subscriber").lower()).strip("-") or "subscriber"


def dump_pending(
    digest: dict[str, Any],
    subscriber: dict[str, Any],
    *,
    review_dir: Path = DEFAULT_REVIEW_DIR,
) -> Path:
    """Persist a built-but-unreviewed digest so another process can review then send it.

    Writes ``{"subscriber": ..., "digest": ...}`` as JSON, converting any date/datetime
    to its ISO string. The filename is derived from the subscriber email and the digest's
    asof date so a re-run for the same person/day overwrites rather than piling up.
    """
    review_dir.mkdir(parents=True, exist_ok=True)
    path = review_dir / f"{_slug(subscriber.get('email'))}-{digest['asof']}.json"
    payload = {"subscriber": subscriber, "digest": digest}
    path.write_text(json.dumps(payload, default=_json_default, indent=2), encoding="utf-8")
    return path


def _revive_item_dates(item: dict[str, Any]) -> None:
    """Turn an item's ISO-string date fields back into date objects, in place.

    JSON has no date type, so dump_pending wrote ``deadline`` / ``event_date`` /
    ``actionable_date`` as strings. The renderer and the send gate expect real dates, so
    revive each non-null field. (``asof`` is left a string by design — the renderer parses
    it itself.)
    """
    for field in _ITEM_DATE_FIELDS:
        value = item.get(field)
        if isinstance(value, str):
            item[field] = date.fromisoformat(value)


def load_pending(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read a pending JSON written by :func:`dump_pending`; return ``(digest, subscriber)``.

    Revives the date-typed item fields (``deadline`` / ``event_date`` /
    ``actionable_date``) across ``lead_items`` and every section's buildings' items so the
    loaded digest renders and sends exactly like the in-memory one it was dumped from.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    digest = payload["digest"]
    subscriber = payload["subscriber"]

    for item in digest.get("lead_items", []):
        _revive_item_dates(item)
    for item in _iter_items(digest):
        _revive_item_dates(item)

    return digest, subscriber


# --------------------------------------------------------------------------------------
# (c) main — the standalone CLI
# --------------------------------------------------------------------------------------


def _most_recent_pending(review_dir: Path) -> Path | None:
    """The newest pending *.json in the review dir, or None when the dir is empty."""
    if not review_dir.exists():
        return None
    candidates = sorted(review_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def main(argv: list[str] | None = None) -> int:
    """Interactive review CLI: clear a pending digest's queue, then send it if cleared.

    With ``--digest PATH`` it reviews that pending file; otherwise it picks the most
    recent one in :data:`DEFAULT_REVIEW_DIR`. If nothing is pending, or the chosen digest
    needs no review, it says so and exits cleanly. After an interactive pass it sends the
    digest only when the gate actually cleared.
    """
    from ingest.deliver.send import send_digest

    parser = argparse.ArgumentParser(
        prog="python -m ingest.deliver.review",
        description="Review a pending digest's flagged items, then send it once cleared.",
    )
    parser.add_argument(
        "--digest",
        type=Path,
        default=None,
        help="Path to a pending digest JSON (default: most recent in out/review).",
    )
    args = parser.parse_args(argv)

    path = args.digest or _most_recent_pending(DEFAULT_REVIEW_DIR)
    if path is None:
        print(f"No pending digests to review in {DEFAULT_REVIEW_DIR}.")
        return 0

    digest, subscriber = load_pending(path)

    # Gate on the flagged items actually in the body, not the upstream review_required
    # flag: if that flag is ever left False while a flagged item is still rendered (a
    # builder regression, a hand-edited digest), reviewing the real content still catches it.
    flagged = _flagged_items(digest)
    if not flagged:
        print(f"Nothing pending: {path} has no items awaiting review.")
        return 0

    recipient = digest.get("subscriber_email") or subscriber.get("email") or "(unknown)"
    print(f"Reviewing digest for {recipient}: {len(flagged)} item(s) need review.")

    reviewed = review_digest(digest)

    if reviewed.get("review_required"):
        # Gate did not clear (the decision callable left it required) — do not send.
        print("Review not cleared; nothing sent.")
        return 0

    if reviewed.get("item_count", 0) == 0:
        # Every item was rejected — there is nothing left worth sending.
        print("All items were rejected; the digest is empty, nothing sent.")
        return 0

    written = send_digest(reviewed, subscriber)
    # Consume the pending file so a later run does not re-review and re-send the same
    # digest. Only on a real send — the not-cleared / empty-after-rejection paths above
    # return early and leave the file in place.
    path.unlink(missing_ok=True)
    print(f"Digest sent. Written to: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
