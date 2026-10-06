"""Offline structural tests for supabase/migrations/001_init.sql.

The migration file is the data contract with the frontend and the
security boundary (RLS). These tests parse it and assert that every
required table, column, index and policy is present, without needing a
live database - they run in CI alongside the rest of the offline suite.
Live behaviour is covered by collector/tests/test_postgres_rls.py
(marker: postgres).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "001_init.sql"
)
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

SHARED_TABLES = [
    "procurement_sources",
    "opportunities",
    "opportunity_documents",
    "opportunity_requirements",
    "opportunity_updates",
    "opportunity_ai",
]

OPERATIONAL_TABLES = [
    "source_fetch_logs",
    "analysis_runs",
    "dedup_review_queue",
    "operational_alerts",
    "audit_log",
]

USER_TABLES = [
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
]

#: Tables the UI may write to its own rows; the rest of the user tables
#: are written only by the backend (service role bypasses RLS).
UI_WRITABLE_TABLES = [
    "profiles",
    "company_capabilities",
    "target_regions",
    "target_categories",
    "saved_opportunities",
    "alerts",
    "rfps",
    "rfp_sections",
]

REQUIRED_TABLES = SHARED_TABLES + OPERATIONAL_TABLES + USER_TABLES

OPPORTUNITY_COLUMNS = [
    "id", "source_id", "external_id", "reference_number", "title",
    "organization", "description", "source_url", "source_name",
    "published_at", "deadline", "location", "province", "category",
    "procurement_type", "content_hash", "is_primary", "duplicate_group_id",
    "first_seen_at", "last_seen_at", "last_verified_at", "source_count",
    "raw_content",
]


@pytest.fixture(scope="module")
def sql() -> str:
    """The migration file as text (lowercased for easy matching)."""
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH}"
    return MIGRATION_PATH.read_text(encoding="utf-8")


def create_table_body(sql: str, table: str) -> str:
    """Return the text of one ``create table`` statement (or fail)."""
    match = re.search(
        rf"create table if not exists {table}\s*\((.*?)\n\);",
        sql,
        re.DOTALL,
    )
    assert match is not None, f"table {table} is not created by the migration"
    return match.group(1)


def policy_loop(sql: str) -> list[tuple[str, str]]:
    """Extract ``(tables, block_body)`` from the DO loops in the file.

    ``marker`` identifies the loop by the array literal it starts with
    (e.g. the first table of the group).
    """
    matches = re.findall(
        rf"foreach t in array array\[(.*?)\]\s*loop(.*?)end loop;",
        sql,
        re.DOTALL,
    )
    found = []
    for tables_blob, body in matches:
        tables = re.findall(r"'([a-z_]+)'", tables_blob)
        found.append((tables, body))
    assert found, "no RLS DO loops found in the migration"
    return found


class TestTables:
    def test_every_required_table_is_created(self, sql: str) -> None:
        for table in REQUIRED_TABLES:
            create_table_body(sql, table)

    def test_opportunity_columns_match_contract(self, sql: str) -> None:
        body = create_table_body(sql, "opportunities")
        for column in OPPORTUNITY_COLUMNS:
            assert re.search(rf"\b{column}\b", body), (
                f"opportunities.{column} missing from the contract schema"
            )

    def test_opportunities_unique_source_external(self, sql: str) -> None:
        body = create_table_body(sql, "opportunities")
        assert "unique (source_id, external_id)" in body

    def test_opportunity_matches_unique_and_versioned(self, sql: str) -> None:
        body = create_table_body(sql, "opportunity_matches")
        assert "unique (opportunity_id, user_id)" in body
        assert re.search(r"\bprompt_version\b", body), (
            "opportunity_matches must record prompt_version"
        )

    def test_alert_types_are_constrained(self, sql: str) -> None:
        body = create_table_body(sql, "alerts")
        for alert_type in (
            "new_match",
            "deadline_approaching",
            "opportunity_updated",
            "corrigendum",
        ):
            assert alert_type in body

    def test_rfp_status_gate_exists(self, sql: str) -> None:
        body = create_table_body(sql, "rfps")
        assert "reviewed" in body, "rfps.status must be able to reach 'reviewed'"

    def test_documents_keep_hash_and_storage_path(self, sql: str) -> None:
        body = create_table_body(sql, "opportunity_documents")
        for column in ("content_hash", "storage_path", "extracted_text"):
            assert column in body


class TestIndexes:
    def test_required_indexes_present(self, sql: str) -> None:
        required = {
            "deadline index": "on opportunities (deadline)",
            "published_at index": "on opportunities (published_at)",
            "category/province index": "on opportunities (category, province)",
            "duplicate group index": "on opportunities (duplicate_group_id)",
            "upsert fallback key": "on opportunities (source_id, reference_number)",
            "trigram index": "using gin (title gin_trgm_ops)",
            "match ranking index": "on opportunity_matches (user_id, relevance_score desc)",
        }
        for label, fragment in required.items():
            assert fragment in sql, f"missing {label}: expected {fragment!r}"

    def test_fulltext_gin_covers_contract_fields(self, sql: str) -> None:
        index = re.search(
            r"idx_opportunities_fts\s+on opportunities\s+using gin \((.*?)\);",
            sql,
            re.DOTALL,
        )
        assert index is not None, "missing GIN full-text index on opportunities"
        body = index.group(1)
        assert "to_tsvector" in body
        for field in ("title", "organization", "description", "reference_number"):
            assert f"coalesce({field}, '')" in body, (
                f"full-text index must include {field}"
            )


class TestRowLevelSecurity:
    def test_every_table_enables_rls(self, sql: str) -> None:
        loops = policy_loop(sql)
        for table in REQUIRED_TABLES:
            bodies = [body for tables, body in loops if table in tables]
            assert bodies, f"{table} missing from every RLS group"
            assert any("enable row level security" in body for body in bodies), (
                f"RLS not enabled on {table}"
            )

    def test_shared_tables_select_only(self, sql: str) -> None:
        loops = [
            (tables, body)
            for tables, body in policy_loop(sql)
            if "procurement_sources" in tables
        ]
        assert loops, "shared-table RLS loop not found"
        for tables, body in loops:
            for table in SHARED_TABLES:
                assert table in tables, f"{table} missing from shared RLS group"
            assert "for select" in body
            assert "using (true)" in body
            for write in ("for insert", "for update", "for delete"):
                assert write not in body, (
                    f"shared tables must not allow client {write[4:]}s"
                )

    def test_operational_tables_have_no_policy(self, sql: str) -> None:
        loops = [
            (tables, body)
            for tables, body in policy_loop(sql)
            if "create policy" not in body
        ]
        assert loops, "operational RLS loop (no policies) not found"
        operational = set()
        for tables, body in loops:
            operational.update(tables)
            assert "enable row level security" in body
            assert "create policy" not in body
        for table in OPERATIONAL_TABLES:
            assert table in operational, (
                f"{table} must be service-role only (no client policy)"
            )

    def test_user_tables_scoped_to_owner(self, sql: str) -> None:
        loops = [
            (tables, body)
            for tables, body in policy_loop(sql)
            if "auth.uid() = user_id" in body
        ]
        assert loops, "user-table policies not found"
        scoped: set[str] = set()
        for tables, body in loops:
            scoped.update(tables)
            assert "auth.uid() = user_id" in body
        for table in USER_TABLES:
            assert table in scoped, f"user table {table} lacks an owner policy"

    def test_ui_tables_allow_owner_writes(self, sql: str) -> None:
        write_loops = [
            (tables, body)
            for tables, body in policy_loop(sql)
            if "for insert" in body
        ]
        assert write_loops, "owner-write policies not found"
        writable: set[str] = set()
        for tables, body in write_loops:
            writable.update(tables)
            for command in ("for insert", "for update", "for delete"):
                assert command in body
        for table in UI_WRITABLE_TABLES:
            assert table in writable, f"{table} owner writes missing"
        # Backend-owned user tables must never appear in a write loop.
        for table in ("opportunity_matches", "usage_counters", "subscriptions"):
            assert table not in writable, (
                f"{table} is backend-owned; clients must not write it"
            )

    def test_no_policy_targets_anonymous(self, sql: str) -> None:
        assert "to anon" not in sql
        assert "for anon" not in sql


class TestPostgresGrammar:
    def test_migration_parses_with_real_postgres_grammar(self) -> None:
        """The migration (and every generated policy) must be valid SQL.

        Uses pglast - PostgreSQL's own parser - so a typo fails here and
        in CI instead of on the first `supabase db push`.
        """
        pytest.importorskip("pglast")
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import validate_sql

        files = sorted(MIGRATION_PATH.parent.glob("*.sql")) + [
            SCRIPTS_DIR / "pg_auth_shim.sql"
        ]
        errors = [
            error
            for path in files
            for error in validate_sql.validate_file(path)
        ]
        assert not errors, "\n".join(errors)
