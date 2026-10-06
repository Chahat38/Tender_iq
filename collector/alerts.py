"""User-facing alert worker.

Turns collector state (matches, saved opportunities, updates,
deadlines) into rows in the ``alerts`` table, which the API exposes
read-only. The schema's ``UNIQUE (user_id, opportunity_id, type)``
gives us the de-duplication rule for free: each alert type fires **at
most once per user and opportunity** - the worker only ever inserts
where no such row exists yet.

Alert types and their evidence:

* ``new_match`` - the user's relevance score for the opportunity is at
  or above the profile's ``alert_threshold``;
* ``deadline_approaching`` - the deadline falls inside
  :data:`DEADLINE_WINDOW_DAYS` and the user watches the opportunity
  (matched at/above threshold or saved);
* ``opportunity_updated`` - a non-document field of a watched
  opportunity changed;
* ``corrigendum`` - the document list of a watched opportunity changed
  (sources publish corrigenda by swapping attachments).

Everything is in-app (``channel = "in_app"``); other channels plug in
at the API/notification layer, not here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from storage_common import utc_now_iso

logger = logging.getLogger(__name__)

TYPE_NEW_MATCH = "new_match"
TYPE_DEADLINE = "deadline_approaching"
TYPE_UPDATED = "opportunity_updated"
TYPE_CORRIGENDUM = "corrigendum"

ALERT_TYPES = (TYPE_NEW_MATCH, TYPE_DEADLINE, TYPE_UPDATED, TYPE_CORRIGENDUM)

#: How close (in days) a deadline must be to raise the alert.
DEADLINE_WINDOW_DAYS = 3

#: Cap on candidates pulled per user per type (bounded work per run).
CANDIDATE_LIMIT = 500


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def run(
    storage: Any,
    *,
    now: datetime | None = None,
    deadline_window_days: int = DEADLINE_WINDOW_DAYS,
    channel: str = "in_app",
    limit: int = CANDIDATE_LIMIT,
) -> dict[str, int]:
    """Create every alert that does not exist yet.

    Returns counts per alert type plus ``"total"``.
    """
    now = now or datetime.now(timezone.utc)
    window_end = now + timedelta(days=deadline_window_days)
    created_at = utc_now_iso()
    report: dict[str, int] = {alert_type: 0 for alert_type in ALERT_TYPES}
    report["total"] = 0

    for profile in storage.list_profiles():
        user_id = str(profile["user_id"])
        threshold = int(profile.get("alert_threshold") or 60)

        # 1. new_match: fresh, above-threshold relevance scores.
        for candidate in storage.list_match_alert_candidates(
            user_id, min_score=threshold, limit=limit
        ):
            reasons = _load_reasons(candidate.get("reasons"))
            body = f"Relevance {candidate['relevance_score']} ({candidate['level']})"
            if reasons:
                body += " - " + "; ".join(reasons[:3])
            if storage.insert_alert(
                user_id=user_id,
                opportunity_id=int(candidate["id"]),
                type=TYPE_NEW_MATCH,
                title=str(candidate["title"] or "New matching opportunity"),
                body=body,
                channel=channel,
                created_at=created_at,
            ):
                report[TYPE_NEW_MATCH] += 1
                report["total"] += 1

        # Watched set feeds the other three types.
        watchlist = storage.list_watchlist_opportunities(
            user_id, min_score=threshold, limit=limit
        )

        # 2. deadline_approaching: inside the window (future only).
        for candidate in watchlist:
            deadline = _parse_dt(candidate.get("deadline"))
            if deadline is None or not (now < deadline <= window_end):
                continue
            days_left = (deadline.date() - now.date()).days
            body = f"Deadline {deadline.isoformat()}"
            if days_left > 0:
                body += f" (in {days_left} day{'s' if days_left != 1 else ''})"
            else:
                body += " (today)"
            if storage.insert_alert(
                user_id=user_id,
                opportunity_id=int(candidate["id"]),
                type=TYPE_DEADLINE,
                title=str(candidate["title"] or "Deadline approaching"),
                body=body,
                channel=channel,
                created_at=created_at,
            ):
                report[TYPE_DEADLINE] += 1
                report["total"] += 1

        # 3. opportunity_updated: any non-document field changed.
        for candidate in storage.list_update_alert_candidates(
            user_id,
            min_score=threshold,
            alert_type=TYPE_UPDATED,
            exclude_field="document_urls",
            limit=limit,
        ):
            if storage.insert_alert(
                user_id=user_id,
                opportunity_id=int(candidate["id"]),
                type=TYPE_UPDATED,
                title=str(candidate["title"] or "Opportunity updated"),
                body=f"Latest change recorded {candidate['latest_update']}",
                channel=channel,
                created_at=created_at,
            ):
                report[TYPE_UPDATED] += 1
                report["total"] += 1

        # 4. corrigendum: the document list changed.
        for candidate in storage.list_update_alert_candidates(
            user_id,
            min_score=threshold,
            alert_type=TYPE_CORRIGENDUM,
            field="document_urls",
            limit=limit,
        ):
            if storage.insert_alert(
                user_id=user_id,
                opportunity_id=int(candidate["id"]),
                type=TYPE_CORRIGENDUM,
                title=str(candidate["title"] or "Corrigendum published"),
                body=f"Documents changed (recorded {candidate['latest_update']})",
                channel=channel,
                created_at=created_at,
            ):
                report[TYPE_CORRIGENDUM] += 1
                report["total"] += 1

    if report["total"]:
        logger.info("alerts created: %s", report)
    return report


def _load_reasons(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def list_alerts(
    storage: Any, user_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Newest alerts for one user (thin wrapper for the API)."""
    return storage.list_alerts(user_id, limit=limit)
