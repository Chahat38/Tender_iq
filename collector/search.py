"""FTS5 full-text search over the collected opportunities.

Stage 5: a read-only search layer on top of the existing SQLite
database. The index is an FTS5 external-content table mirroring the
``opportunities`` text columns (title, organization, description,
category, location), kept in sync by triggers on the content table:

* opportunities inserted or updated by
  :meth:`storage.Storage.upsert_opportunity` are indexed automatically
  (only the five indexed text columns trigger a re-index, so the
  frequent ``last_verified_at`` bumps cost nothing),
* :meth:`SearchIndex.rebuild` exists for databases collected before
  this stage and for recovering from a corrupted index.

User queries are never passed to FTS5 raw. They are translated into a
strict query: the input is lowercased and split into unicode word
tokens, each token is double-quoted so FTS5 syntax characters (and
reserved words such as AND/OR/NEAR) cannot change the query structure,
tokens are joined as an implicit AND, the last token becomes a prefix
match, and the whole query is capped at 12 tokens. A query with no
word tokens returns no results. Ranking uses ``bm25()`` (lower rank =
more relevant). Snippets are marked with ``[`` ``]`` and are plain
text -- safe to display later without HTML escaping tricks.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storage import Storage

logger = logging.getLogger(__name__)

#: Indexed text columns of ``opportunities``, in FTS5 column order.
#: ``description`` is column index 2 (used for snippet()).
_INDEXED_COLUMNS = ("title", "organization", "description", "category", "location")

#: Maximum number of tokens taken from one user query.
_QUERY_MAX_TOKENS = 12

#: Hard cap on results per search() call.
_RESULT_LIMIT_MAX = 200

#: Word tokens are what the unicode61 tokenizer treats as searchable
#: text; everything else (punctuation, FTS5 syntax characters, quotes)
#: is dropped before the query is built, so user input can never alter
#: the query structure.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    title,
    organization,
    description,
    category,
    location,
    content='opportunities',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 1'
);

CREATE TRIGGER IF NOT EXISTS opportunities_fts_insert
AFTER INSERT ON opportunities BEGIN
    INSERT INTO search_fts (rowid, title, organization, description, category, location)
    VALUES (new.id, new.title, new.organization, new.description, new.category, new.location);
END;

CREATE TRIGGER IF NOT EXISTS opportunities_fts_update
AFTER UPDATE OF title, organization, description, category, location ON opportunities BEGIN
    INSERT INTO search_fts (search_fts, rowid, title, organization, description, category, location)
    VALUES ('delete', old.id, old.title, old.organization, old.description, old.category, old.location);
    INSERT INTO search_fts (rowid, title, organization, description, category, location)
    VALUES (new.id, new.title, new.organization, new.description, new.category, new.location);
END;

CREATE TRIGGER IF NOT EXISTS opportunities_fts_delete
AFTER DELETE ON opportunities BEGIN
    INSERT INTO search_fts (search_fts, rowid, title, organization, description, category, location)
    VALUES ('delete', old.id, old.title, old.organization, old.description, old.category, old.location);
END;
"""

_ROW_SQL = f"""
SELECT o.id, o.external_id, o.source_id, o.title, o.organization,
       o.deadline, o.location, o.category, o.procurement_type,
       o.reference_number, o.source_url,
       bm25(search_fts) AS fts_rank,
       snippet(search_fts, {_INDEXED_COLUMNS.index("description")}, '[', ']', '...', 12)
           AS snippet_text
  FROM search_fts
  JOIN opportunities AS o ON o.id = search_fts.rowid
 WHERE search_fts MATCH ?
 ORDER BY fts_rank
 LIMIT ? OFFSET ?
"""

#: Match count only -- same strict MATCH translation as _ROW_SQL,
#: without ranking or snippet work.
_COUNT_SQL = """
SELECT COUNT(*) AS n
  FROM search_fts
  JOIN opportunities AS o ON o.id = search_fts.rowid
 WHERE search_fts MATCH ?
"""


@dataclass
class SearchResult:
    """One ranked hit from :meth:`SearchIndex.search`.

    ``rank`` is the FTS5 bm25 score (lower = more relevant). ``snippet``
    is plain text with ``[match]`` markers, never HTML.
    """

    opportunity_id: int
    external_id: str
    source_id: int
    title: str | None
    organization: str | None
    snippet: str | None
    deadline: str | None
    location: str | None
    category: str | None
    procurement_type: str | None
    reference_number: str | None
    source_url: str
    rank: float


def is_fts5_available(connection: sqlite3.Connection) -> bool:
    """Return True when this SQLite build supports FTS5 virtual tables."""
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS tenderscout_fts5_probe USING fts5(x)"
        )
    except sqlite3.OperationalError:
        return False
    finally:
        connection.execute("DROP TABLE IF EXISTS tenderscout_fts5_probe")
    return True


