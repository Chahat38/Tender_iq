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
from uuid import uuid4

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

    # ------------------------------------------------------------------
    # document pipeline (Stage 5)
    # ------------------------------------------------------------------

    def list_documents_needing_fetch(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return document rows never fetched (or never attempted)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, opportunity_id, url, filename FROM opportunity_documents "
                "WHERE storage_path IS NULL AND fetched_at IS NULL "
                "ORDER BY id LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def mark_document_fetched(
        self,
        document_id: int,
        *,
        content_hash: str,
        storage_path: str,
        mime_type: str | None,
        byte_size: int,
        fetched_at: str,
    ) -> None:
        """Record a successful download of one document."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE opportunity_documents SET content_hash = %s, "
                "storage_path = %s, mime_type = %s, byte_size = %s, "
                "fetched_at = %s WHERE id = %s",
                (content_hash, storage_path, mime_type, byte_size, fetched_at, document_id),
            )
        self.conn.commit()

    def mark_document_failed(self, document_id: int, *, fetched_at: str) -> None:
        """Mark one fetch attempt as done-but-empty (no stored content)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE opportunity_documents SET fetched_at = %s "
                "WHERE id = %s AND storage_path IS NULL",
                (fetched_at, document_id),
            )
        self.conn.commit()

    def mark_document_extracted(
        self, document_id: int, *, extracted_text: str | None, extracted_at: str
    ) -> None:
        """Store extracted text (or None when extraction found nothing)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE opportunity_documents SET extracted_text = %s, "
                "extracted_at = %s WHERE id = %s",
                (extracted_text, extracted_at, document_id),
            )
        self.conn.commit()

    def clear_failed_document_fetches(self) -> int:
        """Re-queue every attempted-but-failed document; returns count."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE opportunity_documents SET fetched_at = NULL "
                "WHERE fetched_at IS NOT NULL AND storage_path IS NULL"
            )
            count = cur.rowcount
        self.conn.commit()
        return int(count)

    # ------------------------------------------------------------------
    # AI analysis + relevance matching (Stage 6)
    # ------------------------------------------------------------------

    def list_opportunities_needing_ai(self, limit: int = 500) -> list[dict[str, Any]]:
        """Opportunity rows with no ``opportunity_ai`` analysis yet."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT o.id, o.title, o.description, o.organization, "
                "o.published_at, o.deadline, o.procurement_type, o.category, "
                "o.location, o.province "
                "FROM opportunities o LEFT JOIN opportunity_ai ai "
                "ON ai.opportunity_id = o.id "
                "WHERE ai.opportunity_id IS NULL ORDER BY o.id LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def upsert_opportunity_ai(
        self,
        opportunity_id: int,
        *,
        category: str | None,
        subcategory: str | None,
        keywords: str,
        procurement_type: str | None,
        requirements: str,
        date_conflicts: str,
        prompt_version: str,
        created_at: str,
    ) -> None:
        """Insert or replace the analysis of one opportunity."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_ai (opportunity_id, category, "
                "subcategory, keywords, procurement_type, requirements, "
                "date_conflicts, prompt_version, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (opportunity_id) DO UPDATE SET "
                "category = excluded.category, "
                "subcategory = excluded.subcategory, "
                "keywords = excluded.keywords, "
                "procurement_type = excluded.procurement_type, "
                "requirements = excluded.requirements, "
                "date_conflicts = excluded.date_conflicts, "
                "prompt_version = excluded.prompt_version, "
                "created_at = excluded.created_at",
                (
                    opportunity_id, category, subcategory, keywords,
                    procurement_type, requirements, date_conflicts,
                    prompt_version, created_at,
                ),
            )
        self.conn.commit()

    def get_opportunity_ai(self, opportunity_id: int) -> dict[str, Any] | None:
        """Return the analysis row for one opportunity, or None."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM opportunity_ai WHERE opportunity_id = %s",
                (opportunity_id,),
            )
            row = cur.fetchone()
        return _serialise(row) if row is not None else None

    def insert_analysis_run(
        self,
        task: str,
        *,
        status: str,
        provider: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        duration_ms: int | None = None,
        tokens: int | None = None,
        error_message: str | None = None,
        user_id: str | None = None,
        opportunity_id: int | None = None,
        created_at: str,
    ) -> int:
        """Journal one analysis/LLM run; returns the new run id."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO analysis_runs (task, provider, model, "
                "prompt_version, duration_ms, tokens, status, error_message, "
                "user_id, opportunity_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (task, provider, model, prompt_version, duration_ms, tokens,
                 status, error_message, user_id, opportunity_id, created_at),
            )
            run_id = cur.fetchone()["id"]
        self.conn.commit()
        return int(run_id)

    def list_analysis_runs(
        self, *, task: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent analysis runs, newest first (optionally one task)."""
        with self.conn.cursor() as cur:
            if task is None:
                cur.execute(
                    "SELECT * FROM analysis_runs ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
            else:
                cur.execute(
                    "SELECT * FROM analysis_runs WHERE task = %s "
                    "ORDER BY id DESC LIMIT %s",
                    (task, limit),
                )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def get_user_signals(self, user_id: str) -> dict[str, Any]:
        """Everything the relevance scorer needs about one user."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM profiles WHERE user_id = %s", (user_id,))
            profile_row = cur.fetchone()
            cur.execute(
                "SELECT name, keywords FROM company_capabilities "
                "WHERE user_id = %s ORDER BY name",
                (user_id,),
            )
            capabilities = [dict(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT province FROM target_regions WHERE user_id = %s "
                "ORDER BY province",
                (user_id,),
            )
            regions = [str(row["province"]) for row in cur.fetchall()]
            cur.execute(
                "SELECT category FROM target_categories WHERE user_id = %s "
                "ORDER BY category",
                (user_id,),
            )
            categories = [str(row["category"]) for row in cur.fetchall()]
        return {
            "profile": dict(profile_row) if profile_row is not None else None,
            "capabilities": capabilities,
            "regions": regions,
            "categories": categories,
        }

    def upsert_opportunity_match(
        self,
        opportunity_id: int,
        user_id: str,
        *,
        relevance_score: int,
        level: str,
        reasons: str,
        prompt_version: str,
        model: str,
        created_at: str,
        updated_at: str,
    ) -> None:
        """Insert or update one (opportunity, user) match."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_matches (id, opportunity_id, user_id, "
                "relevance_score, level, reasons, prompt_version, model, "
                "created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (opportunity_id, user_id) DO UPDATE SET "
                "relevance_score = excluded.relevance_score, "
                "level = excluded.level, reasons = excluded.reasons, "
                "prompt_version = excluded.prompt_version, "
                "model = excluded.model, updated_at = excluded.updated_at",
                (
                    uuid4().hex, opportunity_id, user_id, relevance_score,
                    level, reasons, prompt_version, model, created_at,
                    updated_at,
                ),
            )
        self.conn.commit()

    def list_matches(
        self, user_id: str, *, min_score: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Best matches first for one user."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM opportunity_matches "
                "WHERE user_id = %s AND relevance_score >= %s "
                "ORDER BY relevance_score DESC, updated_at DESC LIMIT %s",
                (user_id, min_score, limit),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def list_opportunities_for_matching(
        self, user_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Rows with no match yet, or changed since the last match."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT o.id, o.last_seen_at, m.updated_at AS matched_at "
                "FROM opportunities o "
                "LEFT JOIN opportunity_matches m "
                "ON m.opportunity_id = o.id AND m.user_id = %s "
                "WHERE m.id IS NULL OR m.updated_at < o.last_seen_at "
                "ORDER BY o.id LIMIT %s",
                (user_id, limit),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    # ------------------------------------------------------------------
    # user-facing alerts (Stage 7)
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[dict[str, Any]]:
        """Every profile (user + alert threshold), stable order."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, company_name, plan, alert_threshold "
                "FROM profiles ORDER BY user_id"
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def list_match_alert_candidates(
        self, user_id: str, *, min_score: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Matched opportunities with no ``new_match`` alert yet."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT o.id, o.title, m.relevance_score, m.level, m.reasons "
                "FROM opportunity_matches m "
                "JOIN opportunities o ON o.id = m.opportunity_id "
                "WHERE m.user_id = %s AND m.relevance_score >= %s "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM alerts a WHERE a.user_id = %s "
                "  AND a.opportunity_id = o.id AND a.type = 'new_match')"
                "ORDER BY m.relevance_score DESC, o.id LIMIT %s",
                (user_id, min_score, user_id, limit),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def list_watchlist_opportunities(
        self, user_id: str, *, min_score: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Deadlined opportunities the user watches (matched or saved)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT o.id, o.title, o.deadline FROM opportunities o "
                "WHERE o.deadline IS NOT NULL "
                "AND ("
                "  EXISTS (SELECT 1 FROM opportunity_matches m "
                "          WHERE m.opportunity_id = o.id AND m.user_id = %s "
                "          AND m.relevance_score >= %s)"
                "  OR EXISTS (SELECT 1 FROM saved_opportunities s "
                "             WHERE s.opportunity_id = o.id AND s.user_id = %s)"
                ") "
                "ORDER BY o.deadline, o.id LIMIT %s",
                (user_id, min_score, user_id, limit),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def list_update_alert_candidates(
        self,
        user_id: str,
        *,
        min_score: int,
        alert_type: str,
        field: str | None = None,
        exclude_field: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Watched opportunities with updates and no alert of this type."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT o.id, o.title, MAX(u.detected_at) AS latest_update "
                "FROM opportunities o "
                "JOIN opportunity_updates u ON u.opportunity_id = o.id "
                "WHERE u.field = COALESCE(%s, u.field) "
                "AND u.field <> COALESCE(%s, '~~none~~') "
                "AND ("
                "  EXISTS (SELECT 1 FROM opportunity_matches m "
                "          WHERE m.opportunity_id = o.id AND m.user_id = %s "
                "          AND m.relevance_score >= %s)"
                "  OR EXISTS (SELECT 1 FROM saved_opportunities s "
                "             WHERE s.opportunity_id = o.id AND s.user_id = %s)"
                ") "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM alerts a WHERE a.user_id = %s "
                "  AND a.opportunity_id = o.id AND a.type = %s) "
                "GROUP BY o.id, o.title ORDER BY o.id LIMIT %s",
                (field, exclude_field, user_id, min_score, user_id,
                 user_id, alert_type, limit),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def insert_alert(
        self,
        *,
        user_id: str,
        opportunity_id: int,
        type: str,
        title: str,
        body: str | None,
        channel: str = "in_app",
        created_at: str,
    ) -> int:
        """Insert one alert; returns 1 when created, 0 when it exists."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts (user_id, opportunity_id, type, title, "
                "body, channel, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (user_id, opportunity_id, type) DO NOTHING",
                (user_id, opportunity_id, type, title, body, channel, created_at),
            )
            count = cur.rowcount
        self.conn.commit()
        return int(count)

    def list_alerts(
        self, user_id: str, *, unread_only: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Newest alerts for one user."""
        with self.conn.cursor() as cur:
            if unread_only:
                cur.execute(
                    "SELECT * FROM alerts WHERE user_id = %s "
                    "AND read_at IS NULL "
                    "ORDER BY created_at DESC, id DESC LIMIT %s",
                    (user_id, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM alerts WHERE user_id = %s "
                    "ORDER BY created_at DESC, id DESC LIMIT %s",
                    (user_id, limit),
                )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    # ------------------------------------------------------------------
    # opportunities (read) - mirrors storage.py's API read paths so the
    # read-only HTTP API can serve either backend (SQLite default,
    # Postgres when DATABASE_URL is set).
    # ------------------------------------------------------------------

    def list_opportunities(
        self,
        limit: int = 20,
        offset: int = 0,
        source_id: int | None = None,
        deadline_before: Any = None,
        deadline_after: Any = None,
        location: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a page of opportunities, newest first, as plain dicts.

        Same semantics as :meth:`storage.Storage.list_opportunities`
        (Postgres flavour): ``id`` descending, ``limit``/``offset``
        clamped and validated, ``location`` is an ASCII case-insensitive
        substring match, ``deadline_before``/``deadline_after`` are ISO
        datetimes with the same open/closed window rules.
        """
        limit = int(limit)
        offset = int(offset)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        limit = min(limit, 500)

        clause, params = self._opportunity_filter_params(
            source_id=source_id,
            deadline_before=deadline_before,
            deadline_after=deadline_after,
            location=location,
        )
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT o.id, o.source_id, o.external_id, o.reference_number, "
                "o.title, o.organization, o.description, o.source_url, "
                "o.published_at, o.deadline, o.location, o.category, "
                "o.procurement_type, o.content_hash, o.first_seen_at, "
                "o.last_seen_at, o.last_verified_at "
                f"FROM opportunities o{clause} "
                "ORDER BY o.id DESC LIMIT %s OFFSET %s",
                (*params, limit, offset),
            )
            rows = cur.fetchall()
        return [_serialise(row) for row in rows]

    def count_opportunities(
        self,
        source_id: int | None = None,
        deadline_before: Any = None,
        deadline_after: Any = None,
        location: str | None = None,
    ) -> int:
        """Count opportunities matching the list_opportunities filters.

        Called without arguments this is the plain total row count (the
        health check's behavior keeps working).
        """
        clause, params = self._opportunity_filter_params(
            source_id=source_id,
            deadline_before=deadline_before,
            deadline_after=deadline_after,
            location=location,
        )
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) AS n FROM opportunities o{clause}", params
            )
            row = cur.fetchone()
        return int(row["n"])

    @staticmethod
    def _opportunity_filter_params(
        source_id: int | None,
        deadline_before: Any,
        deadline_after: Any,
        location: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Build the WHERE clause shared by the two read methods.

        ``deadline_after`` uses the :data:`NULL_DEADLINE_FILTER`
        sentinel for "no lower bound"; ``None`` hides undated rows -
        the same contract as storage.py so both backends filter
        identically from the API's point of view.
        """
        from storage_common import NULL_DEADLINE_FILTER

        where = []
        params: list[Any] = []
        if source_id is not None:
            where.append("o.source_id = %s")
            params.append(source_id)
        if location:
            where.append("o.location ILIKE %s")
            params.append(f"%{location}%")
        if deadline_before is not None:
            where.append("o.deadline IS NOT NULL AND o.deadline < %s")
            params.append(deadline_before)
        if deadline_after is not NULL_DEADLINE_FILTER:
            if deadline_after is None:
                where.append("o.deadline IS NOT NULL")
            else:
                where.append("(o.deadline IS NOT NULL AND o.deadline > %s)")
                params.append(deadline_after)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return clause, params

    def list_distinct_locations(self) -> list[str]:
        """Return every distinct non-empty location string, lowercased
        and sorted (SQLite backend semantics preserved)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT lower(location) AS location FROM opportunities "
                "WHERE location IS NOT NULL AND location <> '' "
                "ORDER BY location"
            )
            rows = cur.fetchall()
        return [str(row["location"]) for row in rows]

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
