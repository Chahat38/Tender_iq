"""EPADS adapter tests (offline, saved fixtures)."""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from adapters.epads import OFFLINE_LISTING_GLOB, SOURCE_NAME, EpadsAdapter

_REF_RE = re.compile(r"^P\d+$")


@pytest.fixture()
def adapter() -> EpadsAdapter:
    return EpadsAdapter(offline=True)


def _parse(adapter: EpadsAdapter, page: str):
    return adapter._parse_listing_page(adapter.read_fixture(page))


class TestListingParse:
    def test_parses_all_100_rows_per_page(self, adapter) -> None:
        assert len(_parse(adapter, "epads_listing_p1.html")) == 100
        assert len(_parse(adapter, "epads_listing_p2.html")) == 100

    def test_row_fields(self, adapter) -> None:
        first = _parse(adapter, "epads_listing_p1.html")[0]
        data = first.raw_data
        assert first.external_id.isdigit()
        assert data["reference_number"] == f"P{first.external_id}"
        assert data["title"] and len(data["title"]) > 20
        assert data["organization"]
        assert data["published_text"] and data["closing_text"]
        assert data["procurement_type"]

    def test_reference_badge_is_p_plus_id_on_every_row(self, adapter) -> None:
        for page in ("epads_listing_p1.html", "epads_listing_p2.html"):
            for row in _parse(adapter, page):
                assert row.raw_data["reference_number"] == f"P{row.external_id}"
                assert _REF_RE.match(row.raw_data["reference_number"])

    def test_source_url_is_the_detail_page(self, adapter) -> None:
        first = _parse(adapter, "epads_listing_p1.html")[0]
        assert first.url == (
            f"https://epads.gov.pk/opportunities/federal/procurements/"
            f"{first.external_id}"
        )

    def test_missing_headers_raise(self, adapter) -> None:
        html = "<table><thead><tr><th>Nope</th></tr></thead></table>"
        with pytest.raises(ValueError, match="Unexpected EPADS listing"):
            adapter._parse_listing_page(html)

    def test_page_without_table_is_empty(self, adapter) -> None:
        assert adapter._parse_listing_page("<html><body>maintenance</body></html>") == []

    def test_rows_without_detail_link_are_skipped(self, adapter) -> None:
        html = """
        <table><thead><tr>
          <th>#</th><th>Procurement Title</th><th>Procurement Status</th>
          <th>Type/Procedure</th><th>Details</th>
        </tr></thead><tbody>
          <tr><td>1</td><td>maintenance row</td></tr>
        </tbody></table>
        """
        assert adapter._parse_listing_page(html) == []


class TestNormalize:
    @pytest.fixture()
    def normalized(self, adapter):
        first = _parse(adapter, "epads_listing_p1.html")[0]
        return adapter.normalize(first), first

    def test_identity_fields(self, normalized) -> None:
        opp, raw = normalized
        assert opp.external_id == raw.external_id
        assert opp.reference_number == f"P{raw.external_id}"
        assert opp.source_name == SOURCE_NAME
        assert opp.source_url == raw.url

    def test_timestamps_are_karachi(self, normalized) -> None:
        opp, _ = normalized
        assert opp.published_at is not None
        assert opp.deadline is not None
        assert opp.published_at.tzinfo is not None
        assert opp.published_at.utcoffset().total_seconds() == 5 * 3600
        # Source format: "Monday, September 14, 2026 02:18 PM"
        assert opp.deadline.year >= 2026
        assert isinstance(opp.deadline, datetime)

    def test_federal_rows_have_no_province(self, normalized) -> None:
        opp, _ = normalized
        assert opp.province is None
        assert opp.location is None
        assert opp.category is None
        assert opp.description is None
        assert opp.procurement_type  # kept from the type badge

    def test_titles_keep_full_text_from_title_attribute(self, adapter) -> None:
        rows = _parse(adapter, "epads_listing_p1.html")
        visible_first = adapter.read_fixture("epads_listing_p1.html")
        # The visible span text is truncated; the parsed title must be
        # the full title= attribute value when both exist.
        for row in rows[:10]:
            title = row.raw_data["title"]
            assert "..." not in title


class TestRunOffline:
    def test_run_stores_both_fixture_pages(self, tmp_path) -> None:
        from storage import Storage

        adapter = EpadsAdapter(offline=True)
        db = Storage(tmp_path / "epads.db")
        try:
            result = adapter.run(storage=db)
            assert result.found == 200
            # The two saved pages overlap on 4 ids (live data shifted
            # slightly between saves); the rest are distinct.
            assert result.created == 196
            assert result.failed == 0
            stored = db.conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities"
            ).fetchone()
            assert stored["n"] == 196
        finally:
            db.close()

    def test_offline_without_fixtures_raises(self, monkeypatch, tmp_path) -> None:
        import adapters.epads as mod

        monkeypatch.setattr(mod, "FIXTURES_DIR", tmp_path)
        adapter = EpadsAdapter(offline=True)
        with pytest.raises(FileNotFoundError, match="epads_listing"):
            adapter.fetch_listings()
        assert OFFLINE_LISTING_GLOB == "epads_listing*.html"


class TestNoDetailPagesYet:
    def test_detail_enrichment_is_unsupported(self, adapter) -> None:
        assert adapter.supports_detail_enrichment() is False

    def test_extract_documents_is_empty(self, adapter) -> None:
        assert adapter.extract_documents(_parse(adapter, "epads_listing_p1.html")[0]) == []
