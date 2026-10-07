"""AJ&K PPRA advertisements - one adapter for Azad Kashmir tenders.

Source: ``https://www.ajkppra.gov.pk/advertisements.php`` - the public
"Latest Advertisements [Active Procurement]" page of the Azad Jammu &
Kashmir Public Procurement Regulatory Authority.

Verified facts (2026-10-07, robots.txt absent -> 404 means no declared
restrictions; PoliteClient's robots gate passes):

* a single server-rendered table with columns:
  ``Procurement | Procurement Title | Publishing Date | Closing Date |
  Department | Procuring Agency | Download`` - no JS rendering;
* one page, roughly the latest ~40-50 advertisements (no pagination);
  ``since`` is applied client-side per row;
* the ``Download File`` cell holds the tender-notice document link (a
  PDF) - an absolute URL after ``urljoin``;
* dates are ``DD-MM-YYYY`` and are read as Asia/Karachi time;
* the advertisement number (e.g. ``6654``) is the row's reference.

Offline mode reads ``ajk_advertisements*.html`` fixtures instead.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from dateutil.tz import gettz
from models import NormalizedOpportunity, RawListing

from .base import FIXTURES_DIR, SourceAdapter

logger = logging.getLogger(__name__)

SOURCE_NAME = "AJK PPRA"
BASE_URL = "https://www.ajkppra.gov.pk"
LISTING_PATH = "/advertisements.php"

#: Listing fixtures (page snapshots) read in offline mode, sorted by name.
OFFLINE_LISTING_GLOB = "ajk_advertisements*.html"

#: Column headers that must be present before any row is trusted.
REQUIRED_HEADERS = (
    "procurement",
    "procurement title",
    "publishing date",
    "closing date",
    "department",
    "procuring agency",
    "download",
)

#: The advertisement number is the last token of the first cell.
_TRAILING_NUMBER = re.compile(r"(\d+)\s*$")

_KARACHI_TZ = gettz("Asia/Karachi")
if _KARACHI_TZ is None:
    raise RuntimeError(
        "Cannot resolve the Asia/Karachi timezone on this system; "
        "TenderScout refuses to guess timezone offsets."
    )


def _clean(value: object) -> str | None:
    """Strip whitespace; return None for empty results."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(text: str | None) -> datetime | None:
    """Parse a ``DD-MM-YYYY`` date into an Asia/Karachi datetime, or None."""
    text = _clean(text)
    if text is None:
        return None
    try:
        parsed = date_parser.parse(text, dayfirst=True)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KARACHI_TZ)
    return parsed


class AjkPpraAdapter(SourceAdapter):
    """Adapter for the AJ&K PPRA latest-advertisements page."""

    source_name = SOURCE_NAME
    base_url = BASE_URL

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return every raw listing row on the AJ&K PPRA page.

        Offline mode reads each ``ajk_advertisements*.html`` fixture
        once (filename sort order). Online mode fetches the single
        ``advertisements.php`` page.

        ``since`` is accepted and applied by :meth:`normalize` callers
        via the pipeline; the source has no date-query parameters.
        """
        if self.offline:
            paths = sorted(FIXTURES_DIR.glob(OFFLINE_LISTING_GLOB))
            if not paths:
                raise FileNotFoundError(
                    f"No listing fixtures matching {OFFLINE_LISTING_GLOB!r} in "
                    f"{FIXTURES_DIR}; save some with scripts/save_fixture.py first."
                )
            listings: list[RawListing] = []
            for path in paths:
                listings.extend(
                    self._parse_listing_page(path.read_text(encoding="utf-8"))
                )
            return listings
        html = self.get_html(f"{self.base_url}{LISTING_PATH}")
        return self._parse_listing_page(html)

    # ------------------------------------------------------------------
    # listing-page parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        """Parse the advertisements table into raw listings.

        The table headers are verified first; with an unexpected
        structure this raises ValueError instead of guessing column
        positions.
        """
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        if table is None:
            return []
        headers = [
            th.get_text(" ", strip=True).lower() for th in table.select("th")
        ]
        missing = [h for h in REQUIRED_HEADERS if h not in headers]
        if missing:
            raise ValueError(
                f"Unexpected AJ&K PPRA listing table structure; missing "
                f"columns: {missing}. Refusing to guess column positions."
            )

        listings = []
        for tr in table.select("tr"):
            raw = self._row_to_raw(tr)
            if raw is not None:
                listings.append(raw)
        return listings

    def _row_to_raw(self, tr: Tag) -> RawListing | None:
        """Extract one advertisement row into a RawListing, or None to skip."""
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 7:
            return None
        proc_cell, title_cell = cells[0], cells[1]

        proc_text = _clean(proc_cell.get_text(" ", strip=True)) or ""
        match = _TRAILING_NUMBER.search(proc_text)
        if match is None:
            return None
        external_id = match.group(1)

        title = _clean(title_cell.get_text(" ", strip=True))
        if not title:
            return None

        # The last cell carries the tender-notice document link(s).
        download_cell = cells[6]
        doc_urls = [
            urljoin(self.base_url, a["href"])
            for a in download_cell.find_all("a")
            if a.get("href") and not a["href"].startswith("javascript:")
        ]

        return RawListing(
            external_id=external_id,
            url=doc_urls[0] if doc_urls else f"{self.base_url}{LISTING_PATH}",
            raw_data={
                "notice_type": proc_text[: -len(match.group(1))].strip() or None,
                "title": title,
                "publishing_date": _clean(cells[2].get_text(" ", strip=True)),
                "closing_date": _clean(cells[3].get_text(" ", strip=True)),
                "department": _clean(cells[4].get_text(" ", strip=True)),
                "procuring_agency": _clean(cells[5].get_text(" ", strip=True)),
                "document_urls": doc_urls,
            },
        )

    # ------------------------------------------------------------------
    # normalization
    # ------------------------------------------------------------------

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        """Build the normalized opportunity from one raw advertisement row."""
        data = raw.raw_data or {}
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=raw.external_id,
            title=data.get("title"),
            organization=data.get("procuring_agency"),
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_date(data.get("publishing_date")),
            deadline=_parse_date(data.get("closing_date")),
            location=None,
            province=None,  # AJ&K jurisdiction, not a province field
            category=None,
            # The notice form ("Tender Notice", "Prequalification Notice",
            # ...) is the closest thing the listing carries.
            procurement_type=data.get("notice_type"),
            document_urls=list(data.get("document_urls") or []),
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        """Tender-notice documents live in the Download File cell."""
        data = raw.raw_data or {}
        return list(data.get("document_urls") or [])