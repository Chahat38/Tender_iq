"""AI stage tests: deterministic analysis, optional LLM, matching."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import ai
import pytest
from models import NormalizedOpportunity
from storage import Storage

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(days=30)


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "ai.db")
    try:
        yield st
    finally:
        st.close()


def add_opp(storage, **overrides) -> int:
    defaults = dict(
        external_id=overrides.pop("external_id", "e1"),
        source_url="https://example.org/t1",
        source_name="Src",
        title="Supply of 50 laptops for HEC",
        description="The bidder shall submit a bid security. "
        "Delivery within 30 days in Punjab.",
        organization="Higher Education Commission",
        published_at=NOW - timedelta(days=5),
        deadline=FUTURE,
        province="Punjab",
        category=None,
    )
    defaults.update(overrides)
    opp = NormalizedOpportunity(**defaults)
    source_id = storage.get_or_create_source("Src", "Adapter")
    storage.upsert_opportunity(opp, source_id)
    return int(
        storage.conn.execute(
            "SELECT id FROM opportunities WHERE external_id = ?",
            (defaults["external_id"],),
        ).fetchone()["id"]
    )


def add_user(storage, user_id: str = "user-1") -> None:
    now = ai.utc_now_iso()
    storage.conn.execute(
        "INSERT INTO profiles (id, user_id, plan, alert_threshold, created_at, "
        "updated_at) VALUES (?, ?, 'free', 60, ?, ?)",
        (user_id + "-p", user_id, now, now),
    )
    storage.conn.execute(
        "INSERT INTO company_capabilities (id, user_id, name, keywords, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id + "-c", user_id, "IT hardware",
         json.dumps(["laptop", "software", "network"]), now),
    )
    storage.conn.execute(
        "INSERT INTO target_regions (id, user_id, province, created_at) "
        "VALUES (?, ?, 'Punjab', ?)",
        (user_id + "-r", user_id, now),
    )
    storage.conn.execute(
        "INSERT INTO target_categories (id, user_id, category, created_at) "
        "VALUES (?, ?, 'ICT & Telecom', ?)",
        (user_id + "-t", user_id, now),
    )
    storage.conn.commit()


class TestDeterministicAnalysis:
    def test_classify_picks_taxonomy_category(self) -> None:
        category, _, score = ai.classify("Supply of laptops and network switches")
        assert category == "ICT & Telecom"
        assert score > 0

    def test_classify_unrelated_text_is_none(self) -> None:
        category, sub, score = ai.classify("totally unrelated gibberish")
        assert category is None
        assert sub is None
        assert score == 0

    def test_subcategory_only_at_40_percent(self) -> None:
        # 1 runner-up hit vs 3 winning hits = 33% < 40% -> no subcategory.
        category, sub, _ = ai.classify("laptop laptop laptop chair")
        assert category == "ICT & Telecom"
        assert sub is None
        # 1 vs 2 = 50% >= 40% -> runner-up shown.
        category, sub, _ = ai.classify("chair table laptop")
        assert category == "Furniture & Office Supplies"
        assert sub == "ICT & Telecom"

    def test_normalize_procurement_type_variants(self) -> None:
        assert ai.normalize_procurement_type("EOI") == "eoi"
        assert ai.normalize_procurement_type("Request For Proposal") == "rfp"
        assert ai.normalize_procurement_type("Open Tender") == "open tender"
        assert ai.normalize_procurement_type("single source") == "single source"
        assert ai.normalize_procurement_type(None) is None
        assert ai.normalize_procurement_type("weird value") == "weird value"

    def test_analyze_full_row(self) -> None:
        opp = {
            "title": "Supply of 50 laptops for HEC",
            "description": "The bidder shall submit a bid security. "
            "Software licence required for each unit.",
            "organization": "HEC",
            "published_at": (NOW - timedelta(days=5)).isoformat(),
            "deadline": FUTURE.isoformat(),
            "procurement_type": None,
            "category": None,
        }
        analysis = ai.analyze(opp, now=NOW)
        assert analysis.category == "ICT & Telecom"
        assert "laptops" in analysis.keywords
        assert analysis.procurement_type == "tender"  # inferred from title
        assert any("bid security" in r for r in analysis.requirements)
        assert analysis.date_conflicts == []
        assert analysis.prompt_version == ai.PROMPT_VERSION

    def test_date_conflicts_past_and_precedence(self) -> None:
        past = (NOW - timedelta(days=1)).isoformat()
        analysis = ai.analyze(
            {"title": "x", "description": "y", "published_at": past,
             "deadline": past},
            now=NOW,
        )
        assert "deadline is already in the past" in analysis.date_conflicts
        published = (NOW + timedelta(days=5)).isoformat()
        conflicts = ai.date_conflicts(past, published, now=NOW)
        assert "deadline precedes the publication date" in conflicts

    def test_run_analysis_populates_rows_and_journals(self, storage) -> None:
        add_opp(storage)
        report = ai.run_analysis(storage, use_llm=False)
        assert report == {"analyzed": 1, "llm": 0, "deterministic": 1, "failed": 0}
        row = storage.get_opportunity_ai(1)
        assert row["category"] == "ICT & Telecom"
        assert json.loads(row["keywords"])
        assert row["prompt_version"] == ai.PROMPT_VERSION
        runs = storage.list_analysis_runs(task=ai.TASK_ANALYSIS)
        assert runs[0]["provider"] == ai.MODEL_DETERMINISTIC
        assert runs[0]["status"] == "ok"

        # Second run: nothing left to analyze.
        again = ai.run_analysis(storage, use_llm=False)
        assert again["analyzed"] == 0


class TestOptionalLLM:
    def test_llm_settings_none_without_key(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert ai.llm_settings() is None

    def test_llm_settings_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TENDERSCOUT_LLM_MODEL", "gpt-test")
        settings = ai.llm_settings()
        assert settings is not None
        assert settings["model"] == "gpt-test"

    def test_analyze_with_llm_parses_json_response(self) -> None:
        payload = {
            "category": "ICT & Telecom",
            "subcategory": "laptops",
            "keywords": ["laptop", "tender"],
            "procurement_type": "open tender",
            "requirements": ["Bid security required"],
            "date_conflicts": [],
        }
        raw = json.dumps(payload)

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"choices": [{"message": {"content": raw}}]}

        class FakeTransport:
            @staticmethod
            def post(url, json=None, headers=None, timeout=None):
                assert url.endswith("/chat/completions")
                assert headers["Authorization"] == "Bearer sk-test"
                return FakeResponse()

        analysis = ai.analyze_with_llm(
            {"title": "t"},
            transport=FakeTransport,
            settings={"api_key": "sk-test", "base_url": "https://llm", "model": "m1"},
        )
        assert analysis is not None
        assert analysis.category == "ICT & Telecom"
        assert analysis.provider == "openai"
        assert analysis.model == "m1"

    def test_llm_failure_falls_back_and_is_journaled(self, storage, monkeypatch) -> None:
        add_opp(storage)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        class BoomTransport:
            @staticmethod
            def post(*args, **kwargs):
                raise RuntimeError("llm down")

        report = ai.run_analysis(storage, use_llm=True, transport=BoomTransport)
        assert report == {"analyzed": 1, "llm": 0, "deterministic": 1, "failed": 1}
        row = storage.get_opportunity_ai(1)
        assert row["category"] == "ICT & Telecom"  # deterministic fallback
        runs = storage.list_analysis_runs(task=ai.TASK_ANALYSIS)
        assert runs[0]["status"] == "error"
        assert "llm down" in runs[0]["error_message"]


class TestMatching:
    def test_load_signals_reads_profile_tables(self, storage) -> None:
        add_user(storage, "u1")
        signals = ai.load_signals(storage, "u1")
        assert signals.alert_threshold == 60
        assert signals.capabilities == [("IT hardware", ["laptop", "software", "network"])]
        assert signals.regions == {"Punjab"}
        assert signals.categories == {"ICT & Telecom"}

    def test_empty_signals_score_zero_low(self) -> None:
        match = ai.score_match(
            {"title": "laptop supply", "province": "Punjab"}, None, ai.UserSignals("u")
        )
        assert match.relevance_score == 0
        assert match.level == "LOW"
        assert match.reasons == []

    def test_components_add_up_and_levels_bound(self) -> None:
        opp = {"title": "Supply of 50 laptops", "description": "network switches",
               "province": "Punjab", "category": None}
        analysis = {"category": "ICT & Telecom", "keywords": ["laptop"]}
        signals = ai.UserSignals(
            "u",
            capabilities=[("IT", ["laptop", "software", "network"])],
            categories={"ICT & Telecom"},
            regions={"Punjab"},
        )
        match = ai.score_match(opp, analysis, signals)
        # capability: 3/3 keywords hit -> 45; category 25; region 20;
        # category keywords +10 (if present) - total is in [70, 100].
        assert 70 <= match.relevance_score <= 100
        assert match.level == "HIGH"
        assert any("capability keywords" in r for r in match.reasons)
        assert any("target category" in r for r in match.reasons)
        assert any("target region" in r for r in match.reasons)

    def test_level_boundaries(self) -> None:
        assert ai.score_threshold_level(70) == "HIGH"
        assert ai.score_threshold_level(69) == "MEDIUM"
        assert ai.score_threshold_level(40) == "MEDIUM"
        assert ai.score_threshold_level(39) == "LOW"

    def test_run_matching_creates_matches_and_reports_levels(
        self, storage
    ) -> None:
        add_opp(storage)
        add_user(storage, "u1")
        report = ai.run_matching(storage, "u1")
        assert report["scored"] == 1
        assert report["analyzed"] == 1
        assert report[report and "medium"] >= 0
        matches = storage.list_matches("u1")
        assert len(matches) == 1
        row = matches[0]
        assert row["opportunity_id"] == 1
        assert row["level"] in {"HIGH", "MEDIUM", "LOW"}
        assert 0 <= row["relevance_score"] <= 100
        assert json.loads(row["reasons"])
        assert row["prompt_version"] == ai.PROMPT_VERSION
        runs = storage.list_analysis_runs(task=ai.TASK_MATCH)
        assert runs and runs[0]["user_id"] == "u1"

    def test_matching_skips_unchanged_and_rescores_updated(self, storage) -> None:
        add_opp(storage)
        add_user(storage, "u1")
        first = ai.run_matching(storage, "u1")
        assert first["scored"] == 1
        second = ai.run_matching(storage, "u1")
        assert second["scored"] == 0  # nothing changed

        # Source re-sees the tender -> last_seen_at moves -> re-score.
        future = ai.utc_now_iso()
        storage.conn.execute(
            "UPDATE opportunities SET last_seen_at = ? WHERE id = 1", (future,)
        )
        storage.conn.commit()
        third = ai.run_matching(storage, "u1")
        assert third["scored"] == 1

    def test_match_upsert_is_unique_per_pair(self, storage) -> None:
        add_opp(storage)
        add_user(storage, "u1")
        ai.run_matching(storage, "u1")
        ai.run_matching(storage, "u1")
        count = storage.conn.execute(
            "SELECT COUNT(*) AS n FROM opportunity_matches"
        ).fetchone()["n"]
        assert count == 1


class TestCli:
    def test_analyze_only_prints_report(self, tmp_path, capsys) -> None:
        from main import main as cli_main

        code = cli_main(["--analyze-only", "--db", str(tmp_path / "c.db")])
        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("AI:        analyzed 0")

    def test_match_user_prints_report(self, tmp_path, capsys) -> None:
        from main import main as cli_main

        code = cli_main(
            ["--match-user", "u9", "--db", str(tmp_path / "c.db")]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("Matches:   scored 0")

    def test_standalone_modes_are_mutually_exclusive(self) -> None:
        from main import parse_args

        with pytest.raises(SystemExit):
            parse_args(["--analyze-only", "--dedup-only"])
        with pytest.raises(SystemExit):
            parse_args(["--analyze-only", "--match-user", "u1"])
