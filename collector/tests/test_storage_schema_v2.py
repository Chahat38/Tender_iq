"""Offline tests for the extended SQLite schema (contract parity).

Covers the tables/columns added for deduplication, AI, alerts, RFPs and
the fixed frontend contract, the ALTER TABLE path for pre-existing
databases, and the :func:`storage.open_storage` backend switch.
"""

from __future__ import annotations

import sqlite3

import pytest
from storage import Storage

#: Tables the SQLite backend must share with the Supabase contract
#: (``procurement_sources`` is the Postgres name of ``sources``).
REQUIRED_TABLES = {
    "sources",  # == procurement_sources
    "opportunities",
    "opportunity_documents",
    "opportunity_requirements",
    "opportunity_updates",
    "opportunity_ai",
    "source_fetch_logs",
    "analysis_runs",
    "profiles",
    "company_capabilities",
    "target_regions",
    "target_categories",
    "opportunity_matches",
    "saved_opportunities",
    "alerts",
    "rfps",
    "rfp_sections",
    "subscriptions",
    "usage_counters",
    "dedup_review_queue",
    "operational_alerts",
    "audit_log",
}


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "schema.db")
    yield st
    st.close()


def table_names(storage: Storage) -> set[str]:
    return {
        row["name"]
        for row in storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def column_names(storage: Storage, table: str) -> set[str]:
    return {
        row["name"] for row in storage.conn.execute(f"PRAGMA table_info({table})")
    }


class TestContractTables:
    def test_all_contract_tables_exist(self, storage) -> None:
        assert REQUIRED_TABLES.issubset(table_names(storage))

    def test_opportunity_contract_columns(self, storage) -> None:
        expected = {
            "id", "source_id", "external_id", "reference_number", "title",
            "organization", "description", "source_url", "source_name",
            "published_at", "deadline", "location", "province", "sector",
            "department", "category", "procurement_type", "content_hash",
            "is_primary", "duplicate_group_id", "first_seen_at",
            "last_seen_at", "last_verified_at", "source_count", "raw_content",
        }
        assert expected.issubset(column_names(storage, "opportunities"))

    def test_source_status_columns(self, storage) -> None:
        assert {"last_status", "last_crawled_at", "consecutive_failures"}.issubset(
            column_names(storage, "sources")
        )

    def test_document_pipeline_columns(self, storage) -> None:
        assert {
            "content_hash", "storage_path", "mime_type", "byte_size",
            "fetched_at", "extracted_text", "extracted_at",
        }.issubset(column_names(storage, "opportunity_documents"))

    def test_matches_unique_and_versioned(self, storage) -> None:
        columns = column_names(storage, "opportunity_matches")
        assert {"opportunity_id", "user_id", "relevance_score", "level",
                "reasons", "prompt_version"}.issubset(columns)
        cur = storage.conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'opportunity_matches'"
        )
        ddl = cur.fetchone()["sql"]
        assert "UNIQUE (opportunity_id, user_id)" in ddl

    def test_fallback_key_index(self, storage) -> None:
        names = {
            row["name"]
            for row in storage.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_opportunities_source_ref" in names


class TestColumnMigration:
    def test_old_database_gains_new_columns(self, tmp_path) -> None:
        """A database created before the columns existed is ALTERed."""
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE sources (
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
            CREATE TABLE opportunities (
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
            """
        )
        conn.commit()
        conn.close()

        storage = Storage(path)
        try:
            opp_columns = column_names(storage, "opportunities")
            assert {"province", "source_name", "source_count"}.issubset(opp_columns)
            source_columns = column_names(storage, "sources")
            assert {"last_status", "consecutive_failures"}.issubset(source_columns)
        finally:
            storage.close()

        # Idempotent: reopening changes nothing and keeps the data.
        storage = Storage(path)
        try:
            assert storage.count_opportunities() == 0
        finally:
            storage.close()


class TestOpenStorage:
    def test_defaults_to_sqlite(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        from storage import open_storage

        st = open_storage(tmp_path / "factory.db")
        try:
            assert isinstance(st, Storage)
            assert st.db_path == tmp_path / "factory.db"
        finally:
            st.close()

    def test_database_url_selects_postgres(self, monkeypatch) -> None:
        import storage_postgres
        from storage import open_storage

        class FakePostgresStorage:
            def __init__(self, dsn: str) -> None:
                self.dsn = dsn

        # Swap the class so the factory is tested without a live server.
        monkeypatch.setattr(
            storage_postgres, "PostgresStorage", FakePostgresStorage
        )
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
        st = open_storage("ignored.db")
        assert isinstance(st, FakePostgresStorage)
        assert st.dsn.endswith("@localhost/db")
