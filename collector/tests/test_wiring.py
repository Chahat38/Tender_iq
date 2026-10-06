"""Wiring checks: every adapter is registered, gated and scheduled.

These are cheap consistency tests that fail loudly when Stage 3 adds an
adapter to one place (CLI registry, compliance registry, CI matrix) but
forgets another - the exact drift that would otherwise only show up as
a source silently never being collected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from main import ADAPTERS
from registry import SOURCE_REGISTRY, spec_for

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "collect.yml"


class TestAdapterWiring:
    def test_every_cli_adapter_has_a_registry_spec(self) -> None:
        missing = [key for key in ADAPTERS if spec_for(key) is None]
        assert missing == [], (
            f"adapters without a compliance registry entry: {missing} - "
            "they cannot run (the gate blocks unregistered sources)"
        )

    def test_every_registry_spec_has_a_cli_adapter(self) -> None:
        missing = [key for key in SOURCE_REGISTRY if key not in ADAPTERS]
        assert missing == [], f"registry entries without an adapter: {missing}"

    def test_registered_specs_pass_the_gate(self) -> None:
        from compliance import passes

        for key, spec in SOURCE_REGISTRY.items():
            assert passes(
                {
                    "name": spec.source_name,
                    "active": spec.active,
                    "terms_checked": spec.terms_checked,
                    "requires_authentication": spec.requires_authentication,
                    "robots_allowed": None,
                }
            ), f"{key} would be blocked by the compliance gate"

    def test_registry_adapter_type_matches_the_class(self) -> None:
        for key, spec in SOURCE_REGISTRY.items():
            assert spec.adapter_type == ADAPTERS[key].__name__

    def test_registry_source_name_matches_the_adapter(self) -> None:
        for key, spec in SOURCE_REGISTRY.items():
            assert spec.source_name == ADAPTERS[key].source_name

    def test_ci_matrix_covers_every_adapter(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        match = re.search(r"adapter:\s*\[([^\]]+)\]", workflow)
        assert match, "collect.yml has no matrix adapter list"
        matrix = {item.strip() for item in match.group(1).split(",")}
        assert matrix == set(ADAPTERS), (
            "collect.yml matrix out of sync with ADAPTERS: "
            f"matrix={sorted(matrix)} adapters={sorted(ADAPTERS)}"
        )

    @pytest.mark.parametrize("key", sorted(ADAPTERS))
    def test_adapter_instantiates_offline(self, key: str) -> None:
        adapter = ADAPTERS[key](offline=True)
        assert adapter.source_name == SOURCE_REGISTRY[key].source_name
