"""Cross-source deduplication tests (offline, SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import dedup
import pytest
from models import NormalizedOpportunity
from storage import Storage

DAY = timedelta(days=1)
DEADLINE = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "dedup.db")
    try:
        yield st
    finally:
        st.close()


def add_source(storage, name: str) -> int:
    return storage.get_or_create_source(name, "TestAdapter")


def add_opp(
    storage,
    source_id: int,
    external_id: str,
    *,
    title: str | None = None,
    organization: str | None = None,
    reference_number: str | None = None,
    deadline: datetime | None = DEADLINE,
) -> int:
    opp = NormalizedOpportunity(
        external_id=external_id,
        source_url=f"https://example.org/{external_id}",
        source_name="Test Source",
        title=title,
        organization=organization,
        reference_number=reference_number,
        deadline=deadline,
    )
    storage.upsert_opportunity(opp, source_id)
    row = storage.conn.execute(
        "SELECT id FROM opportunities WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()
    return int(row["id"])


class TestScoringUnits:
    def test_normalize_reference_strips_punctuation(self) -> None:
        assert dedup.normalize_reference("TS-0000/2026") == "TS00002026"
        assert dedup.normalize_reference(" p119659 ") == "P119659"
        assert dedup.normalize_reference(None) is None
        assert dedup.normalize_reference("///") is None

    def test_normalize_text_lowercases_and_pads(self) -> None:
        assert dedup.normalize_text("Supply, of  MEDICAL!") == "supply of medical"

    def test_deadline_score_rules(self) -> None:
        assert dedup.deadline_score(DEADLINE, DEADLINE + 5 * DAY) == 1.0
        assert dedup.deadline_score(DEADLINE, DEADLINE + 60 * DAY) == 0.0
        assert dedup.deadline_score(DEADLINE, None) == 0.5
        assert dedup.deadline_score(None, None) == 0.5

    def test_org_similarity_neutrality(self) -> None:
        assert dedup.org_similarity(None, None) == 0.5
        assert dedup.org_similarity("Health Dept", None) == 0.2
        assert dedup.org_similarity("Health Dept", "Health Dept") == 1.0

    def test_exact_reference_with_compatible_deadlines_scores_one(self) -> None:
        a = {"reference_number": "TS-0000/2026", "deadline": DEADLINE}
        b = {"reference_number": "ts0000 2026", "deadline": DEADLINE + 3 * DAY}
        score, reason = dedup.score_pair(a, b)
        assert score == 1.0
        assert "exact reference match" in reason

    def test_exact_reference_with_far_deadlines_is_not_auto(self) -> None:
        a = {"reference_number": "12345", "deadline": DEADLINE}
        b = {"reference_number": "12345", "deadline": DEADLINE + 400 * DAY}
        score, _ = dedup.score_pair(a, b)
        assert score < dedup.AUTO_MERGE_THRESHOLD

    def test_identical_fuzzy_fields_score_one(self) -> None:
        a = {
            "reference_number": None,
            "title": "Supply of medical equipment",
            "organization": "Health Department",
            "deadline": DEADLINE,
        }
        b = dict(a, reference_number=None)
        score, _ = dedup.score_pair(a, b)
        assert score == 1.0

    def test_unrelated_rows_score_below_review(self) -> None:
        a = {
            "reference_number": None,
            "title": "Annual tea supply for offices",
            "organization": "Education Dept",
            "deadline": DEADLINE,
        }
        b = {
            "reference_number": None,
            "title": "Construction of flyover at Swat",
            "organization": "Communications Dept",
            "deadline": DEADLINE + 200 * DAY,
        }
        score, _ = dedup.score_pair(a, b)
        assert score < dedup.REVIEW_THRESHOLD


class TestCandidatePairs:
    def test_same_source_rows_never_pair(self, storage) -> None:
        source = add_source(storage, "Solo Source")
        add_opp(storage, source, "a", title="Same title", organization="Org")
        add_opp(storage, source, "b", title="Same title", organization="Org")
        rows = storage.list_ungrouped_opportunities()
        assert dedup.candidate_pairs(rows) == set()

    def test_cross_source_sharing_deadline_day_pairs(self, storage) -> None:
        s1, s2 = add_source(storage, "One"), add_source(storage, "Two")
        id_a = add_opp(storage, s1, "a", title="X")
        id_b = add_opp(storage, s2, "b", title="Y")
        rows = storage.list_ungrouped_opportunities()
        assert dedup.candidate_pairs(rows) == {
            (min(id_a, id_b), max(id_a, id_b))
        }

    def test_unrelated_rows_do_not_pair(self, storage) -> None:
        s1, s2 = add_source(storage, "One"), add_source(storage, "Two")
        add_opp(
            storage, s1, "a", title="Tea supply", organization="Org A",
            deadline=DEADLINE,
        )
        add_opp(
            storage, s2, "b", title="Bridge construction", organization="Org B",
            deadline=DEADLINE + 120 * DAY,
        )
        rows = storage.list_ungrouped_opportunities()
        assert dedup.candidate_pairs(rows) == set()


class TestRun:
    def test_exact_reference_merges_across_sources(self, storage) -> None:
        s1, s2 = add_source(storage, "EPMS"), add_source(storage, "EPADS")
        id_a = add_opp(
            storage, s1, "e1", reference_number="TS-0000/2026",
            title="Supply of laptops", organization="HEC",
        )
        id_b = add_opp(
            storage, s2, "e2", reference_number="ts 0000 2026",
            title="Supply of laptops", organization="HEC",
        )
        report = dedup.run(storage)
        assert report["merged"] == 1
        assert report["queued"] == 0
        assert storage.list_ungrouped_opportunities() == []

        row_a = storage.get_opportunity(id_a)
        row_b = storage.get_opportunity(id_b)
        assert row_a["duplicate_group_id"] == row_b["duplicate_group_id"]
        primaries = [row for row in (row_a, row_b) if row["is_primary"]]
        assert len(primaries) == 1
        assert primaries[0]["id"] == id_a  # seen first
        assert row_a["source_count"] == 2
        assert row_b["source_count"] == 2

    def test_fuzzy_pair_in_review_range_is_queued_not_merged(
        self, storage
    ) -> None:
        s1, s2 = add_source(storage, "One"), add_source(storage, "Two")
        id_a = add_opp(
            storage, s1, "a",
            title="Supply of medical equipment for hospitals",
            organization="Health Department",
        )
        id_b = add_opp(
            storage, s2, "b",
            title="Supply of office furniture and fittings",
            organization="Health Department",
        )
        score, _ = dedup.score_pair(
            storage.get_opportunity(id_a), storage.get_opportunity(id_b)
        )
        assert dedup.REVIEW_THRESHOLD <= score < dedup.AUTO_MERGE_THRESHOLD

        report = dedup.run(storage)
        assert report["merged"] == 0
        assert report["queued"] == 1
        queue = storage.list_dedup_reviews("pending")
        assert len(queue) == 1
        assert queue[0]["opportunity_id_a"] == min(id_a, id_b)
        assert queue[0]["opportunity_id_b"] == max(id_a, id_b)

        # Re-running must not duplicate the queue row.
        again = dedup.run(storage)
        assert again["queued"] == 0
        assert len(storage.list_dedup_reviews("pending")) == 1

    def test_unrelated_rows_are_left_alone(self, storage) -> None:
        s1, s2 = add_source(storage, "One"), add_source(storage, "Two")
        add_opp(
            storage, s1, "a", title="Annual tea supply",
            organization="Education Dept", deadline=DEADLINE,
        )
        add_opp(
            storage, s2, "b", title="Construction of flyover at Swat",
            organization="Communications Dept", deadline=DEADLINE + 200 * DAY,
        )
        report = dedup.run(storage)
        assert report["candidate_pairs"] == 0
        assert report["merged"] == 0
        assert report["queued"] == 0
        assert len(storage.list_ungrouped_opportunities()) == 2


class TestMergeShapes:
    def test_union_of_two_groups_leaves_one_primary(self, storage) -> None:
        s1, s2, s3, s4 = (add_source(storage, n) for n in "ABCD")
        a = add_opp(storage, s1, "a", reference_number="R-1", title="Same tender")
        b = add_opp(storage, s2, "b", reference_number="R-1", title="Same tender")
        c = add_opp(storage, s3, "c", reference_number="R-2", title="Other tender")
        d = add_opp(storage, s4, "d", reference_number="R-2", title="Other tender")
        dedup.run(storage)
        group1 = storage.get_opportunity(a)["duplicate_group_id"]
        group2 = storage.get_opportunity(c)["duplicate_group_id"]
        assert group1 and group1 != group2

        # Fuse the two groups through any member pair.
        merged_group = dedup.merge_pair(storage, storage.get_opportunity(b), storage.get_opportunity(d))
        assert merged_group is not None

        members = storage.list_opportunities_in_group(merged_group)
        assert len(members) == 4
        assert sum(1 for m in members if m["is_primary"]) == 1
        assert all(m["source_count"] == 4 for m in members)
        assert storage.list_ungrouped_opportunities() == []

    def test_merge_pair_same_group_is_noop(self, storage) -> None:
        s1, s2 = add_source(storage, "One"), add_source(storage, "Two")
        a = add_opp(storage, s1, "a", reference_number="R-1")
        b = add_opp(storage, s2, "b", reference_number="R-1")
        first = dedup.merge_pair(storage, storage.get_opportunity(a), storage.get_opportunity(b))
        second = dedup.merge_pair(storage, storage.get_opportunity(a), storage.get_opportunity(b))
        assert first is not None
        assert second is None


class TestResolve:
    def _queued_pair(self, storage) -> tuple[int, int, int]:
        s1, s2 = add_source(storage, "One"), add_source(storage, "Two")
        id_a = add_opp(
            storage, s1, "a",
            title="Supply of medical equipment for hospitals",
            organization="Health Department",
        )
        id_b = add_opp(
            storage, s2, "b",
            title="Supply of office furniture and fittings",
            organization="Health Department",
        )
        dedup.run(storage)
        queue = storage.list_dedup_reviews("pending")
        assert len(queue) == 1
        return queue[0]["id"], id_a, id_b

    def test_confirmed_merges_and_marks_queue(self, storage) -> None:
        queue_id, id_a, id_b = self._queued_pair(storage)
        group = dedup.resolve(storage, queue_id, "confirmed")
        assert group is not None
        assert storage.get_opportunity(id_a)["duplicate_group_id"] == group
        assert storage.get_opportunity(id_b)["duplicate_group_id"] == group
        assert storage.list_dedup_reviews("pending") == []
        assert storage.list_dedup_reviews("confirmed")

    def test_rejected_keeps_rows_separate(self, storage) -> None:
        queue_id, id_a, id_b = self._queued_pair(storage)
        group = dedup.resolve(storage, queue_id, "rejected")
        assert group is None
        assert storage.get_opportunity(id_a)["duplicate_group_id"] is None
        assert storage.get_opportunity(id_b)["duplicate_group_id"] is None
        assert storage.list_dedup_reviews("rejected")

    def test_unknown_decision_raises(self, storage) -> None:
        queue_id, _, _ = self._queued_pair(storage)
        with pytest.raises(ValueError, match="unknown decision"):
            dedup.resolve(storage, queue_id, "maybe")

    def test_unknown_queue_id_raises(self, storage) -> None:
        with pytest.raises(ValueError, match="no pending dedup review"):
            dedup.resolve(storage, 99999, "confirmed")


class TestCli:
    def test_dedup_only_flag_prints_report(self, tmp_path, capsys) -> None:
        from main import main as cli_main

        db = tmp_path / "cli.db"
        code = cli_main(["--dedup-only", "--db", str(db)])
        out = capsys.readouterr().out
        assert code == 0
        assert out.startswith("Dedup:")
