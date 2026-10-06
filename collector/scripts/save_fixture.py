"""Save a public tender page as a local fixture for offline work.

Usage (from the repository root):

    python collector/scripts/save_fixture.py <url> <name> [--force]

The response body is saved to ``collector/fixtures/<name>.html`` and the
script prints the HTTP status, the size in bytes and the first 500
characters of the body.

Politeness rules (global rules 6 and 7) are enforced by PoliteClient:

* robots.txt is checked before anything is fetched: disallowed URLs are
  refused with a clear error, and so are URLs whose robots.txt cannot
  be fetched (we do not crawl what we cannot verify);
* requests use the TenderScoutBot User-Agent with a contact address
  from the ``CONTACT_EMAIL`` environment variable, a 2.5 s delay
  between requests, a 20 s timeout and bounded retries.

Exit codes: 0 success, 1 error (bad name, existing fixture, HTTP error,
I/O problem), 2 robots.txt refusal.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import requests

# Allow running as ``python collector/scripts/save_fixture.py ...`` from
# the repository root: put the collector directory on sys.path first.
_SCRIPT_DIR = Path(__file__).resolve().parent
_COLLECTOR_DIR = _SCRIPT_DIR.parent
if str(_COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_COLLECTOR_DIR))

from adapters.base import FIXTURES_DIR  # noqa: E402
from http_client import PoliteClient, RobotsError  # noqa: E402

#: Fixture names must be plain filenames so nothing can escape the
#: fixtures directory.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: How many characters of the body to print as a preview.
PREVIEW_CHARS = 500


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Save a public page as a collector fixture "
            "(robots.txt is enforced; nothing is saved on refusal)."
        ),
        epilog=(
            "Example: python collector/scripts/save_fixture.py "
            "https://example.gov.pk/tenders listing_page"
        ),
    )
    parser.add_argument("url", help="Public URL of the listing or detail page")
    parser.add_argument(
        "name", help="Fixture name; stored as collector/fixtures/<name>.html"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the fixture if it already exists",
    )
    return parser.parse_args(argv)


def target_path(name: str) -> Path:
    """Return ``collector/fixtures/<name>.html`` for a safe ``name``.

    A trailing ``".html"`` on ``name`` is accepted and not doubled.
    Raises ValueError for names that are not plain filenames.
    """
    if name.endswith(".html"):
        name = name[: -len(".html")]
    if not _SAFE_NAME.match(name):
        raise ValueError(
            f"Invalid fixture name {name!r}: "
            "use letters, digits, '.', '_' or '-' only"
        )
    return FIXTURES_DIR / f"{name}.html"


def save_fixture(
    url: str,
    name: str,
    force: bool = False,
    client: PoliteClient | None = None,
) -> tuple[Path, requests.Response]:
    """Fetch ``url`` politely and save the body to the fixtures directory.

    Returns ``(path, response)``. Raises:

    * ``ValueError`` for an unsafe fixture name,
    * ``FileExistsError`` when the fixture exists and ``force`` is False,
    * ``RobotsError`` when robots.txt disallows the URL or cannot be
      verified (nothing is fetched in that case),
    * ``RuntimeError`` for non-2xx responses (nothing is saved).
    """
    path = target_path(name)
    if path.exists() and not force:
        raise FileExistsError(
            f"{path} already exists; rerun with --force to overwrite"
        )
    if client is None:
        client = PoliteClient()
    response = client.get(url)
    if response.status_code >= 400:
        raise RuntimeError(
            f"HTTP {response.status_code} for {url}; nothing was saved"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return path, response


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = parse_args(argv)
    if not os.environ.get("CONTACT_EMAIL"):
        print(
            "Warning: CONTACT_EMAIL is not set; the crawler User-Agent will "
            "identify as 'contact: unset'. Set it before crawling real sources.",
            file=sys.stderr,
        )
    try:
        path, response = save_fixture(args.url, args.name, force=args.force)
    except RobotsError as exc:
        print(f"Refused by robots.txt policy: {exc}", file=sys.stderr)
        return 2
    except (ValueError, FileExistsError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Saved:  {path}")
    print(f"Status: {response.status_code}")
    print(f"Size:   {len(response.content)} bytes")
    final_url = response.url or ""
    if final_url and final_url != args.url:
        print(f"Redirected to: {final_url}")
    print(f"--- first {PREVIEW_CHARS} characters ---")
    print(response.text[:PREVIEW_CHARS])
    print("--- end of preview ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
