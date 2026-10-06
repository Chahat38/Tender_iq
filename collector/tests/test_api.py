"""Tests for the Stage 6 read-only FastAPI API.

All tests run offline against a seeded temporary SQLite database built
with :meth:`storage.Storage.upsert_opportunity` (the collector's own
write path) and exercise the app through ``fastapi.testclient`` -- no
server process, no network.
"""

from datetime import datetime, timezone

import json as jsonlib

import pytest
from fastapi.testclient import TestClient

from api import FtsUnavailableError, create_app
from models import NormalizedOpportunity
from search import SearchIndex


def make_opportunity(**overrides) -> NormalizedOpportunity:
    """Build a NormalizedOpportunity with deterministic defaults."""
    fields = dict(
        title="Supply of medical equipment for district hospital",
        organization="Punjab Information Technology Board",
        description="Procurement of ventilators and MRI machines.",
        source_url="https://example.gov.pk/tenders/123",
        source_name="Example Source",
        published_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        deadline=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        location="Lahore",
        category="Health",
        procurement_type="goods",
        reference_number="T-2026-123",
        document_urls=[],
        external_id="123",
    )
    fields.update(overrides)
    return NormalizedOpportunity(**fields)


class ClientHarness:
    """TestClient plus a direct handle on the seeded Storage."""

    def __init__(self, client: TestClient, storage) -> None:
        self.client = client
        self.storage = storage

    def add(self, source_name: str = "Example Source", **overrides) -> int:
        """Insert one opportunity, return its row id."""
        storage = self.storage
        source_id = storage.get_or_create_source(source_name, "example")
        storage.upsert_opportunity(make_opportunity(**overrides), source_id)
        row = storage.conn.execute(
            "SELECT id FROM opportunities WHERE external_id = ? AND source_id = ?",
            (overrides.get("external_id", "123"), source_id),
        ).fetchone()
        return int(row["id"])


@pytest.fixture()
def harness(tmp_path):
    """App + TestClient over a throwaway database."""
    db_path = tmp_path / "api_test.db"
    app = create_app(db_path)
    with TestClient(app) as client:
        # Reach into the app state for the direct-write handle. The
        # lifespan has run by the time TestClient.__enter__ returns.
        yield ClientHarness(client, app.state.storage)


class TestMeta:
    def test_root_describes_the_service(self, harness) -> None:
        response = harness.client.get("/")
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "read-only"
        assert body["endpoints"]["search"] == "/search?q=QUERY"
        assert "/opportunities" in body["endpoints"].values()

    def test_openapi_schema_is_served(self, harness) -> None:
        response = harness.client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/opportunities" in paths
        assert "/opportunities/{opportunity_id}" in paths
        assert "/search" in paths
        assert "/sources" in paths


class TestSources:
    def test_sources_are_listed(self, harness) -> None:
        harness.add(source_name="PPRA EPMS")
        harness.add(source_name="Other Source", external_id="X1")
        response = harness.client.get("/sources")
        assert response.status_code == 200
        names = [row["name"] for row in response.json()]
        # insertion order: "PPRA EPMS" created first, then "Other Source";
        # the shared "Example Source" comes from the fixture defaults.
        assert names == ["PPRA EPMS", "Other Source"]

    def test_sources_empty_is_ok(self, harness) -> None:
        assert harness.client.get("/sources").json() == []


