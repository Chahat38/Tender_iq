"""CLI entry point for the TenderScout collector.

Three modes (from the repository root):

    python collector/main.py [--offline] [--db PATH] [--adapter NAME]
        Run one source adapter, upsert every normalized opportunity
        into SQLite and write a ``source_fetch_logs`` row.

    python collector/main.py --search QUERY [--limit N] [--offset N]
        Full-text search the collected opportunities (FTS5).

    python collector/main.py --rebuild-index
        Rebuild the FTS5 index from all stored opportunities (use after
        collecting with a pre-search database version).

``--offline`` reads from ``collector/fixtures/`` instead of the network
(and is how tests and local runs stay off the live site).

``--enrich-details`` additionally fetches each tender's detail page
(politely, same rate limits) and merges the richer fields (procurement
type, document links) into the stored record. Detail failures are
isolated: they are reported and counted, but a failed detail page never
prevents the listing-level data from being stored.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from adapters.ppra_epms import PPRAEpmsAdapter  # noqa: E402
from search import SearchIndex  # noqa: E402
from storage import Storage, utc_now_iso  # noqa: E402

#: Registry of available source adapters (one entry per source).
ADAPTERS = {
    "ppra_epms": PPRAEpmsAdapter,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="TenderScout collector")
    parser.add_argument(
        "--db",
        default=os.environ.get("TENDERSCOUT_DB_PATH", "collector/tenderscout.db"),
        help="SQLite database path (default: $TENDERSCOUT_DB_PATH or collector/tenderscout.db)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="read from collector/fixtures/ instead of the network",
    )
    parser.add_argument(
        "--enrich-details",
        action="store_true",
        help="also fetch each tender's detail page and merge its richer "
        "fields (procurement type, document links) before storing",
    )
    parser.add_argument(
        "--adapter",
        choices=sorted(ADAPTERS),
        default="ppra_epms",
        help="which source adapter to run",
    )
    parser.add_argument(
        "--search",
        metavar="QUERY",
        default=None,
        help="full-text search the collected opportunities and exit",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum results for --search (default: 20)",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="skip this many results for --search (default: 0)",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="rebuild the FTS5 index from all stored opportunities and exit",
    )
    args = parser.parse_args(argv)
    if args.search is not None and args.rebuild_index:
        parser.error("--search and --rebuild-index are mutually exclusive")
    return args


def _print_search_results(index: SearchIndex, args: argparse.Namespace) -> None:
    """Print ranked search results in a plain-text report."""
    results = index.search(args.search, limit=args.limit, offset=args.offset)
    print(f"Query: {args.search!r} - {len(results)} result(s)")
    for result in results:
        print(f"\n[{result.external_id}] {result.title or '(no title)'}")
        print(f"  rank:      {result.rank:.2f}")
        print(f"  org:       {result.organization or '-'}")
        print(f"  deadline:  {result.deadline or 'none recorded'}")
        print(f"  location:  {result.location or '-'}")
        print(f"  category:  {result.category or '-'}")
        print(f"  url:       {result.source_url}")
        if result.snippet:
            print(f"  snippet:   {result.snippet}")


def main(argv: list[str] | None = None) -> int:
    """Run in search mode or collection mode; see the module docstring."""
    args = parse_args(argv)
    storage = Storage(args.db)

    # ----- search mode: query or rebuild the FTS5 index, then exit -----
    if args.search is not None or args.rebuild_index:
        try:
            index = SearchIndex(storage)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            storage.close()
            return 2
        try:
            if args.rebuild_index:
                count = index.rebuild()
                print(f"Rebuilt search index: {count} opportunity(s) indexed.")
            else:
                _print_search_results(index, args)
        finally:
            storage.close()
        return 0

    # ------------------- collection mode (Stage 1-3) --------------------
    started_at = utc_now_iso()
    try:
        adapter = ADAPTERS[args.adapter](offline=args.offline)
        result = adapter.run(storage=storage, enrich_details=args.enrich_details)
        source_id = storage.get_or_create_source(
            adapter.source_name, type(adapter).__name__
        )
        storage.log_fetch(
            source_id,
            started_at=started_at,
            finished_at=utc_now_iso(),
            found=result.found,
            created=result.created,
            updated=result.updated,
            failed=result.failed,
            error_message="; ".join(result.errors) or None,
        )
        unchanged = result.found - result.created - result.updated - result.failed
        mode = "offline" if args.offline else "online"
        print(f"Source:    {adapter.source_name} ({mode})")
        print(f"Database:  {storage.db_path}")
        print(f"Found:     {result.found}")
        print(f"Created:   {result.created}")
        print(f"Updated:   {result.updated}")
        print(f"Unchanged: {unchanged}")
        print(f"Failed:    {result.failed}")
        if args.enrich_details:
            print(f"Enriched:  {result.enriched}")
            print(f"Details failed: {result.detail_failures}")
        for error in result.errors:
            print(f"  error: {error}")
        _warn_if_index_out_of_sync(storage)
        return 0 if result.failed == 0 else 1
    finally:
        storage.close()


def _warn_if_index_out_of_sync(storage: Storage) -> None:
    """Print a hint when the search index does not cover every row.

    Advisory only: never lets an index problem fail a collection run.
    """
    try:
        index = SearchIndex(storage)
        indexed = index.indexed_doc_count()
    except (RuntimeError, sqlite3.DatabaseError) as exc:
        print(
            f"warning: could not check the search index ({exc}); "
            "full-text search may be unavailable."
        )
        return
    stored = storage.count_opportunities()
    if indexed != stored:
        print(
            f"warning: search index covers {indexed} of {stored} stored "
            "opportunity(s); run: python collector/main.py --rebuild-index"
        )


if __name__ == "__main__":
    raise SystemExit(main())
