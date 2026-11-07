"""POF adapter tests (offline, saved fixtures).

The official POF host (pof.gov.pk) answers a Cloudflare challenge, so the
adapter reads POF's own rows from the public PPRA EPMS listing. These
tests cover that parse + filter path without touching the network.
"""

from __future__ import annotations

import pytest
from adapters.pof import (
    KEYWORD,
    SOURCE_NAME,
    PofAdapter,
)
from adapters.ppra_epms import TENDER_TYPES
from models import RawListing


@pytest.fixture()
def adapter() -> PofAdapter:
    return PofAdapter(offline=True)


class TestListing:
    def test_offline_fixture_yields_only_pof_rows(self, adapter: PofAdapter) -> None:
        rows = adapter.fetch_listings()
        assert len(rows) == 3
        assert all("Pakistan Ordnance Factories" in row.raw_data["organization"] for row in rows)
        assert all(row.external_id.startswith("TS") for row in rows)
        assert all(raw.url.startswith("https://epms.ppra.gov.pk/") for raw in rows)

    def test_non_pof_page_is_filtered_out(self, adapter: PofAdapter) -> None:
        html = adapter.read_fixture("ppra_card_layout.html")
        rows = adapter._parse_listing_page(html)
        assert rows, "sanity: the card-layout fixture must parse"
        assert [raw for raw in rows if adapter.is_pof(raw)] == []

    def test_listing_url_uses_the_keyword_box(self, adapter: PofAdapter) -> None:
        url = adapter._listing_url(2, "3")
        assert url.startswith("https://epms.ppra.gov.pk/public/tenders/active-tenders?")
        assert "keyword=Pakistan+Ordnance+Factories" in url
        assert "tender_type=3" in url
        assert url.endswith("&page=2")
        assert KEYWORD == "Pakistan Ordnance Factories"
        assert len(TENDER_TYPES) == 4


class TestFilter:
    @pytest.mark.parametrize(
        "organization,title,expected",
        [
            ("Pakistan Ordnance Factories (POF)", "INVITATION TO BID", True),
            (None, "PAKISTAN ORDNANCE FACTORIES POF SANJWAL", True),
            ("Pakistan Ordnance Factory", None, True),
            ("Central Ordnance Depot", "KRD-4", False),
            ("NUST, SMME", "Procurement of materials", False),
            (None, None, False),
        ],
    )
    def test_is_pof(
        self,
        organization: str | None,
        title: str | None,
        expected: bool,
    ) -> None:
        raw = RawListing(
            external_id="TS0000000000E",
            url="https://epms.ppra.gov.pk/public/tenders/tender-details/TS0000000000E",
            raw_data={"organization": organization, "title": title},
        )
        assert PofAdapter.is_pof(raw) is expected


class TestNormalize:
    def test_maps_the_common_schema(self, adapter: PofAdapter) -> None:
        opportunity = adapter.normalize(adapter.fetch_listings()[0])
        assert opportunity.source_name == SOURCE_NAME == "POF Tenders"
        assert opportunity.organization == "Pakistan Ordnance Factories (POF)"
        assert opportunity.title
        assert opportunity.source_url.startswith("https://epms.ppra.gov.pk/")
        assert opportunity.published_at is not None
        assert opportunity.published_at.year == 2026
        assert opportunity.deadline is not None
        assert opportunity.deadline.tzinfo is not None
        assert opportunity.location

    def test_detail_enrichment_is_supported(self, adapter: PofAdapter) -> None:
        """POF detail pages are EPMS detail pages, so enrichment works."""
        assert adapter.supports_detail_enrichment()
