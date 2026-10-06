"""Tests for the Stage 5 FTS5 search layer.

All tests run offline against a seeded temporary SQLite database built
with :meth:`storage.Storage.upsert_opportunity` -- the same write path
the collector uses, so trigger-based index sync is exercised end to
end. No network access happens in this module.
"""

from datetime import datetime, timezone

import pytest

from models import NormalizedOpportunity
from search import SearchIndex, _fts_query_from, is_fts5_available


@pytest.fixture()
def storage(tmp_path):
    """A Storage on a throwaway SQLite file."""
    from storage import Storage

    st = Storage(tmp_path / "search_test.db")
    yield st
    st.close()


@pytest.fixture()
def index(storage):
    """A SearchIndex attached to the throwaway Storage."""
    return SearchIndex(storage)


@pytest.fixture()
def source_id(storage) -> int:
    return storage.get_or_create_source("Example Source", "example")


def make_opportunity(**overrides) -> NormalizedOpportunity:
    """Build a NormalizedOpportunity with deterministic defaults."""
    fields = dict(
        title="Supply of medical equipment for district hospital",
        organization="Punjab Information Technology Board",
        description="Procurement of ventilators and MRI machines.",
        source_url="https://example.gov.pk/tenders/123",
        source_name="Example Source",
        published_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        deadline=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        location="Lahore",
        category="Health",
        procurement_type="goods",
        reference_number="T-2026-123",
        document_urls=[],
        external_id="123",
    )
    fields.update(overrides)
    return NormalizedOpportunity(**fields)


def upsert(storage_obj, source_id, **overrides) -> int:
    """Insert one opportunity and return its row id."""
    storage_obj.upsert_opportunity(make_opportunity(**overrides), source_id)
    return storage_obj.conn.execute(
        "SELECT id FROM opportunities WHERE external_id = ? AND source_id = ?",
        (overrides.get("external_id", "123"), source_id),
    ).fetchone()["id"]


class TestIndexSync:
    def test_upserted_rows_are_indexed(self, storage, index, source_id) -> None:
        upsert(storage, source_id)
        upsert(
            storage,
            source_id,
            title="Construction of a school boundary wall",
            external_id="124",
        )
        assert index.indexed_doc_count() == 2

    def test_content_update_is_reflected(self, storage, index, source_id) -> None:
        upsert(storage, source_id)
        storage.upsert_opportunity(
            make_opportunity(description="Now includes dialysis machines."),
            source_id,
        )
        hits = index.search("dialysis")
        assert len(hits) == 1
        assert hits[0].external_id == "123"

    def test_delete_is_reflected(self, storage, index, source_id) -> None:
        upsert(storage, source_id)
        storage.conn.execute("DELETE FROM opportunities")
        storage.conn.commit()
        assert index.indexed_doc_count() == 0

    def test_unchanged_bump_does_not_duplicate_index(
        self, storage, index, source_id
    ) -> None:
        upsert(storage, source_id)
        storage.upsert_opportunity(make_opportunity(), source_id)  # unchanged
        assert index.indexed_doc_count() == 1
        hits = index.search("ventilators")
        assert len(hits) == 1

    def test_rebuild_backfills_pre_search_rows(
        self, storage, index, source_id
    ) -> None:
        upsert(storage, source_id, external_id="201")
        upsert(
            storage,
            source_id,
            title="Construction of a school boundary wall",
            external_id="202",
        )
        # Simulate a database collected before the index existed: without
        # the insert trigger, upserts never reach the index. The index is
        # emptied with the supported 'delete-all' command -- a plain
        # DELETE FROM search_fts corrupts external-content indexes.
        storage.conn.execute("DROP TRIGGER opportunities_fts_insert")
        storage.conn.execute(
            "INSERT INTO search_fts (search_fts) VALUES ('delete-all')"
        )
        storage.conn.commit()
        assert index.indexed_doc_count() == 0
        assert index.rebuild() == 2
        assert index.indexed_doc_count() == 2
        assert len(index.search("hospital")) == 1  # only 201 mentions it
        assert len(index.search("school")) == 1  # only 202 mentions it

    def test_indexed_doc_count_reflects_real_index_state(
        self, storage, index, source_id
    ) -> None:
        upsert(storage, source_id)
        assert index.indexed_doc_count() == 1
        storage.conn.execute(
            "INSERT INTO search_fts (search_fts) VALUES ('delete-all')"
        )
        storage.conn.commit()
        assert index.indexed_doc_count() == 0  # COUNT(*) would lie here


class TestQueryTranslation:
    def test_tokens_are_quoted_and_anded(self) -> None:
        assert _fts_query_from("medical equipment") == '"medical" "equipment"*'

    def test_syntax_characters_are_stripped(self) -> None:
        assert _fts_query_from('medi"; DROP * (NEAR)') == '"medi" "drop" "near"*'

    def test_reserved_words_are_quoted_away(self) -> None:
        # 'OR' must be a search term, never an FTS5 operator.
        assert _fts_query_from('") OR (x') == '"or" "x"*'

    def test_punctuation_only_query_has_no_tokens(self) -> None:
        assert _fts_query_from('") * ( -') is None

    def test_unicode_word_characters_survive(self) -> None:
        assert _fts_query_from("Médecins") == '"médecins"*'

    def test_token_count_is_capped(self) -> None:
        query = _fts_query_from(" ".join(f"w{i}" for i in range(50)))
        assert query.count('"') == 24  # 12 tokens, two quotes each


