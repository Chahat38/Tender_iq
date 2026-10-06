"""Compliance gate tests (offline): registry facts, gate rules, report."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from compliance import (
    ComplianceError,
    INACTIVE,
    ROBOTS_DISALLOWED,
    REQUIRES_AUTH,
    TERMS_UNCHECKED,
    enforce_compliance,
    passes,
    record_robots_status,
    violations,
)
from registry import SOURCE_REGISTRY, SourceSpec, register_source, spec_for
from storage import Storage

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture()
def storage(tmp_path):
    st = Storage(tmp_path / "compliance.db")
    try:
        yield st
    finally:
        st.close()


def make_source(**overrides):
    source = {
        "name": "Example",
        "active": True,
        "terms_checked": True,
        "requires_authentication": False,
        "robots_allowed": True,
    }
    source.update(overrides)
    return source


class TestGateRules:
    def test_all_flags_ok_passes(self) -> None:
        assert violations(make_source()) == []
        assert passes(make_source())

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"active": False}, INACTIVE),
            ({"active": 0}, INACTIVE),
            ({"terms_checked": False}, TERMS_UNCHECKED),
            ({"terms_checked": None}, TERMS_UNCHECKED),
            ({"requires_authentication": True}, REQUIRES_AUTH),
            ({"robots_allowed": False}, ROBOTS_DISALLOWED),
        ],
    )
    def test_each_bad_flag_fails(
        self, overrides: dict, reason: str
    ) -> None:
        assert violations(make_source(**overrides)) == [reason]

    def test_unknown_robots_is_not_a_violation(self) -> None:
        assert passes(make_source(robots_allowed=None))

    def test_all_reasons_are_reported_together(self) -> None:
        reasons = violations(
            make_source(
                active=False,
                terms_checked=False,
                requires_authentication=True,
                robots_allowed=False,
            )
        )
        assert reasons == [
            INACTIVE,
            TERMS_UNCHECKED,
            REQUIRES_AUTH,
            ROBOTS_DISALLOWED,
        ]

    def test_enforce_raises_loudly(self) -> None:
        with pytest.raises(ComplianceError) as excinfo:
            enforce_compliance(make_source(terms_checked=False))
        message = str(excinfo.value)
        assert "REFUSING TO CRAWL" in message
        assert TERMS_UNCHECKED in message
        assert "never bypass this gate" in message

    def test_enforce_is_silent_when_ok(self) -> None:
        enforce_compliance(make_source())

    def test_record_robots_status_words(self) -> None:
        assert record_robots_status(True) == "robots=allow"
        assert record_robots_status(False) == "robots=DISALLOW"
        assert record_robots_status(None) == "robots=unknown"


class TestRegistry:
    def test_register_source_inserts_compliance_facts(self, storage) -> None:
        spec = SourceSpec(
            key="example",
            source_name="Example Portal",
            base_url="https://example.org",
            adapter_type="ExampleAdapter",
            active=True,
            terms_checked=True,
            requires_authentication=False,
            crawl_frequency="every 6h",
        )
        source_id = register_source(storage, spec)
        row = storage.get_source_by_id(source_id)
        assert row["name"] == "Example Portal"
        assert row["base_url"] == "https://example.org"
        assert row["active"] == 1
        assert row["terms_checked"] == 1
        assert row["requires_authentication"] == 0
        assert row["crawl_frequency"] == "every 6h"

    def test_register_source_preserves_runtime_columns(self, storage) -> None:
        spec = SourceSpec(
            key="example",
            source_name="Example Portal",
            base_url="https://example.org",
            adapter_type="ExampleAdapter",
            terms_checked=True,
        )
        source_id = register_source(storage, spec)
        storage.set_source_status(
            source_id,
            last_status="ok",
            last_crawled_at="2026-10-06T00:00:00+00:00",
            consecutive_failures=0,
        )
        storage.set_robots_allowed(source_id, True)

        register_source(storage, spec)  # re-register (every run does)
        row = storage.get_source_by_id(source_id)
        assert row["last_status"] == "ok"
        assert row["last_crawled_at"] == "2026-10-06T00:00:00+00:00"
        assert row["robots_allowed"] == 1

    def test_register_source_overwrites_compliance_columns(self, storage) -> None:
        first = SourceSpec(
            key="example",
            source_name="Example Portal",
            base_url="https://example.org",
            adapter_type="ExampleAdapter",
            terms_checked=False,
        )
        source_id = register_source(storage, first)
        assert storage.get_source_by_id(source_id)["terms_checked"] == 0

        updated = SourceSpec(
            key="example",
            source_name="Example Portal",
            base_url="https://example.org",
            adapter_type="ExampleAdapter",
            terms_checked=True,
            active=False,
        )
        assert register_source(storage, updated) == source_id
        row = storage.get_source_by_id(source_id)
        assert row["terms_checked"] == 1
        assert row["active"] == 0

    def test_ppra_entry_is_researched_and_allowed(self) -> None:
        spec = spec_for("ppra_epms")
        assert spec is not None
        assert spec.terms_checked is True
        assert spec.requires_authentication is False
        assert spec.active is True
        assert "epms.ppra.gov.pk" in spec.base_url

    def test_spec_for_unknown_key_is_none(self) -> None:
        assert spec_for("does_not_exist") is None


class TestComplianceCheckScript:
    @pytest.fixture()
    def script(self):
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import compliance_check

        return compliance_check

    def test_offline_check_passes_registered_source(self, storage, script) -> None:
        report, exit_code = script.check(storage, offline=True)
        assert exit_code == 0
        assert any(line.startswith("PASS") for line in report)
        assert any("PPRA EPMS" in line for line in report)

    def test_offline_check_fails_unresearched_source(
        self, storage, script, monkeypatch
    ) -> None:
        spec = SourceSpec(
            key="unresearched",
            source_name="Unresearched Portal",
            base_url="https://unresearched.example",
            adapter_type="UnresearchedAdapter",
            terms_checked=False,  # nobody has vetted it yet
        )
        monkeypatch.setitem(SOURCE_REGISTRY, "unresearched", spec)
        report, exit_code = script.check(
            storage, offline=True, keys=["unresearched"]
        )
        assert exit_code == 1
        assert report[0].startswith("FAIL")
        assert TERMS_UNCHECKED in report[0]

    def test_offline_check_does_not_fetch_robots(self, storage, script) -> None:
        class ExplodingClient:
            def robots_allowed(self, url):
                raise AssertionError("offline mode must not touch the network")

        report, exit_code = script.check(
            storage, offline=True, client=ExplodingClient()
        )
        assert exit_code == 0
        assert len(report) == len(SOURCE_REGISTRY)

    def test_unknown_key_fails_loudly(self, storage, script) -> None:
        report, exit_code = script.check(storage, offline=True, keys=["nope"])
        assert exit_code == 1
        assert "no registry entries matched" in report[0]