class TestOpportunityList:
    def test_list_is_newest_first_with_paging_metadata(
        self, harness
    ) -> None:
        first = harness.add()
        second = harness.add(external_id="124")
        response = harness.client.get("/opportunities")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert [row["id"] for row in body["opportunities"]] == [second, first]

    def test_list_carries_display_fields(self, harness) -> None:
        harness.add()
        row = harness.client.get("/opportunities").json()["opportunities"][0]
        assert row["external_id"] == "123"
        assert row["title"].startswith("Supply of medical equipment")
        assert row["source_url"] == "https://example.gov.pk/tenders/123"
        assert row["deadline"] == datetime(
            2026, 10, 1, 12, 0, tzinfo=timezone.utc
        ).isoformat()
        assert row["location"] == "Lahore"
        assert row["category"] == "Health"
        assert row["content_hash"]
        # internal columns are not exposed
        assert "raw_content" not in row
        assert "is_primary" not in row
        assert "duplicate_group_id" not in row

    def test_limit_and_offset_page(self, harness) -> None:
        ids = [harness.add(external_id=str(200 + i)) for i in range(5)]
        page1 = harness.client.get(
            "/opportunities", params={"limit": 2}
        ).json()
        page2 = harness.client.get(
            "/opportunities", params={"limit": 2, "offset": 2}
        ).json()
        last = harness.client.get(
            "/opportunities", params={"limit": 2, "offset": 4}
        ).json()
        assert page1["total"] == 5 and len(page1["opportunities"]) == 2
        # newest first: page 1 holds the newest two ids, and so on
        assert [r["id"] for r in page1["opportunities"]] == ids[::-1][:2]
        assert [r["id"] for r in page2["opportunities"]] == ids[::-1][2:4]
        assert [r["id"] for r in last["opportunities"]] == ids[::-1][4:]

    def test_limit_is_validated(self, harness) -> None:
        too_big = harness.client.get("/opportunities", params={"limit": 201})
        negative = harness.client.get("/opportunities", params={"limit": -1})
        assert too_big.status_code == 422
        assert negative.status_code == 422

    def test_source_filter(self, harness) -> None:
        other = harness.add(source_name="Other Source")
        harness.add()  # Example Source row
        response = harness.client.get(
            "/opportunities",
            params={"source_id": harness.storage.conn.execute(
                "SELECT id FROM sources WHERE name = 'Other Source'"
            ).fetchone()["id"]},
        )
        rows = response.json()["opportunities"]
        assert [r["id"] for r in rows] == [other]

    def test_deadline_window_filters(self, harness) -> None:
        harness.add(
            deadline=datetime(2026, 10, 1, tzinfo=timezone.utc),
            external_id="301",
        )
        harness.add(
            deadline=datetime(2026, 11, 1, tzinfo=timezone.utc),
            external_id="302",
        )
        harness.add(deadline=None, external_id="303")
        before = harness.client.get(
            "/opportunities",
            params={"deadline_before": "2026-10-15T00:00:00Z"},
        ).json()
        assert [r["external_id"] for r in before["opportunities"]] == ["301"]
        after = harness.client.get(
            "/opportunities",
            params={"deadline_after": "2026-10-15T00:00:00Z"},
        ).json()
        assert [r["external_id"] for r in after["opportunities"]] == ["302"]


class TestOpportunityQueryPost:
    def test_deadline_after_null_keeps_undated_rows(self, harness) -> None:
        harness.add(deadline=None, external_id="401")
        body = {"deadline_after": None}
        rows = harness.client.post(
            "/opportunities/query", json=body
        ).json()["opportunities"]
        assert [r["external_id"] for r in rows] == ["401"]

    def test_deadline_after_false_hides_undated_rows(self, harness) -> None:
        harness.add(deadline=None, external_id="402")
        harness.add(
            deadline=datetime(2026, 12, 1, tzinfo=timezone.utc),
            external_id="403",
        )
        rows = harness.client.post(
            "/opportunities/query", json={"deadline_after": False}
        ).json()["opportunities"]
        assert [r["external_id"] for r in rows] == ["403"]

    def test_deadline_after_datetime_is_lower_bound(self, harness) -> None:
        harness.add(
            deadline=datetime(2026, 10, 1, tzinfo=timezone.utc),
            external_id="404",
        )
        harness.add(
            deadline=datetime(2026, 11, 1, tzinfo=timezone.utc),
            external_id="405",
        )
        body = {"deadline_after": "2026-10-15T00:00:00Z"}
        rows = harness.client.post(
            "/opportunities/query", json=body
        ).json()["opportunities"]
        assert [r["external_id"] for r in rows] == ["405"]

    def test_deadline_after_true_is_rejected(self, harness) -> None:
        response = harness.client.post(
            "/opportunities/query", json={"deadline_after": True}
        )
        assert response.status_code == 422


