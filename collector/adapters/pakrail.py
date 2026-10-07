"""Pakistan Railways tenders - one adapter for railway procurements.

Source: ``https://www.pakrail.gov.pk/Tenders/index.aspx`` - the public
"Tenders" archive of Pakistan Railways (the operating side, separate
from the ministry site ``railways.gov.pk``).

Verified facts (2026-10-07, robots.txt returns 404 -> no restrictions
declared, PoliteClient's robots gate passes):

* each tender renders as a server-side ``tr`` inside the "Latest
  Tenders" table (no JS rendering) carrying a title, a department
  (``Department:``), a description paragraph, opening/closing dates
  (e.g. ``17 November, 2026``) and a ``View Details`` link to
  ``TenderDetails.aspx?ID=N``;
* pagination is a plain GET ``?pno=N`` (50 items per page, newest
  first; ``?pno=1`` is identical to the default page and is skipped);
  ``since`` stops the crawl early once a page has no newer opening
  dates left to collect;
* the stable identifier is the numeric detail ``ID``; external_id is
  ``PRL-<ID>`` to keep it unique across sources;
* the listing rows have no city or procurement-type and the ref number
  lives on the (unfetched) detail page - listing-only adapter, same as
  EPADS/GB PPRA; departments usually embed their city in the name.

Offline mode reads ``pakrail_tenders*.html`` fixtures instead.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag
from dateutil import parser as date_parser
from dateutil.tz import gettz
from models import NormalizedOpportunity, RawListing

from .base import FIXTURES_DIR, SourceAdapter

logger = logging.getLogger(__name__)

SOURCE_NAME = "Pakistan Railways"
BASE_URL = "https://www.pakrail.gov.pk"
LISTING_PATH = "/Tenders/index.aspx"

#: Listing fixtures (page snapshots) read in offline mode, sorted by name.
OFFLINE_LISTING_GLOB = "pakrail_tenders*.html"

#: Detail link fragment that carries the numeric tender ID.
_DETAIL_HREF = "TenderDetails.aspx?ID="

#: External-id prefix so PRL ids never collide with other sources.
_ID_PREFIX = "PRL-"

#: Safety cap for online pagination. The site hosts 46+ pages of 50 but
#: is also flaky (intermittent 20s timeouts despite polite pacing), so
#: the crawl sticks to the 10 newest pages (~500 tenders); scheduled
#: runs then mostly re-verify already-stored rows through dedup.
MAX_LISTING_PAGES = 10

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
    """Parse a ``17 November, 2026`` cell date into Asia/Karachi time."""
    text = _clean(text)
    if text is None:
        return None
    try:
        parsed = date_parser.parse(text)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_KARACHI_TZ)
    return parsed


class PakrailAdapter(SourceAdapter):
    """Adapter for the Pakistan Railways tenders archive."""

    source_name = SOURCE_NAME
    base_url = BASE_URL

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return every raw listing row currently on the page 1..N range.

        Offline mode reads each ``pakrail_tenders*.html`` fixture once
        (page order = filename sort order). Online mode walks the
        paginated list until a page yields no rows or stops adding
        newer publications than ``since``.
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
                listings.extend(self._parse_listing_page(path.read_text(encoding="utf-8")))
            return listings

        listings = []
        first = self._fetch_and_parse()
        if not first:
            return listings
        listings.extend(first)
        for page in range(2, MAX_LISTING_PAGES + 1):
            html = self.get_html(f"{self.base_url}{LISTING_PATH}?pno={page}")
            rows = self._parse_listing_page(html)
            if not rows:
                break
            listings.extend(rows)
            if since is not None:
                newest = max(
                    (self._row_opening_at(row) for row in rows),
                    default=None,
                )
                if newest is None or newest < since:
                    break
        return listings

    def _fetch_and_parse(self) -> list[RawListing]:
        """Fetch the default first page and parse it."""
        html = self.get_html(f"{self.base_url}{LISTING_PATH}")
        return self._parse_listing_page(html)

    @staticmethod
    def _row_opening_at(raw: RawListing) -> datetime | None:
        """The parsed opening date of a listing row raw."""
        return _parse_date((raw.raw_data or {}).get("opening_text"))

    # ------------------------------------------------------------------
    # listing-page parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        """Parse one page of table rows into raw listings."""
        soup = BeautifulSoup(html, "lxml")
        listings = []
        for table in soup.select("table.table-hover"):
            for row in table.select("tr"):
                raw = self._row_to_raw(row)
                if raw is not None:
                    listings.append(raw)
        return listings

    def _row_to_raw(self, row: Tag) -> RawListing | None:
        """Extract one row into a RawListing, or None to skip."""
        link = None
        for a in row.find_all("a", href=True):
            if _DETAIL_HREF in a["href"]:
                link = a
                break
        if link is None:
            return None
        match = re.search(r"ID=(\d+)", link["href"])
        if match is None:
            return None
        external_id = f"{_ID_PREFIX}{match.group(1)}"
        detail_url = urljoin(f"{self.base_url}{LISTING_PATH}", link["href"])

        title = None
        h4 = row.select_one("h4")
        if h4 is not None:
            title = _clean(h4.get_text(" ", strip=True))

        organization = paragraph = None
        p = row.select_one("p")
        if p is not None:
            # Children order is: <strong>Department:</strong> <org name>
            # <br/> <description>. Walk the direct children so the org
            # name after the label is not merged into the description.
            label = None
            after_break = False
            for child in p.children:
                child_name = getattr(child, "name", None)
                if child_name == "strong":
                    label = _clean(child.get_text(" ", strip=True))
                    continue
                if child_name == "br":
                    after_break = True
                    continue
                text = _clean(child)
                if not text:
                    continue
                if after_break and paragraph is None:
                    paragraph = text
                elif organization is None and after_break is False:
                    organization = text
            if organization is None and label:
                organization = label

        cells = row.find_all("td")
        opening_text = _clean(cells[1].get_text(" ", strip=True)) if len(cells) > 1 else None
        closing_text = _clean(cells[2].get_text(" ", strip=True)) if len(cells) > 2 else None

        return RawListing(
            external_id=external_id,
            url=detail_url,
            raw_data={
                "title": title,
                "organization": organization,
                "description": paragraph,
                "opening_text": opening_text,
                "closing_text": closing_text,
            },
        )

    # ------------------------------------------------------------------
    # normalization
    # ------------------------------------------------------------------

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        """Build the normalized opportunity from one raw row."""
        data = raw.raw_data or {}
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=None,
            title=data.get("title"),
            organization=data.get("organization"),
            description=data.get("description"),
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_date(data.get("opening_text")),
            deadline=_parse_date(data.get("closing_text")),
            location=None,
            province=None,  # nationwide operator, not a province source
            category=None,
            procurement_type=None,
            document_urls=list(data.get("document_urls") or []),
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        """Listing rows expose no direct document files yet."""
        return []