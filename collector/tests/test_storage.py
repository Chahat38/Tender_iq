"""Tests for the SQLite storage layer: upsert paths and update history."""

from datetime import datetime, timezone

import pytest
from models import NormalizedOpportunity
from storage import Storage

DEADLINE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
NEW_DEADLINE = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def storage(tmp_path):
    """A Storage on a throwaway SQLite file."""
    st = Storage(tmp_path / "test.db")
    yield st
    st.close()


@pytest.fixture()
def source_id(storage) -> int:
    return storage.get_or_create_source("Example Source", "example")


def make_opportunity(**overrides) -> NormalizedOpportunity:
    """Build a NormalizedOpportunity with deterministic defaults."""
    fields = dict(
        title="Supply of laptops",
        organization="Punjab Information Technology Board",
        description="Procurement of 500 laptops.",
        source_url="https://example.gov.pk/tenders/123",
        source_name="Example Source",
        published_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        deadline=DEADLINE,
        location="Lahore",
        category=None,
        procurement_type="goods",
        reference_number="T-2026-123",
        document_urls=["https://example.gov.pk/tenders/123/tor.pdf"],
        external_id="123",
    )
    fields.update(overrides)
    return NormalizedOpportunity(**fields)


class TestUpsertPaths:
    def test_new_opportunity_is_created(self, storage, source_id) -> None:
        status = storage.upsert_opportunity(make_opportunity(), source_id)
        assert status == "created"
        row = storage.get_opportunity(1)
        assert row is not None
        assert row["title"] == "Supply of laptops"
        assert row["source_url"] == "https://example.gov.pk/tenders/123"
        assert row["deadline"] == DEADLINE.isoformat()
        assert row["is_primary"] == 1
        assert row["first_seen_at"] == row["last_seen_at"] == row["last_verified_at"]

    def test_identical_content_is_unchanged(self, storage, source_id) -> None:
        storage.upsert_opportunity(make_opportunity(), source_id)
        before = storage.get_opportunity(1)
        status = storage.upsert_opportunity(make_opportunity(), source_id)
        assert status == "unchanged"
        after = storage.get_opportunity(1)
        assert after["last_verified_at"] >= before["last_verified_at"]
        assert storage.get_updates(1) == []

    def test_unchanged_never_touches_source_url(self, storage, source_id) -> None:
        storage.upsert_opportunity(make_opportunity(), source_id)
        tampered = make_opportunity(source_url="https://mirror.example.pk/t/123")
        status = storage.upsert_opportunity(tampered, source_id)
        assert status == "unchanged"
        assert (
            storage.get_opportunity(1)["source_url"]
            == "https://example.gov.pk/tenders/123"
        )

    def test_updated_row_keeps_original_source_url(self, storage, source_id) -> None:
        storage.upsert_opportunity(make_opportunity(), source_id)
        changed = make_opportunity(
            title="Supply of 600 laptops",
            source_url="https://mirror.example.pk/t/123",
        )
        status = storage.upsert_opportunity(changed, source_id)
        assert status == "updated"
        assert (
            storage.get_opportunity(1)["source_url"]
            == "https://example.gov.pk/tenders/123"
        )

    def test_deadline_change_records_deadline_change_update(
        self, storage, source_id
    ) -> None:
        storage.upsert_opportunity(make_opportunity(), source_id)
        status = storage.upsert_opportunity(
            make_opportunity(deadline=NEW_DEADLINE), source_id
        )
        assert status == "updated"
        updates = storage.get_updates(1)
        assert len(updates) == 1
        update = updates[0]
        assert update["field"] == "deadline"
        assert update["update_type"] == "deadline_change"
        assert update["old_value"] == DEADLINE.isoformat()
        assert update["new_value"] == NEW_DEADLINE.isoformat()
        assert storage.get_opportunity(1)["deadline"] == NEW_DEADLINE.isoformat()

    def test_multiple_changes_create_one_update_row_per_field(
        self, storage, source_id
    ) -> None:
        storage.upsert_opportunity(make_opportunity(), source_id)
        changed = make_opportunity(
            title="Supply of 600 laptops", description="Updated scope."
        )
        status = storage.upsert_opportunity(changed, source_id)
        assert status == "updated"
        updates = storage.get_updates(1)
        assert {u["field"] for u in updates} == {"title", "description"}
        assert all(u["update_type"] == "field_change" for u in updates)

    def test_document_change_is_tracked_and_synced(self, storage, source_id) -> None:
        storage.upsert_opportunity(make_opportunity(), source_id)
        changed = make_opportunity(
            document_urls=[
                "https://example.gov.pk/tenders/123/tor.pdf",
                "https://example.gov.pk/tenders/123/boq.pdf",
            ]
        )
        status = storage.upsert_opportunity(changed, source_id)
        assert status == "updated"
        docs = storage.get_documents(1)
        assert len(docs) == 2
        assert docs[1]["filename"] == "boq.pdf"
        assert "document_urls" in {u["field"] for u in storage.get_updates(1)}

    def test_external_id_scopes_per_source(self, storage) -> None:
        first = storage.get_or_create_source("Source A", "example")
        second = storage.get_or_create_source("Source B", "example")
        assert storage.upsert_opportunity(make_opportunity(), first) == "created"
        assert storage.upsert_opportunity(make_opportunity(), second) == "created"
        assert storage.count_opportunities() == 2


class TestFetchLogs:
    def test_log_fetch_persists_counts(self, storage, source_id) -> None:
        log_id = storage.log_fetch(
            source_id,
            started_at="2026-10-03T00:00:00+00:00",
            finished_at="2026-10-03T00:01:00+00:00",
            found=12,
            created=10,
            updated=1,
            failed=1,
        )
        row = storage.conn.execute(
            "SELECT * FROM source_fetch_logs WHERE id = ?", (log_id,)
        ).fetchone()
        assert row["found"] == 12
        assert row["created"] == 10
        assert row["failed"] == 1

    def test_schema_contains_all_stage_tables(self, storage) -> None:
        names = {
            row["name"]
            for row in storage.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        expected = {
            "sources",
            "opportunities",
            "opportunity_documents",
            "opportunity_updates",
            "source_fetch_logs",
            "opportunity_ai",
            "analysis_runs",
        }
        assert expected.issubset(names)
