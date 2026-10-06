"""Operational run handling: compliance gate, run logging, failure
alerting.

``run_source`` is the single entry point the CLI and CI use to collect
one source. It enforces the compliance gate *before* any request is
made, records the fetch, and turns a streak of failed runs into a
persistent alert row plus a best-effort notification (log line now;
Slack webhook when ``SLACK_WEBHOOK_URL`` is set).

A run counts as failed when any listing could not be fetched or parsed
(``result.failed > 0``) - partial data is a failure too, because the
alternatives (silently serving stale rows) are worse.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping

from compliance import enforce_compliance
from storage_common import utc_now_iso

LOGGER = logging.getLogger("tenderscout.ops")

#: Alert once this many runs in a row have failed.
FAILURE_THRESHOLD = 3

#: Stored in ``operational_alerts.alert_type`` for streak alerts.
ALERT_SOURCE_CONSECUTIVE_FAILURES = "source_consecutive_failures"

#: ``(alert_row) -> None`` - called once per newly written alert.
Notifier = Callable[[Mapping[str, Any]], None]

#: Exit code used by the CLI when the compliance gate refuses a source.
EXIT_COMPLIANCE = 3


def notify_default(alert: Mapping[str, Any]) -> None:
    """Deliver an alert: always log it, POST it to Slack if configured.

    Delivery is best-effort and never raises - losing a notification
    must not fail a collection run.
    """
    message = (
        f"[tenderscout operational alert] {alert.get('alert_type')} on "
        f"source_id={alert.get('source_id')}: {alert.get('message')}"
    )
    LOGGER.error(message)
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    try:  # pragma: no cover - network side effect, guarded by tests
        import requests

        requests.post(webhook, json={"text": message}, timeout=10)
    except Exception:  # noqa: BLE001 - notification is best-effort
        LOGGER.warning("SLACK_WEBHOOK_URL delivery failed", exc_info=True)


def record_source_run(
    storage: Any,
    source_id: int,
    *,
    success: bool,
    finished_at: str | None = None,
    error_message: str | None = None,
    notifier: Notifier | None = None,
) -> dict[str, Any]:
    """Update a source's health after one run; alert on failure streaks.

    Returns ``{"consecutive_failures": int, "alert": dict | None}``.
    On the first failing run at/after ``FAILURE_THRESHOLD`` a single
    ``operational_alerts`` row is written and handed to the notifier;
    while an unacknowledged alert stays open, no duplicate rows are
    written (recovery clears it by resetting the counter; a human can
    also acknowledge the alert).
    """
    source = storage.get_source_by_id(source_id)
    if source is None:
        raise ValueError(f"unknown source id {source_id}")

    previous = int(source.get("consecutive_failures") or 0)
    failures = 0 if success else previous + 1
    storage.set_source_status(
        source_id,
        last_status="ok" if success else "failed",
        last_crawled_at=finished_at or utc_now_iso(),
        consecutive_failures=failures,
    )

    alert: dict[str, Any] | None = None
    if not success and failures >= FAILURE_THRESHOLD:
        if not storage.has_open_operational_alert(
            source_id, ALERT_SOURCE_CONSECUTIVE_FAILURES
        ):
            name = source.get("name", str(source_id))
            message = (
                f"{name} failed {failures} runs in a row"
                + (f": {error_message}" if error_message else "")
            )
            alert_id = storage.insert_operational_alert(
                source_id,
                ALERT_SOURCE_CONSECUTIVE_FAILURES,
                severity="error",
                message=message,
                consecutive_failures=failures,
            )
            alert = {
                "id": alert_id,
                "source_id": source_id,
                "alert_type": ALERT_SOURCE_CONSECUTIVE_FAILURES,
                "message": message,
                "consecutive_failures": failures,
            }
            (notifier or notify_default)(alert)

    return {"consecutive_failures": failures, "alert": alert}


def run_source(
    storage: Any,
    adapter: Any,
    adapter_key: str,
    *,
    since: Any = None,
    enrich_details: bool = False,
    notifier: Notifier | None = None,
    registry_spec: Any = None,
) -> dict[str, Any]:
    """Collect one source end-to-end: gate -> run -> log -> health.

    Returns ``{"source_id", "result", "fetch_log_id", "ops"}``.
    Raises :class:`compliance.ComplianceError` before any network or
    fixture request when the source fails the gate.
    """
    from registry import register_source, spec_for  # avoid import cycle

    started_at = utc_now_iso()
    spec = registry_spec if registry_spec is not None else spec_for(adapter_key)
    if spec is not None:
        source_id = register_source(storage, spec)
    else:
        source_id = storage.get_or_create_source(
            adapter.source_name, type(adapter).__name__
        )

    source_row = storage.get_source_by_id(source_id)
    if source_row is None:
        raise ValueError(f"unknown source id {source_id}")
    enforce_compliance(source_row)

    result = adapter.run(
        storage=storage, since=since, enrich_details=enrich_details
    )
    finished_at = utc_now_iso()
    error_message = "; ".join(result.errors) or None
    fetch_log_id = storage.log_fetch(
        source_id,
        started_at,
        finished_at,
        found=result.found,
        created=result.created,
        updated=result.updated,
        failed=result.failed,
        error_message=error_message,
    )
    ops = record_source_run(
        storage,
        source_id,
        success=result.failed == 0,
        finished_at=finished_at,
        error_message=error_message,
        notifier=notifier,
    )
    return {
        "source_id": source_id,
        "result": result,
        "fetch_log_id": fetch_log_id,
        "ops": ops,
    }
