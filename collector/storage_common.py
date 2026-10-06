"""Storage facts shared by the SQLite and Postgres backends.

Change detection, timestamp and document helpers live here so both
implementations of ``upsert_opportunity`` behave identically (one set of
rules, two sets of SQL).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from models import NormalizedOpportunity

#: Opportunity fields compared for change detection (source_url and the
#: identity fields are deliberately excluded -- source_url is never
#: overwritten, and GET-style date-window filters never drop undated
#: rows unless asked to).
TRACKED_FIELDS = (
    "title",
    "organization",
    "description",
    "published_at",
    "deadline",
    "location",
    "province",
    "category",
    "procurement_type",
    "reference_number",
)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def db_value(value: Any) -> Any:
    """Convert a normalized field value to its storage form.

    Datetimes become ISO-8601 strings and lists/tuples become JSON;
    everything else passes through (``None`` stays ``None``).
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return value


def filename_from_url(url: str) -> str | None:
    """Best-effort filename extracted from a document URL."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name or None


def collect_changes(
    existing: Mapping[str, Any],
    stored_docs: list[str],
    normalized: NormalizedOpportunity,
) -> list[tuple[str, Any, Any]]:
    """Compare a stored row against a normalized listing.

    Returns ``(field, old_value, new_value)`` tuples for every tracked
    field that differs plus one ``document_urls`` entry when the stored
    document list differs. ``existing`` is a mapping with the stored
    column values already in storage form (ISO strings for datetimes).
    """
    changes: list[tuple[str, Any, Any]] = []
    for field_name in TRACKED_FIELDS:
        new_value = db_value(getattr(normalized, field_name))
        old_value = existing.get(field_name)
        if old_value != new_value:
            changes.append((field_name, old_value, new_value))
    if stored_docs != normalized.document_urls:
        changes.append(
            (
                "document_urls",
                json.dumps(stored_docs),
                json.dumps(normalized.document_urls),
            )
        )
    return changes


def serialise_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert DB driver values into the storage form used everywhere.

    * ``datetime`` -> ISO-8601 UTC string (both backends answer the API
      with ISO strings, never driver-specific objects),
    * ``Decimal`` -> float (relevance scores),
    * ``bool`` kept as-is for SQLite's 0/1 columns where the column is
      already an int.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            out[key] = value.astimezone(timezone.utc).isoformat()
        else:
            out[key] = value
    return out
