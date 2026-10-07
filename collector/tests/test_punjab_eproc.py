"""Punjab eProcurement adapter tests (offline, saved fixtures)."""

from __future__ import annotations

import pytest
from adapters.punjab_eproc import (
    BASE_URL,
    PROVINCE,
    SOURCE_NAME,
    PunjabEprocAdapter,
    _external_id_from_href,
    _pager_target,
    _webforms_state,
)


@pytest.fixture()
def adapter() -> PunjabEprocAdapter:
    return PunjabEprocAdapter(offline=True)


def _parse(adapter: PunjabEprocAdapter) -> list:
    return adapter._parse_listing_page(
        adapter.read_fixture("punjab_eproc_listing_p1.html")
    )


class TestListingParse:
    def test_parses_all_50_rows(self, adapter) -> None:
        # "1660 items in 34 pages" at 50 rows/page is the live grid.
        assert len(_parse(adapter)) == 50

    def test_first_data_row_ground_truth(self, adapter) -> None:
        first = _parse(adapter)[0]
        assert first.external_id == "0210202603173644331803256952"
        assert first.raw_data["title"] == "OSR 2026-27"
        assert first.raw_data["procurement_title"] == "Corrigendum"
        assert first.raw_data["type"] == "Work"
        assert first.raw_data["published_text"] == "02 Oct 2026"
        assert first.raw_data["closing_text"] == "06 Oct 2026"
        assert first.raw_data["organization"].startswith(
            "Executive Engineer Highway Division Bahawalnagar"
        )

    def test_external_ids_are_unique(self, adapter) -> None:
        rows = _parse(adapter)
        ids = [row.external_id for row in rows]
        assert len(set(ids)) == len(ids)

    def test_every_row_has_a_notice_document(self, adapter) -> None:
        rows = _parse(adapter)
        docs = [row.raw_data["document_urls"] for row in rows]
        assert all(docs), "each verified row links a Tender Notice PDF"
        assert all(url.startswith(f"{BASE_URL}/") for urls in docs for url in urls)

    def test_unexpected_grid_raises(self, adapter) -> None:
        html = (
            "<html><body>__VIEWSTATE value='x'<table id='grid'>"
            "<tr><th>Nope</th></tr></table></body></html>"
        )
        with pytest.raises(ValueError, match="not found; refusing to guess"):
            adapter._parse_listing_page(html)

    def test_maintenance_page_is_empty_not_an_error(self, adapter) -> None:
        assert adapter._parse_listing_page("<html><body>Server Error</body></html>") == []


class TestHelpers:
    def test_external_id_from_notice_href(self) -> None:
        href = "Tenders/50485054/4948/0210202603173644331803256952.pdf"
        assert _external_id_from_href(href) == "0210202603173644331803256952"

    def test_external_id_none_for_non_pdf(self) -> None:
        assert _external_id_from_href("Tenders/50485054/4948/page.aspx") is None
        assert _external_id_from_href(None) is None

    def test_webforms_state_captured(self, adapter) -> None:
        html = adapter.read_fixture("punjab_eproc_listing_p1.html")
        state = _webforms_state(html)
        assert state is not None
        assert sorted(state) == [
            "__EVENTVALIDATION",
            "__VIEWSTATE",
            "__VIEWSTATEGENERATOR",
        ]

    def test_pager_target_for_page_2(self, adapter) -> None:
        html = adapter.read_fixture("punjab_eproc_listing_p1.html")
        target = _pager_target(html, 2)
        assert target == (
            "ctl00$ContentPlaceHolderSRIS$rdgrdManageTender$ctl00$ctl03$ctl01$ctl07"
        )

    def test_pager_target_absent_page_is_none(self, adapter) -> None:
        html = adapter.read_fixture("punjab_eproc_listing_p1.html")
        assert _pager_target(html, 999) is None


class TestNormalize:
    @pytest.fixture()
    def normalized(self, adapter):
        return adapter.normalize(_parse(adapter)[0])

    def test_identity_fields(self, normalized, adapter) -> None:
        assert normalized.source_name == SOURCE_NAME
        assert normalized.province == PROVINCE
        assert normalized.external_id == "0210202603173644331803256952"
        assert normalized.source_url == f"{BASE_URL}/Admin_Tender_Search.aspx"

    def test_type_is_lowercased_and_dates_parsed(self, normalized) -> None:
        assert normalized.procurement_type == "work"
        assert normalized.published_at is not None
        assert normalized.deadline is not None
        assert normalized.deadline > normalized.published_at
        assert normalized.deadline.tzinfo is not None

    def test_stated_gaps_stay_none(self, normalized) -> None:
        assert normalized.reference_number is None
        assert normalized.location is None
        assert normalized.category is None
        assert normalized.description is None

    def test_documents_are_the_notice_and_bidding_pdfs(self, normalized) -> None:
        assert len(normalized.document_urls) == 2
        assert "/Tenders/" in normalized.document_urls[0]
        assert "/BiddingDocuments/" in normalized.document_urls[1]


class TestRunOffline:
    def test_run_stores_the_fixture_page(self, tmp_path) -> None:
        from storage import Storage

        adapter = PunjabEprocAdapter(offline=True)
        db = Storage(tmp_path / "punjab.db")
        try:
            result = adapter.run(storage=db)
            assert result.found == 50
            assert result.created == 50
            assert result.failed == 0
            stored = db.conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities"
            ).fetchone()
            assert stored["n"] == 50
        finally:
            db.close()

    def test_offline_without_fixtures_raises(self, monkeypatch, tmp_path) -> None:
        import adapters.punjab_eproc as mod

        monkeypatch.setattr(mod, "FIXTURES_DIR", tmp_path)
        adapter = PunjabEprocAdapter(offline=True)
        with pytest.raises(FileNotFoundError, match="punjab_eproc_listing"):
            adapter.fetch_listings()
