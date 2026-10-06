"""Live-Postgres parity tests for the collector storage backend.

Marker ``postgres`` (deselected by default; see pytest.ini). Requires
``TEST_DATABASE_URL``. Asserts that :class:`storage_postgres.PostgresStorage`
behaves exactly like the SQLite backend for the collector-facing
contract: created/updated/unchanged statuses, change history rows,
write-once ``source_url``, fetch logs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from models import NormalizedOpportunity
from storage_postgres import PostgresStorage

pytestmark = pytest.mark.postgres


def make_opportunity(**overrides) -> NormalizedOpportunity:
    """One well-formed normalized opportunity."""
    values = dict(
        title="Supply of laptops",
        organization="Ministry of Education",
        description="Lot of 500 laptops",
        source_url="https://example.gov.pk/tender/TS1",
        source_name="Example Portal",
        published_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
        deadline=datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc),
        location="Islamabad",
        province="Islamabad",
        category="ICT",
        procurement_type="goods",
        reference_number="P-100",
        document_urls=["https://example.gov.pk/docs/TS1.pdf"],
        external_id="TS1",
    )
    values.update(overrides)
    return NormalizedOpportunity(**values)


@pytest.fixture
def pg_storage(pg_db):
    """PostgresStorage bound to the per-test schema."""
    conn, _schema = pg_db
    storage = PostgresStorage(dsn="unused://injected", connection=conn)
    yield storage
    storage.conn.rollback()


class TestUpsertContract:
    def test_created_updated_unchanged(self, pg_storage) -> None:
        source_id = pg_storage.get_or_create_source("Example Portal", "Example")
        normalized = make_opportunity()

        assert pg_storage.upsert_opportunity(normalized, source_id) == "created"
        assert pg_storage.upsert_opportunity(normalized, source_id) == "unchanged"

        changed = make_opportunity(deadline=normalized.deadline + timedelta(days=7))
        assert pg_storage.upsert_opportunity(changed, source_id) == "updated"

        row = pg_storage.get_opportunity(
            pg_storage._select_opportunity(source_id, "TS1")["id"]
        )
        assert row is not None
        assert row["deadline"].startswith("2026-11-08")
        assert row["province"] == "Islamabad"
        assert row["source_name"] == "Example Portal"
        assert row["source_count"] == 1
        assert row["is_primary"] in (True, 1)

    def test_change_history_is_recorded(self, pg_storage) -> None:
        source_id = pg_storage.get_or_create_source("Example Portal", "Example")
        pg_storage.upsert_opportunity(make_opportunity(), source_id)
        changed = make_opportunity(title="Supply of laptops (revised)")
        pg_storage.upsert_opportunity(changed, source_id)

        existing = pg_storage._select_opportunity(source_id, "TS1")
        updates = pg_storage.get_updates(existing["id"])
        fields = {u["field"] for u in updates}
        assert "title" in fields
        assert all(u["detected_at"] for u in updates)

    def test_source_url_is_write_once(self, pg_storage) -> None:
        source_id = pg_storage.get_or_create_source("Example Portal", "Example")
        original = make_opportunity()
        pg_storage.upsert_opportunity(original, source_id)

        moved = make_opportunity(source_url="https://example.gov.pk/moved/TS1")
        assert pg_storage.upsert_opportunity(moved, source_id) == "unchanged"
        row = pg_storage._select_opportunity(source_id, "TS1")
        assert row["source_url"] == original.source_url

    def test_document_sync(self, pg_storage) -> None:
        source_id = pg_storage.get_or_create_source("Example Portal", "Example")
        pg_storage.upsert_opportunity(make_opportunity(), source_id)
        existing = pg_storage._select_opportunity(source_id, "TS1")
        docs = pg_storage.get_documents(existing["id"])
        assert [d["url"] for d in docs] == [
            "https://example.gov.pk/docs/TS1.pdf"
        ]
        assert docs[0]["filename"] == "TS1.pdf"

    def test_source_count_and_primary_default(self, pg_storage) -> None:
        source_id = pg_storage.get_or_create_source("Example Portal", "Example")
        pg_storage.upsert_opportunity(make_opportunity(), source_id)
        row = pg_storage._select_opportunity(source_id, "TS1")
        assert row["source_count"] == 1
        assert row["is_primary"] in (True, 1)
        assert row["first_seen_at"] is not None
        assert row["last_verified_at"] is not None


class TestSourcesAndLogs:
    def test_get_or_create_is_idempotent(self, pg_storage) -> None:
        first = pg_storage.get_or_create_source("Portal A", "TypeA")
        second = pg_storage.get_or_create_source("Portal A", "TypeA")
        assert first == second
        sources = pg_storage.list_sources()
        assert [s["name"] for s in sources].count("Portal A") == 1

    def test_log_fetch_roundtrip(self, pg_storage) -> None:
        source_id = pg_storage.get_or_create_source("Portal A", "TypeA")
        log_id = pg_storage.log_fetch(
            source_id,
            started_at="2026-10-06T00:00:00+00:00",
            finished_at="2026-10-06T00:01:00+00:00",
            found=5,
            created=3,
            updated=1,
            failed=1,
            error_message="one bad row",
        )
        with pg_storage.conn.cursor() as cur:
            cur.execute("select * from source_fetch_logs where id = %s", (log_id,))
            row = cur.fetchone()
        assert row["found"] == 5
        assert row["error_message"] == "one bad row"
