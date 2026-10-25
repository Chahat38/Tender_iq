"""Stage 9 filter tests: category, organization and province facets.

Storage-level: :meth:`Storage.list_opportunities` /
:meth:`Storage.count_opportunities` and the new facet helpers.
API-level (via the existing harness elsewhere in this directory): the
expanded GET/POST parameters answer exactly the rows the storage layer
selects, and the facet endpoints list lowercase distinct values.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from models import NormalizedOpportunity
from storage import Storage


def _opp(**overrides) -> NormalizedOpportunity:
    """A deterministic opportunity with overridable filter fields."""
    fields = dict(
        title="Supply of laptops",
        organization="Punjab Information Technology Board",
        source_url="https://example.gov.pk/tenders/123",
        source_name="Filter Source",
        deadline=datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc),
        location="Lahore",
        category="Health",
        province="Punjab",
        external_id="123",
    )
    fields.update(overrides)
    return NormalizedOpportunity(**fields)


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "filters.db")
    yield st
    st.close()


@pytest.fixture()
def source_id(storage) -> int:
    return storage.get_or_create_source("Filter Source", "filter")


@pytest.fixture()
def three_rows(storage, source_id) -> None:
    """Punjab/Health/Lahore, Sindh/Education/Karachi, Punjab/Road works."""
    storage.upsert_opportunity(
        _opp(
            category="Health",
            province="Punjab",
            sector="IT & Telecom",
            department="Information Technology",
            organization="Punjab Information Technology Board",
            external_id="101",
        ),
        source_id,
    )
    storage.upsert_opportunity(
        _opp(
            category="Education",
            province="Sindh",
            sector="Water & Sanitation",
            department="Water & Sanitation",
            organization="Karachi Water and Sewerage Board",
            external_id="102",
        ),
        source_id,
    )
    storage.upsert_opportunity(
        _opp(
            category=None,
            province="Punjab",
            sector="Construction & Works",
            department="Works & Services",
            organization="Executive Engineer Highway Division",
            description="Rehabilitation of the highway and drainage network",
            external_id="103",
        ),
        source_id,
    )


class TestStorageFilters:
    def test_category_exact_case_insensitive(
        self, storage, source_id, three_rows
    ) -> None:
        rows = storage.list_opportunities(category="health")
        assert [row["external_id"] for row in rows] == ["101"]
        assert storage.count_opportunities(category="HEALTH") == 1

    def test_category_without_value_matches_nothing(
        self, storage, source_id, three_rows
    ) -> None:
        # category IS NULL rows are excluded by the exact-match filter.
        assert storage.count_opportunities(category="roads") == 0

    def test_province_exact_case_insensitive(
        self, storage, source_id, three_rows
    ) -> None:
        rows = storage.list_opportunities(province="punjab")
        assert {row["external_id"] for row in rows} == {"101", "103"}
        assert storage.count_opportunities(province="Sindh") == 1

    def test_sector_exact_case_insensitive(
        self, storage, source_id, three_rows
    ) -> None:
        rows = storage.list_opportunities(sector="it & telecom")
        assert [row["external_id"] for row in rows] == ["101"]
        assert storage.count_opportunities(sector="IT & TELECOM") == 1

    def test_department_exact_case_insensitive(
        self, storage, source_id, three_rows
    ) -> None:
        rows = storage.list_opportunities(department="information technology")
        assert [row["external_id"] for row in rows] == ["101"]
        assert storage.count_opportunities(department="Works & Services") == 1

    def test_location_matches_title_organization_and_description(
        self, storage, source_id, three_rows
    ) -> None:
        # The stored location is Lahore for every row, so these matches can
        # only come from the broadened location filter.
        assert [
            row["external_id"]
            for row in storage.list_opportunities(location="information technology")
        ] == ["101"]
        assert [
            row["external_id"]
            for row in storage.list_opportunities(location="highway")
        ] == ["103"]
        assert [
            row["external_id"]
            for row in storage.list_opportunities(location="drainage")
        ] == ["103"]
        assert {
            row["external_id"]
            for row in storage.list_opportunities(location="laptops")
        } == {"101", "102", "103"}
        assert storage.list_opportunities(location="no-such-token") == []

    def test_organization_substring_case_insensitive(
        self, storage, source_id, three_rows
    ) -> None:
        rows = storage.list_opportunities(organization="water")
        assert [row["external_id"] for row in rows] == ["102"]
        assert storage.count_opportunities(
            organization="information technology"
        ) == 1

    def test_filters_combine_with_and(
        self, storage, source_id, three_rows
    ) -> None:
        rows = storage.list_opportunities(
            province="punjab", category="health", organization="information technology"
        )
        assert [row["external_id"] for row in rows] == ["101"]

    def test_no_filters_return_everything(
        self, storage, source_id, three_rows
    ) -> None:
        assert storage.count_opportunities() == 3

    def test_distinct_categories_facet(
        self, storage, source_id, three_rows
    ) -> None:
        assert storage.list_distinct_categories() == ["education", "health"]

    def test_distinct_provinces_facet(
        self, storage, source_id, three_rows
    ) -> None:
        assert storage.list_distinct_provinces() == ["punjab", "sindh"]

    def test_distinct_sector_and_department_facets(
        self, storage, source_id, three_rows
    ) -> None:
        assert storage.list_distinct_sectors() == [
            "construction & works",
            "it & telecom",
            "water & sanitation",
        ]
        assert storage.list_distinct_departments() == [
            "information technology",
            "water & sanitation",
            "works & services",
        ]

    def test_facets_ignore_null_and_empty(
        self, storage, source_id
    ) -> None:
        storage.upsert_opportunity(_opp(category=None, province=None), source_id)
        storage.upsert_opportunity(
            _opp(category="", province="", external_id="104"), source_id
        )
        assert storage.list_distinct_categories() == []
        assert storage.list_distinct_provinces() == []


class TestApiFilterEndpoints:
    """FastAPI surface: same filters via GET params and POST body."""

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import api

        db_path = tmp_path / "api.db"
        storage = Storage(db_path)
        source_id = storage.get_or_create_source("Filter Source", "filter")
        storage.upsert_opportunity(
            _opp(
                category="Health",
                province="Punjab",
                sector="IT & Telecom",
                department="Information Technology",
                organization="Punjab Information Technology Board",
                external_id="201",
            ),
            source_id,
        )
        storage.upsert_opportunity(
            _opp(
                category="Education",
                province="Sindh",
                sector="Water & Sanitation",
                department="Water & Sanitation",
                organization="Karachi Water and Sewerage Board",
                external_id="202",
            ),
            source_id,
        )
        storage.close()
        monkeypatch.setenv("TENDERSCOUT_DB_PATH", str(db_path))
        app = api.create_app(db_path)
        with TestClient(app) as test_client:
            yield test_client

    def test_get_category_and_province(self, client) -> None:
        body = client.get(
            "/opportunities",
            params={"category": "education", "province": "sindh"},
        ).json()
        assert [row["external_id"] for row in body["opportunities"]] == ["202"]
        assert body["total"] == 1

    def test_get_organization_substring(self, client) -> None:
        body = client.get(
            "/opportunities", params={"organization": "technology"}
        ).json()
        assert [row["external_id"] for row in body["opportunities"]] == ["201"]

    def test_post_query_accepts_new_filters(self, client) -> None:
        body = client.post(
            "/opportunities/query",
            json={"province": "Punjab", "category": "Health"},
        ).json()
        assert [row["external_id"] for row in body["opportunities"]] == ["201"]

    def test_get_sector_and_department(self, client) -> None:
        body = client.get(
            "/opportunities",
            params={"sector": "it & telecom", "department": "information technology"},
        ).json()
        assert [row["external_id"] for row in body["opportunities"]] == ["201"]
        assert body["total"] == 1

    def test_post_query_accepts_sector_and_department(self, client) -> None:
        body = client.post(
            "/opportunities/query",
            json={"sector": "Water & Sanitation"},
        ).json()
        assert [row["external_id"] for row in body["opportunities"]] == ["202"]

    def test_categories_facet_endpoint(self, client) -> None:
        assert client.get("/categories").json() == ["education", "health"]

    def test_provinces_facet_endpoint(self, client) -> None:
        assert client.get("/provinces").json() == ["punjab", "sindh"]

    def test_sector_and_department_facet_endpoints(self, client) -> None:
        assert client.get("/sectors").json() == [
            "it & telecom",
            "water & sanitation",
        ]
        assert client.get("/departments").json() == [
            "information technology",
            "water & sanitation",
        ]

    def test_include_all_facets_complete_the_dropdowns(self, client) -> None:
        provinces = client.get("/provinces?include_all=true").json()
        assert provinces[:3] == ["Punjab", "Sindh", "Khyber Pakhtunkhwa"]
        assert "Azad Jammu & Kashmir" in provinces

        cities = client.get("/locations?include_all=true").json()
        assert "Karachi" in cities
        assert "Lahore" in cities
        assert len(cities) > 300
        # Stored free text is folded onto the canonical name, not repeated.
        assert cities.count("Lahore") == 1

    def test_categories_endpoint_is_listed_in_root(self, client) -> None:
        endpoints = client.get("/").json()["endpoints"]
        assert "categories" in endpoints
        assert "provinces" in endpoints
