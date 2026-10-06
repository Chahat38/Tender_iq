"""Offline tests for the PPRA EPMS adapter. Only saved fixtures are used."""

from datetime import datetime, timedelta

import pytest
from adapters.ppra_epms import (
    BASE_URL,
    KARACHI_TZ,
    PPRAEpmsAdapter,
)
from models import RawListing
from storage import Storage


@pytest.fixture(scope="module")
def adapter() -> PPRAEpmsAdapter:
    return PPRAEpmsAdapter(offline=True)


@pytest.fixture(scope="module")
def listings(adapter) -> list[RawListing]:
    return adapter.fetch_listings()


@pytest.fixture(scope="module")
def normalized(adapter, listings) -> list:
    return [adapter.normalize(raw) for raw in listings]


class TestFetchListings:
    def test_offline_fixture_pages_yield_enough_listings(self, listings) -> None:
        assert len(listings) >= 50  # two saved pages x 50 rows

    def test_external_ids_are_unique_and_stable_format(self, listings) -> None:
        ids = [raw.external_id for raw in listings]
        assert len(set(ids)) == len(ids)
        assert all(id_.startswith(("TS", "TND")) for id_ in ids)

    def test_every_raw_listing_has_an_absolute_url(self, listings) -> None:
        for raw in listings:
            assert raw.url.startswith(f"{BASE_URL}/public/tenders/tender-details/")

    def test_row_data_keeps_source_text(self, listings) -> None:
        first = listings[0]
        assert first.raw_data["deadline_text"] == "Oct 21, 2026 11:00 AM"
        assert first.raw_data["published_text"] == "Oct 03, 2026"


class TestNormalize:
    def test_no_empty_titles(self, normalized) -> None:
        assert normalized
        for opportunity in normalized:
            assert opportunity.title
            assert opportunity.title == opportunity.title.strip()

    def test_source_url_is_https_detail_page(self, normalized) -> None:
        for opportunity in normalized:
            assert opportunity.source_url.startswith("https://")
            assert "/public/tenders/tender-details/" in opportunity.source_url

    def test_deadlines_are_datetimes_where_present(self, normalized) -> None:
        deadlines = [o.deadline for o in normalized]
        assert sum(d is not None for d in deadlines) >= 50
        assert all(d is None or isinstance(d, datetime) for d in deadlines)

    def test_published_dates_parsed(self, normalized) -> None:
        published = [o.published_at for o in normalized]
        assert sum(p is not None for p in published) >= 50
        assert all(isinstance(p, datetime) for p in published if p is not None)

    def test_datetimes_are_asia_karachi(self, normalized) -> None:
        sample = next(o for o in normalized if o.deadline is not None)
        assert sample.deadline.utcoffset() == timedelta(hours=5)
        assert sample.deadline.tzinfo is KARACHI_TZ or sample.deadline.tzinfo == KARACHI_TZ

    def test_organizations_present_and_stripped(self, normalized) -> None:
        for opportunity in normalized:
            assert opportunity.organization
            assert opportunity.organization == opportunity.organization.strip()

    def test_first_fixture_row_matches_known_values(self, normalized) -> None:
        first = next(
            o for o in normalized if o.external_id == "TS0000015263E"
        )
        assert first.title == "Re-tender for Maintenance Store Items SZHL FY \u2013 2026-27"
        assert first.organization == (
            "Ministry of National Health Services, Regulation & Coordination"
        )
        assert first.location == "Lahore"
        assert first.category == "Electrical Items"
        assert first.reference_number == "P89833"
        assert first.deadline == datetime(2026, 10, 21, 11, 0, tzinfo=KARACHI_TZ)
        assert first.published_at == datetime(2026, 10, 3, 0, 0, tzinfo=KARACHI_TZ)
        assert first.source_name == "PPRA EPMS"
        assert first.procurement_type is None  # only stated on detail pages

    def test_corrigendum_row_deadline_updated(self, normalized) -> None:
        row = next(o for o in normalized if o.external_id == "TS0000014189E")
        assert "Pre-Engineered Building" in row.title
        assert row.deadline == datetime(2026, 10, 9, 11, 0, tzinfo=KARACHI_TZ)

    def test_placeholder_reference_becomes_none(self, normalized) -> None:
        # Row "Repair & Maint of Transport" shows the placeholder "Tender
        # Notice" in the reference field; TenderScout must not copy it.
        row = next(o for o in normalized if o.external_id == "TS0000015267E")
        assert row.reference_number is None

    def test_reference_numbers_are_strings_when_present(self, normalized) -> None:
        present = [o.reference_number for o in normalized if o.reference_number]
        assert len(present) >= 50
        assert all(isinstance(r, str) and r.strip() for r in present)


class TestDetailPages:
    @pytest.fixture()
    def detail_raw(self, adapter) -> RawListing:
        return adapter.fetch_opportunity("TS0000015263E")

    def test_fetch_opportunity_offline_finds_fixture(self, detail_raw) -> None:
        assert "TS0000015263E" in detail_raw.raw_html
        assert detail_raw.url == (
            f"{BASE_URL}/public/tenders/tender-details/TS0000015263E"
        )

    def test_fetch_opportunity_unknown_ref_raises(self, adapter) -> None:
        with pytest.raises(FileNotFoundError):
            adapter.fetch_opportunity("TS0000000000X")

    def test_extract_documents_from_detail_page(self, adapter, detail_raw) -> None:
        docs = adapter.extract_documents(detail_raw)
        assert len(docs) == 2
        assert all(url.startswith(f"{BASE_URL}/pdf?file=") for url in docs)

    def test_extract_documents_empty_for_listing_rows(
        self, adapter, listings
    ) -> None:
        assert adapter.extract_documents(listings[0]) == []

    def test_normalize_detail_fills_richer_fields(self, adapter, detail_raw) -> None:
        opportunity = adapter.normalize_detail(detail_raw)
        assert opportunity.title == (
            "Re-tender for Maintenance Store Items SZHL FY \u2013 2026-27"
        )
        assert opportunity.procurement_type == "goods"
        assert opportunity.location == "Lahore"
        assert opportunity.reference_number == "P89833"
        assert opportunity.deadline == datetime(2026, 10, 21, 11, 0, tzinfo=KARACHI_TZ)
        assert opportunity.published_at == datetime(2026, 10, 3, 0, 0, tzinfo=KARACHI_TZ)
        assert len(opportunity.document_urls) == 2


class TestRunWithStorage:
    def test_run_offline_upserts_all_listings(self, adapter, tmp_path) -> None:
        storage = Storage(tmp_path / "test.db")
        try:
            result = adapter.run(storage=storage)
            assert result.found >= 50
            assert result.created == result.found
            assert result.failed == 0
            assert result.errors == []
            assert storage.count_opportunities() == result.found
        finally:
            storage.close()

    def test_second_run_is_all_unchanged(self, adapter, tmp_path) -> None:
        storage = Storage(tmp_path / "test.db")
        try:
            adapter.run(storage=storage)
            second = adapter.run(storage=storage)
            assert second.created == 0
            assert second.updated == 0
            assert second.failed == 0
            assert storage.count_opportunities() == second.found
        finally:
            storage.close()
