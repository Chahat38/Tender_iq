"""SQLite storage for TenderScout: sources, opportunities and logs.

Separation rules enforced here:

* Source data lives in ``opportunities`` (+ documents/updates),
* AI interpretation lives in ``opportunity_ai``,
* run/AI telemetry lives in ``source_fetch_logs`` / ``analysis_runs``.
* ``source_url`` is written once on insert and never overwritten.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from models import NormalizedOpportunity
from storage_common import (
    NULL_DEADLINE_FILTER,
    TRACKED_FIELDS as _TRACKED_FIELDS,
    collect_changes,
    db_value as _db_value,
    filename_from_url as _filename_from_url,
    utc_now_iso,
)

if TYPE_CHECKING:
    from storage_postgres import PostgresStorage

__all__ = [
    "NULL_DEADLINE_FILTER",
    "Storage",
    "open_storage",
    "utc_now_iso",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    adapter_type TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    robots_allowed INTEGER,
    terms_checked INTEGER,
    requires_authentication INTEGER,
    crawl_frequency TEXT,
    last_crawled_at TEXT,
    base_url TEXT,
    last_status TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    reference_number TEXT,
    title TEXT,
    organization TEXT,
    description TEXT,
    source_url TEXT NOT NULL,
    source_name TEXT,
    published_at TEXT,
    deadline TEXT,
    location TEXT,
    province TEXT,
    category TEXT,
    sector TEXT,
    department TEXT,
    procurement_type TEXT,
    content_hash TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 1,
    duplicate_group_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 1,
    raw_content TEXT,
    UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS opportunity_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    filename TEXT,
    content_hash TEXT,
    storage_path TEXT,
    mime_type TEXT,
    byte_size INTEGER,
    fetched_at TEXT,
    extracted_text TEXT,
    extracted_at TEXT,
    UNIQUE (opportunity_id, url)
);

CREATE TABLE IF NOT EXISTS opportunity_requirements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    requirement TEXT,
    is_mandatory INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'source',
    created_at TEXT NOT NULL,
    UNIQUE (opportunity_id, label)
);

CREATE TABLE IF NOT EXISTS opportunity_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    detected_at TEXT NOT NULL,
    update_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_fetch_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    found INTEGER,
    created INTEGER,
    updated INTEGER,
    failed INTEGER,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS opportunity_ai (
    opportunity_id INTEGER PRIMARY KEY REFERENCES opportunities(id) ON DELETE CASCADE,
    category TEXT,
    subcategory TEXT,
    keywords TEXT,
    procurement_type TEXT,
    requirements TEXT,
    date_conflicts TEXT,
    prompt_version TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    prompt_version TEXT,
    duration_ms INTEGER,
    tokens INTEGER,
    status TEXT NOT NULL,
    error_message TEXT,
    user_id TEXT,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

-- Deduplication review queue: uncertain duplicate pairs are parked here
-- instead of being auto-merged (see collector/dedup.py).
CREATE TABLE IF NOT EXISTS dedup_review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id_a INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    opportunity_id_b INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK (opportunity_id_a < opportunity_id_b),
    UNIQUE (opportunity_id_a, opportunity_id_b)
);

-- Operational alerts raised by the collector (e.g. 3 consecutive failed
-- runs of a source). Separate from the per-user `alerts` table.
CREATE TABLE IF NOT EXISTS operational_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'error',
    message TEXT NOT NULL,
    consecutive_failures INTEGER,
    channel TEXT NOT NULL DEFAULT 'internal',
    created_at TEXT NOT NULL,
    notified_at TEXT,
    acknowledged_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    details TEXT
);

-- ===== user-owned tables (mirrors the Supabase contract for offline
-- ===== testing; production RLS lives in supabase/migrations) =====
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    company_name TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    alert_threshold INTEGER NOT NULL DEFAULT 60,
    contact_email TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_capabilities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    keywords TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS target_regions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    province TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, province)
);

CREATE TABLE IF NOT EXISTS target_categories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, category)
);

CREATE TABLE IF NOT EXISTS opportunity_matches (
    id TEXT PRIMARY KEY,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    relevance_score INTEGER NOT NULL CHECK (relevance_score BETWEEN 0 AND 100),
    level TEXT NOT NULL CHECK (level IN ('HIGH', 'MEDIUM', 'LOW')),
    reasons TEXT NOT NULL DEFAULT '[]',
    prompt_version TEXT,
    model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (opportunity_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_user_score
    ON opportunity_matches (user_id, relevance_score DESC);

CREATE TABLE IF NOT EXISTS saved_opportunities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, opportunity_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN
        ('new_match', 'deadline_approaching', 'opportunity_updated', 'corrigendum')),
    title TEXT NOT NULL,
    body TEXT,
    channel TEXT NOT NULL DEFAULT 'in_app',
    created_at TEXT NOT NULL,
    read_at TEXT,
    UNIQUE (user_id, opportunity_id, type)
);

CREATE TABLE IF NOT EXISTS rfps (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'in_review', 'reviewed', 'exported')),
    content TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rfp_sections (
    id TEXT PRIMARY KEY,
    rfp_id TEXT NOT NULL REFERENCES rfps(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    heading TEXT NOT NULL,
    content TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (rfp_id, heading)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    status TEXT NOT NULL DEFAULT 'active',
    provider TEXT,
    provider_ref TEXT,
    current_period_start TEXT,
    current_period_end TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_counters (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    period TEXT NOT NULL,
    feature TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, period, feature)
);

CREATE INDEX IF NOT EXISTS idx_opportunities_source_ref
    ON opportunities (source_id, reference_number);

CREATE INDEX IF NOT EXISTS idx_opportunities_deadline ON opportunities (deadline);
CREATE INDEX IF NOT EXISTS idx_opportunities_source ON opportunities (source_id);
"""

#: Hard cap on rows per list_opportunities() page (shared by the API).
#: Kept deliberately loose; the API layer uses a smaller default.
_API_RESULT_LIMIT_MAX = 500

