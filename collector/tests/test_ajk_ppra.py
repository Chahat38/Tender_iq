"""AJ&K PPRA adapter tests (offline, saved fixtures)."""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from adapters.ajk_ppra import (
    BASE_URL,
    LISTING_PATH,
    OFFLINE_LISTING_GLOB,
    SOURCE_NAME,
    AjkPpraAdapter,
)


@pytest.fixture()
def adapter() -> AjkPpraAdapter:
    return AjkPpraAdapter(offline=True)


def _parse(adapter: AjkPpraAdapter, page: str):
    return adapter._parse_listing_page(adapter.read_fixture(page))


class TestListingParse:
    def test_parses_the_latest_advertisements_page(self, adapter) -> None:
        rows = _parse(adapter, "ajk_advertisements.html")
        assert len(rows) >= 40

    def test_row_fields(self, adapter) -> None:
        first = _parse(adapter, "ajk_advertisements.html")[0]
        data = first.raw_data
        assert first.external_id.isdigit()
        assert data["title"] and len(data["title"]) > 10
        assert data["department"]
        assert data["procuring_agency"]
        assert data["publishing_date"] and data["closing_date"]
        assert data["notice_type"]
        assert data["document_urls"], "every row has a Download File link"

    def test_ids_are_unique_per_row(self, adapter) -> None:
        rows = _parse(adapter, "ajk_advertisements.html")
        ids = [row.external_id for row in rows]
        assert len(set(ids)) == len(ids)

    def test_source_url_joins_the_download_pdf(self, adapter) -> None:
        first = _parse(adapter, "ajk_advertisements.html")[0]
        assert first.url.startswith(f"{BASE_URL}/uploadfiles/")
        assert first.url.endswith(".pdf")

    def test_missing_headers_raise(self, adapter) -> None:
        html = "<table><tr><th>Nope</th></tr></table>"
        with pytest.raises(ValueError, match="Unexpected AJ&K PPRA listing"):
            adapter._parse_listing_page(html)

    def test_page_without_table_is_empty(self, adapter) -> None:
        assert adapter._parse_listing_page("<html><body>maintenance</body></html>") == []


class TestNormalize:
    @pytest.fixture()
    def normalized(self, adapter):
        first = _parse(adapter, "ajk_advertisements.html")[0]
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
        assert opp.procurement_type in (
            "Tender Notice",
            "Prequalification Notice",
            "Sales/Auction/Disposal",
        )
        assert opp.location is None
        assert opp.province is None
        assert opp.category is None
        assert opp.description is None

    def test_notice_pdf_is_both_url_and_document(self, normalized) -> None:
        opp, _ = normalized
        assert len(opp.document_urls) == 1
        assert opp.document_urls[0] == opp.source_url


class TestRunOffline:
    def test_run_stores_every_advertisement(self, tmp_path) -> None:
        from storage import Storage

        adapter = AjkPpraAdapter(offline=True)
        db = Storage(tmp_path / "ajk.db")
        try:
            result = adapter.run(storage=db)
            assert result.found >= 40
            assert result.created == result.found
            assert result.failed == 0
            stored = db.conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities"
            ).fetchone()
            assert stored["n"] == result.found
        finally:
            db.close()

    def test_offline_without_fixtures_raises(self, monkeypatch, tmp_path) -> None:
        import adapters.ajk_ppra as mod

        monkeypatch.setattr(mod, "FIXTURES_DIR", tmp_path)
        adapter = AjkPpraAdapter(offline=True)
        with pytest.raises(FileNotFoundError, match="ajk_advertisements"):
            adapter.fetch_listings()
        assert OFFLINE_LISTING_GLOB == "ajk_advertisements*.html"


class TestNoDetailPagesYet:
    def test_detail_enrichment_is_unsupported(self, adapter) -> None:
        assert adapter.supports_detail_enrichment() is False

    def test_extract_documents_returns_the_notice_pdf(self, adapter) -> None:
        row = _parse(adapter, "ajk_advertisements.html")[0]
        docs = adapter.extract_documents(row)
        assert len(docs) == 1
        assert docs[0].startswith(f"{BASE_URL}/uploadfiles/")
        assert re.search(r"\.pdf$", docs[0])

    def test_listing_path_constant(self) -> None:
        assert LISTING_PATH == "/advertisements.php"
        assert re.search(r"^https://", BASE_URL)