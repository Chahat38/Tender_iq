"""Tests for the pydantic models and content_hash."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from models import NormalizedOpportunity, RawListing

DEADLINE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def make_opportunity(**overrides):
    """Build a NormalizedOpportunity with deterministic defaults."""
    fields = dict(
        title="Supply of laptops",
        organization="Punjab Information Technology Board",
        description="Procurement of 500 laptops.",
        source_url="https://example.gov.pk/tenders/123",
        source_name="Example Source",
        published_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        deadline=DEADLINE,
        location="Lahore",
        category=None,
        procurement_type="goods",
        reference_number="T-2026-123",
        document_urls=["https://example.gov.pk/tenders/123/tor.pdf"],
        external_id="123",
    )
    fields.update(overrides)
    return NormalizedOpportunity(**fields)


class TestContentHash:
    def test_stable_for_identical_content(self) -> None:
        first = make_opportunity()
        second = make_opportunity()
        assert first.content_hash() == second.content_hash()

    def test_changes_when_deadline_changes(self) -> None:
        base = make_opportunity()
        moved = make_opportunity(deadline=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc))
        assert base.content_hash() != moved.content_hash()

    def test_changes_when_deadline_removed(self) -> None:
        base = make_opportunity()
        no_deadline = make_opportunity(deadline=None)
        assert base.content_hash() != no_deadline.content_hash()

    def test_independent_of_document_order(self) -> None:
        urls = ["https://example.gov.pk/a.pdf", "https://example.gov.pk/b.pdf"]
        first = make_opportunity(document_urls=list(urls))
        second = make_opportunity(document_urls=list(reversed(urls)))
        assert first.content_hash() == second.content_hash()

    def test_changes_when_description_changes(self) -> None:
        base = make_opportunity()
        changed = make_opportunity(description="Procurement of 600 laptops.")
        assert base.content_hash() != changed.content_hash()

    def test_changes_when_title_changes(self) -> None:
        base = make_opportunity()
        changed = make_opportunity(title="Supply of desktop computers")
        assert base.content_hash() != changed.content_hash()


class TestModelValidation:
    def test_missing_values_default_to_none(self) -> None:
        opp = NormalizedOpportunity(
            source_url="https://example.gov.pk/t/1",
            source_name="Example Source",
            external_id="1",
        )
        assert opp.title is None
        assert opp.deadline is None
        assert opp.organization is None
        assert opp.reference_number is None
        assert opp.document_urls == []

    def test_accepts_missing_dates(self) -> None:
        opp = make_opportunity(published_at=None, deadline=None)
        assert opp.published_at is None
        assert opp.deadline is None

    def test_rejects_non_http_source_url(self) -> None:
        with pytest.raises(ValidationError):
            make_opportunity(source_url="ftp://example.gov.pk/t/1")

    def test_rejects_relative_source_url(self) -> None:
        with pytest.raises(ValidationError):
            make_opportunity(source_url="/tenders/123")

    def test_raw_listing_rejects_bad_url(self) -> None:
        with pytest.raises(ValidationError):
            RawListing(external_id="1", url="example.gov.pk/t/1")

    def test_raw_listing_keeps_raw_payload(self) -> None:
        listing = RawListing(
            external_id="1",
            url="https://example.gov.pk/t/1",
            raw_html="<html></html>",
            raw_data={"id": 1},
        )
        assert listing.raw_html == "<html></html>"
        assert listing.raw_data == {"id": 1}
