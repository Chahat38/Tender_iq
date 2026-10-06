"""Tests for Stage 7: detail-page enrichment in the collector run.

All tests run offline on saved fixtures (two listing pages, three
detail pages whose tender numbers exist in the listing fixtures). No
network access happens in this module.
"""

import pytest
from datetime import datetime

from adapters.base import SourceAdapter
from adapters.ppra_epms import PPRAEpmsAdapter
from main import main as cli_main
from models import NormalizedOpportunity, RawListing
from storage import Storage

#: Tender numbers that have both a listing row and a detail fixture.
DETAIL_FIXTURE_IDS = ("TS0000015263E", "TS0000015253E", "TS0000014189E")
#: Detail-page procurement types observed on those fixtures (ground truth
#: asserted so a fixture change fails loudly instead of silently).
DETAIL_TYPES = {
    "TS0000015263E": "goods",
    "TS0000015253E": "consultancy services",
    "TS0000014189E": "works",
}


@pytest.fixture(scope="module")
def adapter() -> PPRAEpmsAdapter:
    return PPRAEpmsAdapter(offline=True)


def make_listing(**overrides) -> NormalizedOpportunity:
    """A listing-level normalized opportunity with deterministic values."""
    fields = dict(
        title="Supply of medical equipment for district hospital",
        organization="Punjab Information Technology Board",
        description="Procurement of ventilators and MRI machines.",
        source_url="https://example.gov.pk/tenders/123",
        source_name="Example Source",
        published_at=None,
        deadline=None,
        location="Lahore",
        category="Health",
        procurement_type=None,
        reference_number=None,
        document_urls=[],
        external_id="123",
    )
    fields.update(overrides)
    return NormalizedOpportunity(**fields)


class TestMergeListingWithDetail:
    def test_detail_fills_missing_fields(self) -> None:
        listing = make_listing()
        detail = make_listing(procurement_type="goods", reference_number="R-1")
        merged = SourceAdapter.merge_listing_with_detail(listing, detail)
        assert merged.procurement_type == "goods"
        assert merged.reference_number == "R-1"

    def test_listing_values_are_never_overwritten(self) -> None:
        listing = make_listing(deadline=None, title="Listing title")
        detail = make_listing(
            deadline=datetime(2026, 11, 1, 12, 0),
            title="Different detail title",
            organization="Different org",
        )
        merged = SourceAdapter.merge_listing_with_detail(listing, detail)
        # non-None listing values survive...
        assert merged.title == "Listing title"
        assert merged.organization == "Punjab Information Technology Board"
        # ...while a missing listing field is filled from the detail page
        assert merged.deadline == datetime(2026, 11, 1, 12, 0)

    def test_empty_string_fields_are_filled_too(self) -> None:
        listing = make_listing(location="")
        detail = make_listing(location="Multan")
        merged = SourceAdapter.merge_listing_with_detail(listing, detail)
        assert merged.location == "Multan"

    def test_document_urls_are_unioned_in_order(self) -> None:
        listing = make_listing(document_urls=["https://example.gov.pk/a.pdf"])
        detail = make_listing(
            document_urls=[
                "https://example.gov.pk/a.pdf",
                "https://example.gov.pk/b.pdf",
            ]
        )
        merged = SourceAdapter.merge_listing_with_detail(listing, detail)
        assert merged.document_urls == [
            "https://example.gov.pk/a.pdf",
            "https://example.gov.pk/b.pdf",
        ]

    def test_inputs_are_not_mutated(self) -> None:
        listing = make_listing()
        detail = make_listing(procurement_type="works")
        SourceAdapter.merge_listing_with_detail(listing, detail)
        assert listing.procurement_type is None
        assert detail.procurement_type == "works"

    def test_identity_mismatch_is_refused(self) -> None:
        listing = make_listing()
        detail = make_listing(source_url="https://example.gov.pk/tenders/999")
        with pytest.raises(ValueError, match="source_url"):
            SourceAdapter.merge_listing_with_detail(listing, detail)

    def test_mismatched_external_id_is_refused(self) -> None:
        listing = make_listing()
        detail = make_listing(external_id="999")
        with pytest.raises(ValueError, match="external_id"):
            SourceAdapter.merge_listing_with_detail(listing, detail)


