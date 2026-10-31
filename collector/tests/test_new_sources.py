"""Offline tests for the newly added public-source adapters.

Every test reads a saved fixture; no test touches the network.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from adapters.ndma import NdmaAdapter
from adapters.ptcl import PtclAdapter
from adapters.world_bank import WorldBankAdapter
from registry import COVERAGE_STATUSES, SOURCE_CATALOG


def test_requested_source_catalog_is_complete_and_valid() -> None:
    assert len(SOURCE_CATALOG) == 12
    assert len({item.key for item in SOURCE_CATALOG}) == 12
    assert all(item.status in COVERAGE_STATUSES for item in SOURCE_CATALOG)
    assert {item.requested_name for item in SOURCE_CATALOG} == {
        "Zong",
        "Jazz",
        "PTCL",
        "NDC",
        "C4I",
        "POF",
        "NDMA",
        "World Bank",
        "UNDP Pakistan",
        "KPPRA",
        "PPPPRA",
        "Federal PPRA",
    }


class TestPtcl:
    @pytest.fixture()
    def adapter(self) -> PtclAdapter:
        return PtclAdapter(offline=True)

    def test_listing_and_identity(self, adapter: PtclAdapter) -> None:
        rows = adapter.fetch_listings()
        assert [row.external_id for row in rows] == ["PTCL-4634", "PTCL-4620"]
        assert rows[0].url == "https://ptcl.com.pk/Home/TenderDetail/?ItemId=4634"
        assert "linkId" not in rows[0].url

    def test_normalize_date_range(self, adapter: PtclAdapter) -> None:
        opportunity = adapter.normalize(adapter.fetch_listings()[0])
        assert opportunity.title.startswith("Tender Notice")
        assert opportunity.organization == (
            "Pakistan Telecommunication Company Limited (PTCL)"
        )
        assert opportunity.published_at is not None
        assert opportunity.published_at.day == 21
        assert opportunity.deadline is not None
        assert opportunity.deadline.day == 31

    def test_detail_parser_finds_pdf(self) -> None:
        html = (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "ptcl_detail_4634.html"
        ).read_text(encoding="utf-8")
        data = PtclAdapter._parse_detail(html)
        assert "Re-construction" in data["title"]
        assert data["document_urls"] == [
            "https://ptcl.com.pk/images/tender/tendernotice-reconstruction.pdf"
        ]


class TestNdma:
    @pytest.fixture()
    def adapter(self) -> NdmaAdapter:
        return NdmaAdapter(offline=True)

    def test_evaluation_reports_are_skipped(self, adapter: NdmaAdapter) -> None:
        rows = adapter.fetch_listings()
        assert len(rows) == 2
        assert [row.external_id for row in rows] == [
            "NDMA-6abfa1a6ea2a9",
            "NDMA-6abfa1547fd64",
        ]
        assert all("evaluation report" not in (row.raw_data["title"] or "").lower() for row in rows)

    def test_normalize_uses_submission_deadline(self, adapter: NdmaAdapter) -> None:
        opportunity = adapter.normalize(adapter.fetch_listings()[0])
        assert opportunity.organization == (
            "National Disaster Management Authority (NDMA)"
        )
        assert opportunity.published_at is None
        assert opportunity.deadline is not None
        assert (opportunity.deadline.month, opportunity.deadline.day) == (10, 22)
        assert opportunity.document_urls
        assert opportunity.document_urls[0].endswith(".pdf")


class TestWorldBank:
    @pytest.fixture()
    def adapter(self) -> WorldBankAdapter:
        return WorldBankAdapter(offline=True)

    def test_awards_and_expired_notices_are_excluded(
        self, adapter: WorldBankAdapter
    ) -> None:
        rows = adapter.fetch_listings()
        assert [row.external_id for row in rows] == [
            "WB-OP00476983",
            "WB-OP00476984",
        ]

    def test_normalize_maps_official_fields(self, adapter: WorldBankAdapter) -> None:
        opportunity = adapter.normalize(adapter.fetch_listings()[0])
        assert opportunity.title == "Development of Scanning and Data Entry Application"
        assert opportunity.organization == "School Education Department, Govt of Punjab"
        assert opportunity.reference_number == "PK-SED PUNJAB-573936-CS-CQS"
        assert opportunity.procurement_type == "Invitation for Bids"
        assert opportunity.category == "Consultant Services"
        assert opportunity.published_at is not None
        assert opportunity.published_at.year == 2026
        assert opportunity.deadline is not None
        assert opportunity.deadline.year == 2030

    def test_deadline_date_is_kept_in_pakistan_local_calendar(
        self, adapter: WorldBankAdapter
    ) -> None:
        deadline = adapter.normalize(adapter.fetch_listings()[0]).deadline
        assert deadline is not None
        assert deadline.date() == datetime(2030, 11, 5).date()
        assert deadline.utcoffset() is not None