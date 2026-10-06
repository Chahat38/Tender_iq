"""Live cross-user access tests (marker: ``postgres``).

A user must never be able to read or modify another user's rows. These
tests stand up the real migration on a disposable schema, create two
auth users and exercise the RLS policies exactly as the Supabase API
would (role ``authenticated`` + JWT subject claim).

Deselected by the default run; needs ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import uuid

import pg_support
import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def two_users(pg_db):
    """(connection, schema, user_a, user_b) with both users created."""
    conn, schema = pg_db
    user_a = pg_support.add_auth_user(conn, "a@example.test")
    user_b = pg_support.add_auth_user(conn, "b@example.test")
    return conn, schema, user_a, user_b


def seed_owner_rows(conn, schema: str, user_id: str) -> dict[str, str]:
    """Insert one owned row per user table (as the table owner).

    Returns ``{table: row_id}`` so tests can target specific rows.
    """
    ids: dict[str, str] = {}
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f'set search_path to "{schema}"')
            opportunity_id = _seed_opportunity(cur)

            cur.execute(
                "insert into profiles (id, user_id, company_name) "
                "values (%s, %s, %s) returning id",
                (str(uuid.uuid4()), user_id, f"Company of {user_id[:6]}"),
            )
            ids["profiles"] = cur.fetchone()["id"]

            cur.execute(
                "insert into opportunity_matches "
                "(id, opportunity_id, user_id, relevance_score, level) "
                "values (%s, %s, %s, 88, 'HIGH') returning id",
                (str(uuid.uuid4()), opportunity_id, user_id),
            )
            ids["opportunity_matches"] = cur.fetchone()["id"]

            cur.execute(
                "insert into saved_opportunities (id, user_id, opportunity_id) "
                "values (%s, %s, %s) returning id",
                (str(uuid.uuid4()), user_id, opportunity_id),
            )
            ids["saved_opportunities"] = cur.fetchone()["id"]

            cur.execute(
                "insert into alerts (id, user_id, opportunity_id, type, title) "
                "values (%s, %s, %s, 'new_match', 'New match for you') "
                "returning id",
                (str(uuid.uuid4()), user_id, opportunity_id),
            )
            ids["alerts"] = cur.fetchone()["id"]

            cur.execute(
                "insert into rfps (id, user_id, opportunity_id, title) "
                "values (%s, %s, %s, %s) returning id",
                (
                    str(uuid.uuid4()),
                    user_id,
                    opportunity_id,
                    f"RFP of {user_id[:6]}",
                ),
            )
            ids["rfps"] = cur.fetchone()["id"]
    return ids


def _seed_opportunity(cur) -> int:
    """Insert one shared source row as the (RLS-bypassing) owner."""
    source_id = _seed_source(cur)
    cur.execute(
        "insert into opportunities (source_id, external_id, source_url, "
        " content_hash, title) values (%s, %s, %s, %s, %s) returning id",
        (
            source_id,
            f"EXT-{uuid.uuid4().hex[:8]}",
            "https://example.gov.pk/tender/1",
            "deadbeef",
            "Shared tender",
        ),
    )
    return cur.fetchone()["id"]


#: User tables covered by the cross-user guarantee.
USER_OWNED_TABLES = (
    "profiles",
    "opportunity_matches",
    "saved_opportunities",
    "alerts",
    "rfps",
)


class TestCrossUserIsolation:
    def test_user_b_never_sees_user_a_rows(self, two_users) -> None:
        conn, schema, user_a, user_b = two_users
        seed_owner_rows(conn, schema, user_a)
        seed_owner_rows(conn, schema, user_b)

        for table in USER_OWNED_TABLES:
            with pg_support.as_authenticated(conn, schema, user_b) as cur:
                cur.execute(f"select count(*) as n from {table}")
                total_as_b = int(cur.fetchone()["n"])
                cur.execute(
                    f"select count(*) as n from {table} where user_id = %s",
                    (user_a,),
                )
                leaked = int(cur.fetchone()["n"])
            assert total_as_b == 1, f"{table}: user B should see only their row"
            assert leaked == 0, f"{table}: user B saw user A's rows"

    def test_user_b_cannot_update_user_a_rows(self, two_users) -> None:
        conn, schema, user_a, user_b = two_users
        ids_a = seed_owner_rows(conn, schema, user_a)

        for table in USER_OWNED_TABLES:
            with pg_support.as_authenticated(conn, schema, user_b) as cur:
                cur.execute(
                    f"update {table} set user_id = %s where id = %s",
                    (user_b, ids_a[table]),
                )
                assert cur.rowcount == 0, f"{table}: B modified A's row"

            # The row still belongs to A.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(f'set search_path to "{schema}"')
                    cur.execute(
                        f"select user_id from {table} where id = %s",
                        (ids_a[table],),
                    )
                    assert cur.fetchone()["user_id"] == user_a

    def test_user_b_cannot_delete_user_a_rows(self, two_users) -> None:
        conn, schema, user_a, user_b = two_users
        ids_a = seed_owner_rows(conn, schema, user_a)
        for table in USER_OWNED_TABLES:
            with pg_support.as_authenticated(conn, schema, user_b) as cur:
                cur.execute(f"delete from {table} where id = %s", (ids_a[table],))
                assert cur.rowcount == 0, f"{table}: B deleted A's row"

    def test_owner_can_insert_own_rows_only(self, two_users) -> None:
        conn, schema, user_a, user_b = two_users
        with pg_support.as_authenticated(conn, schema, user_a) as cur:
            cur.execute(
                "insert into target_regions (id, user_id, province) "
                "values (%s, %s, 'Punjab')",
                (str(uuid.uuid4()), user_a),
            )
        # ...but never on someone else's behalf.
        with pg_support.as_authenticated(conn, schema, user_a) as cur:
            with pytest.raises(Exception):
                cur.execute(
                    "insert into target_regions (id, user_id, province) "
                    "values (%s, %s, 'Sindh')",
                    (str(uuid.uuid4()), user_b),
                )
        conn.rollback()

    def test_backend_owned_tables_are_read_only_for_clients(self, two_users) -> None:
        conn, schema, user_a, _user_b = two_users
        seed_owner_rows(conn, schema, user_a)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f'set search_path to "{schema}"')
                cur.execute("select id from opportunities limit 1")
                opportunity_id = cur.fetchone()["id"]
        with pg_support.as_authenticated(conn, schema, user_a) as cur:
            cur.execute("select count(*) as n from opportunity_matches")
            assert int(cur.fetchone()["n"]) == 1
            with pytest.raises(Exception):
                cur.execute(
                    "insert into opportunity_matches "
                    "(id, opportunity_id, user_id, relevance_score, level) "
                    "values (%s, %s, %s, 50, 'MEDIUM')",
                    (str(uuid.uuid4()), opportunity_id, user_a),
                )
        conn.rollback()


class TestSharedAndOperationalTables:
    def test_authenticated_can_read_shared_data(self, two_users) -> None:
        conn, schema, _user_a, user_b = two_users
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f'set search_path to "{schema}"')
                _seed_opportunity(cur)
        with pg_support.as_authenticated(conn, schema, user_b) as cur:
            cur.execute("select count(*) as n from opportunities")
            assert int(cur.fetchone()["n"]) >= 1

    def test_authenticated_cannot_write_shared_data(self, two_users) -> None:
        conn, schema, _user_a, user_b = two_users
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f'set search_path to "{schema}"')
                source_id = _seed_source(cur)
        with pg_support.as_authenticated(conn, schema, user_b) as cur:
            with pytest.raises(Exception):
                cur.execute(
                    "insert into opportunities (source_id, external_id, "
                    "source_url, content_hash) values (%s, 'X', "
                    "'https://example.gov.pk/x', 'hash')",
                    (source_id,),
                )
        conn.rollback()

    def test_operational_tables_are_invisible(self, two_users) -> None:
        conn, schema, _user_a, user_b = two_users
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f'set search_path to "{schema}"')
                source_id = _seed_source(cur)
                cur.execute(
                    "insert into source_fetch_logs (source_id, started_at) "
                    "values (%s, now())",
                    (source_id,),
                )
                cur.execute(
                    "insert into analysis_runs (task, status) values (%s, %s)",
                    ("match", "ok"),
                )
        for table in ("source_fetch_logs", "analysis_runs", "audit_log"):
            with pg_support.as_authenticated(conn, schema, user_b) as cur:
                cur.execute(f"select count(*) as n from {table}")
                assert int(cur.fetchone()["n"]) == 0, (
                    f"{table} must be service-role only"
                )


def _seed_source(cur) -> int:
    cur.execute(
        "insert into procurement_sources (name, adapter_type) "
        "values (%s, 'Test') returning id",
        (f"Ops Portal {uuid.uuid4().hex[:8]}",),
    )
    return cur.fetchone()["id"]
