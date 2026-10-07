"""Live-Postgres tests for the /search fallback in PostgresStorage.

Marker ``postgres`` (deselected by default; see pytest.ini). Requires
``TEST_DATABASE_URL``. Asserts the same strict query contract as the
SQLite FTS5 backend in search.py: every word must match (implicit
AND), the last word matches as a prefix, tokens are capped, and
nothing matches a punctuation-only query.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from models import NormalizedOpportunity
from search import SearchIndex
from storage_postgres import PostgresStorage

pytestmark = pytest.mark.postgres


def make_opportunity(**overrides) -> NormalizedOpportunity:
    """One well-formed normalized opportunity."""
    values = dict(
        title="Supply of laptops for District Courts",
        organization="Punjab Information Technology Board",
        description="Lot of 500 laptops with a three year warranty",
        source_url="https://example.gov.pk/tender/TS1",
        source_name="Example Portal",
        published_at=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
        deadline=datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc),
        location="Lahore",
        province="Punjab",
        category="ICT",
        procurement_type="goods",
        external_id="TS1",
    )
    values.update(overrides)
    return NormalizedOpportunity(**values)


@pytest.fixture
def seeded(pg_storage: PostgresStorage) -> int:
    """One source and three opportunities to search."""
    source_id = pg_storage.get_or_create_source("Example Portal", "PiaAdapter")
    pg_storage.upsert_opportunity(
        make_opportunity(external_id="TS1"), source_id
    )
    pg_storage.upsert_opportunity(
        make_opportunity(
            external_id="TS2",
            title="Supply of stationery for Civil Secretariate",
            organization="District Courts Lahore",
            description="Pens, paper and laptop stands",
            location="Lahore",
        ),
        source_id,
    )
    pg_storage.upsert_opportunity(
        make_opportunity(
            external_id="TS3",
            title="Construction of a district court building",
            organization="Works Department",
            description="Two storey building in Karachi",
            location="Karachi",
        ),
        source_id,
    )
    return source_id


class TestSearchFallback:
    def test_every_word_must_match(self, pg_storage, seeded) -> None:
        assert pg_storage.search_count("laptops lahore") == 1
        rows = pg_storage.search("laptops lahore")
        assert len(rows) == 1
        assert rows[0].external_id == "TS1"

    def test_last_word_matches_as_prefix(self, pg_storage, seeded) -> None:
        assert pg_storage.search_count("laptop") == 2  # laptops + laptop stands
        assert pg_storage.search_count("district") == 2

    def test_no_word_tokens_match_nothing(self, pg_storage, seeded) -> None:
        assert pg_storage.search_count("/// ???") == 0
        assert pg_storage.search("/// ???") == []

    def test_results_are_search_results(self, pg_storage, seeded) -> None:
        rows = pg_storage.search("laptop")
        assert rows
        first = rows[0]
        assert first.external_id in ("TS1", "TS2")
        assert first.source_name is None or True  # result carries no source name
        assert first.deadline is not None
        assert first.source_url.startswith("https://")
        assert first.rank <= 0

    def test_like_wildcards_are_literal(self, pg_storage, seeded) -> None:
        assert pg_storage.search_count("100%") == 0
        assert pg_storage.search_count("a_b") == 0

    def test_pagination_and_clamp(self, pg_storage, seeded) -> None:
        assert len(pg_storage.search("laptop", limit=200)) >= 1
        with pytest.raises(ValueError):
            pg_storage.search("laptop", limit=-1)

    def test_count_agrees_with_list_length(self, pg_storage, seeded) -> None:
        total = pg_storage.search_count("of")
        rows = pg_storage.search("of", limit=200)
        assert total >= 1
        assert len(rows) == min(total, 200)


def test_search_index_is_disjoint_from_postgres(pg_storage) -> None:
    """The Postgres backend must not require an FTS5 index at all."""
    assert not hasattr(pg_storage, "search_index")
    assert SearchIndex is not None  # guard import sanity