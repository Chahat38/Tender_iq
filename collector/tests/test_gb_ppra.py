"""GB PPRA adapter tests (offline, saved fixtures)."""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from adapters.gb_ppra import (
    BASE_URL,
    LISTING_PATH,
    OFFLINE_LISTING_GLOB,
    SOURCE_NAME,
    GbPpraAdapter,
)

_REF_RE = re.compile(r"^TSE-\d+$")


@pytest.fixture()
def adapter() -> GbPpraAdapter:
    return GbPpraAdapter(offline=True)


def _parse(adapter: GbPpraAdapter, page: str):
    return adapter._parse_listing_page(adapter.read_fixture(page))


class TestListingParse:
    def test_parses_ten_cards_per_page(self, adapter) -> None:
        assert len(_parse(adapter, "gb_viewall_page1.html")) == 10
        assert len(_parse(adapter, "gb_viewall_page2.html")) == 10

    def test_row_fields(self, adapter) -> None:
        first = _parse(adapter, "gb_viewall_page1.html")[0]
        data = first.raw_data
        assert _REF_RE.match(first.external_id)
        assert data["title"] and len(data["title"]) > 5
        assert data["organization"]
        assert data["published_text"] and data["closing_text"]
        assert data["notice_type"]
        assert data["document_urls"], "every card links a Notice PDF"

    def test_source_url_is_the_detail_page(self, adapter) -> None:
        first = _parse(adapter, "gb_viewall_page1.html")[0]
        assert re.match(
            re.escape(f"{BASE_URL}/procurementdetails/") + r"\d+$",
            first.url,
        )

    def test_id_tails_are_unique_within_a_page(self, adapter) -> None:
        rows = _parse(adapter, "gb_viewall_page1.html")
        ids = [row.external_id for row in rows]
        assert len(set(ids)) == len(ids)

    def test_page_without_cards_is_empty(self, adapter) -> None:
        assert adapter._parse_listing_page("<html><body>maintenance</body></html>") == []


class TestFetchOffline:
    def test_fetch_reads_every_fixture_page(self, adapter) -> None:
        rows = adapter.fetch_listings()
        assert len(rows) == 20


class TestNormalize:
    @pytest.fixture()
    def normalized(self, adapter):
        first = _parse(adapter, "gb_viewall_page1.html")[0]
        return adapter.normalize(first), first

    def test_identity_fields(self, normalized) -> None:
        opp, raw = normalized
        assert opp.external_id == raw.external_id
        assert opp.reference_number == raw.external_id
        assert opp.source_name == SOURCE_NAME
        assert opp.source_url == raw.url

    def test_timestamps_are_karachi(self, normalized) -> None:
        opp, _ = normalized
        assert opp.published_at is not None
        assert opp.deadline is not None
        assert opp.published_at.tzinfo is not None
        assert opp.published_at.utcoffset().total_seconds() == 5 * 3600
        assert opp.deadline >= opp.published_at
        assert isinstance(opp.deadline, datetime)

    def test_rows_keep_source_notice_type(self, normalized) -> None:
        opp, _ = normalized
        assert opp.procurement_type
        assert opp.location is None
        assert opp.province is None
        assert opp.category is None
        assert opp.description is None


class TestRunOffline:
    def test_run_stores_both_fixture_pages(self, tmp_path) -> None:
        from storage import Storage

        adapter = GbPpraAdapter(offline=True)
        db = Storage(tmp_path / "gb.db")
        try:
            result = adapter.run(storage=db)
            assert result.found == 20
            assert result.created == 20
            assert result.failed == 0
            stored = db.conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities"
            ).fetchone()
            assert stored["n"] == 20
        finally:
            db.close()

    def test_offline_without_fixtures_raises(self, monkeypatch, tmp_path) -> None:
        import adapters.gb_ppra as mod

        monkeypatch.setattr(mod, "FIXTURES_DIR", tmp_path)
        adapter = GbPpraAdapter(offline=True)
        with pytest.raises(FileNotFoundError, match="gb_viewall"):
            adapter.fetch_listings()
        assert OFFLINE_LISTING_GLOB == "gb_viewall*.html"


class TestNoDetailPagesYet:
    def test_detail_enrichment_is_unsupported(self, adapter) -> None:
        assert adapter.supports_detail_enrichment() is False

    def test_extract_documents_returns_the_notice_pdf(self, adapter) -> None:
        row = _parse(adapter, "gb_viewall_page1.html")[0]
        docs = adapter.extract_documents(row)
        assert len(docs) == 1
        assert docs[0].startswith(f"{BASE_URL}/storage/proc_files/")
        assert re.search(r"\.pdf$", docs[0])

    def test_listing_path_constant(self) -> None:
        assert LISTING_PATH == "/viewall"
        assert re.search(r"^https://", BASE_URL)