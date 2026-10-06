"""Pytest bootstrap for collector tests.

Puts the ``collector`` directory on ``sys.path`` so tests can import the
collector modules directly. All tests run offline on saved fixtures and
in-memory data; no test ever touches the live network.

The ``pg_dsn`` / ``pg_db`` fixtures serve the ``postgres``-marked tests:
they need ``TEST_DATABASE_URL`` (a disposable Postgres or a Supabase
project) and are deselected by the default run anyway (pytest.ini).
"""

import os
import sys
from pathlib import Path

import pytest

COLLECTOR_DIR = Path(__file__).resolve().parents[1]
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """DSN for live-Postgres tests, or skip."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "TEST_DATABASE_URL not set; live-Postgres tests need a disposable "
            "database (local `postgres` container or Supabase project)"
        )
    pytest.importorskip("psycopg")
    return dsn


@pytest.fixture
def pg_db(pg_dsn: str):
    """One migration-applied schema, dropped again after the test."""
    import pg_support

    conn, schema = pg_support.create_test_schema(pg_dsn)
    try:
        yield conn, schema
    finally:
        pg_support.drop_test_schema(conn, schema)
