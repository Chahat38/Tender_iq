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

from adapters.ajk_ppra import AjkPpraAdapter  # noqa: E402
from adapters.epads import EpadsAdapter  # noqa: E402
from adapters.gb_ppra import GbPpraAdapter  # noqa: E402
from adapters.kppra import KppraActiveTendersAdapter  # noqa: E402
from adapters.pakrail import PakrailAdapter  # noqa: E402
from adapters.ppra_epms import PPRAEpmsAdapter  # noqa: E402
from ai import run_analysis, run_matching  # noqa: E402
from alerts import run as run_alerts  # noqa: E402
from compliance import ComplianceError  # noqa: E402
from dedup import run as run_dedup  # noqa: E402
from documents import DOCUMENTS_DIR, download_documents  # noqa: E402
from ops import EXIT_COMPLIANCE, run_source  # noqa: E402
from search import SearchIndex  # noqa: E402
from storage import Storage, open_storage  # noqa: E402

#: Registry of available source adapters (one entry per source).
ADAPTERS = {
    "ppra_epms": PPRAEpmsAdapter,
    "kppra_active_tenders": KppraActiveTendersAdapter,
    "epads": EpadsAdapter,
    "ajk_ppra": AjkPpraAdapter,
    "gb_ppra": GbPpraAdapter,
    "pakrail": PakrailAdapter,
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
        "--enrich-limit",
        type=int,
        default=None,
        help="maximum detail pages to fetch per run when --enrich-details "
        "is set (default: no limit)",
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
    parser.add_argument(
        "--dedup-only",
        action="store_true",
        help="run cross-source duplicate detection on stored opportunities "
        "and exit (also runs automatically after every collection)",
    )
    parser.add_argument(
        "--documents",
        action="store_true",
        help="download queued tender documents (content-addressed, "
        "polite client) and exit",
    )
    parser.add_argument(
        "--docs-limit",
        type=int,
        default=50,
        help="maximum documents to fetch per --documents run (default: 50)",
    )
    parser.add_argument(
        "--docs-dir",
        default=os.environ.get("TENDERSCOUT_DOCUMENTS_DIR"),
        help="where stored documents live (default: $TENDERSCOUT_DOCUMENTS_DIR "
        "or var/documents)",
    )
    parser.add_argument(
        "--docs-retry-failed",
        action="store_true",
        help="re-queue documents whose previous fetch attempt failed",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="run the AI analysis pass on opportunities missing an "
        "opportunity_ai row and exit (also runs after every collection)",
    )
    parser.add_argument(
        "--match-user",
        metavar="USER_ID",
        default=None,
        help="score opportunity_matches for one user and exit",
    )
    parser.add_argument(
        "--match-limit",
        type=int,
        default=500,
        help="maximum opportunities to score per --match-user run "
        "(default: 500)",
    )
    parser.add_argument(
        "--alerts-only",
        action="store_true",
        help="run the user-facing alert worker and exit (also runs after "
        "every collection)",
    )
    args = parser.parse_args(argv)
    if args.search is not None and args.rebuild_index:
        parser.error("--search and --rebuild-index are mutually exclusive")
    exclusive = {
        "--dedup-only": args.dedup_only,
        "--documents": args.documents,
        "--analyze-only": args.analyze_only,
        "--match-user": args.match_user is not None,
        "--alerts-only": args.alerts_only,
    }
    if sum(bool(value) for value in exclusive.values()) > 1:
        parser.error(
            "--dedup-only, --documents, --analyze-only, --match-user and "
            "--alerts-only are mutually exclusive"
        )
    if any(exclusive.values()) and (
        args.search is not None or args.rebuild_index
    ):
        parser.error(
            "standalone modes cannot be combined with --search/--rebuild-index"
        )
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
    # Postgres when DATABASE_URL is set (production), SQLite otherwise
    # (tests, offline runs, single-box installs).
    storage = open_storage(args.db)

    # ----- search mode: query or rebuild the FTS5 index, then exit -----
    if args.search is not None or args.rebuild_index:
        if not isinstance(storage, Storage):
            print(
                "error: --search/--rebuild-index run against the SQLite "
                "database only (unset DATABASE_URL or pass --db)",
                file=sys.stderr,
            )
            storage.close()
            return 2
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

    # ------------------- document pipeline (Stage 5) --------------------
    if args.documents:
        try:
            report = download_documents(
                storage,
                documents_dir=args.docs_dir or DOCUMENTS_DIR,
                limit=args.docs_limit,
                retry_failed=args.docs_retry_failed,
            )
            print(
                "Documents: "
                f"downloaded {report['downloaded']}, "
                f"extracted {report['extracted']}, "
                f"failed {report['failed']}"
            )
            return 0
        finally:
            storage.close()

    # -------------------- AI stage (Stage 6) ---------------------------
    if args.analyze_only:
        try:
            report = run_analysis(storage)
            print(
                "AI:        "
                f"analyzed {report['analyzed']} "
                f"(llm {report['llm']}, deterministic {report['deterministic']}, "
                f"llm errors {report['failed']})"
            )
            return 0
        finally:
            storage.close()

    if args.match_user is not None:
        try:
            report = run_matching(storage, args.match_user, limit=args.match_limit)
            print(
                "Matches:   "
                f"scored {report['scored']} "
                f"(high {report['high']}, medium {report['medium']}, "
                f"low {report['low']}), analyzed {report['analyzed']}"
            )
            return 0
        finally:
            storage.close()

    if args.alerts_only:
        try:
            report = run_alerts(storage)
            print(
                "Alerts:    "
                f"created {report['total']} "
                f"(match {report['new_match']}, deadline {report['deadline_approaching']}, "
                f"updated {report['opportunity_updated']}, "
                f"corrigendum {report['corrigendum']})"
            )
            return 0
        finally:
            storage.close()

    # ------------------- collection mode (Stage 1-3) --------------------
    if args.dedup_only:
        try:
            report = run_dedup(storage)
            print(
                "Dedup:     "
                f"examined {report['examined']}, "
                f"candidates {report['candidate_pairs']}, "
                f"merged {report['merged']}, queued {report['queued']}"
            )
            return 0
        finally:
            storage.close()

    try:
        adapter = ADAPTERS[args.adapter](offline=args.offline)
        try:
            outcome = run_source(
                storage,
                adapter,
                args.adapter,
                enrich_details=args.enrich_details,
                enrich_limit=args.enrich_limit,
            )
        except ComplianceError as exc:
            # Loud, non-zero exit: a policy stop, not a transient error.
            print(str(exc), file=sys.stderr)
            return EXIT_COMPLIANCE
        result = outcome["result"]
        unchanged = result.found - result.created - result.updated - result.failed
        mode = "offline" if args.offline else "online"
        print(f"Source:    {adapter.source_name} ({mode})")
        print(f"Database:  {getattr(storage, 'db_path', getattr(storage, 'dsn', '?'))}")
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
        if outcome["ops"]["alert"] is not None:
            print(
                "  operational alert written: "
                f"{outcome['ops']['alert']['message']}"
            )
        dedup_report = run_dedup(storage)
        print(
            "Dedup:     "
            f"examined {dedup_report['examined']}, "
            f"merged {dedup_report['merged']}, "
            f"queued {dedup_report['queued']}"
        )
        ai_report = run_analysis(storage)
        print(
            "AI:        "
            f"analyzed {ai_report['analyzed']} "
            f"(llm {ai_report['llm']}, deterministic {ai_report['deterministic']})"
        )
        alert_report = run_alerts(storage)
        print(
            "Alerts:    "
            f"created {alert_report['total']} "
            f"(match {alert_report['new_match']}, "
            f"deadline {alert_report['deadline_approaching']})"
        )
        _warn_if_index_out_of_sync(storage)
        return 0 if result.failed == 0 else 1
    finally:
        storage.close()


def _warn_if_index_out_of_sync(storage: Storage) -> None:
    """Print a hint when the search index does not cover every row.

    Advisory only: never lets an index problem fail a collection run.
    The FTS5 index is a SQLite feature; runs against Postgres skip it
    (Supabase serves search from its own GIN indexes).
    """
    if not isinstance(storage, Storage):
        return
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