class TestSearch:
    def test_matches_multiple_fields(self, index, storage, source_id) -> None:
        upsert(storage, source_id)
        upsert(
            storage,
            source_id,
            title="Streetlight maintenance contract",
            organization="City District Government",
            description="LED upgrades across the district.",
            external_id="124",
        )
        by_org = index.search("punjab")
        by_desc = index.search("ventilators")
        assert [r.external_id for r in by_org] == ["123"]
        assert [r.external_id for r in by_desc] == ["123"]

    def test_all_words_must_match(self, index, storage, source_id) -> None:
        upsert(storage, source_id)
        assert index.search("ventilators xyzzy") == []
        assert index.search("medical ventilators") != []

    def test_empty_or_punctuation_query_returns_nothing(self, index) -> None:
        assert index.search("") == []
        assert index.search('   ") OR ( ') == []

    def test_prefix_on_last_word(self, index, storage, source_id) -> None:
        upsert(storage, source_id)
        hits = index.search("ventil")
        assert [r.external_id for r in hits] == ["123"]

    def test_prefix_does_not_apply_to_earlier_words(
        self, index, storage, source_id
    ) -> None:
        upsert(storage, source_id)
        assert index.search("med equip") == []  # "med" alone must not match

    def test_limit_and_offset_page_results(self, index, storage, source_id) -> None:
        for i in range(5):
            upsert(
                storage,
                source_id,
                title=f"Bus fleet procurement lot {i}",
                description="Diesel buses.",
                external_id=f"3{i}",
            )
        all_rows = index.search("bus diesel", limit=200)
        assert len(all_rows) == 5
        assert len(index.search("bus diesel", limit=2)) == 2
        page2 = index.search("bus diesel", limit=2, offset=2)
        assert len(page2) == 2
        # 5 rows / limit 2 -> the page at offset 4 holds the last row.
        assert len(index.search("bus diesel", limit=2, offset=4)) == 1
        assert index.search("bus diesel", limit=2, offset=5) == []
        # Paging is stable: page 2 == the tail of the full result list.
        assert [r.opportunity_id for r in page2] == [
            r.opportunity_id for r in all_rows[2:4]
        ]

    def test_limit_is_clamped(self, index) -> None:
        assert index.search("anything", limit=10_000) == []
        with pytest.raises(ValueError):
            index.search("anything", limit=-5)

    def test_results_carry_display_fields(self, index, storage, source_id) -> None:
        upsert(storage, source_id)
        hit = index.search("ventilators")[0]
        assert hit.opportunity_id >= 1
        assert hit.source_url == "https://example.gov.pk/tenders/123"
        assert hit.deadline == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).isoformat()
        assert hit.location == "Lahore"
        assert hit.category == "Health"
        assert hit.procurement_type == "goods"
        assert hit.reference_number == "T-2026-123"
        assert isinstance(hit.rank, float)

    def test_snippet_marks_match_and_is_plain_text(
        self, index, storage, source_id
    ) -> None:
        upsert(storage, source_id)
        snippet = index.search("ventilators")[0].snippet
        assert snippet is not None
        assert "[ventilators]" in snippet
        assert "<" not in snippet  # plain text, not HTML

    def test_better_matches_rank_first(self, index, storage, source_id) -> None:
        # 123 matches 'ventilator' in title AND description (twice);
        # 124 only once in a longer description. bm25 must separate them.
        upsert(storage, source_id)
        upsert(
            storage,
            source_id,
            title="Vague supplies tender",
            description=(
                "General procurement of various ventilator-compatible spare "
                "parts and accessories for existing hospital equipment."
            ),
            external_id="124",
        )
        hits = index.search("ventilator")
        assert [r.external_id for r in hits] == ["123", "124"]
        assert hits[0].rank < hits[1].rank


class TestFts5Availability:
    def test_probe_succeeds_on_standard_sqlite(self, storage) -> None:
        assert is_fts5_available(storage.conn) is True


class TestCli:
    def test_cli_search_prints_ranked_results(
        self, tmp_path, capsys
    ) -> None:
        import main as main_module

        db = tmp_path / "cli.db"
        store = main_module.Storage(db)
        sid = store.get_or_create_source("Example Source", "example")
        store.upsert_opportunity(make_opportunity(), sid)
        store.close()

        # The row was written before any SearchIndex existed (no sync
        # triggers); the CLI must self-heal and still find it.
        code = main_module.main(["--db", str(db), "--search", "ventilators"])
        out = capsys.readouterr().out
        assert code == 0
        assert "[123]" in out
        assert "1 result(s)" in out
        assert "[ventilators]" in out

    def test_cli_search_no_match_exits_zero(self, tmp_path, capsys) -> None:
        import main as main_module

        db = tmp_path / "cli.db"
        main_module.Storage(db).close()
        code = main_module.main(["--db", str(db), "--search", "nothing"])
        out = capsys.readouterr().out
        assert code == 0
        assert "0 result(s)" in out

    def test_cli_rebuild_index(self, tmp_path, capsys) -> None:
        import main as main_module

        db = tmp_path / "cli.db"
        store = main_module.Storage(db)
        sid = store.get_or_create_source("Example Source", "example")
        # Create the index schema first, then simulate rows collected
        # before it existed: drop the sync trigger and insert unindexed.
        from search import SearchIndex

        SearchIndex(store)
        store.conn.execute("DROP TRIGGER opportunities_fts_insert")
        store.upsert_opportunity(make_opportunity(), sid)
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM search_fts_docsize"
        ).fetchone()
        assert row["n"] == 0  # drift confirmed before the CLI runs
        store.close()

        code = main_module.main(["--db", str(db), "--rebuild-index"])
        out = capsys.readouterr().out
        assert code == 0
        assert "1 opportunity(s) indexed" in out

    def test_cli_search_and_rebuild_are_exclusive(self, capsys) -> None:
        import main as main_module

        with pytest.raises(SystemExit) as excinfo:
            main_module.main(["--search", "x", "--rebuild-index"])
        assert excinfo.value.code == 2
