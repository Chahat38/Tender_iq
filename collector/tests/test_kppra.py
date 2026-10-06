"""KPPRA active-tenders adapter tests (offline, saved fixtures)."""

from __future__ import annotations

from datetime import datetime

import pytest
from adapters.kppra import (
    PAGE_PATH_TEMPLATE,
    PROVINCE,
    REF_URL_TEMPLATE,
    SOURCE_NAME,
    KppraActiveTendersAdapter,
)

#: First row of the saved fixture (verified against the live page).
FIXTURE_FIRST = {
    "external_id": "33282",
    "title": "Construction of Shelter Homes for patient attendants etc",
    "organization": "DHQ Teaching  Hospital, Kohat",
    "advertised": (2026, 10, 6),
    "closing": (2026, 10, 24),
}


@pytest.fixture()
def adapter() -> KppraActiveTendersAdapter:
    return KppraActiveTendersAdapter(offline=True)


class TestListingParse:
    def test_parses_all_25_rows(self, adapter) -> None:
        listings = adapter._parse_listing_page(
            adapter.read_fixture("kppra_listing_p1.html")
        )
        assert len(listings) == 25  # "Showing 1 - 25 of 225"

    def test_first_row_values(self, adapter) -> None:
        listings = adapter._parse_listing_page(
            adapter.read_fixture("kppra_listing_p1.html")
        )
        first = listings[0]
        assert first.external_id == FIXTURE_FIRST["external_id"]
        assert first.raw_data["description"] == FIXTURE_FIRST["title"]
        assert FIXTURE_FIRST["organization"] in first.raw_data["organization"]
        assert first.raw_data["advertised"] == "06-Oct-2026"
        assert first.raw_data["closing"] == "24-Oct-2026"

    def test_hidden_corrigendum_rows_are_skipped(self, adapter) -> None:
        import re

        html = adapter.read_fixture("kppra_listing_p1.html")
        # 25 visible data rows + 25 hidden corrigendum rows
        # (<tr style="display:none; ..."> id="item_temp_section_...">).
        hidden = re.findall(r'<tr style="display:none[^"]*"', html)
        assert len(hidden) == 25
        listings = adapter._parse_listing_page(html)
        assert len(listings) == 25

    def test_document_hrefs_are_absolute(self, adapter) -> None:
        listings = adapter._parse_listing_page(
            adapter.read_fixture("kppra_listing_p1.html")
        )
        with_pdf = [row for row in listings if row.raw_data["document_hrefs"]]
        assert with_pdf, "fixture contains tender-PDF download links"
        href = with_pdf[0].raw_data["document_hrefs"][0]
        assert href.startswith(
            "http://kppra.gov.pk/kppra/staff/force_download.php?file="
        )

    def test_external_id_and_url_are_the_search_link(self, adapter) -> None:
        listings = adapter._parse_listing_page(
            adapter.read_fixture("kppra_listing_p1.html")
        )
        first = listings[0]
        assert first.external_id == FIXTURE_FIRST["external_id"]
        assert first.url == (
            "http://kppra.gov.pk/kppra/activetenders.php"
            "?tender_ref=33282&search=Search"
        )

    def test_page_url_uses_observed_pager_pattern(self) -> None:
        assert PAGE_PATH_TEMPLATE.format(page=3) == (
            "/kppra/activetenders.php/?&sort=&order=&&p=3"
        )
        assert REF_URL_TEMPLATE.format(ref="33282").endswith(
            "tender_ref=33282&search=Search"
        )

    def test_missing_headers_raise(self, adapter) -> None:
        html = "<table><thead><tr><th>Something Else</th></tr></thead></table>"
        with pytest.raises(ValueError, match="Unexpected KPPRA listing"):
            adapter._parse_listing_page(html)

    def test_page_without_table_is_empty(self, adapter) -> None:
        assert adapter._parse_listing_page("<html><body>maintenance</body></html>") == []


class TestNormalize:
    @pytest.fixture()
    def normalized(self, adapter):
        listings = adapter._parse_listing_page(
            adapter.read_fixture("kppra_listing_p1.html")
        )
        return adapter.normalize(listings[0])

    def test_identity_fields(self, normalized) -> None:
        assert normalized.external_id == "33282"
        assert normalized.reference_number == "33282"
        assert normalized.source_name == SOURCE_NAME
        assert normalized.source_url.startswith("http://kppra.gov.pk/")

    def test_dates_are_karachi_midnight(self, normalized) -> None:
        assert normalized.published_at == datetime(2026, 10, 6, tzinfo=normalized.published_at.tzinfo)
        assert normalized.deadline == datetime(2026, 10, 24, tzinfo=normalized.deadline.tzinfo)
        assert normalized.published_at.utcoffset().total_seconds() == 5 * 3600
        assert normalized.deadline.hour == 0
        assert normalized.title == FIXTURE_FIRST["title"]

    def test_province_is_kp_and_unstated_fields_are_none(self, normalized) -> None:
        assert normalized.province == PROVINCE
        assert normalized.location is None
        assert normalized.category is None
        assert normalized.procurement_type is None
        assert normalized.description is None

    def test_documents_pass_through(self, adapter) -> None:
        listings = adapter._parse_listing_page(
            adapter.read_fixture("kppra_listing_p1.html")
        )
        normalized = adapter.normalize(listings[0])
        assert normalized.document_urls == listings[0].raw_data["document_hrefs"]
        assert adapter.extract_documents(listings[0]) == normalized.document_urls


class TestRunOffline:
    def test_run_stores_every_row(self, adapter, tmp_path) -> None:
        from storage import Storage

        db = Storage(tmp_path / "kppra.db")
        try:
            result = adapter.run(storage=db)
            assert result.found == 25
            assert result.created == 25
            assert result.failed == 0
            stored = db.conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities"
            ).fetchone()
            assert stored["n"] == 25
        finally:
            db.close()

    def test_offline_without_fixtures_raises(self, monkeypatch, tmp_path) -> None:
        import adapters.kppra as mod

        monkeypatch.setattr(mod, "FIXTURES_DIR", tmp_path)
        adapter = KppraActiveTendersAdapter(offline=True)
        with pytest.raises(FileNotFoundError, match="kppra_listing"):
            adapter.fetch_listings()


class TestNoDetailPages:
    def test_detail_enrichment_is_unsupported(self, adapter) -> None:
        assert adapter.supports_detail_enrichment() is False

    def test_fetch_opportunity_not_implemented(self, adapter) -> None:
        with pytest.raises(NotImplementedError):
            adapter.fetch_opportunity("33282")