class TestOpportunityDetail:
    def test_detail_includes_documents_and_updates(self, harness) -> None:
        opportunity_id = harness.add(
            document_urls=["https://example.gov.pk/docs/boq.pdf"]
        )
        response = harness.client.get(f"/opportunities/{opportunity_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == opportunity_id
        assert body["documents"][0]["url"] == "https://example.gov.pk/docs/boq.pdf"
        assert body["documents"][0]["filename"] == "boq.pdf"
        assert body["updates"] == []

    def test_detail_update_history_is_exposed(self, harness) -> None:
        opportunity_id = harness.add()
        harness.storage.upsert_opportunity(
            make_opportunity(deadline=datetime(2026, 10, 9, tzinfo=timezone.utc)),
            harness.storage.get_or_create_source("Example Source", "example"),
        )
        updates = harness.client.get(
            f"/opportunities/{opportunity_id}"
        ).json()["updates"]
        assert len(updates) == 1
        assert updates[0]["field"] == "deadline"
        assert updates[0]["update_type"] == "deadline_change"

    def test_unknown_id_is_404(self, harness) -> None:
        response = harness.client.get("/opportunities/9999")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_invalid_id_is_422(self, harness) -> None:
        assert harness.client.get("/opportunities/0").status_code == 422
        assert harness.client.get("/opportunities/abc").status_code == 422


class TestSearch:
    def test_search_returns_ranked_hits(self, harness) -> None:
        harness.add()
        response = harness.client.get(
            "/search", params={"q": "ventilators", "limit": 5}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["query"] == "ventilators"
        assert body["total"] == 1
        assert len(body["results"]) == 1
        hit = body["results"][0]
        assert hit["external_id"] == "123"
        assert isinstance(hit["rank"], float)
        assert "[ventilators]" in hit["snippet"]

    def test_search_total_is_independent_of_paging(self, harness) -> None:
        for i in range(4):
            harness.add(
                title=f"Bus fleet procurement lot {i}",
                description="Diesel buses.",
                external_id=f"5{i}",
            )
        full = harness.client.get("/search", params={"q": "bus diesel"}).json()
        paged = harness.client.get(
            "/search", params={"q": "bus diesel", "limit": 2, "offset": 2}
        ).json()
        assert full["total"] == 4
        assert len(full["results"]) == 4
        assert paged["total"] == 4
        assert len(paged["results"]) == 2
        assert [r["opportunity_id"] for r in paged["results"]] == [
            r["opportunity_id"] for r in full["results"][2:4]
        ]

    def test_search_no_match_is_empty_200(self, harness) -> None:
        harness.add()
        response = harness.client.get("/search", params={"q": "zzznotfound"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["results"] == []

    def test_search_injection_attempt_is_harmless(self, harness) -> None:
        harness.add()
        response = harness.client.get(
            "/search", params={"q": '") OR (1=1 --'}
        )
        assert response.status_code == 200
        assert response.json()["total"] == 0

    def test_search_validates_parameters(self, harness) -> None:
        missing = harness.client.get("/search")
        too_long = harness.client.get("/search", params={"q": "x" * 257})
        bad_limit = harness.client.get(
            "/search", params={"q": "x", "limit": 0 - 1}
        )
        assert missing.status_code == 422
        assert too_long.status_code == 422
        assert bad_limit.status_code == 422

    def test_search_index_is_created_lazily(self, harness) -> None:
        # The index is built on the first /search call, not at startup.
        assert harness.client.app.state.search_index is None
        harness.add()
        response = harness.client.get("/search", params={"q": "ventilators"})
        assert response.status_code == 200
        assert isinstance(harness.client.app.state.search_index, SearchIndex)


class TestFtsUnavailable:
    def test_search_answers_503_without_fts5(self, harness) -> None:
        # Force the lazy index creation to fail: no other endpoint is
        # affected by a missing FTS5 module.
        harness.client.app.state.search_index = None
        original = SearchIndex.__init__

        def failing_init(self, storage):
            raise FtsUnavailableError("no FTS5 in this build")

        SearchIndex.__init__ = failing_init
        try:
            response = harness.client.get("/search", params={"q": "supply"})
        finally:
            SearchIndex.__init__ = original
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"]

    def test_other_endpoints_work_without_fts5(self, harness) -> None:
        harness.client.app.state.search_index = None
        harness.add()
        assert harness.client.get("/opportunities").json()["total"] == 1
        assert harness.client.get("/sources").json() != []


class TestConcurrency:
    def test_parallel_requests_share_one_storage(self, harness) -> None:
        for i in range(3):
            harness.add(external_id=str(600 + i))
        client = harness.client

        import concurrent.futures

        def fetch(_):
            response = client.get("/opportunities", params={"limit": 2})
            assert response.status_code == 200
            return response.json()["total"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            totals = list(pool.map(fetch, range(8)))
        assert totals == [3] * 8


class TestReadOnly:
    def test_no_write_methods_are_registered(self, harness) -> None:
        routes = {
            getattr(route, "path", None): getattr(route, "methods", set())
            for route in harness.client.app.routes
        }
        for path, methods in routes.items():
            if path and path != "/opportunities/query":
                assert not (methods & {"POST", "PUT", "PATCH", "DELETE"}), (
                    f"{path} must not accept mutating methods"
                )

    def test_unknown_route_is_404(self, harness) -> None:
        response = harness.client.post("/opportunities", json={})
        assert response.status_code == 405  # known path, wrong method
        assert harness.client.get("/definitely/not/here").status_code == 404


class TestHealth:
    def test_health_reports_counts(self, harness) -> None:
        harness.add()
        response = harness.client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["opportunities"] == 1
        assert body["sources"] >= 1

    def test_root_lists_new_endpoints(self, harness) -> None:
        endpoints = harness.client.get("/").json()["endpoints"]
        assert endpoints["health"] == "/health"
        assert "alerts" in endpoints
        assert "matches" in endpoints
        assert "opportunity_analysis" in endpoints


class TestSecurityHeaders:
    def test_headers_on_success(self, harness) -> None:
        response = harness.client.get("/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"

    def test_rate_limit_answers_429_with_retry_after(
        self, harness, monkeypatch
    ) -> None:
        monkeypatch.setenv("TENDERSCOUT_RATE_LIMIT", "3")
        for _ in range(3):
            assert harness.client.get("/health").status_code == 200
        response = harness.client.get("/health")
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.json()["detail"] == "rate limit exceeded"

    def test_rate_limit_zero_disables_the_limiter(
        self, harness, monkeypatch
    ) -> None:
        monkeypatch.setenv("TENDERSCOUT_RATE_LIMIT", "0")
        for _ in range(10):
            assert harness.client.get("/health").status_code == 200

    def test_invalid_rate_limit_env_falls_back_to_default(
        self, harness, monkeypatch
    ) -> None:
        monkeypatch.setenv("TENDERSCOUT_RATE_LIMIT", "not-a-number")
        for _ in range(5):
            assert harness.client.get("/health").status_code == 200


class TestAnalysisEndpoint:
    def test_unknown_opportunity_is_404(self, harness) -> None:
        assert harness.client.get("/opportunities/999/analysis").status_code == 404

    def test_missing_analysis_is_404_with_hint(self, harness) -> None:
        opp_id = harness.add()
        response = harness.client.get(f"/opportunities/{opp_id}/analysis")
        assert response.status_code == 404
        assert "no analysis" in response.json()["detail"]

    def test_analysis_decodes_json_lists(self, harness) -> None:
        opp_id = harness.add()
        harness.storage.upsert_opportunity_ai(
            opp_id,
            category="ICT & Telecom",
            subcategory=None,
            keywords=jsonlib.dumps(["laptop", "tender"]),
            procurement_type="open tender",
            requirements=jsonlib.dumps(["Bid security required"]),
            date_conflicts=jsonlib.dumps([]),
            prompt_version="2026-10-v1",
            created_at="2026-10-06T00:00:00+00:00",
        )
        response = harness.client.get(f"/opportunities/{opp_id}/analysis")
        assert response.status_code == 200
        body = response.json()
        assert body["category"] == "ICT & Telecom"
        assert body["keywords"] == ["laptop", "tender"]
        assert body["requirements"] == ["Bid security required"]
        assert body["date_conflicts"] == []

    def test_invalid_id_is_422(self, harness) -> None:
        assert harness.client.get("/opportunities/0/analysis").status_code == 422


class TestAlertsEndpoint:
    def test_empty_is_ok(self, harness) -> None:
        response = harness.client.get("/alerts", params={"user_id": "u1"})
        assert response.status_code == 200
        assert response.json() == []

    def test_user_id_is_required(self, harness) -> None:
        assert harness.client.get("/alerts").status_code == 422

    def test_alerts_returned_newest_first(self, harness) -> None:
        from storage_common import utc_now_iso

        opp_id = harness.add()
        for kind in ("new_match", "deadline_approaching"):
            harness.storage.insert_alert(
                user_id="u1",
                opportunity_id=opp_id,
                type=kind,
                title=f"Alert {kind}",
                body="body",
                created_at=utc_now_iso(),
            )
        response = harness.client.get("/alerts", params={"user_id": "u1"})
        assert response.status_code == 200
        rows = response.json()
        assert len(rows) == 2
        assert {row["type"] for row in rows} == {
            "new_match",
            "deadline_approaching",
        }
        assert rows[0]["id"] and rows[0]["user_id"] == "u1"

    def test_unread_filter_and_limit_validation(self, harness) -> None:
        from storage_common import utc_now_iso

        opp_id = harness.add()
        harness.storage.insert_alert(
            user_id="u1",
            opportunity_id=opp_id,
            type="new_match",
            title="Only alert",
            body=None,
            created_at=utc_now_iso(),
        )
        unread = harness.client.get(
            "/alerts", params={"user_id": "u1", "unread_only": True}
        )
        assert len(unread.json()) == 1
        assert harness.client.get(
            "/alerts", params={"user_id": "u1", "limit": 0}
        ).status_code == 422


class TestMatchesEndpoint:
    def test_matches_returned_score_desc(self, harness) -> None:
        from storage_common import utc_now_iso

        opp_id = harness.add()
        now = utc_now_iso()
        for score in (55, 90):
            harness.storage.upsert_opportunity_match(
                opp_id if score == 90 else opp_id,
                "u1",
                relevance_score=score,
                level="HIGH" if score >= 70 else "MEDIUM",
                reasons=jsonlib.dumps(["capability keywords matched: x"]),
                prompt_version="v1",
                model="deterministic-v1",
                created_at=now,
                updated_at=now,
            )
        response = harness.client.get("/matches", params={"user_id": "u1"})
        assert response.status_code == 200
        rows = response.json()
        assert [row["relevance_score"] for row in rows] == [90]
        assert rows[0]["reasons"] == ["capability keywords matched: x"]

    def test_min_score_filters(self, harness) -> None:
        from storage_common import utc_now_iso

        opp_id = harness.add()
        now = utc_now_iso()
        harness.storage.upsert_opportunity_match(
            opp_id,
            "u1",
            relevance_score=30,
            level="LOW",
            reasons="[]",
            prompt_version="v1",
            model="deterministic-v1",
            created_at=now,
            updated_at=now,
        )
        response = harness.client.get(
            "/matches", params={"user_id": "u1", "min_score": 40}
        )
        assert response.json() == []
