"""Print the first 5 normalized tenders as JSON for manual review.

Reads saved fixtures by default (offline, per project rule 10); pass
``--online`` to crawl the live portal politely instead.

Usage (from the repository root):

    python collector/scripts/preview.py [--online] [--limit N] [--details]

``--details`` additionally resolves each tender's detail page (offline
fixtures or live) to attach document URLs and the richer detail fields.
Output is a JSON array on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_COLLECTOR_DIR = _SCRIPT_DIR.parent
if str(_COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_COLLECTOR_DIR))

from adapters.ppra_epms import PPRAEpmsAdapter  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Preview normalized tenders as JSON (fixtures by default)."
    )
    parser.add_argument(
        "--online", action="store_true", help="crawl the live site instead of fixtures"
    )
    parser.add_argument("--limit", type=int, default=5, help="how many tenders to show")
    parser.add_argument(
        "--details",
        action="store_true",
        help="also resolve detail pages (document URLs, procurement type)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Print up to ``limit`` normalized tenders as a JSON array."""
    args = parse_args(argv)
    adapter = PPRAEpmsAdapter(offline=not args.online)
    listings = adapter.fetch_listings()[: max(args.limit, 0)]
    output = []
    for raw in listings:
        opportunity = adapter.normalize(raw)
        if args.details:
            try:
                detail_raw = adapter.fetch_opportunity(raw.external_id)
                opportunity = adapter.normalize_detail(detail_raw)
            except FileNotFoundError:
                pass  # no fixture for this tender; keep the listing-level data
        output.append(opportunity.model_dump(mode="json"))
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