class TestSupportsDetailEnrichment:
    def test_ppra_adapter_supports_it(self, adapter) -> None:
        assert adapter.supports_detail_enrichment() is True

    def test_bare_adapter_does_not(self) -> None:
        class Bare(SourceAdapter):
            source_name = "Bare"
            base_url = "https://example.gov.pk"

            def fetch_listings(self, since=None):
                return []

            def normalize(self, raw):
                raise NotImplementedError

            def extract_documents(self, raw):
                return []

        assert Bare(offline=True).supports_detail_enrichment() is False


class TestEnrichedRun:
    @pytest.fixture()
    def storage(self, tmp_path):
        storage = Storage(tmp_path / "enrich.db")
        yield storage
        storage.close()

    def test_run_offline_enriches_rows_with_detail_fixtures(
        self, adapter, storage
    ) -> None:
        result = adapter.run(storage=storage, enrich_details=True)
        assert result.found >= 100
        assert result.created == result.found
        assert result.failed == 0
        # 3 tenders have detail fixtures; the rest fail offline detail
        # fetches -- isolated, counted, never blocking the listing data.
        assert result.detail_failures == result.found - len(DETAIL_FIXTURE_IDS)
        assert result.enriched == len(DETAIL_FIXTURE_IDS)

        rows = storage.conn.execute(
            "SELECT external_id, procurement_type FROM opportunities"
        ).fetchall()
        by_id = {row["external_id"]: row["procurement_type"] for row in rows}
        for ref, expected_type in DETAIL_TYPES.items():
            assert by_id[ref] == expected_type, ref
        # rows without a detail fixture keep the listing-level None
        enriched_count = sum(1 for t in by_id.values() if t is not None)
        assert enriched_count == len(DETAIL_FIXTURE_IDS)

    def test_enriched_rows_have_documents_in_storage(
        self, adapter, storage
    ) -> None:
        adapter.run(storage=storage, enrich_details=True)
        for ref in DETAIL_FIXTURE_IDS:
            row = storage.conn.execute(
                "SELECT id FROM opportunities WHERE external_id = ?", (ref,)
            ).fetchone()
            docs = storage.get_documents(row["id"])
            assert len(docs) == 2, ref
            assert all(
                doc["url"].startswith("https://epms.ppra.gov.pk/pdf?file=")
                for doc in docs
            )

    def test_second_enriched_run_is_stable(self, adapter, storage) -> None:
        adapter.run(storage=storage, enrich_details=True)
        second = adapter.run(storage=storage, enrich_details=True)
        assert second.created == 0
        assert second.updated == 0
        assert second.failed == 0
        # same isolation as the first run: 97 rows have no detail fixture
        assert second.detail_failures == second.found - len(DETAIL_FIXTURE_IDS)
        # every detail fixture still counts as an enrichment on re-run
        assert second.enriched == len(DETAIL_FIXTURE_IDS)

    def test_enrichment_off_by_default(self, adapter, storage) -> None:
        result = adapter.run(storage=storage)
        assert result.enriched == 0
        assert result.detail_failures == 0
        types = storage.conn.execute(
            "SELECT procurement_type FROM opportunities WHERE "
            "procurement_type IS NOT NULL"
        ).fetchall()
        assert types == []

    def test_detail_failure_is_isolated(
        self, adapter, storage, monkeypatch
    ) -> None:
        # The two other fixture tenders enrich normally; one fails.
        original = adapter.fetch_opportunity

        def flaky(ref: str) -> RawListing:
            if ref == "TS0000015253E":
                raise FileNotFoundError(f"detail page vanished: {ref}")
            return original(ref)

        monkeypatch.setattr(adapter, "fetch_opportunity", flaky)
        result = adapter.run(storage=storage, enrich_details=True)
        # 97 rows without fixtures + the flaky one = 98 isolated failures
        assert result.detail_failures == result.found - len(DETAIL_FIXTURE_IDS) + 1
        assert result.enriched == len(DETAIL_FIXTURE_IDS) - 1
        assert any("TS0000015253E detail:" in e for e in result.errors)
        # the failed tender's listing-level data is still stored
        row = storage.conn.execute(
            "SELECT procurement_type FROM opportunities WHERE external_id = ?",
            ("TS0000015253E",),
        ).fetchone()
        assert row is not None
        assert row["procurement_type"] is None

    def test_missing_detail_fixtures_count_as_isolated_failures(
        self, adapter, storage
    ) -> None:
        # Offline fetch_opportunity raises FileNotFoundError for tenders
        # without a fixture. The run must count them as detail failures.
        result = adapter.run(storage=storage, enrich_details=True)
        missing = result.found - len(DETAIL_FIXTURE_IDS)
        assert result.detail_failures == missing
        # ...but the run itself still succeeds (failed == 0).
        assert result.failed == 0
        assert result.errors  # one error entry per missing detail fixture

    def test_offline_run_without_enrichment_makes_zero_detail_requests(
        self, adapter, storage, monkeypatch
    ) -> None:
        calls: list[str] = []

        def spy(ref: str) -> RawListing:
            calls.append(ref)
            raise AssertionError("fetch_opportunity must not be called")

        monkeypatch.setattr(adapter, "fetch_opportunity", spy)
        adapter.run(storage=storage)
        assert calls == []


