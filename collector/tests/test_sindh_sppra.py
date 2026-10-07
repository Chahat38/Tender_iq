"""SPPRA Sindh adapter tests (offline, saved fixtures)."""

from __future__ import annotations

from datetime import datetime

import pytest
from adapters.sindh_sppra import (
    PAGE_PARAM,
    PROVINCE,
    SOURCE_NAME,
    SindhSppraAdapter,
)


@pytest.fixture()
def adapter() -> SindhSppraAdapter:
    return SindhSppraAdapter(offline=True)


def _parse(adapter: SindhSppraAdapter, page: str):
    return adapter._parse_listing_page(adapter.read_fixture(page))


class TestListingParse:
    def test_parses_both_fixture_pages(self, adapter) -> None:
        assert len(_parse(adapter, "sindh_listing_p1.html")) == 15
        assert len(_parse(adapter, "sindh_listing_p2.html")) == 15

    def test_first_row_ground_truth(self, adapter) -> None:
        first = _parse(adapter, "sindh_listing_p1.html")[0]
        assert first.external_id == "37834"
        assert first.raw_data["title"].startswith("PROJECT DIRECTOR MALIR")
        assert first.raw_data["organization"] == (
            "Government of Sindh Local Government Department"
        )
        assert first.raw_data["advertise_date"] == "2019-07-31 00:00:00"
        assert first.raw_data["close_date"] == "2019-11-01 23:59:59"

    def test_serial_is_the_external_key_and_unique(self, adapter) -> None:
        rows = _parse(adapter, "sindh_listing_p1.html") + _parse(
            adapter, "sindh_listing_p2.html"
        )
        ids = [row.external_id for row in rows]
        assert all(i.isdigit() for i in ids)
        assert len(set(ids)) == len(ids)

    def test_row_url_uses_the_site_search_filter(self, adapter) -> None:
        first = _parse(adapter, "sindh_listing_p1.html")[0]
        assert first.url == (
            "http://e.pprasindh.gov.pk/tenderlst"
            "?tender_list%5Bfilters%5D%5BSerial-eq%5D=37834"
        )

    def test_document_hrefs_are_absolute(self, adapter) -> None:
        first = _parse(adapter, "sindh_listing_p1.html")[0]
        docs = first.raw_data["document_urls"]
        assert docs, "the first fixture row carries tender ZIP links"
        assert all(url.startswith("http://") for url in docs)

    def test_unexpected_structure_raises(self, adapter) -> None:
        html = "<table id='tender_list'><tr><th>Nope</th></tr></table>"
        with pytest.raises(ValueError, match="Unexpected SPPRA Sindh"):
            adapter._parse_listing_page(html)

    def test_page_without_table_is_empty(self, adapter) -> None:
        assert adapter._parse_listing_page("<html><body>maintenance</body></html>") == []


class TestNormalize:
    @pytest.fixture()
    def normalized(self, adapter):
        first = _parse(adapter, "sindh_listing_p1.html")[0]
        return adapter.normalize(first)

    def test_identity_fields(self, normalized) -> None:
        assert normalized.source_name == SOURCE_NAME
        assert normalized.reference_number == normalized.external_id
        assert normalized.title

    def test_dates_are_karachi_and_western(self, normalized) -> None:
        assert normalized.published_at == datetime(
            2019, 7, 31, 0, 0, tzinfo=normalized.published_at.tzinfo
        )
        assert normalized.deadline is not None
        row_tz = normalized.published_at.tzinfo
        assert row_tz.utcoffset(normalized.published_at).total_seconds() == 5 * 3600
        assert normalized.deadline > normalized.published_at

    def test_province_is_sindh_and_stated_gaps_stay_none(
        self, normalized
    ) -> None:
        assert normalized.province == PROVINCE
        assert normalized.category is None
        assert normalized.procurement_type is None
        assert normalized.location is None
        assert normalized.description is None

    def test_documents_flow_through(self, normalized) -> None:
        assert normalized.document_urls
        assert all(u.endswith(".zip") for u in normalized.document_urls)


class TestPaging:
    def test_page_url_template_is_the_observed_pager_param(self) -> None:
        assert PAGE_PARAM == "tender_list[page]"

    def test_run_offline_stores_both_fixture_pages(self, tmp_path) -> None:
        from storage import Storage

        adapter = SindhSppraAdapter(offline=True)
        db = Storage(tmp_path / "sindh.db")
        try:
            result = adapter.run(storage=db)
            assert result.found == 30
            assert result.created == 30
            assert result.failed == 0
            stored = db.conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities"
            ).fetchone()
            assert stored["n"] == 30
        finally:
            db.close()

    def test_offline_without_fixtures_raises(self, monkeypatch, tmp_path) -> None:
        import adapters.sindh_sppra as mod

        monkeypatch.setattr(mod, "FIXTURES_DIR", tmp_path)
        adapter = SindhSppraAdapter(offline=True)
        with pytest.raises(FileNotFoundError, match="sindh_listing"):
            adapter.fetch_listings()
