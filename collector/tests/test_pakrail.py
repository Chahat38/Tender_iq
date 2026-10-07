"""Offline tests for the Pakistan Railways tenders adapter."""

import pytest
from adapters.pakrail import (
    BASE_URL,
    LISTING_PATH,
    PakrailAdapter,
    _parse_date,
)
from models import RawListing


@pytest.fixture
def adapter() -> PakrailAdapter:
    return PakrailAdapter(offline=True)


def _parse(adapter: PakrailAdapter, page: str):
    return adapter._parse_listing_page(adapter.read_fixture(page))


def test_source_identifiers():
    assert PakrailAdapter.source_name == "Pakistan Railways"
    assert PakrailAdapter.base_url == BASE_URL


def test_listing_rows_exclusive_and_unique(adapter):
    for page in ("pakrail_tenders_p1.html", "pakrail_tenders_p2.html"):
        rows = _parse(adapter, page)
        assert len(rows) >= 30
        ids = [r.external_id for r in rows]
        assert len(ids) == len(set(ids)), "duplicate external ids on one page"
        assert all(r.external_id.startswith("PRL-") for r in rows)


def test_first_row_details(adapter):
    rows = _parse(adapter, "pakrail_tenders_p1.html")
    first = rows[0]
    assert first.external_id == "PRL-19328"
    assert first.url == f"{BASE_URL}/Tenders/TenderDetails.aspx?ID=19328"
    data = first.raw_data
    assert "SUPPLY OF DRY SAND" in data["title"]
    assert data["organization"] == "Managing Director, Concrete Sleeper Factories, Pakistan Railways"
    assert data["opening_text"] == "17 November, 2026"
    assert data["closing_text"] == "03 October, 2026"


def test_normalize_maps_fields(adapter):
    row = _parse(adapter, "pakrail_tenders_p1.html")[0]
    opp = adapter.normalize(row)
    assert opp.external_id == "PRL-19328"
    assert opp.source_name == "Pakistan Railways"
    assert "SUPPLY OF DRY SAND" in opp.title
    assert opp.organization.startswith("Managing Director")
    assert opp.deadline is not None and opp.published_at is not None
    assert opp.published_at.year == 2026 or opp.published_at.year == 2025
    assert opp.source_url.endswith("TenderDetails.aspx?ID=19328")
    assert opp.document_urls == []


def test_row_without_detail_link_skipped(adapter):
    rows = _parse(adapter, "pakrail_tenders_p1.html")
    for row in rows:
        assert "TenderDetails.aspx?ID=" in row.url


def test_date_parsing_timezone_aware():
    parsed = _parse_date("06 October, 2026")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.month == 10 and parsed.day == 6


def test_offline_fetch_concatenates_all_fixtures(adapter):
    rows = adapter.fetch_listings()
    assert len(rows) >= 60
    ids = [r.external_id for r in rows]
    assert len(ids) == len(set(ids))


def test_listing_path_points_at_tenders_archive():
    assert "index.aspx" in LISTING_PATH


@pytest.mark.parametrize("bad", [None, "", "    ", "17 November, 2026 garbage"])
def test_bad_dates_are_none(bad):
    assert _parse_date(bad) is None


def test_raw_is_rawlisting(adapter):
    rows = _parse(adapter, "pakrail_tenders_p1.html")
    assert all(isinstance(r, RawListing) for r in rows)