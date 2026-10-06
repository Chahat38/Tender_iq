"""Ops tests (offline): failure streaks, alert rows, run_source gate."""

from __future__ import annotations

import pytest

from adapters.base import RunResult
from compliance import ComplianceError
from ops import (
    ALERT_SOURCE_CONSECUTIVE_FAILURES,
    FAILURE_THRESHOLD,
    notify_default,
    record_source_run,
    run_source,
)
from registry import SOURCE_REGISTRY, SourceSpec, register_source
from storage import Storage


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "ops.db")
    try:
        yield st
    finally:
        st.close()


class FakeAdapter:
    """Adapter double: counts calls, returns a canned RunResult."""

    source_name = "Fake Source"

    def __init__(self, failed: int = 0, found: int = 2, created: int = 1):
        self.failed = failed
        self.found = found
        self.created = created
        self.calls = 0

    def run(self, storage=None, since=None, enrich_details=False):
        self.calls += 1
        return RunResult(
            found=self.found,
            created=self.created,
            failed=self.failed,
            errors=["boom"] * self.failed,
        )


def registered_source(storage, *, terms_checked=True, name="Fake Source"):
    spec = SourceSpec(
        key="fake",
        source_name=name,
        base_url="https://fake.example",
        adapter_type="FakeAdapter",
        terms_checked=terms_checked,
    )
    source_id = register_source(storage, spec)
    return source_id, spec


class TestRecordSourceRun:
    def test_success_resets_failure_counter(self, storage) -> None:
        source_id, _ = registered_source(storage)
        storage.set_source_status(
            source_id,
            last_status="failed",
            last_crawled_at="2026-10-05T00:00:00+00:00",
            consecutive_failures=2,
        )
        outcome = record_source_run(storage, source_id, success=True)
        row = storage.get_source_by_id(source_id)
        assert outcome["consecutive_failures"] == 0
        assert outcome["alert"] is None
        assert row["last_status"] == "ok"
        assert row["consecutive_failures"] == 0

    def test_failures_accumulate_and_alert_at_threshold(self, storage) -> None:
        source_id, _ = registered_source(storage)
        seen = []
        for attempt in range(1, FAILURE_THRESHOLD + 1):
            outcome = record_source_run(
                storage, source_id, success=False, notifier=seen.append
            )
            assert outcome["consecutive_failures"] == attempt
        row = storage.get_source_by_id(source_id)
        assert row["last_status"] == "failed"
        assert row["consecutive_failures"] == FAILURE_THRESHOLD
        assert outcome["alert"] is not None
        assert outcome["alert"]["alert_type"] == (
            ALERT_SOURCE_CONSECUTIVE_FAILURES
        )
        assert len(seen) == 1

    def test_no_duplicate_alert_while_one_is_open(self, storage) -> None:
        source_id, _ = registered_source(storage)
        seen = []
        for _ in range(FAILURE_THRESHOLD + 2):
            record_source_run(storage, source_id, success=False, notifier=seen.append)
        assert len(seen) == 1  # one open alert -> one notification
        assert storage.has_open_operational_alert(
            source_id, ALERT_SOURCE_CONSECUTIVE_FAILURES
        )

    def test_recovery_clears_counter_but_alert_needs_ack(self, storage) -> None:
        source_id, _ = registered_source(storage)
        for _ in range(FAILURE_THRESHOLD):
            record_source_run(storage, source_id, success=False, notifier=lambda a: None)
        assert storage.has_open_operational_alert(
            source_id, ALERT_SOURCE_CONSECUTIVE_FAILURES
        )
        outcome = record_source_run(storage, source_id, success=True)
        row = storage.get_source_by_id(source_id)
        assert outcome["consecutive_failures"] == 0
        assert row["last_status"] == "ok"
        # The alert row stays for the operator to acknowledge; only the
        # counter resets, so the next streak starts from zero.
        assert storage.has_open_operational_alert(
            source_id, ALERT_SOURCE_CONSECUTIVE_FAILURES
        )

    def test_alert_message_carries_source_name_and_error(self, storage) -> None:
        source_id, _ = registered_source(storage, name="Broken Portal")
        for _ in range(FAILURE_THRESHOLD):
            record_source_run(
                storage,
                source_id,
                success=False,
                error_message="HTTP 503",
                notifier=lambda a: None,
            )
        row = storage.conn.execute(
            "SELECT * FROM operational_alerts WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        assert row is not None
        assert "Broken Portal" in row["message"]
        assert "HTTP 503" in row["message"]
        assert row["consecutive_failures"] == FAILURE_THRESHOLD

    def test_unknown_source_id_raises(self, storage) -> None:
        with pytest.raises(ValueError, match="unknown source id"):
            record_source_run(storage, 9999, success=True)

    def test_notify_default_needs_no_webhook(self, monkeypatch) -> None:
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        notify_default({"alert_type": "x", "source_id": 1, "message": "m"})


class TestRunSource:
    def test_gate_blocks_unregistered_source_before_fetch(
        self, storage, monkeypatch
    ) -> None:
        monkeypatch.setitem(
            SOURCE_REGISTRY,
            "fake_unvetted",
            SourceSpec(
                key="fake_unvetted",
                source_name="Fake Source",
                base_url="https://fake.example",
                adapter_type="FakeAdapter",
                terms_checked=False,
            ),
        )
        adapter = FakeAdapter()
        with pytest.raises(ComplianceError, match="REFUSING TO CRAWL"):
            run_source(storage, adapter, "fake_unvetted")
        assert adapter.calls == 0  # nothing was fetched

    def test_gate_blocks_unknown_adapter_key(self, storage) -> None:
        # No registry entry -> row is created with terms_checked NULL.
        adapter = FakeAdapter()
        with pytest.raises(ComplianceError):
            run_source(storage, adapter, "not_in_registry")
        assert adapter.calls == 0

    def test_happy_path_logs_fetch_and_marks_source_ok(self, storage) -> None:
        source_id, spec = registered_source(storage)
        adapter = FakeAdapter()
        outcome = run_source(
            storage,
            adapter,
            "fake",
            registry_spec=SourceSpec(
                key="fake",
                source_name=spec.source_name,
                base_url=spec.base_url,
                adapter_type=spec.adapter_type,
                terms_checked=True,
            ),
        )
        assert outcome["source_id"] == source_id
        assert adapter.calls == 1
        row = storage.get_source_by_id(source_id)
        assert row["last_status"] == "ok"
        assert row["consecutive_failures"] == 0
        assert row["last_crawled_at"] is not None
        log = storage.conn.execute(
            "SELECT * FROM source_fetch_logs WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        assert log is not None
        assert log["found"] == 2
        assert log["created"] == 1
        assert log["failed"] == 0
        assert outcome["fetch_log_id"] == log["id"]

    def test_failed_run_counts_towards_streak(self, storage) -> None:
        source_id, spec = registered_source(storage)
        adapter = FakeAdapter(failed=1)
        spec_kwargs = dict(
            key="fake",
            source_name=spec.source_name,
            base_url=spec.base_url,
            adapter_type=spec.adapter_type,
            terms_checked=True,
        )
        for _ in range(FAILURE_THRESHOLD):
            outcome = run_source(
                storage,
                adapter,
                "fake",
                registry_spec=SourceSpec(**spec_kwargs),
            )
        assert outcome["ops"]["consecutive_failures"] == FAILURE_THRESHOLD
        assert outcome["ops"]["alert"] is not None
