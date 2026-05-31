"""Stage 1 (Fetch) — community board agendas. NYC-SPECIFIC.

Single responsibility: discover and download NYC community board meeting agendas
(PDFs), and identify which board / meeting each belongs to. This is a DIRTY
source: agendas are PDFs across ~59 board websites with wildly inconsistent
layouts. This module only fetches bytes + identity metadata; it does NOT parse or
extract — those are the city-agnostic Parse and Extract stages.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): no LLM here. Fetch is deterministic; the LLM
  fires later, in Extract, on the parsed PDF.
- Rule 3 (quote the source): preserve raw bytes faithfully so every downstream
  fact can trace back to a verbatim line in the original agenda.
- Rule 4 (NYC-specific code in nyc/): board URLs and the ~59-board roster are NYC
  knowledge and stay in this package.

This is one of the two PDF connectors (with ``ulurp_packet``). The ~59 boards
cluster by website template; Phase 2 builds fetchers per cluster (likely 5-20),
not per board.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgendaRef:
    """A discovered community board agenda, before download.

    Attributes:
        board: Community board id (e.g. ``"MN07"`` for Manhattan CB7). NYC-SPECIFIC.
        url: Direct link to the agenda PDF.
        meeting_date: ISO date of the meeting, if discoverable from the listing.
        title: Human label as it appears on the source site, if any.
    """

    board: str
    url: str
    meeting_date: str | None = None
    title: str | None = None


def discover_agendas(board: str | None = None) -> list[AgendaRef]:
    """Find new community board agendas to fetch.

    Args:
        board: Restrict discovery to a single board id; ``None`` scans all
            configured boards.

    Returns:
        References to agendas not yet ingested (caller dedups against the store).
    """
    raise NotImplementedError(
        "Phase 2: cluster ~59 board sites by template, then discover per cluster."
    )


def fetch(url: str) -> bytes:
    """Download one agenda PDF.

    Args:
        url: Direct PDF link from an :class:`AgendaRef`.

    Returns:
        Raw PDF bytes, handed verbatim to Parse (preserved for source grounding,
        Rule 3).
    """
    raise NotImplementedError("Phase 2: fetch agenda PDF bytes; no parsing here.")


# TODO Phase 2: build the board roster + per-cluster fetchers; verify real cluster
#   count in week 4 (may be 15-20; the long tail is where time goes).
# TODO Phase 2: link discovered agendas to a project_thread_id where the meeting
#   references a known ULURP/ZAP item (Rule 7).
