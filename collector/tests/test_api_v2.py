"""Tests for the dedup, filter, notification and AI-assist API additions.

Built on the same offline TestClient harness as test_api.py: a seeded
throwaway SQLite database, no network, no API keys. Duplicate groups are
written directly with SQL (the dedup worker owns that state; these tests
only exercise how the API *reads* it).
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from test_api import ClientHarness, make_opportunity

from api import create_app


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture()
def harness(tmp_path):
    """App + TestClient over a throwaway database."""
    db_path = tmp_path / "api_v2_test.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        yield ClientHarness(client, app.state.storage)


def _mark_duplicate(storage, sibling_id: int, group_id: str, primary_id: int) -> None:
    """Turn ``sibling_id`` into a non-primary copy of ``primary_id``."""
    storage.conn.execute(
        "UPDATE opportunities SET is_primary = 0, duplicate_group_id = ? "
        "WHERE id = ?",
        (group_id, sibling_id),
    )
    storage.conn.execute(
        "UPDATE opportunities SET duplicate_group_id = ? WHERE id = ?",
        (group_id, primary_id),
    )
    storage.conn.commit()


class TestDedupVisibleOnce:
    def test_list_hides_duplicates_by_default(self, harness) -> None:
        primary = harness.add(title="Supply of ventilators", external_id="T-1")
        sibling = harness.add(
            title="Supply of ventilators",
            source_name="Mirror Source",
            external_id="T-1-mirror",
        )
        _mark_duplicate(harness.storage, sibling, "group-1", primary)

        page = harness.client.get("/opportunities").json()
        assert page["total"] == 1
        assert [row["id"] for row in page["opportunities"]] == [primary]

    def test_list_include_duplicates_shows_all(self, harness) -> None:
        primary = harness.add(title="Supply of ventilators", external_id="T-1")
        sibling = harness.add(
            title="Supply of ventilators",
            source_name="Mirror Source",
            external_id="T-1-mirror",
        )
        _mark_duplicate(harness.storage, sibling, "group-1", primary)

        page = harness.client.get(
            "/opportunities?include_duplicates=1"
        ).json()
        assert page["total"] == 2

    def test_search_hides_duplicates_by_default(self, harness) -> None:
        primary = harness.add(title="Supply of ventilators", external_id="T-1")
        sibling = harness.add(
            title="Supply of ventilators",
            source_name="Mirror Source",
            external_id="T-1-mirror",
        )
        _mark_duplicate(harness.storage, sibling, "group-1", primary)

        search = harness.client.get("/search", params={"q": "ventilators"}).json()
        assert search["total"] == 1
        assert search["results"][0]["opportunity_id"] == primary

        both = harness.client.get(
            "/search", params={"q": "ventilators", "include_duplicates": 1}
        ).json()
        assert both["total"] == 2

    def test_duplicates_endpoint_lists_siblings(self, harness) -> None:
        primary = harness.add(title="Supply of ventilators", external_id="T-1")
        sibling = harness.add(
            title="Supply of ventilators",
            source_name="Mirror Source",
            external_id="T-1-mirror",
        )
        _mark_duplicate(harness.storage, sibling, "group-1", primary)

        siblings = harness.client.get(
            f"/opportunities/{primary}/duplicates"
        ).json()
        assert [row["id"] for row in siblings] == [sibling]
        primary_row = harness.client.get(f"/opportunities/{primary}").json()
        assert siblings[0]["source_id"] != primary_row["source_id"]
        assert siblings[0]["source_url"] == make_opportunity().source_url

    def test_duplicates_endpoint_single_group_primary_row_also_lists(
        self, harness
    ) -> None:
        primary = harness.add(title="Supply of ventilators", external_id="T-1")
        sibling = harness.add(
            title="Supply of ventilators",
            source_name="Mirror Source",
            external_id="T-1-mirror",
        )
        _mark_duplicate(harness.storage, sibling, "group-1", primary)

        from_sibling = harness.client.get(
            f"/opportunities/{sibling}/duplicates"
        ).json()
        assert [row["id"] for row in from_sibling] == [primary]

    def test_unknown_opportunity_duplicates_is_404(self, harness) -> None:
        assert (
            harness.client.get("/opportunities/999999/duplicates").status_code
            == 404
        )


class TestProcurementAndSort:
    def test_procurement_type_filter_is_case_insensitive(self, harness) -> None:
        harness.add(title="Road works", procurement_type="works", external_id="R1")
        other = harness.add(
            title="Consultancy study", procurement_type="Consultancy Services",
            external_id="C1",
        )
        page = harness.client.get(
            "/opportunities", params={"procurement_type": "CONSULTANCY SERVICES"}
        ).json()
        assert [row["id"] for row in page["opportunities"]] == [other]

    def test_procurement_types_facet_most_common_first(self, harness) -> None:
        harness.add(title="Works A", procurement_type="works", external_id="W1")
        harness.add(title="Works B", procurement_type="works", external_id="W2")
        harness.add(title="Goods A", procurement_type="goods", external_id="G1")
        types = harness.client.get("/procurement_types").json()
        assert "Works" in types or "works" in types
        assert len(types) >= 2

    def test_sort_closing_orders_by_soonest_deadline(self, harness) -> None:
        soon = _now() + timedelta(days=3)
        later = _now() + timedelta(days=30)
        later_id = harness.add(
            title="Later deadline", deadline=later, external_id="LATER"
        )
        soon_id = harness.add(
            title="Soon deadline", deadline=soon, external_id="SOON"
        )

        closing = harness.client.get(
            "/opportunities", params={"sort": "closing", "limit": 10}
        ).json()
        ids = [row["id"] for row in closing["opportunities"]]
        assert ids.index(soon_id) < ids.index(later_id)

        newest = harness.client.get(
            "/opportunities", params={"sort": "newest", "limit": 10}
        ).json()
        ids_newest = [row["id"] for row in newest["opportunities"]]
        assert ids_newest[0] == max(soon_id, later_id)


class TestNotifications:
    def test_notifications_returns_new_and_closing_buckets(self, harness) -> None:
        now = _now()
        harness.add(
            title="Fresh tender", published_at=now - timedelta(days=1),
            deadline=now + timedelta(days=5), external_id="FRESH",
        )
        harness.add(
            title="Old tender", published_at=now - timedelta(days=60),
            deadline=now + timedelta(days=50), external_id="OLD",
        )
        body = harness.client.get(
            "/notifications", params={"window_days": 7}
        ).json()
        assert body["window_days"] == 7
        assert body["new_total"] >= 1
        assert [row["external_id"] for row in body["new_tenders"]] == ["FRESH"]
        assert body["closing_total"] >= 1
        assert any(
            row["external_id"] == "FRESH" for row in body["closing_soon"]
        )

    def test_notifications_are_primary_only(self, harness) -> None:
        now = _now()
        primary = harness.add(
            title="Dup tender", published_at=now - timedelta(days=1),
            deadline=now + timedelta(days=5), external_id="DUP-P",
        )
        sibling = harness.add(
            title="Dup tender", source_name="Mirror Source",
            published_at=now - timedelta(days=1),
            deadline=now + timedelta(days=5), external_id="DUP-S",
        )
        _mark_duplicate(harness.storage, sibling, "group-x", primary)
        body = harness.client.get(
            "/notifications", params={"window_days": 7}
        ).json()
        assert body["new_total"] == 1


class TestAiAssist:
    def test_ai_analyze_works_deterministically_without_key(self, harness) -> None:
        opportunity_id = harness.add(
            title="Supply of medical equipment", external_id="AI1"
        )
        body = harness.client.post(
            f"/ai/analyze/{opportunity_id}"
        ).json()
        assert body["provider"] == "deterministic"
        assert body["llm_available"] is False
        assert body["model"] == "deterministic-v1"
        assert body["opportunity_id"] == opportunity_id
        assert body["category"] is not None
        assert isinstance(body["summary"], str) and body["summary"]
        assert isinstance(body["red_flags"], list)

    def test_ai_analyze_unknown_is_404(self, harness) -> None:
        assert harness.client.post("/ai/analyze/98765").status_code == 404

    def test_ai_dedup_deterministic_same_reference(self, harness) -> None:
        a = harness.add(title="Road upgrade", external_id="R-10",
                        reference_number="RD-2026-10")
        b = harness.add(title="Road upgrade", source_name="Mirror Source",
                        external_id="R-10-b", reference_number="RD-2026-10")
        body = harness.client.post(
            "/ai/dedup", json={"opportunity_ids": [a, b]}
        ).json()
        assert body["same_tender"] is True
        assert body["canonical"]["title"] == "Road upgrade"
        assert body["listings"][0]["opportunity_id"] == a

    def test_ai_dedup_deterministic_distinct_titles(self, harness) -> None:
        a = harness.add(title="Supply of laptops", external_id="L1",
                        reference_number="L-1")
        b = harness.add(title="Construction of a bridge", source_name="Mirror",
                        external_id="L2", reference_number="B-2")
        body = harness.client.post(
            "/ai/dedup", json={"opportunity_ids": [a, b]}
        ).json()
        assert body["same_tender"] is False

    def test_ai_dedup_needs_two_known_ids(self, harness) -> None:
        a = harness.add(title="Anything", external_id="ANY")
        response = harness.client.post(
            "/ai/dedup", json={"opportunity_ids": [a, 999999]}
        )
        assert response.status_code == 422

    def test_ai_rfp_without_key_is_503(self, harness) -> None:
        opportunity_id = harness.add(title="Supply of generators", external_id="G")
        response = harness.client.post(
            "/ai/rfp", json={"opportunity_id": opportunity_id,
                            "company_name": "Acme", "capabilities": ["generators"]}
        )
        assert response.status_code == 503