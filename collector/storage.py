"""SQLite storage for TenderScout: sources, opportunities and logs.

Separation rules enforced here:

* Source data lives in ``opportunities`` (+ documents/updates),
* AI interpretation lives in ``opportunity_ai``,
* run/AI telemetry lives in ``source_fetch_logs`` / ``analysis_runs``.
* ``source_url`` is written once on insert and never overwritten.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import NormalizedOpportunity

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
    last_crawled_at TEXT
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
    published_at TEXT,
    deadline TEXT,
    location TEXT,
    category TEXT,
    procurement_type TEXT,
    content_hash TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 1,
    duplicate_group_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    raw_content TEXT,
    UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS opportunity_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    filename TEXT,
    UNIQUE (opportunity_id, url)
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
    status TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opportunities_deadline ON opportunities (deadline);
CREATE INDEX IF NOT EXISTS idx_opportunities_source ON opportunities (source_id);
"""

#: Hard cap on rows per list_opportunities() page (shared by the API).
#: Kept deliberately loose; the API layer uses a smaller default.
_API_RESULT_LIMIT_MAX = 500

#: Sentinel for list_opportunities(deadline_after=...): "no lower bound".
#: A unique object() so callers can never collide with a real datetime
#: or None; the API maps `deadline_after=False` (POST) / `deadline_after
#: =null` (GET) onto it.
NULL_DEADLINE_FILTER = object()

#: Opportunity fields compared for change detection (source_url and the
#: identity fields are deliberately excluded -- source_url is never
#: overwritten, and GET-style date-window filters never drop undated
#: rows unless asked to).
_TRACKED_FIELDS = (
    "title",
    "organization",
    "description",
    "published_at",
    "deadline",
    "location",
    "category",
    "procurement_type",
    "reference_number",
)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _db_value(value: Any) -> Any:
    """Convert a normalized field value to its SQLite storage form."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return value


def _filename_from_url(url: str) -> str | None:
    """Best-effort filename extracted from a document URL."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name or None


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
        self.conn.commit()

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
                    organization, description, source_url, published_at,
                    deadline, location, category, procurement_type,
                    content_hash, first_seen_at, last_seen_at,
                    last_verified_at, raw_content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    normalized.external_id,
                    normalized.reference_number,
                    normalized.title,
                    normalized.organization,
                    normalized.description,
                    normalized.source_url,
                    _db_value(normalized.published_at),
                    _db_value(normalized.deadline),
                    normalized.location,
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

        Returns ``(field, old_value, new_value)`` tuples for every
        tracked field and for the document list that differs.
        """
        changes: list[tuple[str, Any, Any]] = []
        for field_name in _TRACKED_FIELDS:
            new_value = _db_value(getattr(normalized, field_name))
            old_value = existing[field_name]
            if old_value != new_value:
                changes.append((field_name, old_value, new_value))
        stored_docs = self._stored_document_urls(opportunity_id)
        if stored_docs != normalized.document_urls:
            changes.append(
                (
                    "document_urls",
                    json.dumps(stored_docs),
                    json.dumps(normalized.document_urls),
                )
            )
        return changes

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

    def count_opportunities(
        self,
        source_id: int | None = None,
        deadline_before: datetime | None = None,
        deadline_after: Any = NULL_DEADLINE_FILTER,
    ) -> int:
        """Count opportunities matching the list_opportunities filters.

        Called without arguments this is the plain total row count
        (the pre-Stage-6 behavior and call sites keep working).
        """
        clause, params = self._opportunity_filter_clause(
            source_id=source_id,
            deadline_before=deadline_before,
            deadline_after=deadline_after,
        )
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM opportunities{clause}", params
        ).fetchone()
        return int(row["n"])

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
    ) -> list[dict[str, Any]]:
        """Return a page of opportunities, newest first, as plain dicts.

        Ordered by ``id`` descending (insertion order = crawl order, so
        the newest rows are the most recently discovered tenders).
        ``limit`` is clamped to [0, :data:`_API_RESULT_LIMIT_MAX`] and
        negative ``limit``/``offset`` raise ``ValueError`` (the same
        contract as :meth:`search.SearchIndex.search`).

        Filtering:

        * ``source_id`` restricts to one source,
        * ``deadline_before`` / ``deadline_after`` keep rows whose
          deadline (an ISO-8601 string, so string comparison works) is
          strictly inside the window. By default the window's end is
          open (``deadline_after`` defaults to ``NULL_DEADLINE_FILTER``,
          the sentinel meaning "no lower bound"), so tenders without a
          deadline are NOT silently hidden. Pass a real datetime to
          exclude undated rows; pass the ``NO_DEADLINE`` sentinel to
          hide them while keeping an open lower bound.
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
        )
        rows = self.conn.execute(
            f"SELECT * FROM opportunities{clause} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _opportunity_filter_clause(
        source_id: int | None,
        deadline_before: datetime | None,
        deadline_after: Any,
    ) -> tuple[str, list[Any]]:
        """Build the WHERE clause shared by list_opportunities and
        count_opportunities so both always filter identically.

        ``deadline_after`` uses the :data:`NULL_DEADLINE_FILTER` sentinel
        for "no lower bound"; ``None`` hides rows without a deadline.
        """
        where = []
        params: list[Any] = []
        if source_id is not None:
            where.append("source_id = ?")
            params.append(source_id)
        if deadline_before is not None:
            where.append("deadline IS NOT NULL AND deadline < ?")
            params.append(_db_value(deadline_before))
        if deadline_after is not NULL_DEADLINE_FILTER:
            if deadline_after is None:
                # NO_DEADLINE sentinel: hide undated rows, no lower bound.
                where.append("deadline IS NOT NULL")
            else:
                where.append(
                    "(deadline IS NOT NULL AND deadline > ?)"
                )
                params.append(_db_value(deadline_after))
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return clause, params

    def list_sources(self) -> list[dict[str, Any]]:
        """Return every registered source row as a plain dict."""
        rows = self.conn.execute(
            "SELECT * FROM sources ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

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
