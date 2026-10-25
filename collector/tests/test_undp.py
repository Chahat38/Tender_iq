"""Offline tests for the (unregistered) UNDP listing parser.

The source itself is not wired into the collector: its robots.txt is
``Disallow: /`` (see the adapter docstring). These tests keep the parser
honest so it can be enabled the day UNDP exposes a crawlable feed.
No network access is performed.
"""

from __future__ import annotations

import pytest
from adapters.undp import BASE_URL, UndpAdapter, _parse_date
from models import RawListing

PAKISTAN_ROW = """
<a class="vacanciesTableLink" href="/view_negotiation.cfm?nego_id=1234">
  <div class="vacanciesTable__cell">
    <div class="vacanciesTable__cell__label">Title</div>
    <span>Supply of laptops for country office</span>
  </div>
  <div class="vacanciesTable__cell">
    <div class="vacanciesTable__cell__label">Ref No</div>
    <span>UNDP-PAK-00123</span>
  </div>
  <div class="vacanciesTable__cell">
    <div class="vacanciesTable__cell__label">UNDP Office/Country</div>
    <span>UNDP-PAK/PAKISTAN</span>
  </div>
  <div class="vacanciesTable__cell">
    <div class="vacanciesTable__cell__label">Process</div>
    <span>Individual Consultant</span>
  </div>
  <div class="vacanciesTable__cell">
    <div class="vacanciesTable__cell__label">Deadline</div>
    <span>01-Nov-26 03:00 AM (New York time)</span>
  </div>
  <div class="vacanciesTable__cell">
    <div class="vacanciesTable__cell__label">Posted</div>
    <span>15-Oct-26</span>
  </div>
</a>
"""

KENYA_ROW = """
<a class="vacanciesTableLink" href="/view_negotiation.cfm?nego_id=9999">
  <div class="vacanciesTable__cell">
    <div class="vacanciesTable__cell__label">Title</div>
    <span>Supply of vehicles</span>
  </div>
  <div class="vacanciesTable__cell">
    <div class="vacanciesTable__cell__label">UNDP Office/Country</div>
    <span>UNDP-KEN/KENYA</span>
  </div>
</a>
"""


@pytest.fixture()
def adapter() -> UndpAdapter:
    return UndpAdapter(offline=True)


def test_pakistan_rows_are_kept_and_others_skipped(adapter) -> None:
    listings = adapter._parse_listing_page(PAKISTAN_ROW + KENYA_ROW)
    assert len(listings) == 1
    listing = listings[0]
    assert listing.external_id == "1234"
    assert listing.url == f"{BASE_URL}/view_negotiation.cfm?nego_id=1234"
    assert listing.raw_data is not None
    assert listing.raw_data["title"] == "Supply of laptops for country office"
    assert listing.raw_data["reference_number"] == "UNDP-PAK-00123"
    assert listing.raw_data["office"] == "UNDP-PAK/PAKISTAN"
    assert listing.raw_data["posted_text"] == "15-Oct-26"


def test_deadline_is_converted_from_new_york_to_karachi() -> None:
    parsed = _parse_date("01-Nov-26 03:00 AM (New York time)")
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 11, 1)
    assert parsed.utcoffset().total_seconds() == 5 * 3600


def test_posted_date_is_karachi_midnight() -> None:
    parsed = _parse_date("15-Oct-26")
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 10, 15)
    assert parsed.hour == 0
    assert parsed.utcoffset().total_seconds() == 5 * 3600


def test_unparseable_date_returns_none() -> None:
    assert _parse_date("not a date") is None
    assert _parse_date(None) is None


def test_normalize_sets_pakistan_source_fields(adapter) -> None:
    listing = RawListing(
        external_id="1234",
        url=f"{BASE_URL}/view_negotiation.cfm?nego_id=1234",
        raw_data={
            "title": "Supply of laptops for country office",
            "reference_number": "UNDP-PAK-00123",
            "office": "UNDP-PAK/PAKISTAN",
            "process": "Individual Consultant",
            "deadline_text": "01-Nov-26 03:00 AM (New York time)",
            "posted_text": "15-Oct-26",
        },
    )
    normalized = adapter.normalize(listing)
    assert normalized.source_name == "UNDP"
    assert normalized.organization == "UNDP Pakistan"
    assert normalized.location == "Pakistan"
    assert normalized.department == "UNDP"
    assert normalized.procurement_type == "Individual Consultant"
    assert normalized.reference_number == "UNDP-PAK-00123"
    assert normalized.deadline is not None
    assert normalized.deadline.utcoffset().total_seconds() == 5 * 3600


def test_parser_ignores_links_without_a_notice_id(adapter) -> None:
    html = PAKISTAN_ROW.replace("nego_id=1234", "something_else=1")
    assert adapter._parse_listing_page(html) == []