class TestDetailEnrichmentParsing:
    def test_normalize_detail_matches_listing_for_fixture_ids(
        self, adapter
    ) -> None:
        listings = {raw.external_id: raw for raw in adapter.fetch_listings()}
        for ref in DETAIL_FIXTURE_IDS:
            listing = adapter.normalize(listings[ref])
            detail_raw = adapter.fetch_opportunity(ref)
            detail = adapter.normalize_detail(detail_raw)
            merged = SourceAdapter.merge_listing_with_detail(listing, detail)
            # identity fields survive the merge
            assert merged.source_url == listing.source_url
            assert merged.external_id == ref
            assert merged.source_name == "PPRA EPMS"
            # detail fills procurement_type and documents
            assert merged.procurement_type == DETAIL_TYPES[ref]
            assert len(merged.document_urls) == 2
            # listing values are never lost
            assert merged.title == listing.title
            assert merged.organization == listing.organization
            assert merged.deadline == listing.deadline

    def test_merged_description_comes_from_listing(self, adapter) -> None:
        # Detail fixtures carry no description block; the listing snippet
        # must survive the merge.
        listings = {raw.external_id: raw for raw in adapter.fetch_listings()}
        ref = DETAIL_FIXTURE_IDS[0]
        listing = adapter.normalize(listings[ref])
        detail = adapter.normalize_detail(adapter.fetch_opportunity(ref))
        merged = SourceAdapter.merge_listing_with_detail(listing, detail)
        assert merged.description == listing.description


class TestCli:
    def test_cli_enrich_flag_reports_counters(self, tmp_path, capsys) -> None:
        db = tmp_path / "cli.db"
        code = cli_main(
            [
                "--offline",
                "--db",
                str(db),
                "--enrich-details",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Enriched:  3" in out
        assert "Details failed: 97" in out  # 100 rows, 3 detail fixtures

    def test_cli_without_flag_prints_no_enrichment_lines(
        self, tmp_path, capsys
    ) -> None:
        db = tmp_path / "cli.db"
        code = cli_main(["--offline", "--db", str(db)])
        out = capsys.readouterr().out
        assert code == 0
        assert "Enriched:" not in out
        assert "Details failed:" not in out
