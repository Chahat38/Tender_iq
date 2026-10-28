"""Offline tests for the deterministic taxonomy enrichment.

These cover the closed vocabularies used to fill the city/province,
sector and department browse filters: exact-name matching, the rule order
(first match wins), never overwriting an adapter-supplied value, and the
storage backfill path.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from models import NormalizedOpportunity
from storage import Storage
from taxonomy import (
    CITY_PROVINCE,
    all_cities,
    backfill,
    derive_taxonomy,
    find_city,
    find_province,
    normalize_province,
    provinces_for_filter,
)


class TestGeography:
    def test_canonical_province_list_is_complete(self) -> None:
        provinces = provinces_for_filter()
        assert len(provinces) == 7
        assert "Punjab" in provinces
        assert "Sindh" in provinces
        assert "Khyber Pakhtunkhwa" in provinces
        assert "Balochistan" in provinces
        assert "Gilgit-Baltistan" in provinces
        assert "Azad Jammu & Kashmir" in provinces
        assert "Islamabad Capital Territory" in provinces

    def test_city_list_covers_every_province(self) -> None:
        cities = all_cities()
        assert len(cities) > 150
        for city in (
            "Karachi",
            "Lahore",
            "Peshawar",
            "Quetta",
            "Gilgit",
            "Muzaffarabad",
            "Islamabad",
        ):
            assert city in cities
            assert CITY_PROVINCE[city] in provinces_for_filter()

    def test_province_aliases_are_folded(self) -> None:
        assert normalize_province("punjab") == "Punjab"
        assert normalize_province("KPK") == "Khyber Pakhtunkhwa"
        assert normalize_province("AJK") == "Azad Jammu & Kashmir"
        assert normalize_province("ICT") == "Islamabad Capital Territory"
        assert normalize_province("Punjab, Pakistan") == "Punjab"
        assert normalize_province("not a province") is None

    def test_city_matching_is_whole_word(self) -> None:
        assert find_city("Construction of a road in Lahore") == "Lahore"
        assert find_city("water tank cleaning services") is None
        # \"Mirpur Khas\" must win over the shorter \"Mirpur\".
        assert find_city("Tender for Mirpur Khas district") == "Mirpur Khas"

    def test_province_matching_uses_aliases(self) -> None:
        assert find_province("Government of Sindh") == "Sindh"
        assert find_province("no jurisdiction here") is None


class TestDerivation:
    def test_it_procurement_gets_sector_and_department(self) -> None:
        changes = derive_taxonomy(
            title="Supply of laptops and network switches",
            organization="Punjab Information Technology Board",
        )
        assert changes == {
            "province": "Punjab",
            "sector": "IT & Telecom",
            "department": "Information Technology",
            "category": "Equipment & IT Infrastructure",
        }

    def test_city_implies_province(self) -> None:
        changes = derive_taxonomy(title="Construction of boundary wall, Quetta")
        assert changes["location"] == "Quetta"
        assert changes["province"] == "Balochistan"
        assert changes["sector"] == "Construction & Works"

    def test_existing_values_are_never_overwritten(self) -> None:
        changes = derive_taxonomy(
            title="Supply of laptops",
            organization="Punjab Information Technology Board",
            location="Karachi",
            province="Sindh",
            sector="Works",
            department="Custom Department",
            category="Equipments",
        )
        assert changes == {}

    def test_nothing_derivable_returns_empty(self) -> None:
        assert derive_taxonomy(title="Quarterly progress meeting minutes") == {}

    def test_business_area_categories_are_derived(self) -> None:
        cases = {
            "Supply of 100 laptops and docking stations": (
                "Equipment & IT Infrastructure"
            ),
            "Consultancy services for software development": "Software Consultancy",
            "Recruitment and manpower services for contract staff": (
                "HR & Consultancy"
            ),
            "Security guards and CCTV surveillance services": (
                "Security & Surveillance"
            ),
        }
        for title, expected in cases.items():
            assert derive_taxonomy(title=title)["category"] == expected

    def test_category_is_not_derived_from_unrelated_text(self) -> None:
        assert "category" not in derive_taxonomy(
            title="Construction of boundary wall, Quetta"
        )
        assert "category" not in derive_taxonomy(
            title="Supply of medical equipment"
        )

    def test_stored_category_is_never_overwritten(self) -> None:
        changes = derive_taxonomy(
            title="Supply of laptops and CCTV cameras",
            category="Equipments",
        )
        assert "category" not in changes

    def test_undp_is_its_own_department(self) -> None:
        changes = derive_taxonomy(
            title="Consultant for Financial Audit Services",
            organization="UNDP Pakistan",
        )
        assert changes["department"] == "UNDP"
        assert changes["sector"] == "Consulting & Professional Services"


class TestBackfill:
    @pytest.fixture()
    def storage(self, tmp_path):
        st = Storage(tmp_path / "taxonomy.db")
        yield st
        st.close()

    def test_backfill_fills_missing_fields(self, storage) -> None:
        source_id = storage.get_or_create_source("Example Source", "example")
        storage.upsert_opportunity(
            NormalizedOpportunity(
                title="Supply of laptops",
                organization="Punjab Information Technology Board",
                source_url="https://example.gov.pk/tenders/1",
                source_name="Example Source",
                deadline=datetime(2099, 1, 1, tzinfo=timezone.utc),
                external_id="1",
            ),
            source_id,
        )
        assert backfill(storage) == 1
        row = storage.conn.execute(
            "SELECT province, sector, department FROM opportunities"
        ).fetchone()
        assert row["province"] == "Punjab"
        assert row["sector"] == "IT & Telecom"
        assert row["department"] == "Information Technology"

    def test_backfill_is_idempotent(self, storage) -> None:
        source_id = storage.get_or_create_source("Example Source", "example")
        storage.upsert_opportunity(
            NormalizedOpportunity(
                title="Supply of laptops",
                organization="Punjab Information Technology Board",
                source_url="https://example.gov.pk/tenders/1",
                source_name="Example Source",
                external_id="1",
            ),
            source_id,
        )
        assert backfill(storage) == 1
        assert backfill(storage) == 0
