"""Offline tests for the PIA tenders adapter."""

import pytest
from adapters.pia import (
    BASE_URL,
    LISTING_PATH,
    PiaAdapter,
    _parse_date,
)
from models import RawListing


@pytest.fixture
def adapter() -> PiaAdapter:
    return PiaAdapter(offline=True)


def _parse(adapter: PiaAdapter):
    return adapter._parse_listing_page(adapter.read_fixture("pia_tenders_p1.html"))


def test_source_identifiers():
    assert PiaAdapter.source_name == "PIA Tenders"
    assert PiaAdapter.base_url == BASE_URL


def test_rows_exclusive_and_unique(adapter):
    rows = _parse(adapter)
    assert len(rows) >= 10
    ids = [r.external_id for r in rows]
    assert len(ids) == len(set(ids))
    assert all(r.external_id.startswith("PIA-") for r in rows)


def test_first_row_details(adapter):
    rows = _parse(adapter)
    first = rows[0]
    assert first.external_id == "PIA-927"
    assert first.url == f"{BASE_URL}/corporate/business-with-pia/tenders/927"
    data = first.raw_data
    assert "Cutlery Pack" in data["title"]
    assert data["organization"] == "Supply Chain Management"
    assert data["published_text"] == "2026-10-05"
    assert data["closing_text"] == "2026-10-19"


def test_normalize_maps_fields(adapter):
    opp = adapter.normalize(_parse(adapter)[0])
    assert opp.external_id == "PIA-927"
    assert opp.source_name == "PIA Tenders"
    assert "Cutlery Pack" in opp.title
    assert opp.organization == "Supply Chain Management"
    assert opp.published_at is not None and opp.deadline is not None
    assert opp.published_at.year == 2026
    assert opp.source_url.endswith("tenders/927")
    assert opp.document_urls == []


def test_date_parsing_timezone_aware():
    parsed = _parse_date("2026-10-05")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.month == 10 and parsed.day == 5


def test_offline_fetch_from_fixtures(adapter):
    rows = adapter.fetch_listings()
    assert len(rows) >= 10
    assert all(isinstance(r, RawListing) for r in rows)


def test_listing_path():
    assert "tenders" in LISTING_PATH


@pytest.mark.parametrize("bad", [None, "", "   ", "2026-13-40"])
def test_bad_dates_are_none(bad):
    assert _parse_date(bad) is None