"""Helpers for the live-Postgres tests (marker: ``postgres``).

These tests are deselected by the default pytest invocation (see
pytest.ini) and additionally skip unless ``TEST_DATABASE_URL`` points at
a disposable Postgres. Each test gets its own schema, so nothing touches
an existing database; a Supabase project URL works too (the auth shim is
skipped when Supabase's ``auth`` schema already exists).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_SQL = REPO_ROOT / "supabase" / "migrations" / "001_init.sql"
AUTH_SHIM_SQL = REPO_ROOT / "scripts" / "pg_auth_shim.sql"

#: Roles granted on the per-test schema (mirrors Supabase's default
#: grants: the API roles get broad table privileges, RLS does the
#: restricting).
GRANT_SQL = """
grant usage on schema {schema} to anon, authenticated, service_role;
grant all on all tables in schema {schema} to authenticated;
grant all on all sequences in schema {schema} to authenticated;
grant all on all tables in schema {schema} to service_role;
alter default privileges in schema {schema} grant all on tables to authenticated;
alter default privileges in schema {schema} grant all on sequences to authenticated;
"""


def connect(dsn: str) -> Any:
    """Open a dict-row psycopg connection."""
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row)


def create_test_schema(dsn: str) -> tuple[Any, str]:
    """Create a fresh schema with the migration applied.

    Returns ``(connection, schema_name)``; the caller owns closing the
    connection and dropping the schema.
    """
    schema = f"tenderscout_test_{uuid.uuid4().hex[:12]}"
    conn = connect(dsn)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f'create schema "{schema}"')
            cur.execute(f'set search_path to "{schema}"')
            _install_auth_shim(cur)
            cur.execute(MIGRATION_SQL.read_text(encoding="utf-8"))
            cur.execute(GRANT_SQL.format(schema=schema))
    return conn, schema


def _install_auth_shim(cur: Any) -> None:
    """Install scripts/pg_auth_shim.sql when Supabase's auth is absent."""
    cur.execute("select to_regclass('auth.users') is null as missing")
    if cur.fetchone()["missing"]:
        cur.execute(AUTH_SHIM_SQL.read_text(encoding="utf-8"))


def drop_test_schema(conn: Any, schema: str) -> None:
    """Drop the per-test schema (best effort; ignores a closed conn)."""
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f'drop schema if exists "{schema}" cascade')
    finally:
        conn.close()


def add_auth_user(conn: Any, email: str | None = None) -> str:
    """Insert an auth.users row (a real UUID) and return it."""
    user_id = str(uuid.uuid4())
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "insert into auth.users (id, email) values (%s, %s)",
                (user_id, email or f"{user_id[:8]}@example.test"),
            )
    return user_id


@contextmanager
def as_authenticated(conn: Any, schema: str, user_id: str) -> Iterator[Any]:
    """Run the block as the ``authenticated`` role for one user.

    Sets the JWT subject claim the RLS policies read via ``auth.uid()``
    and the search_path of the per-test schema. The role and claim are
    transaction-local, so they revert when the block ends.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("set local role authenticated")
            cur.execute(
                "select set_config('request.jwt.claim.sub', %s, true)",
                (user_id,),
            )
            cur.execute(f'set local search_path to "{schema}", public')
            yield cur
