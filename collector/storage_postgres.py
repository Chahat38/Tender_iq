"""Postgres/Supabase storage backend for the TenderScout collector.

Activated by the ``DATABASE_URL`` environment variable (see
:func:`storage.open_storage`). The class mirrors the collector-facing
behaviour of :class:`storage.Storage` -- same method names, same
"created"/"updated"/"unchanged" upsert contract, same change-detection
rules (shared through :mod:`storage_common`), ``source_url`` written
once and never overwritten -- against Postgres instead of SQLite.

SQLite stays the default for tests and offline runs; this backend is
exercised live by the ``postgres``-marked test suite
(``collector/tests/test_postgres_storage.py``), which needs a real
database URL (a local ``postgres``/``supabase start`` container or a
Supabase project).

Row values are serialised to the same storage form SQLite produces
(datetimes as ISO-8601 strings) so callers cannot tell the backends
apart by inspecting returned dicts.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from models import NormalizedOpportunity
from storage_common import (
    TRACKED_FIELDS as _TRACKED_FIELDS,
    collect_changes,
    db_value,
    filename_from_url,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

_INSERT_COLUMNS = (
    "source_id",
    "external_id",
    "reference_number",
    "title",
    "organization",
    "description",
    "source_url",
    "source_name",
    "published_at",
    "deadline",
    "location",
    "province",
    "category",
    "procurement_type",
    "content_hash",
    "first_seen_at",
    "last_seen_at",
    "last_verified_at",
    "raw_content",
)


class PostgresStorage:
    """Collector-side persistence on Postgres (psycopg 3).

    One instance owns one connection. Like the SQLite backend, callers
    share a single instance across threads only if they serialise
    access themselves (``check_same_thread`` does not apply here; the
    psycopg connection is not thread-safe for concurrent use).
    """

    def __init__(self, dsn: str | None = None, *, connection: Any = None) -> None:
        """Connect to ``$DATABASE_URL`` (or an explicit ``dsn``).

        ``connection`` injects an existing psycopg connection (tests).
        """
        dsn = dsn or os.environ.get("DATABASE_URL")
        if not dsn:
            raise ValueError(
                "PostgresStorage requires a DSN: pass dsn=... or set DATABASE_URL"
            )
        if connection is not None:
            self.conn = connection
            self._owns_connection = False
        else:
            import psycopg
            from psycopg.rows import dict_row

            self.conn = psycopg.connect(dsn, row_factory=dict_row)
            self._owns_connection = True
        # Timestamps round-trip as UTC so serialised values always carry
        # the same +00:00 offset regardless of the server timezone.
        with self.conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        self.conn.commit()
        self.dsn = _redact_dsn(dsn)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the connection (no-op for injected connections)."""
        if self._owns_connection and not self.conn.closed:
            self.conn.close()

    def __enter__(self) -> "PostgresStorage":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------

    def get_or_create_source(
        self, name: str, adapter_type: str, base_url: str | None = None
    ) -> int:
        """Return the id of the named source row, creating it if needed."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM procurement_sources WHERE name = %s", (name,)
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO procurement_sources (name, adapter_type, base_url) "
                    "VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE "
                    "SET name = excluded.name RETURNING id",
                    (name, adapter_type, base_url),
                )
                row = cur.fetchone()
            source_id = int(row["id"])
        self.conn.commit()
        return source_id

    def get_source(self, name: str) -> dict[str, Any] | None:
        """Return one source row by name as a dict, or None."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM procurement_sources WHERE name = %s", (name,))
            row = cur.fetchone()
        return _serialise(row)

    def list_sources(self) -> list[dict[str, Any]]:
        """Return every registered source row as a plain dict."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM procurement_sources ORDER BY id")
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    # ------------------------------------------------------------------
    # deduplication
    # ------------------------------------------------------------------

    #: Columns dedup needs to score a candidate pair.
    _DEDUP_PROJECTION = (
        "id, source_id, reference_number, title, organization, deadline, "
        "first_seen_at, duplicate_group_id, is_primary, source_count"
    )

    def list_ungrouped_opportunities(self) -> list[dict[str, Any]]:
        """Return dedup-relevant columns for rows not yet in a group."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._DEDUP_PROJECTION} FROM opportunities "
                "WHERE duplicate_group_id IS NULL ORDER BY id"
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def list_opportunities_in_group(self, group_id: str) -> list[dict[str, Any]]:
        """Return every row belonging to a duplicate group, oldest first."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._DEDUP_PROJECTION} FROM opportunities "
                "WHERE duplicate_group_id = %s ORDER BY first_seen_at, id",
                (group_id,),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def update_duplicate_state(
        self,
        opportunity_id: int,
        *,
        duplicate_group_id: str | None,
        is_primary: bool,
        source_count: int,
    ) -> None:
        """Write the three dedup columns for one row in a single update."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE opportunities SET duplicate_group_id = %s, "
                "is_primary = %s, source_count = %s WHERE id = %s",
                (duplicate_group_id, bool(is_primary), int(source_count), opportunity_id),
            )
        self.conn.commit()

    def enqueue_dedup_review(
        self,
        opportunity_id_a: int,
        opportunity_id_b: int,
        score: float,
        reason: str | None = None,
    ) -> int:
        """Queue one candidate pair for human review; 0 if already queued."""
        low, high = sorted((opportunity_id_a, opportunity_id_b))
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dedup_review_queue "
                "(opportunity_id_a, opportunity_id_b, score, reason, status, created_at) "
                "VALUES (%s, %s, %s, %s, 'pending', %s) "
                "ON CONFLICT (opportunity_id_a, opportunity_id_b) DO NOTHING",
                (low, high, score, reason, utc_now_iso()),
            )
            inserted = cur.rowcount
        self.conn.commit()
        return int(inserted or 0)

    def list_dedup_reviews(self, status: str = "pending") -> list[dict[str, Any]]:
        """Return dedup review rows for one status, oldest first."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM dedup_review_queue WHERE status = %s "
                "ORDER BY created_at, id",
                (status,),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def resolve_dedup_review(
        self, queue_id: int, status: str, *, resolved_at: str | None = None
    ) -> None:
        """Mark a review row resolved (``confirmed`` / ``rejected``)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE dedup_review_queue SET status = %s, resolved_at = %s "
                "WHERE id = %s",
                (status, resolved_at or utc_now_iso(), queue_id),
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # sources (registry + runtime health) / operational alerts
    # ------------------------------------------------------------------

    def register_source(
        self,
        name: str,
        adapter_type: str,
        *,
        base_url: str | None = None,
        active: bool = True,
        terms_checked: bool = False,
        requires_authentication: bool = False,
        crawl_frequency: str | None = None,
    ) -> int:
        """Insert or refresh a source's compliance facts.

        Mirrors :meth:`storage.Storage.register_source`: the registry
        owns the compliance columns and overwrites them; runtime health
        columns are never touched.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM procurement_sources WHERE name = %s", (name,)
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO procurement_sources "
                    "(name, adapter_type, base_url, active, terms_checked, "
                    "requires_authentication, crawl_frequency) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        name,
                        adapter_type,
                        base_url,
                        active,
                        terms_checked,
                        requires_authentication,
                        crawl_frequency,
                    ),
                )
                source_id = int(cur.fetchone()["id"])
            else:
                source_id = int(row["id"])
                cur.execute(
                    "UPDATE procurement_sources SET adapter_type = %s, "
                    "base_url = %s, active = %s, terms_checked = %s, "
                    "requires_authentication = %s, crawl_frequency = %s "
                    "WHERE id = %s",
                    (
                        adapter_type,
                        base_url,
                        active,
                        terms_checked,
                        requires_authentication,
                        crawl_frequency,
                        source_id,
                    ),
                )
        self.conn.commit()
        return source_id

    def get_source_by_id(self, source_id: int) -> dict[str, Any] | None:
        """Return one source row by id as a plain dict, or None."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM procurement_sources WHERE id = %s", (source_id,)
            )
            row = cur.fetchone()
        return _serialise(row)

    def set_robots_allowed(self, source_id: int, allowed: bool | None) -> None:
        """Record the latest robots.txt verdict for a source."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE procurement_sources SET robots_allowed = %s WHERE id = %s",
                (allowed, source_id),
            )
        self.conn.commit()

    def set_source_status(
        self,
        source_id: int,
        *,
        last_status: str,
        last_crawled_at: str,
        consecutive_failures: int,
    ) -> None:
        """Update a source's runtime health fields after a run."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE procurement_sources SET last_status = %s, "
                "last_crawled_at = %s, consecutive_failures = %s WHERE id = %s",
                (last_status, last_crawled_at, consecutive_failures, source_id),
            )
        self.conn.commit()

    def insert_operational_alert(
        self,
        source_id: int,
        alert_type: str,
        *,
        severity: str = "error",
        message: str,
        consecutive_failures: int = 0,
        channel: str = "internal",
    ) -> int:
        """Write one ``operational_alerts`` row and return its id."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operational_alerts "
                "(source_id, alert_type, severity, message, "
                "consecutive_failures, channel, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    source_id,
                    alert_type,
                    severity,
                    message,
                    consecutive_failures,
                    channel,
                    utc_now_iso(),
                ),
            )
            alert_id = int(cur.fetchone()["id"])
        self.conn.commit()
        return alert_id

    def has_open_operational_alert(self, source_id: int, alert_type: str) -> bool:
        """True when an unacknowledged alert of this type is open."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM operational_alerts WHERE source_id = %s "
                "AND alert_type = %s AND acknowledged_at IS NULL LIMIT 1",
                (source_id, alert_type),
            )
            row = cur.fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # opportunities
    # ------------------------------------------------------------------

    def upsert_opportunity(
        self,
        normalized: NormalizedOpportunity,
        source_id: int,
        raw_content: str | None = None,
    ) -> str:
        """Insert or update an opportunity, returning the upsert status.

        Identical contract to :meth:`storage.Storage.upsert_opportunity`:
        ``"created"`` / ``"unchanged"`` / ``"updated"``, change detection
        via :func:`storage_common.collect_changes`, one
        ``opportunity_updates`` row per changed field, and ``source_url``
        set only on insert.
        """
        now = utc_now_iso()
        new_hash = normalized.content_hash()
        existing = self._select_opportunity(source_id, normalized.external_id)

        if existing is None:
            values = {
                "source_id": source_id,
                "external_id": normalized.external_id,
                "reference_number": normalized.reference_number,
                "title": normalized.title,
                "organization": normalized.organization,
                "description": normalized.description,
                "source_url": normalized.source_url,
                "source_name": normalized.source_name,
                "published_at": db_value(normalized.published_at),
                "deadline": db_value(normalized.deadline),
                "location": normalized.location,
                "province": normalized.province,
                "category": normalized.category,
                "procurement_type": normalized.procurement_type,
                "content_hash": new_hash,
                "first_seen_at": now,
                "last_seen_at": now,
                "last_verified_at": now,
                "raw_content": raw_content,
            }
            placeholders = ", ".join(["%s"] * len(_INSERT_COLUMNS))
            columns = ", ".join(_INSERT_COLUMNS)
            with self.conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO opportunities ({columns}) VALUES ({placeholders}) "
                    "ON CONFLICT (source_id, external_id) DO NOTHING "
                    "RETURNING id",
                    [values[c] for c in _INSERT_COLUMNS],
                )
                row = cur.fetchone()
                if row is None:
                    # Lost a race with a concurrent writer: fall through
                    # to the update path with the row the other writer
                    # inserted.
                    self.conn.commit()
                    existing = self._select_opportunity(
                        source_id, normalized.external_id
                    )
                    assert existing is not None
                else:
                    opportunity_id = int(row["id"])
                    self._sync_documents(cur, opportunity_id, normalized.document_urls)
                    self.conn.commit()
                    return "created"

        opportunity_id = int(existing["id"])
        stored_docs = self._document_urls(opportunity_id)
        changes = collect_changes(existing, stored_docs, normalized)

        if not changes:
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE opportunities SET last_verified_at = %s WHERE id = %s",
                    (now, opportunity_id),
                )
            self.conn.commit()
            return "unchanged"

        with self.conn.cursor() as cur:
            for field_name, old_value, new_value in changes:
                update_type = (
                    "deadline_change" if field_name == "deadline" else "field_change"
                )
                cur.execute(
                    "INSERT INTO opportunity_updates "
                    "(opportunity_id, field, old_value, new_value, detected_at, update_type) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (opportunity_id, field_name, old_value, new_value, now, update_type),
                )
            column_changes = [c for c in changes if c[0] in _TRACKED_FIELDS]
            set_clauses = [f"{name} = %s" for name, _, _ in column_changes]
            params = [new_value for _, _, new_value in column_changes]
            set_clauses += [
                "content_hash = %s",
                "last_seen_at = %s",
                "last_verified_at = %s",
            ]
            cur.execute(
                f"UPDATE opportunities SET {', '.join(set_clauses)} WHERE id = %s",
                (*params, new_hash, now, now, opportunity_id),
            )
            self._sync_documents(cur, opportunity_id, normalized.document_urls)
        self.conn.commit()
        return "updated"

    def _select_opportunity(
        self, source_id: int, external_id: str
    ) -> dict[str, Any] | None:
        """Return the serialised row for (source_id, external_id), or None."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM opportunities WHERE source_id = %s AND external_id = %s",
                (source_id, external_id),
            )
            row = cur.fetchone()
        return _serialise(row)

    def _document_urls(self, opportunity_id: int) -> list[str]:
        """Return stored document URLs in insertion order."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT url FROM opportunity_documents WHERE opportunity_id = %s "
                "ORDER BY id",
                (opportunity_id,),
            )
            rows = cur.fetchall()
        return [row["url"] for row in rows]

    def _sync_documents(
        self, cur: Any, opportunity_id: int, urls: list[str]
    ) -> None:
        """Replace the stored document list for an opportunity."""
        cur.execute(
            "DELETE FROM opportunity_documents WHERE opportunity_id = %s",
            (opportunity_id,),
        )
        for url in urls:
            cur.execute(
                "INSERT INTO opportunity_documents (opportunity_id, url, filename) "
                "VALUES (%s, %s, %s)",
                (opportunity_id, url, filename_from_url(url)),
            )

    # ------------------------------------------------------------------
    # read helpers (parity with the SQLite backend)
    # ------------------------------------------------------------------

    def get_opportunity(self, opportunity_id: int) -> dict[str, Any] | None:
        """Return one opportunity row as a dict, or None."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM opportunities WHERE id = %s", (opportunity_id,))
            row = cur.fetchone()
        return _serialise(row)

    def get_documents(self, opportunity_id: int) -> list[dict[str, Any]]:
        """Return the documents attached to an opportunity."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM opportunity_documents WHERE opportunity_id = %s "
                "ORDER BY id",
                (opportunity_id,),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def get_updates(self, opportunity_id: int) -> list[dict[str, Any]]:
        """Return the update history of an opportunity, oldest first."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM opportunity_updates WHERE opportunity_id = %s "
                "ORDER BY id",
                (opportunity_id,),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def count_opportunities(self) -> int:
        """Total number of stored opportunities."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM opportunities")
            row = cur.fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    def log_fetch(
        self,
        source_id: int,
        started_at: str,
        finished_at: str | None = None,
        found: int = 0,
        created: int = 0,
        updated: int = 0,
        failed: int = 0,
        error_message: str | None = None,
    ) -> int:
        """Write one ``source_fetch_logs`` row and return its id."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO source_fetch_logs "
                "(source_id, started_at, finished_at, found, created, updated, "
                " failed, error_message) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (source_id, started_at, finished_at, found, created, updated,
                 failed, error_message),
            )
            log_id = int(cur.fetchone()["id"])
        self.conn.commit()
        return log_id


def _serialise(row: Any) -> dict[str, Any] | None:
    """Convert a dict_row result to storage form (ISO strings)."""
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            out[key] = value.astimezone(timezone.utc).isoformat()
        else:
            out[key] = value
    return out


def _redact_dsn(dsn: str) -> str:
    """Hide the password when logging a connection string."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        creds = creds.split(":", 1)[0] + ":***"
    return f"{scheme}://{creds}@{host}"