#: Sentinel for list_opportunities(deadline_after=...): "no lower bound".
#: A unique object() so callers can never collide with a real datetime
#: or None; the API maps `deadline_after=False` (POST) / `deadline_after
#: =null` (GET) onto it. Defined (and imported) in storage_common so the
#: Postgres backend compares against the very same object.

#: Columns added after the first schema shipped (applied to pre-existing
#: databases with ALTER TABLE; new databases get them from _SCHEMA).
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "sources": {
        "base_url": "TEXT",
        "last_status": "TEXT",
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
    },
    "opportunities": {
        "source_name": "TEXT",
        "province": "TEXT",
        "sector": "TEXT",
        "department": "TEXT",
        "source_count": "INTEGER NOT NULL DEFAULT 1",
    },
    "opportunity_documents": {
        "content_hash": "TEXT",
        "storage_path": "TEXT",
        "mime_type": "TEXT",
        "byte_size": "INTEGER",
        "fetched_at": "TEXT",
        "extracted_text": "TEXT",
        "extracted_at": "TEXT",
    },
    "analysis_runs": {
        "error_message": "TEXT",
        "user_id": "TEXT",
        "opportunity_id": "INTEGER",
    },
}


class Storage:
    """SQLite-backed persistence for the TenderScout collector.

    A single SQLite file holds sources, opportunities (source data only),
    per-opportunity documents and update history, fetch logs, AI analysis
    results and AI run logs.
    """

    def __init__(
        self,
        db_path: str | Path = "tenderscout.db",
        *,
        check_same_thread: bool = True,
    ) -> None:
        """Open (and create if needed) the database at ``db_path``.

        ``check_same_thread=False`` lifts SQLite's same-thread check so
        one Storage can be shared across threads (the API layer does
        this) -- the caller then owns serializing access to
        :attr:`conn`.
        """
        self.db_path = Path(db_path)
        if str(self.db_path.parent) not in ("", ".") and not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.db_path), check_same_thread=check_same_thread
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        """Create all tables and indexes if they do not exist."""
        self.conn.executescript(_SCHEMA)
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self) -> None:
        """Add columns that shipped after the first schema version.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so
        databases created before a column existed get it here (guarded by
        PRAGMA table_info, hence idempotent).
        """
        for table, columns in _COLUMN_MIGRATIONS.items():
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue
            for name, declaration in columns.items():
                if name not in existing:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------

    def get_or_create_source(self, name: str, adapter_type: str) -> int:
        """Return the id of the named source row, creating it if needed."""
        row = self.conn.execute(
            "SELECT id FROM sources WHERE name = ?", (name,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO sources (name, adapter_type) VALUES (?, ?)",
            (name, adapter_type),
        )
        self.conn.commit()
        return int(cur.lastrowid)

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

        The registry owns ``base_url``/``active``/``terms_checked``/
        ``requires_authentication``/``crawl_frequency`` and overwrites
        them on every call; runtime columns (``last_crawled_at``,
        ``last_status``, ``consecutive_failures``, ``robots_allowed``)
        are never touched here.
        """
        row = self.conn.execute(
            "SELECT id FROM sources WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO sources (name, adapter_type, base_url, active, "
                "terms_checked, requires_authentication, crawl_frequency) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    adapter_type,
                    base_url,
                    int(active),
                    int(terms_checked),
                    int(requires_authentication),
                    crawl_frequency,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)
        source_id = int(row["id"])
        self.conn.execute(
            "UPDATE sources SET adapter_type = ?, base_url = ?, active = ?, "
            "terms_checked = ?, requires_authentication = ?, crawl_frequency = ? "
            "WHERE id = ?",
            (
                adapter_type,
                base_url,
                int(active),
                int(terms_checked),
                int(requires_authentication),
                crawl_frequency,
                source_id,
            ),
        )
        self.conn.commit()
        return source_id

    def get_source_by_id(self, source_id: int) -> dict[str, Any] | None:
        """Return one source row by id as a plain dict, or None."""
        row = self.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def set_robots_allowed(self, source_id: int, allowed: bool | None) -> None:
        """Record the latest robots.txt verdict for a source."""
        self.conn.execute(
            "UPDATE sources SET robots_allowed = ? WHERE id = ?",
            (None if allowed is None else int(allowed), source_id),
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
        self.conn.execute(
            "UPDATE sources SET last_status = ?, last_crawled_at = ?, "
            "consecutive_failures = ? WHERE id = ?",
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
        cur = self.conn.execute(
            "INSERT INTO operational_alerts "
            "(source_id, alert_type, severity, message, consecutive_failures, "
            "channel, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        self.conn.commit()
        return int(cur.lastrowid)

    def has_open_operational_alert(self, source_id: int, alert_type: str) -> bool:
        """True when an unacknowledged alert of this type is open."""
        row = self.conn.execute(
            "SELECT 1 FROM operational_alerts WHERE source_id = ? "
            "AND alert_type = ? AND acknowledged_at IS NULL LIMIT 1",
            (source_id, alert_type),
        ).fetchone()
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

        Returns ``"created"``, ``"unchanged"`` or ``"updated"``:

        * unknown (source_id, external_id) -> insert and return
          ``"created"``.
        * no changed fields -> bump ``last_verified_at`` only and return
          ``"unchanged"``.
        * otherwise -> write one ``opportunity_updates`` row per changed
          field (deadline changes use update_type ``"deadline_change"``),
          update the record, and return ``"updated"``.

        Change detection compares every normalized field (a superset of
        the fields covered by ``content_hash``), so changes in fields
        such as ``location`` are detected too. ``source_url`` is set only
        on insert and is NEVER overwritten.
        """
        now = utc_now_iso()
        new_hash = normalized.content_hash()
        existing = self.conn.execute(
            "SELECT * FROM opportunities WHERE source_id = ? AND external_id = ?",
            (source_id, normalized.external_id),
        ).fetchone()

        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO opportunities (
                    source_id, external_id, reference_number, title,
                    organization, description, source_url, source_name,
                    published_at, deadline, location, province, sector,
                    department, category, procurement_type, content_hash,
                    first_seen_at, last_seen_at, last_verified_at, raw_content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    normalized.external_id,
                    normalized.reference_number,
                    normalized.title,
                    normalized.organization,
                    normalized.description,
                    normalized.source_url,
                    normalized.source_name,
                    _db_value(normalized.published_at),
                    _db_value(normalized.deadline),
                    normalized.location,
                    normalized.province,
                    normalized.sector,
                    normalized.department,
                    normalized.category,
                    normalized.procurement_type,
                    new_hash,
                    now,
                    now,
                    now,
                    raw_content,
                ),
            )
            opportunity_id = int(cur.lastrowid)
            self._sync_documents(opportunity_id, normalized.document_urls)
            self.conn.commit()
            return "created"

        opportunity_id = int(existing["id"])
        changes = self._collect_changes(existing, opportunity_id, normalized)

        if not changes:
            self.conn.execute(
                "UPDATE opportunities SET last_verified_at = ? WHERE id = ?",
                (now, opportunity_id),
            )
            self.conn.commit()
            return "unchanged"

        for field_name, old_value, new_value in changes:
            update_type = (
                "deadline_change" if field_name == "deadline" else "field_change"
            )
            self._record_update(
                opportunity_id, field_name, old_value, new_value, update_type, now
            )

        # document_urls has no column on `opportunities` (documents live in
        # opportunity_documents); only tracked scalar fields go into the UPDATE.
        column_changes = [c for c in changes if c[0] in _TRACKED_FIELDS]
        set_clauses = [f"{name} = ?" for name, _, _ in column_changes]
        params = [new_value for _, _, new_value in column_changes]
        set_clauses += ["content_hash = ?", "last_seen_at = ?", "last_verified_at = ?"]
        self.conn.execute(
            f"""
            UPDATE opportunities
               SET {", ".join(set_clauses)}
             WHERE id = ?
            """,
            (*params, new_hash, now, now, opportunity_id),
        )
        self._sync_documents(opportunity_id, normalized.document_urls)
        self.conn.commit()
        return "updated"

    def _collect_changes(
        self,
        existing: sqlite3.Row,
        opportunity_id: int,
        normalized: NormalizedOpportunity,
    ) -> list[tuple[str, Any, Any]]:
        """Compare stored values against a normalized listing.

        Delegates to :func:`storage_common.collect_changes` so the
        SQLite and Postgres backends detect changes with identical
        rules. Returns ``(field, old_value, new_value)`` tuples for
        every tracked field and for the document list that differs.
        """
        return collect_changes(
            dict(existing),
            self._stored_document_urls(opportunity_id),
            normalized,
        )

    def _stored_document_urls(self, opportunity_id: int) -> list[str]:
        """Return stored document URLs in insertion order."""
        rows = self.conn.execute(
            "SELECT url FROM opportunity_documents WHERE opportunity_id = ? ORDER BY id",
            (opportunity_id,),
        ).fetchall()
        return [row["url"] for row in rows]

    def _sync_documents(self, opportunity_id: int, urls: list[str]) -> None:
        """Replace the stored document list for an opportunity."""
        self.conn.execute(
            "DELETE FROM opportunity_documents WHERE opportunity_id = ?",
            (opportunity_id,),
        )
        for url in urls:
            self.conn.execute(
                "INSERT INTO opportunity_documents (opportunity_id, url, filename) "
                "VALUES (?, ?, ?)",
                (opportunity_id, url, _filename_from_url(url)),
            )

    def _record_update(
        self,
        opportunity_id: int,
        field: str,
        old_value: Any,
        new_value: Any,
        update_type: str,
        detected_at: str,
    ) -> None:
        """Write one row into ``opportunity_updates``."""
        self.conn.execute(
            "INSERT INTO opportunity_updates "
            "(opportunity_id, field, old_value, new_value, detected_at, update_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (opportunity_id, field, old_value, new_value, detected_at, update_type),
        )

    # ------------------------------------------------------------------
    # read helpers (used by tests and later stages)
    # ------------------------------------------------------------------

    def get_opportunity(self, opportunity_id: int) -> dict[str, Any] | None:
        """Return one opportunity row as a dict, or None."""
        row = self.conn.execute(
            "SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_documents(self, opportunity_id: int) -> list[dict[str, Any]]:
        """Return the documents attached to an opportunity."""
        rows = self.conn.execute(
            "SELECT * FROM opportunity_documents WHERE opportunity_id = ? ORDER BY id",
            (opportunity_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_updates(self, opportunity_id: int) -> list[dict[str, Any]]:
        """Return the update history of an opportunity, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM opportunity_updates WHERE opportunity_id = ? ORDER BY id",
            (opportunity_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # document pipeline (Stage 5)
    # ------------------------------------------------------------------

    def list_documents_needing_fetch(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return document rows never fetched (or never attempted)."""
        rows = self.conn.execute(
            "SELECT id, opportunity_id, url, filename FROM opportunity_documents "
            "WHERE storage_path IS NULL AND fetched_at IS NULL "
            "ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

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
        self.conn.execute(
            "UPDATE opportunity_documents SET content_hash = ?, storage_path = ?, "
            "mime_type = ?, byte_size = ?, fetched_at = ? WHERE id = ?",
            (content_hash, storage_path, mime_type, byte_size, fetched_at, document_id),
        )
        self.conn.commit()

    def mark_document_failed(self, document_id: int, *, fetched_at: str) -> None:
        """Mark one fetch attempt as done-but-empty (no stored content).

        The row keeps ``storage_path IS NULL`` so failures are visible,
        but ``fetched_at`` set means it will not be auto-retried;
        ``clear_failed_document_fetches`` puts it back in the queue.
        """
        self.conn.execute(
            "UPDATE opportunity_documents SET fetched_at = ? "
            "WHERE id = ? AND storage_path IS NULL",
            (fetched_at, document_id),
        )
        self.conn.commit()

    def mark_document_extracted(
        self, document_id: int, *, extracted_text: str | None, extracted_at: str
    ) -> None:
        """Store extracted text (or None when extraction found nothing)."""
        self.conn.execute(
            "UPDATE opportunity_documents SET extracted_text = ?, extracted_at = ? "
            "WHERE id = ?",
            (extracted_text, extracted_at, document_id),
        )
        self.conn.commit()

    def clear_failed_document_fetches(self) -> int:
        """Re-queue every attempted-but-failed document; returns count."""
        cursor = self.conn.execute(
            "UPDATE opportunity_documents SET fetched_at = NULL "
            "WHERE fetched_at IS NOT NULL AND storage_path IS NULL"
        )
        self.conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # AI analysis + relevance matching (Stage 6)
    # ------------------------------------------------------------------

    def list_opportunities_needing_ai(self, limit: int = 500) -> list[dict[str, Any]]:
        """Opportunity rows with no ``opportunity_ai`` analysis yet."""
        rows = self.conn.execute(
            "SELECT o.id, o.title, o.description, o.organization, "
            "o.published_at, o.deadline, o.procurement_type, o.category, "
            "o.location, o.province "
            "FROM opportunities o LEFT JOIN opportunity_ai ai "
            "ON ai.opportunity_id = o.id "
            "WHERE ai.opportunity_id IS NULL ORDER BY o.id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

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
        self.conn.execute(
            "INSERT INTO opportunity_ai (opportunity_id, category, subcategory, "
            "keywords, procurement_type, requirements, date_conflicts, "
            "prompt_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (opportunity_id) DO UPDATE SET "
            "category = excluded.category, subcategory = excluded.subcategory, "
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
        row = self.conn.execute(
            "SELECT * FROM opportunity_ai WHERE opportunity_id = ?",
            (opportunity_id,),
        ).fetchone()
        return dict(row) if row is not None else None

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
        cursor = self.conn.execute(
            "INSERT INTO analysis_runs (task, provider, model, prompt_version, "
            "duration_ms, tokens, status, error_message, user_id, "
            "opportunity_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task, provider, model, prompt_version, duration_ms, tokens, status,
             error_message, user_id, opportunity_id, created_at),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_analysis_runs(
        self, *, task: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent analysis runs, newest first (optionally one task)."""
        if task is None:
            rows = self.conn.execute(
                "SELECT * FROM analysis_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM analysis_runs WHERE task = ? "
                "ORDER BY id DESC LIMIT ?",
                (task, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_user_signals(self, user_id: str) -> dict[str, Any]:
        """Everything the relevance scorer needs about one user."""
        profile_row = self.conn.execute(
            "SELECT * FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        capabilities = [
            dict(row)
            for row in self.conn.execute(
                "SELECT name, keywords FROM company_capabilities "
                "WHERE user_id = ? ORDER BY name",
                (user_id,),
            ).fetchall()
        ]
        regions = [
            str(row["province"])
            for row in self.conn.execute(
                "SELECT province FROM target_regions WHERE user_id = ? "
                "ORDER BY province",
                (user_id,),
            ).fetchall()
        ]
        categories = [
            str(row["category"])
            for row in self.conn.execute(
                "SELECT category FROM target_categories WHERE user_id = ? "
                "ORDER BY category",
                (user_id,),
            ).fetchall()
        ]
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
        self.conn.execute(
            "INSERT INTO opportunity_matches (id, opportunity_id, user_id, "
            "relevance_score, level, reasons, prompt_version, model, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (opportunity_id, user_id) DO UPDATE SET "
            "relevance_score = excluded.relevance_score, "
            "level = excluded.level, reasons = excluded.reasons, "
            "prompt_version = excluded.prompt_version, model = excluded.model, "
            "updated_at = excluded.updated_at",
            (
                uuid4().hex, opportunity_id, user_id, relevance_score, level,
                reasons, prompt_version, model, created_at, updated_at,
            ),
        )
        self.conn.commit()

    def list_matches(
        self, user_id: str, *, min_score: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Best matches first for one user."""
        rows = self.conn.execute(
            "SELECT * FROM opportunity_matches "
            "WHERE user_id = ? AND relevance_score >= ? "
            "ORDER BY relevance_score DESC, updated_at DESC LIMIT ?",
            (user_id, min_score, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_opportunities_for_matching(
        self, user_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Rows with no match yet, or changed since the last match."""
        rows = self.conn.execute(
            "SELECT o.id, o.last_seen_at, m.updated_at AS matched_at "
            "FROM opportunities o "
            "LEFT JOIN opportunity_matches m "
            "ON m.opportunity_id = o.id AND m.user_id = ? "
            "WHERE m.id IS NULL OR m.updated_at < o.last_seen_at "
            "ORDER BY o.id LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # user-facing alerts (Stage 7)
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[dict[str, Any]]:
        """Every profile (user + alert threshold), stable order."""
        rows = self.conn.execute(
            "SELECT user_id, company_name, plan, alert_threshold "
            "FROM profiles ORDER BY user_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_match_alert_candidates(
        self, user_id: str, *, min_score: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Matched opportunities with no ``new_match`` alert yet."""
        rows = self.conn.execute(
            "SELECT o.id, o.title, m.relevance_score, m.level, m.reasons "
            "FROM opportunity_matches m "
            "JOIN opportunities o ON o.id = m.opportunity_id "
            "WHERE m.user_id = ? AND m.relevance_score >= ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM alerts a WHERE a.user_id = ? "
            "  AND a.opportunity_id = o.id AND a.type = 'new_match')"
            "ORDER BY m.relevance_score DESC, o.id LIMIT ?",
            (user_id, min_score, user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_watchlist_opportunities(
        self, user_id: str, *, min_score: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Deadlined opportunities the user watches (matched or saved)."""
        rows = self.conn.execute(
            "SELECT o.id, o.title, o.deadline FROM opportunities o "
            "WHERE o.deadline IS NOT NULL "
            "AND ("
            "  EXISTS (SELECT 1 FROM opportunity_matches m "
            "          WHERE m.opportunity_id = o.id AND m.user_id = ? "
            "          AND m.relevance_score >= ?)"
            "  OR EXISTS (SELECT 1 FROM saved_opportunities s "
            "             WHERE s.opportunity_id = o.id AND s.user_id = ?)"
            ") "
            "ORDER BY o.deadline, o.id LIMIT ?",
            (user_id, min_score, user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

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
        """Watched opportunities with updates and no alert of this type.

        ``alert_type`` selects which alert kind is de-duplicated;
        ``field``/``exclude_field`` narrow which ``opportunity_updates``
        rows count as evidence (e.g. ``field="document_urls"`` for
        corrigenda).
        """
        rows = self.conn.execute(
            "SELECT o.id, o.title, MAX(u.detected_at) AS latest_update "
            "FROM opportunities o "
            "JOIN opportunity_updates u ON u.opportunity_id = o.id "
            "WHERE u.field = COALESCE(?, u.field) "
            "AND u.field <> COALESCE(?, '~~none~~') "
            "AND ("
            "  EXISTS (SELECT 1 FROM opportunity_matches m "
            "          WHERE m.opportunity_id = o.id AND m.user_id = ? "
            "          AND m.relevance_score >= ?)"
            "  OR EXISTS (SELECT 1 FROM saved_opportunities s "
            "             WHERE s.opportunity_id = o.id AND s.user_id = ?)"
            ") "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM alerts a WHERE a.user_id = ? "
            "  AND a.opportunity_id = o.id AND a.type = ?) "
            "GROUP BY o.id, o.title ORDER BY o.id LIMIT ?",
            (field, exclude_field, user_id, min_score, user_id,
             user_id, alert_type, limit),
        ).fetchall()
        return [dict(row) for row in rows]

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
        cursor = self.conn.execute(
            "INSERT INTO alerts (id, user_id, opportunity_id, type, title, "
            "body, channel, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, opportunity_id, type) DO NOTHING",
            (uuid4().hex, user_id, opportunity_id, type, title, body,
             channel, created_at),
        )
        self.conn.commit()
        return int(cursor.rowcount)

    def list_alerts(
        self, user_id: str, *, unread_only: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Newest alerts for one user."""
        if unread_only:
            rows = self.conn.execute(
                "SELECT * FROM alerts WHERE user_id = ? AND read_at IS NULL "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM alerts WHERE user_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]


    def count_opportunities(
        self,
        source_id: int | None = None,
        deadline_before: datetime | None = None,
        deadline_after: Any = NULL_DEADLINE_FILTER,
        location: str | None = None,
        category: str | None = None,
        organization: str | None = None,
        province: str | None = None,
        deadline_from: str | None = None,
        deadline_to: str | None = None,
        primary_only: bool = True,
        procurement_type: str | None = None,
        sector: str | None = None,
        department: str | None = None,
        exclude_expired: datetime | None = None,
    ) -> int:
        """Count opportunities matching the list_opportunities filters.

        Called without arguments this is the plain total row count
        (the pre-Stage-6 behavior and call sites keep working). When
        ``primary_only`` is True (default) duplicates are excluded, so
        the count matches what the list endpoint shows.
        """
        clause, params = self._opportunity_filter_clause(
            source_id=source_id,
            deadline_before=deadline_before,
            deadline_after=deadline_after,
            location=location,
            category=category,
            organization=organization,
            province=province,
            deadline_from=deadline_from,
            deadline_to=deadline_to,
            primary_only=primary_only,
            procurement_type=procurement_type,
            sector=sector,
            department=department,
            exclude_expired=exclude_expired,
        )
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM opportunities{clause}", params
        ).fetchone()
        return int(row["n"])

    def prune_expired(self, now: datetime | None = None) -> int:
        """Delete opportunities whose deadline has passed.

        Deadlines are stored as ISO strings (usually with a UTC offset
        from the adapters); naive values are treated as Karachi local
        time (UTC+5) so a date-only deadline counts as the close of that
        local day. Rows without a deadline are kept. Dependent rows are
        removed by ``ON DELETE CASCADE``; callers should rebuild the
        FTS5 index afterwards.
        """
        now = now or datetime.now(timezone.utc)
        expired_ids: list[int] = []
        for row in self.conn.execute(
            "SELECT id, deadline FROM opportunities WHERE deadline IS NOT NULL"
        ):
            deadline = row["deadline"]
            try:
                parsed = datetime.fromisoformat(str(deadline))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=5)))
            if parsed.astimezone(timezone.utc) <= now:
                expired_ids.append(row["id"])
        for chunk_start in range(0, len(expired_ids), 500):
            chunk = expired_ids[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            self.conn.execute(
                f"DELETE FROM opportunities WHERE id IN ({placeholders})", chunk
            )
        if expired_ids:
            self.conn.commit()
        return len(expired_ids)

    def count_sources(self) -> int:
        """Return the total number of registered sources."""
        row = self.conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # read helpers for the API layer (Stage 6)
    # ------------------------------------------------------------------

    def list_opportunities(
        self,
        limit: int = 20,
        offset: int = 0,
        source_id: int | None = None,
        deadline_before: datetime | None = None,
        deadline_after: Any = NULL_DEADLINE_FILTER,
        location: str | None = None,
        category: str | None = None,
        organization: str | None = None,
        province: str | None = None,
        deadline_from: str | None = None,
        deadline_to: str | None = None,
        primary_only: bool = True,
        procurement_type: str | None = None,
        sector: str | None = None,
        department: str | None = None,
        sort: str = "newest",
        exclude_expired: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return a page of opportunities as plain dicts.

        By default only primary rows are returned (``primary_only``) -
        the same notice found on several sources shows once, under the
        source that reported it first - and rows are ordered newest
        first (``sort`` ``"newest"``). ``sort="closing"`` orders by
        earliest deadline (undated rows last, ties newest first).
        ``limit`` is clamped to [0, :data:`_API_RESULT_LIMIT_MAX`] and
        negative ``limit``/``offset`` raise ``ValueError`` (the same
        contract as :meth:`search.SearchIndex.search`).

        Filtering:

        * ``source_id`` restricts to one source,
        * ``location`` keeps rows whose location, organization, title or
          description contains the given text, case-insensitively (a
          loose match - scraped location strings are free text, and some
          sources state the city only in the title),
        * ``category`` / ``province`` / ``sector`` / ``department`` keep
          rows whose stored value matches case-insensitively (exact
          value after lower()),
        * ``procurement_type`` keeps rows whose stored procurement type
          matches case-insensitively (use
          :meth:`list_distinct_procurement_types` for valid values),
        * ``organization`` keeps rows whose organization contains the
          given text, case-insensitively (loose, like ``location``),
        * ``deadline_before`` / ``deadline_after`` keep rows whose
          deadline (an ISO-8601 string, normalized to UTC) is
          strictly inside the window. By default the window's end is
          open (``deadline_after`` defaults to ``NULL_DEADLINE_FILTER``,
          the sentinel meaning "no lower bound"), so tenders without a
          deadline are NOT silently hidden. Pass a real datetime to
          exclude undated rows; pass the ``NO_DEADLINE`` sentinel to
          hide them while keeping an open lower bound,
        * ``deadline_from`` / ``deadline_to`` are the UI-facing
          date-only window (``YYYY-MM-DD``), inclusive on both ends and
          compared against the first ten characters of the stored ISO
          deadline - the deadline's Pakistan-local date. When either
          bound is given, rows without a deadline drop out (the range
          is meaningless for them).
        """
        limit = int(limit)
        offset = int(offset)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        limit = min(limit, _API_RESULT_LIMIT_MAX)

        clause, params = self._opportunity_filter_clause(
            source_id=source_id,
            deadline_before=deadline_before,
            deadline_after=deadline_after,
            location=location,
            category=category,
            organization=organization,
            province=province,
            deadline_from=deadline_from,
            deadline_to=deadline_to,
            primary_only=primary_only,
            procurement_type=procurement_type,
            sector=sector,
            department=department,
            exclude_expired=exclude_expired,
        )
        if sort == "closing":
            # Earliest deadline first; undated rows last; ties newest
            # first so recently discovered tenders surface within a day.
            order_by = "(deadline IS NULL) ASC, deadline ASC, id DESC"
        else:
            order_by = "id DESC"
        rows = self.conn.execute(
            f"SELECT * FROM opportunities{clause} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _opportunity_filter_clause(
        source_id: int | None,
        deadline_before: datetime | None,
        deadline_after: Any,
        location: str | None = None,
        category: str | None = None,
        organization: str | None = None,
        province: str | None = None,
        deadline_from: str | None = None,
        deadline_to: str | None = None,
        primary_only: bool = True,
        procurement_type: str | None = None,
        sector: str | None = None,
        department: str | None = None,
        exclude_expired: datetime | None = None,
    ) -> tuple[str, list[Any]]:
        """Build the WHERE clause shared by list_opportunities and
        count_opportunities so both always filter identically.

        ``location``/``organization`` use ``LIKE`` (ASCII
        case-insensitive) around the given substring; they are bounded
        to keep the pattern honest. ``category``/``province`` are
        facet-style exact matches after ``lower()`` (their facet values
        come from :meth:`list_distinct_categories` /
        :meth:`list_distinct_provinces`, which are already lowercased).
        ``deadline_after`` uses the :data:`NULL_DEADLINE_FILTER` sentinel
        for "no lower bound"; ``None`` hides rows without a deadline.
        ``exclude_expired`` keeps rows without a deadline and rows whose
        deadline is strictly later than the given instant (the default
        "live tenders only" view; it leaves deadline semantics
        untouched and combines with any explicit window).
        """
        where = []
        params: list[Any] = []
        if source_id is not None:
            where.append("source_id = ?")
            params.append(source_id)
        if deadline_from:
            # Date-only inclusive lower bound on the deadline's local date.
            where.append("deadline IS NOT NULL AND substr(deadline, 1, 10) >= ?")
            params.append(deadline_from)
        if deadline_to:
            where.append("deadline IS NOT NULL AND substr(deadline, 1, 10) <= ?")
            params.append(deadline_to)
        if location:
            # Scraped locations are free text and several portals leave the
            # column empty (federal/national sources). Matching the stated
            # text as well keeps the city filter useful: a tender that
            # names "Karachi" in its title or organization is a Karachi
            # tender even when the source has no dedicated location field.
            pattern = f"%{location}%"
            where.append(
                "(location LIKE ? OR organization LIKE ? OR title LIKE ? "
                "OR description LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern])
        if category:
            where.append("category IS NOT NULL AND lower(category) = ?")
            params.append(category.lower())
        if organization:
            where.append("organization LIKE ?")
            params.append(f"%{organization}%")
        if province:
            where.append("province IS NOT NULL AND lower(province) = ?")
            params.append(province.lower())
        if deadline_before is not None:
            # Deadlines are stored as ISO strings with a UTC offset; the
            # cutoff is normalized to UTC on both sides (naive values are
            # Karachi local time, mirroring prune_expired) so an instant
            # comparison is exact regardless of the stored offset.
            where.append(
                "deadline IS NOT NULL AND datetime("
                "deadline, CASE WHEN instr(deadline, '+') > 0 OR "
                "instr(deadline, 'Z') > 0 THEN '0 hours' ELSE '-5 hours' END"
                ") < datetime(?)"
            )
            params.append(_db_value(deadline_before))
        if deadline_after is not NULL_DEADLINE_FILTER:
            if deadline_after is None:
                # NO_DEADLINE sentinel: hide undated rows, no lower bound.
                where.append("deadline IS NOT NULL")
            else:
                where.append(
                    "(deadline IS NOT NULL AND datetime("
                    "deadline, CASE WHEN instr(deadline, '+') > 0 OR "
                    "instr(deadline, 'Z') > 0 THEN '0 hours' ELSE '-5 hours' END"
                    ") > datetime(?))"
                )
                params.append(_db_value(deadline_after))
        if exclude_expired is not None:
            # Live view: undated rows stay, past deadlines go. Both sides
            # normalized to UTC so a deadline that already passed earlier
            # today (in its own offset) is excluded right away.
            where.append(
                "(deadline IS NULL OR datetime("
                "deadline, CASE WHEN instr(deadline, '+') > 0 OR "
                "instr(deadline, 'Z') > 0 THEN '0 hours' ELSE '-5 hours' END"
                ") > datetime(?))"
            )
            params.append(_db_value(exclude_expired))
        if primary_only:
            # One listed tender per notice even when the collector
            # found the same notice on several sources (the primary row
            # is the source that saw it first).
            where.append("is_primary = 1")
        if procurement_type:
            where.append(
                "procurement_type IS NOT NULL AND "
                "lower(procurement_type) = lower(?)"
            )
            params.append(procurement_type)
        if sector:
            where.append(
                "sector IS NOT NULL AND "
                "lower(sector) = lower(?)"
            )
            params.append(sector)
        if department:
            where.append(
                "department IS NOT NULL AND "
                "lower(department) = lower(?)"
            )
            params.append(department)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return clause, params

    def list_distinct_locations(self) -> list[str]:
        """Return every distinct non-empty location string, lowercased
        and sorted.

        Fed directly into the API's city facet; values are the scraped
        source text collapsed case-insensitively (``Karachi,
        Pakistan`` and ``karachi, pakistan`` are the same city), so the
        frontend treats them as opaque labels, never parses them.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT lower(location) AS location FROM opportunities "
            "WHERE location IS NOT NULL AND location <> '' "
            "ORDER BY location"
        ).fetchall()
        return [str(row["location"]) for row in rows]

    def list_distinct_categories(self) -> list[str]:
        """Every distinct non-empty category, lowercased and sorted.

        Fed directly into the API's category facet; like the location
        facet the values are opaque scraped labels collapsed
        case-insensitively (never parsed by the frontend).
        """
        rows = self.conn.execute(
            "SELECT DISTINCT lower(category) AS category FROM opportunities "
            "WHERE category IS NOT NULL AND category <> '' "
            "ORDER BY category"
        ).fetchall()
        return [str(row["category"]) for row in rows]

    def list_distinct_provinces(self) -> list[str]:
        """Every distinct non-empty province, lowercased and sorted.

        Province is written by each adapter (``Sindh``, ``Punjab``, ...)
        so the facet is a small controlled vocabulary; the lowercase
        form matches the ``province`` filter's exact comparison.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT lower(province) AS province FROM opportunities "
            "WHERE province IS NOT NULL AND province <> '' "
            "ORDER BY province"
        ).fetchall()
        return [str(row["province"]) for row in rows]

    def list_distinct_sectors(self) -> list[str]:
        """Every distinct non-empty sector, lowercased and sorted.

        Adapters that know a tender's sector (IT & Telecom, Construction,
        ...) write it; the lowercase form matches the sector filter.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT lower(sector) AS sector FROM opportunities "
            "WHERE sector IS NOT NULL AND sector <> '' "
            "ORDER BY sector"
        ).fetchall()
        return [str(row["sector"]) for row in rows]

    def list_distinct_departments(self) -> list[str]:
        """Every distinct non-empty department, lowercased and sorted.

        Adapters that know the procuring department write it; the
        lowercase form matches the department filter.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT lower(department) AS department FROM opportunities "
            "WHERE department IS NOT NULL AND department <> '' "
            "ORDER BY department"
        ).fetchall()
        return [str(row["department"]) for row in rows]

    def iter_taxonomy_rows(self) -> list[dict[str, Any]]:
        """Return the columns needed by the taxonomy backfill.

        A dedicated projection (not ``SELECT *``) keeps the backfill cheap
        on large tables and independent of unrelated schema changes.
        """
        rows = self.conn.execute(
            "SELECT id, title, organization, description, location, province, "
            "sector, department, category FROM opportunities"
        ).fetchall()
        return [dict(row) for row in rows]

    def set_taxonomy(self, opportunity_id: int, values: dict[str, str]) -> None:
        """Fill taxonomy columns of one stored opportunity.

        Only known column names are accepted; the backfill builds them
        from :mod:`taxonomy`, never from user input.
        """
        allowed = ("location", "province", "sector", "department", "category")
        updates = [(key, values[key]) for key in allowed if values.get(key)]
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key, _ in updates)
        params: list[Any] = [value for _, value in updates]
        params.append(opportunity_id)
        self.conn.execute(
            f"UPDATE opportunities SET {assignments} WHERE id = ?", params
        )
        self.conn.commit()

    def list_sources(self) -> list[dict[str, Any]]:
        """Return every source row plus its tender count, last success and
        the latest run's error.

        ``last_error`` is the error of the **most recent** fetch log row,
        so a source that failed once and later recovered reports ``None``
        instead of a stale message.
        """
        rows = self.conn.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM opportunities o "
            " WHERE o.source_id = s.id) AS opportunity_count, "
            "(SELECT l.finished_at FROM source_fetch_logs l "
            " WHERE l.source_id = s.id AND l.failed = 0 "
            " ORDER BY l.id DESC LIMIT 1) AS last_success_at, "
            "(SELECT l.error_message FROM source_fetch_logs l "
            " WHERE l.source_id = s.id "
            " ORDER BY l.id DESC LIMIT 1) AS last_error "
            "FROM sources s ORDER BY s.id"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_distinct_procurement_types(self) -> list[str]:
        """Every distinct non-empty procurement type, most common first.

        Values are the raw scraped labels (``Works``, ``Tender Notice``,
        ...) so the frontend treats them as opaque facet labels; the
        ``procurement_type`` filter compares case-insensitively.
        """
        rows = self.conn.execute(
            "SELECT procurement_type AS label, COUNT(*) AS n "
            "FROM opportunities "
            "WHERE procurement_type IS NOT NULL AND procurement_type <> '' "
            "GROUP BY lower(procurement_type) "
            "ORDER BY n DESC, label"
        ).fetchall()
        return [str(row["label"]) for row in rows]

    def list_duplicates(self, opportunity_id: int) -> list[dict[str, Any]]:
        """Sibling rows in the same duplicate group, newest first.

        The opportunity itself is excluded; rows not in a group (or an
        unknown id) answer an empty list. Each row is the light
        projection used by the UI's "also listed on" panel.
        """
        group_row = self.conn.execute(
            "SELECT duplicate_group_id FROM opportunities WHERE id = ?",
            (opportunity_id,),
        ).fetchone()
        if group_row is None or group_row["duplicate_group_id"] is None:
            return []
        rows = self.conn.execute(
            "SELECT id, source_id, source_name, title, organization, "
            "deadline, source_url, is_primary FROM opportunities "
            "WHERE duplicate_group_id = ? AND id <> ? "
            "ORDER BY first_seen_at, id",
            (group_row["duplicate_group_id"], opportunity_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_new_tenders(
        self, days: int, limit: int = 50, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Primary rows newly published within the last ``days`` days.

        Ordered by publication date, newest first. ``published_at`` is
        a stored ISO-8601 string so string comparison against the
        computed cutoff works; undated rows are ignored.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=days)).isoformat()
        limit = min(max(int(limit), 0), _API_RESULT_LIMIT_MAX)
        rows = self.conn.execute(
            "SELECT * FROM opportunities "
            "WHERE is_primary = 1 AND published_at IS NOT NULL "
            "AND published_at >= ? "
            "ORDER BY published_at DESC, id DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_closing_soon(
        self, days: int, limit: int = 50, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Primary rows whose deadline falls within the next ``days`` days.

        Ordered by deadline, soonest first (undated rows are never
        "closing soon"). Rows whose deadline already passed drop out.
        """
        now = now or datetime.now(timezone.utc)
        today = now.isoformat()
        horizon = (now + timedelta(days=days)).isoformat()
        limit = min(max(int(limit), 0), _API_RESULT_LIMIT_MAX)
        rows = self.conn.execute(
            "SELECT * FROM opportunities "
            "WHERE is_primary = 1 AND deadline IS NOT NULL "
            "AND deadline >= ? AND deadline <= ? "
            "ORDER BY deadline ASC, id DESC LIMIT ?",
            (today, horizon, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # deduplication
    # ------------------------------------------------------------------

    #: Columns dedup needs to score a candidate pair.
    _DEDUP_PROJECTION = (
        "id, source_id, reference_number, title, organization, deadline, "
        "first_seen_at, duplicate_group_id, is_primary, source_count"
    )

    def list_ungrouped_opportunities(self) -> list[dict[str, Any]]:
        """Return dedup-relevant columns for rows not yet in a group.

        Projection only: raw content and document lists stay out of the
        dedup pass (they are irrelevant to matching and expensive).
        """
        rows = self.conn.execute(
            f"SELECT {self._DEDUP_PROJECTION} FROM opportunities "
            "WHERE duplicate_group_id IS NULL ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_opportunities_in_group(self, group_id: str) -> list[dict[str, Any]]:
        """Return every row belonging to a duplicate group, oldest first."""
        rows = self.conn.execute(
            f"SELECT {self._DEDUP_PROJECTION} FROM opportunities "
            "WHERE duplicate_group_id = ? ORDER BY first_seen_at, id",
            (group_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_duplicate_state(
        self,
        opportunity_id: int,
        *,
        duplicate_group_id: str | None,
        is_primary: bool,
        source_count: int,
    ) -> None:
        """Write the three dedup columns for one row in a single update."""
        self.conn.execute(
            "UPDATE opportunities SET duplicate_group_id = ?, is_primary = ?, "
            "source_count = ? WHERE id = ?",
            (duplicate_group_id, int(is_primary), source_count, opportunity_id),
        )
        self.conn.commit()

    def enqueue_dedup_review(
        self,
        opportunity_id_a: int,
        opportunity_id_b: int,
        score: float,
        reason: str | None = None,
    ) -> int:
        """Queue one candidate pair for human review; 0 if already queued.

        The caller guarantees ``opportunity_id_a < opportunity_id_b``
        (the table enforces it too); an already-known pair is left
        untouched so repeated scans never duplicate queue rows.
        """
        low, high = sorted((opportunity_id_a, opportunity_id_b))
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO dedup_review_queue "
            "(opportunity_id_a, opportunity_id_b, score, reason, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (low, high, score, reason, utc_now_iso()),
        )
        self.conn.commit()
        return int(cur.rowcount)

    def list_dedup_reviews(self, status: str = "pending") -> list[dict[str, Any]]:
        """Return dedup review rows for one status, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM dedup_review_queue WHERE status = ? "
            "ORDER BY created_at, id",
            (status,),
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_dedup_review(
        self, queue_id: int, status: str, *, resolved_at: str | None = None
    ) -> None:
        """Mark a review row resolved (``confirmed`` / ``rejected``)."""
        self.conn.execute(
            "UPDATE dedup_review_queue SET status = ?, resolved_at = ? WHERE id = ?",
            (status, resolved_at or utc_now_iso(), queue_id),
        )
        self.conn.commit()

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
        cur = self.conn.execute(
            "INSERT INTO source_fetch_logs "
            "(source_id, started_at, finished_at, found, created, updated, failed, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                started_at,
                finished_at,
                found,
                created,
                updated,
                failed,
                error_message,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)


def open_storage(
    db_path: str | Path | None = None,
    *,
    dsn: str | None = None,
) -> "Storage | PostgresStorage":
    """Open the storage backend selected by the environment.

    When ``dsn`` is given, or the ``DATABASE_URL`` environment variable
    is set, the collector writes to Postgres/Supabase through
    :class:`storage_postgres.PostgresStorage`. Otherwise the SQLite
    backend (:class:`Storage`) is used, which remains the default for
    tests, offline runs and small self-hosted installs.

    ``db_path`` is ignored when a DSN is in effect (kept so call sites
    can pass their CLI value unconditionally).
    """
    dsn = dsn or os.environ.get("DATABASE_URL")
    if dsn:
        from storage_postgres import PostgresStorage

        return PostgresStorage(dsn)
    return Storage(db_path or os.environ.get("TENDERSCOUT_DB_PATH", "tenderscout.db"))
