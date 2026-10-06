"""Cross-source duplicate detection and merging.

Two sources frequently list the same tender (PPRA + EPADS, KPPRA +
EPADS, ...). Dedup keeps every source row - nothing is deleted - but
marks one row as the group's ``is_primary`` representative, stamps the
rest with ``duplicate_group_id`` and maintains ``source_count`` (how
many distinct sources list the opportunity).

Matching tiers:

1. **Deterministic** - identical normalized reference numbers
   (case/punctuation-insensitive) with compatible deadlines (within
   :data:`REF_DEADLINE_WINDOW_DAYS`, or one side undated): score 1.0,
   auto-merged.
2. **Fuzzy** - weighted title/organization/deadline similarity. At or
   above :data:`AUTO_MERGE_THRESHOLD` the pair is merged automatically;
   between :data:`REVIEW_THRESHOLD` and that it lands in
   ``dedup_review_queue`` for a human decision; below it nothing
   happens.

Only rows not already in a group are scanned (:meth:`run`), pairs are
never formed within one source (the source's own ``external_id`` is the
identity there), and merges are unions: joining an ungrouped row to a
group, or fusing two groups, always leaves exactly one primary row.

Human decisions: :func:`resolve` applies a queued pair as confirmed
(merge) or rejected (status only).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Iterable
from uuid import uuid4

logger = logging.getLogger(__name__)

#: Auto-merge at or above this score (fuzzy tier).
AUTO_MERGE_THRESHOLD = 0.85
#: Queue for human review at or above this score; below = ignore.
REVIEW_THRESHOLD = 0.60

#: An exact reference match only counts when deadlines are compatible
#: within this many days (one undated side is compatible).
REF_DEADLINE_WINDOW_DAYS = 30

#: Full deadline credit within this many days; undated sides get 0.5.
MATCH_DEADLINE_WINDOW_DAYS = 10

#: Deadline-day bucket expansion when generating candidate pairs, so a
#: tender whose closing date drifted by a day or two still lands in the
#: same bucket.
DEADLINE_BUCKET_DAYS = 3

_WEIGHT_TITLE = 0.60
_WEIGHT_ORG = 0.25
_WEIGHT_DEADLINE = 0.15

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_REF_CLEAN = re.compile(r"[^A-Za-z0-9]+")


def normalize_reference(reference: str | None) -> str | None:
    """Uppercase alphanumerics only (``TS-0000/2026`` -> ``TS00002026``)."""
    if not reference:
        return None
    cleaned = _REF_CLEAN.sub("", str(reference)).upper()
    return cleaned or None


def normalize_text(text: str | None) -> str:
    """Lowercase, punctuation-free, single-spaced form for comparisons."""
    if not text:
        return ""
    return _NON_ALNUM.sub(" ", str(text).lower()).strip()


def _deadline_date(value: Any) -> date | None:
    """Best-effort date for a stored deadline value, or None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def title_similarity(a: str | None, b: str | None) -> float:
    """SequenceMatcher ratio on normalized titles (0 when either empty)."""
    left, right = normalize_text(a), normalize_text(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def org_similarity(a: str | None, b: str | None) -> float:
    """Organization similarity with neutral scores for missing values."""
    if not a and not b:
        return 0.5
    if not a or not b:
        return 0.2
    left, right = normalize_text(a), normalize_text(b)
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def deadline_score(a: Any, b: Any) -> float:
    """1.0 within the window, 0.5 when either side is undated, else 0."""
    left, right = _deadline_date(a), _deadline_date(b)
    if left is None or right is None:
        return 0.5
    if abs((left - right).days) <= MATCH_DEADLINE_WINDOW_DAYS:
        return 1.0
    return 0.0


def score_pair(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, str]:
    """Score how likely two opportunity rows are the same tender.

    Returns ``(score, reason)``; ``score == 0`` means "not a match".
    """
    ref_a = normalize_reference(a.get("reference_number"))
    ref_b = normalize_reference(b.get("reference_number"))
    if ref_a and ref_b and ref_a == ref_b:
        day_gap = _day_gap(a.get("deadline"), b.get("deadline"))
        if day_gap is None or day_gap <= REF_DEADLINE_WINDOW_DAYS:
            return 1.0, f"exact reference match ({ref_a})"

    title = title_similarity(a.get("title"), b.get("title"))
    if title == 0.0:
        return 0.0, ""
    org = org_similarity(a.get("organization"), b.get("organization"))
    deadline = deadline_score(a.get("deadline"), b.get("deadline"))
    score = _WEIGHT_TITLE * title + _WEIGHT_ORG * org + _WEIGHT_DEADLINE * deadline
    reason = f"title={title:.2f} org={org:.2f} deadline={deadline:.2f}"
    return score, reason


def _day_gap(a: Any, b: Any) -> int | None:
    """Absolute day distance between two deadlines; None if undated."""
    left, right = _deadline_date(a), _deadline_date(b)
    if left is None or right is None:
        return None
    return abs((left - right).days)


def candidate_pairs(rows: Iterable[dict[str, Any]]) -> set[tuple[int, int]]:
    """Generate candidate id pairs from shared reference/deadline/org buckets.

    Rows land in a bucket when they share a normalized reference number,
    an exact normalized organization, or a deadline within
    :data:`DEADLINE_BUCKET_DAYS` of each other. Only pairs that share at
    least one bucket are ever scored - the full cross-product is neither
    needed nor affordable.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ref = normalize_reference(row.get("reference_number"))
        if ref:
            buckets.setdefault(f"ref:{ref}", []).append(row)
        org = normalize_text(row.get("organization"))
        if org:
            buckets.setdefault(f"org:{org}", []).append(row)
        day = _deadline_date(row.get("deadline"))
        if day is not None:
            for offset in range(-DEADLINE_BUCKET_DAYS, DEADLINE_BUCKET_DAYS + 1):
                buckets.setdefault(f"day:{day + timedelta(days=offset)}", []).append(row)

    pairs: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        for i, first in enumerate(members):
            for second in members[i + 1 :]:
                if first["source_id"] == second["source_id"]:
                    continue  # one source's own rows are not cross-source dupes
                pairs.add((min(first["id"], second["id"]), max(first["id"], second["id"])))
    return pairs


def _recalculate_group(storage: Any, rows: dict[int, dict], group_id: str) -> None:
    """Persist one primary + source_count for every row of ``group_id``.

    ``rows`` must contain every member of the group.
    """
    members = [row for row in rows.values() if row["duplicate_group_id"] == group_id]
    members.sort(key=lambda row: (str(row["first_seen_at"]), row["id"]))
    source_count = len({row["source_id"] for row in members})
    for row in members:
        is_primary = row["id"] == members[0]["id"]
        row["is_primary"] = is_primary
        row["source_count"] = source_count
        storage.update_duplicate_state(
            row["id"],
            duplicate_group_id=group_id,
            is_primary=is_primary,
            source_count=source_count,
        )


def merge_pair(storage: Any, a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """Merge two opportunity rows into one duplicate group.

    Handles all three shapes: both ungrouped (new group), one already
    grouped (join), two different groups (union - the older row's group
    wins). Returns the group id, or None when they already share one.
    """
    group_a = a.get("duplicate_group_id")
    group_b = b.get("duplicate_group_id")
    if group_a and group_a == group_b:
        return None

    rows: dict[int, dict] = {int(a["id"]): dict(a), int(b["id"]): dict(b)}
    for group_id in {group_a, group_b} - {None}:
        for member in storage.list_opportunities_in_group(group_id):
            rows.setdefault(int(member["id"]), member)

    if group_a or group_b:
        # Keep the group of whichever row was seen first; the other
        # group's members are absorbed into it.
        keeper_row = min(rows.values(), key=lambda r: (str(r["first_seen_at"]), r["id"]))
        group_id = keeper_row["duplicate_group_id"] or group_a or group_b or uuid4().hex
    else:
        group_id = uuid4().hex

    for row in rows.values():
        if row["duplicate_group_id"] != group_id:
            row["duplicate_group_id"] = group_id
    _recalculate_group(storage, rows, group_id)
    return str(group_id)


def run(storage: Any) -> dict[str, int]:
    """Scan ungrouped opportunities; merge or queue every duplicate.

    Returns a report: ``{"examined", "candidate_pairs", "scored",
    "merged", "queued"}``. Safe to run repeatedly: already-grouped rows
    are outside the scan and queued pairs are unique.
    """
    rows = storage.list_ungrouped_opportunities()
    by_id = {int(row["id"]): row for row in rows}
    pairs = candidate_pairs(rows)

    report = {
        "examined": len(rows),
        "candidate_pairs": len(pairs),
        "scored": 0,
        "merged": 0,
        "queued": 0,
    }
    scored: list[tuple[float, str, int, int]] = []
    for id_a, id_b in pairs:
        score, reason = score_pair(by_id[id_a], by_id[id_b])
        if score >= REVIEW_THRESHOLD:
            report["scored"] += 1
            scored.append((score, reason, id_a, id_b))

    # Strongest evidence first: a merge makes later weaker pairs about
    # the same rows redundant (they are skipped as same-group).
    scored.sort(reverse=True)
    for score, reason, id_a, id_b in scored:
        row_a, row_b = by_id[id_a], by_id[id_b]
        if (
            row_a.get("duplicate_group_id")
            and row_a["duplicate_group_id"] == row_b.get("duplicate_group_id")
        ):
            continue
        if score >= AUTO_MERGE_THRESHOLD:
            merge_pair(storage, row_a, row_b)
            report["merged"] += 1
        else:
            added = storage.enqueue_dedup_review(id_a, id_b, score, reason)
            report["queued"] += added
            if added:
                logger.info(
                    "dedup review queued: %s <-> %s (%.2f: %s)",
                    id_a,
                    id_b,
                    score,
                    reason,
                )
    return report


def resolve(storage: Any, queue_id: int, decision: str) -> str | None:
    """Apply a human decision to one queued pair.

    ``decision`` is ``"confirmed"`` (merge the two rows) or
    ``"rejected"`` (leave them separate). Returns the resulting group id
    for confirmed merges, None otherwise.
    """
    if decision not in {"confirmed", "rejected"}:
        raise ValueError(f"unknown decision {decision!r}")
    reviews = storage.list_dedup_reviews(status="pending")
    review = next((row for row in reviews if int(row["id"]) == int(queue_id)), None)
    if review is None:
        raise ValueError(f"no pending dedup review with id {queue_id}")

    group_id = None
    if decision == "confirmed":
        row_a = storage.get_opportunity(int(review["opportunity_id_a"]))
        row_b = storage.get_opportunity(int(review["opportunity_id_b"]))
        if row_a is None or row_b is None:
            raise ValueError("queued pair references a deleted opportunity")
        group_id = merge_pair(storage, row_a, row_b)
    storage.resolve_dedup_review(queue_id, decision)
    return group_id