def _fts_query_from(user_query: str) -> str | None:
    """Translate free user text into a strict, quoted FTS5 query.

    The input is lowercased (FTS5 string matching is case-insensitive
    for ASCII via unicode61; lowercasing here keeps query tokens in the
    same form the tokenizer stores). Tokens are ANDed, the last token
    matches as a prefix, and the query is capped at
    :data:`_QUERY_MAX_TOKENS` tokens. Returns None when the input
    contains no word tokens.
    """
    tokens = _TOKEN_RE.findall((user_query or "").lower())[:_QUERY_MAX_TOKENS]
    if not tokens:
        return None
    parts = []
    for position, token in enumerate(tokens):
        if position == len(tokens) - 1:
            # FTS5 prefix query: the * must directly follow the phrase.
            parts.append(f'"{token}"*')
        else:
            parts.append(f'"{token}"')
    return " ".join(parts)


class SearchIndex:
    """FTS5 index over the ``opportunities`` table of a Storage.

    Creating the index is idempotent and safe on every open: the virtual
    table and sync triggers are created with ``IF NOT EXISTS``. Rows
    collected before the index existed are picked up by ``rebuild()``.
    """

    def __init__(self, storage: Storage) -> None:
        """Attach to a Storage and make sure the index schema exists."""
        self.storage = storage
        self.conn = storage.conn
        if not is_fts5_available(self.conn):
            raise RuntimeError(
                "This SQLite build has no FTS5 support; full-text search "
                "is unavailable. Use Python's standard sqlite3 module "
                "compiled with SQLITE_ENABLE_FTS5."
            )
        self._create_schema()
        # Heal databases collected before this stage existed: when rows
        # are stored but the index is empty, rebuild once. Indexed rows
        # are counted via the search_fts_docsize shadow table (one row
        # per indexed document -- real index state), not COUNT(*) on
        # the FTS table, which for external-content tables merely
        # reflects the content table.
        if self.storage.count_opportunities() > 0 and self.indexed_doc_count() == 0:
            logger.warning(
                "Search index is empty but opportunities are stored; rebuilding."
            )
            self.rebuild()

    def _create_schema(self) -> None:
        """Create the virtual table and sync triggers if missing."""
        self.conn.executescript(_FTS_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------
    # indexing
    # ------------------------------------------------------------------

    def rebuild(self) -> int:
        """Rebuild the index from every stored opportunity.

        Returns the number of stored opportunities the index now covers
        (post-rebuild ``COUNT(*)`` of the content table).
        """
        self.conn.execute("INSERT INTO search_fts (search_fts) VALUES ('rebuild')")
        self.conn.commit()
        return self.storage.count_opportunities()

    def indexed_doc_count(self) -> int:
        """Return how many opportunities are actually in the index.

        ``COUNT(*)`` over the ``search_fts_docsize`` shadow table, which
        FTS5 keeps with one row per indexed document. ``COUNT(*)`` on
        the FTS table itself cannot be used: for external-content
        tables it is derived from the content table and reports stale
        numbers. A value of 0 with stored opportunities means the index
        is empty (rows collected before this stage); ``rebuild()`` and
        the auto-heal in :meth:`__init__` fix that.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM search_fts_docsize"
        ).fetchone()
        return int(row["n"])

    def count(self, user_query: str) -> int:
        """Return how many stored opportunities match ``user_query``.

        Same strict query translation as :meth:`search` (tokens are
        quoted and ANDed, the last token matches as a prefix) but no
        ranking or snippet work. Queries without word tokens match
        nothing and return 0. The API uses this for ``total`` counts.
        """
        fts_query = _fts_query_from(user_query)
        if fts_query is None:
            return 0
        row = self.conn.execute(_COUNT_SQL, (fts_query,)).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # searching
    # ------------------------------------------------------------------

    def search(
        self, user_query: str, limit: int = 20, offset: int = 0
    ) -> list[SearchResult]:
        """Search stored opportunities; best matches first.

        ``user_query`` is free text: every word must be present (implicit
        AND) and the last word matches as a prefix. Queries without word
        tokens return no results. ``limit`` is clamped to
        [0, 200]; ``offset`` pages through results. Negative
        ``limit``/``offset`` raise ``ValueError``.
        """
        fts_query = _fts_query_from(user_query)
        if fts_query is None:
            return []
        limit = int(limit)
        offset = int(offset)
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        limit = min(limit, _RESULT_LIMIT_MAX)
        if limit == 0:
            return []
        rows = self.conn.execute(_ROW_SQL, (fts_query, limit, offset)).fetchall()
        return [self._row_to_result(row) for row in rows]

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> SearchResult:
        """Map one joined row onto a SearchResult."""
        return SearchResult(
            opportunity_id=int(row["id"]),
            external_id=row["external_id"],
            source_id=int(row["source_id"]),
            title=row["title"],
            organization=row["organization"],
            snippet=row["snippet_text"],
            deadline=row["deadline"],
            location=row["location"],
            category=row["category"],
            procurement_type=row["procurement_type"],
            reference_number=row["reference_number"],
            source_url=row["source_url"],
            rank=float(row["fts_rank"]),
        )
