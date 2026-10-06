"""Check - and record - every registered source's crawl compliance.

For each entry in collector/registry.py this script:

1. writes the registry's compliance facts onto the source row
   (``register_source``),
2. fetches ``<origin>/robots.txt`` through PoliteClient (unless
   ``--offline``) and records the verdict in ``robots_allowed``,
3. evaluates the gate from compliance.violations() and prints a
   PASS/FAIL report.

Exit codes: 0 = every active source passes, 1 = at least one FAIL
(the "fail loudly" case), 2 = usage/storage error.

Usage (from the repo root):

    python scripts/compliance_check.py [--offline] [--source NAME]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))

from compliance import record_robots_status, violations  # noqa: E402
from http_client import PoliteClient  # noqa: E402
from registry import SOURCE_REGISTRY, register_source  # noqa: E402
from storage import open_storage  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip the robots.txt fetch; evaluate the recorded flags only",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="KEY",
        help="limit the check to one registry key (repeatable)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite database path (default: $TENDERSCOUT_DB_PATH / tenderscout.db)",
    )
    return parser.parse_args(argv)


def check(
    storage,
    *,
    offline: bool = False,
    keys: list[str] | None = None,
    client: PoliteClient | None = None,
) -> tuple[list[str], int]:
    """Run the gate over the registry.

    Returns ``(report_lines, exit_code)`` where exit_code is 0 when
    every checked source passes and 1 otherwise.
    """
    report: list[str] = []
    exit_code = 0
    selected = [
        spec
        for key, spec in SOURCE_REGISTRY.items()
        if keys is None or key in keys
    ]
    if not selected:
        return [f"no registry entries matched: {keys}"], 1

    client = client if client is not None else (None if offline else PoliteClient())
    for spec in selected:
        source_id = register_source(storage, spec)
        row = storage.get_source_by_id(source_id)
        if row is None:
            report.append(f"FAIL  {spec.source_name}: source row disappeared")
            exit_code = 1
            continue

        if not offline:
            assert client is not None
            status = client.robots_allowed(f"{spec.base_url}/")
            storage.set_robots_allowed(source_id, status)
            row = storage.get_source_by_id(source_id) or row

        reasons = violations(row)
        if reasons:
            exit_code = 1
            verdict = "FAIL"
        else:
            verdict = "PASS"
        terms = "yes" if row.get("terms_checked") else "NO"
        auth = "yes" if row.get("requires_authentication") else "no"
        active = "yes" if row.get("active") else "NO"
        report.append(
            f"{verdict} {spec.source_name:<16} "
            f"{record_robots_status(row.get('robots_allowed')):<16} "
            f"terms={terms:<3} auth={auth:<3} active={active}"
            + ("" if not reasons else "  -> " + "; ".join(reasons))
        )
    return report, exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    storage = open_storage(args.db)
    try:
        report, exit_code = check(
            storage, offline=args.offline, keys=args.sources
        )
    finally:
        storage.close()
    for line in report:
        print(line)
    if exit_code:
        print(
            "\ncompliance gate: at least one source MUST NOT be crawled - "
            "fix its registry entry or source row (never bypass the gate).",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
