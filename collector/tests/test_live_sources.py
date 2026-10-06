"""Live smoke tests for every registered, compliance-passing source.

Marked ``network`` and excluded from the default suite by pytest.ini;
run explicitly with:

    python -m pytest -o addopts="-ra" -m network

Each test drives the *real* pipeline for one source - compliance gate,
polite listing pass, fetch log, health bookkeeping - against a
throwaway database, and asserts that live HTML still parses into the
normalized contract. Fixtures under ``collector/fixtures`` are
snapshots; these tests are the tripwire when a site changes.

Deliberately minimal: one listing request per source, no detail-page
enrichment, no document downloads.
"""

from __future__ import annotations

import pytest
from main import ADAPTERS
from ops import run_source
from registry import SOURCE_REGISTRY
from storage import Storage

pytestmark = pytest.mark.network

#: Sources the gate would actually let run today (see DECISIONS.md for
#: the sources that are deliberately not registered).
LIVE_KEYS = sorted(
    key
    for key, spec in SOURCE_REGISTRY.items()
    if spec.active and spec.terms_checked and not spec.requires_authentication
)


@pytest.fixture(autouse=True)
def polite_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identify the crawler honestly, as the live CLI does."""
    monkeypatch.setenv("CONTACT_EMAIL", "tenderscout-tests@example.org")


@pytest.mark.parametrize("key", LIVE_KEYS)
def test_live_listing_parses_and_stores(key: str, tmp_path) -> None:
    storage = Storage(tmp_path / "live.db")
    try:
        adapter = ADAPTERS[key](offline=False)
        outcome = run_source(storage, adapter, key, enrich_details=False)
        result = outcome["result"]

        assert result.errors == [], result.errors
        assert result.found > 0, "listing request came back empty"
        assert result.created + result.updated > 0, "nothing stored"

        row = storage.conn.execute(
            "SELECT external_id, title, first_seen_at, source_id"
            " FROM opportunities"
            " ORDER BY first_seen_at DESC, id DESC"
            " LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["external_id"], "every opportunity needs a reference"
        assert row["title"], "every opportunity needs a title"
        assert row["first_seen_at"]
        assert row["source_id"] == outcome["source_id"]

        # Bookkeeping the operational layer depends on.
        fetch_log = storage.conn.execute(
            "SELECT source_id, error_message FROM source_fetch_logs"
            " WHERE id = ?",
            (outcome["fetch_log_id"],),
        ).fetchone()
        assert fetch_log is not None
        assert fetch_log["source_id"] == outcome["source_id"]
        assert fetch_log["error_message"] is None
    finally:
        storage.close()
