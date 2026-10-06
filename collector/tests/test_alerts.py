"""User-facing alerts worker tests (offline, SQLite)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import alerts
import pytest
from ai import utc_now_iso
from models import NormalizedOpportunity
from storage import Storage

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "alerts.db")
    try:
        yield st
    finally:
        st.close()


def add_opp(storage, external_id="e1", deadline=None, **overrides) -> int:
    defaults = dict(
        external_id=external_id,
        source_url=f"https://example.org/{external_id}",
        source_name="Src",
        title="Supply of laptops",
        description="desc",
        organization="HEC",
        deadline=deadline if deadline is not None else NOW + timedelta(days=2),
    )
    defaults.update(overrides)
    source_id = storage.get_or_create_source("Src", "Adapter")
    storage.upsert_opportunity(NormalizedOpportunity(**defaults), source_id)
    return int(
        storage.conn.execute(
            "SELECT id FROM opportunities WHERE external_id = ?",
            (external_id,),
        ).fetchone()["id"]
    )


def add_profile(storage, user_id="u1", threshold=60) -> None:
    now = utc_now_iso()
    storage.conn.execute(
        "INSERT INTO profiles (id, user_id, plan, alert_threshold, created_at, "
        "updated_at) VALUES (?, ?, 'free', ?, ?, ?)",
        (user_id + "-p", user_id, threshold, now, now),
    )
    storage.conn.commit()


def add_match(storage, opportunity_id, user_id="u1", score=80) -> None:
    now = utc_now_iso()
    storage.upsert_opportunity_match(
        opportunity_id,
        user_id,
        relevance_score=score,
        level="HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW",
        reasons=json.dumps(["capability keywords matched: laptop"]),
        prompt_version="v",
        model="deterministic-v1",
        created_at=now,
        updated_at=now,
    )


def add_update(storage, opportunity_id, field="title", update_type="field_change"):
    storage.conn.execute(
        "INSERT INTO opportunity_updates (opportunity_id, field, old_value, "
        "new_value, detected_at, update_type) VALUES (?, ?, 'old', 'new', ?, ?)",
        (opportunity_id, field, utc_now_iso(), update_type),
    )
    storage.conn.commit()


def alert_types(storage, user_id="u1"):
    return [row["type"] for row in storage.list_alerts(user_id)]


class TestNewMatch:
    def test_creates_alert_for_above_threshold_match(self, storage) -> None:
        opp = add_opp(storage, deadline=NOW + timedelta(days=10))
        add_profile(storage, "u1", threshold=60)
        add_match(storage, opp, "u1", score=85)
        report = alerts.run(storage, now=NOW)
        assert report["new_match"] == 1
        assert report["total"] == 1
        row = storage.list_alerts("u1")[0]
        assert row["type"] == "new_match"
        assert row["opportunity_id"] == opp
        assert row["channel"] == "in_app"
        assert "85" in row["body"]
        assert "laptop" in row["body"]

    def test_dedup_on_second_run(self, storage) -> None:
        opp = add_opp(storage, deadline=NOW + timedelta(days=10))
        add_profile(storage, "u1")
        add_match(storage, opp, "u1", score=85)
        assert alerts.run(storage, now=NOW)["new_match"] == 1
        assert alerts.run(storage, now=NOW)["total"] == 0

    def test_below_threshold_is_not_alerted(self, storage) -> None:
        opp = add_opp(storage)
        add_profile(storage, "u1", threshold=60)
        add_match(storage, opp, "u1", score=50)
        report = alerts.run(storage, now=NOW)
        assert report["total"] == 0

    def test_no_profiles_means_no_alerts(self, storage) -> None:
        opp = add_opp(storage)
        add_match(storage, opp, "ghost", score=90)
        assert alerts.run(storage, now=NOW)["total"] == 0


class TestDeadlineApproaching:
    def test_inside_window_alerts(self, storage) -> None:
        opp = add_opp(storage, deadline=NOW + timedelta(days=2))
        add_profile(storage, "u1")
        add_match(storage, opp, "u1", score=80)
        report = alerts.run(storage, now=NOW, deadline_window_days=3)
        assert report["deadline_approaching"] == 1
        row = next(
            r for r in storage.list_alerts("u1")
            if r["type"] == "deadline_approaching"
        )
        assert "in 2 days" in row["body"]

    def test_outside_window_and_past_are_ignored(self, storage) -> None:
        far = add_opp(storage, external_id="far", deadline=NOW + timedelta(days=10))
        past = add_opp(storage, external_id="past", deadline=NOW - timedelta(days=1))
        add_profile(storage, "u1")
        add_match(storage, far, "u1", score=80)
        add_match(storage, past, "u1", score=80)
        report = alerts.run(storage, now=NOW, deadline_window_days=3)
        assert report["deadline_approaching"] == 0

    def test_saved_opportunity_without_match_is_watched(self, storage) -> None:
        opp = add_opp(storage, deadline=NOW + timedelta(days=1))
        add_profile(storage, "u1")
        storage.conn.execute(
            "INSERT INTO saved_opportunities (id, user_id, opportunity_id, "
            "created_at) VALUES ('s1', 'u1', ?, ?)",
            (opp, utc_now_iso()),
        )
        storage.conn.commit()
        report = alerts.run(storage, now=NOW)
        assert report["deadline_approaching"] == 1
        assert report["new_match"] == 0


class TestUpdateAlerts:
    def test_field_change_gives_opportunity_updated_only(self, storage) -> None:
        opp = add_opp(storage)
        add_profile(storage, "u1")
        add_match(storage, opp, "u1", score=80)
        add_update(storage, opp, field="title", update_type="field_change")
        report = alerts.run(storage, now=NOW)
        assert report["opportunity_updated"] == 1
        assert report["corrigendum"] == 0

    def test_document_change_gives_corrigendum_only(self, storage) -> None:
        opp = add_opp(storage)
        add_profile(storage, "u1")
        add_match(storage, opp, "u1", score=80)
        add_update(storage, opp, field="document_urls", update_type="field_change")
        report = alerts.run(storage, now=NOW)
        assert report["corrigendum"] == 1
        assert report["opportunity_updated"] == 0

    def test_update_needs_a_watched_opportunity(self, storage) -> None:
        opp = add_opp(storage)  # profile exists but no match, not saved
        add_profile(storage, "u1")
        add_update(storage, opp, field="title")
        report = alerts.run(storage, now=NOW)
        assert report["opportunity_updated"] == 0


class TestMultiUser:
    def test_users_are_independent(self, storage) -> None:
        opp_a = add_opp(storage, external_id="a", deadline=NOW + timedelta(days=10))
        opp_b = add_opp(storage, external_id="b", deadline=NOW + timedelta(days=10))
        add_profile(storage, "u1", threshold=60)
        add_profile(storage, "u2", threshold=60)
        add_match(storage, opp_a, "u1", score=90)
        add_match(storage, opp_b, "u2", score=90)
        report = alerts.run(storage, now=NOW)
        assert report["new_match"] == 2
        assert report["total"] == 2
        u1_types = alert_types(storage, "u1")
        assert u1_types == ["new_match"]
        assert alert_types(storage, "u2") == ["new_match"]
        assert storage.list_alerts("u1")[0]["opportunity_id"] == opp_a


class TestCli:
    def test_alerts_only_prints_report(self, tmp_path, capsys) -> None:
        from main import main as cli_main

        code = cli_main(["--alerts-only", "--db", str(tmp_path / "c.db")])
        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("Alerts:    created 0")

    def test_alerts_only_is_exclusive(self) -> None:
        from main import parse_args

        with pytest.raises(SystemExit):
            parse_args(["--alerts-only", "--documents"])
        with pytest.raises(SystemExit):
            parse_args(["--alerts-only", "--analyze-only"])
