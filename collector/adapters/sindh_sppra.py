"""SPPRA (Sindh Public Procurement Regulatory Authority) - one adapter.

Source: ``http://e.pprasindh.gov.pk/tenderlst`` - the public "Tenders
List" of the SPPRA e-Procurement portal.

Verified facts (2026-10-07, robots.txt is ``User-agent: * / Disallow:``
- crawl-all allowed; PoliteClient's robots gate reads it as allowed):

* one server-rendered table ``table.table-striped#tender_list``;
* 15 rows per page, plain GET pagination
  ``tenderlst?tender_list[page]=N`` (37828 total rows at verify time - a
  large historical archive, so ``MAX_LISTING_PAGES`` bounds each run);
* row fields come from per-column ``class="column-*"`` cells:
  Serial No. | SPPRA ID | Department | Advertisement Date |
  Call-Closing Date | Upload On | Tender Title | (.zip/.pdf links in
  ``column-image_location`` / correction columns);
* the tender Serial (``column-Serial`` digit) is the stable key used as
  ``external_id`` - the per-row search link the form builds is
  ``tenderlst?tender_list[filters][Serial-eq]=<serial>`` (verified to
  return exactly that one row);
* all timestamps are naive verified-format dates ("2019-11-01 23:59:59"
  or "16 Sept, 2019") - Pakistan procurement, so they are read as
  Asia/Karachi time without inventing a timezone;
* some rows carry ``SPPRA ID WITHHELD`` in a sibling cell - the SPPRA ID
  column is never used for matching, the Serial is the key.

Offline mode reads ``sindh_listing*.html`` fixtures instead.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from dateutil.tz import gettz
from models import NormalizedOpportunity, RawListing

from .base import FIXTURES_DIR, SourceAdapter

logger = logging.getLogger(__name__)

SOURCE_NAME = "SPPRA Sindh"
BASE_URL = "http://e.pprasindh.gov.pk"
LISTING_PATH = "/tenderlst"
#: Verbatim pager params observed in the page's footer links.
PAGE_PARAM = "tender_list[page]"
FILTER_SERIAL_PARAM = "tender_list[filters][Serial-eq]"
PAGE_PATH_TEMPLATE = "/tenderlst?" + urlencode({PAGE_PARAM: "{page}"})

#: Search URL the site's own filter form builds; verified to return
#: exactly the one tender row for a serial.
REF_URL_TEMPLATE = "/tenderlst?" + urlencode(
    {"tender_list[filters][Serial-eq]": "{ref}"}
)

#: Listing fixtures (page snapshots) read in offline mode, sorted by name.
OFFLINE_LISTING_GLOB = "sindh_listing*.html"

#: How many listing pages one run may fetch (started from page 1). The
#: archive is 37k+ rows deep; this keeps a polite crawl bounded.
MAX_LISTING_PAGES = 20

PROVINCE = "Sindh"

#: Column classes on #tender_list that must be present before trusting
#: the structure (first 7 columns carry the data this adapter reads).
REQUIRED_COLUMN_CLASSES = (
    "column-Serial",
    "column-sppra_id",
    "column-organization_name",
    "column-advertise_date",
    "column-close_date",
    "column-upload_on",
    "column-tender_title",
)

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


def _plain_text(tr: Tag, column_class: str) -> str | None:
    """One row's column-cell text, cleaned; None when the cell is absent."""
    cell = tr.select_one(f"td.{column_class}")
    if cell is None:
        return None
    return _clean(cell.get_text(" ", strip=True))


def _parse_date(text: str | None) -> datetime | None:
    """Parse one of the source's date strings into an Asia/Karachi datetime, or None.

    Observed formats: ``2019-11-01 23:59:59``, ``dd-mmm-YYYY``,
    ``d Mon, YYYY``. Naive results are Asia/Karachi - the source states
    no timezone and serves Pakistani data. Unparseable input yields
    None - never a guessed date.
    """
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


class SindhSppraAdapter(SourceAdapter):
    """Adapter for the SPPRA Sindh public tender-list page."""

    source_name = SOURCE_NAME
    base_url = BASE_URL

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return every raw listing from the tender list (page-bounded).

        Offline mode reads each ``sindh_listing*.html`` fixture once
        (filename sort order). Online mode walks LISTING_PATH page 1,
        2, ... until :data:`MAX_LISTING_PAGES` or an empty page.
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

        listings = []
        for page in range(1, MAX_LISTING_PAGES + 1):
            html = self.get_html(f"{self.base_url}{self._page_path(page)}")
            rows = self._parse_listing_page(html)
            if not rows:
                break
            listings.extend(rows)
        return listings

    @classmethod
    def _page_path(cls, page: int) -> str:
        """Build the URL path for one listing page."""
        return f"{LISTING_PATH}?{PAGE_PARAM}={page}"

    # ------------------------------------------------------------------
    # listing-page parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        """Parse one tender-list page into raw listings.

        The table structure is verified first (:data:`REQUIRED_COLUMN_CLASSES`
        must all be present); anything unexpected raises ValueError
        instead of guessing.
        """
        soup = BeautifulSoup(html, "lxml")
        table = soup.select_one("table#tender_list")
        if table is None:
            return []
        missing = [
            cls_
            for cls_ in REQUIRED_COLUMN_CLASSES
            if table.select_one(f"th.{cls_}") is None
        ]
        if missing:
            raise ValueError(
                "Unexpected SPPRA Sindh tender-list structure; missing "
                f"column headers: {missing}. Refusing to guess."
            )

        listings: list[RawListing] = []
        seen_in_page: set[str] = set()
        for tr in table.select("tbody tr"):
            cells = tr.find_all("td", recursive=False)
            if not cells:
                continue
            serial_cell = tr.select_one("td.column-Serial")
            serial = _clean(serial_cell.get_text(" ", strip=True)) if serial_cell else None
            if not serial or not serial.isdigit():
                continue  # separator row or unexpected structure
            if serial in seen_in_page:
                continue
            seen_in_page.add(serial)
            title = _plain_text(tr, "column-tender_title")
            if not title:
                continue

            document_urls = self._row_document_hrefs(tr)
            listings.append(
                RawListing(
                    external_id=serial,
                    url=self._ref_url(serial),
                    raw_data={
                        "title": title,
                        "organization": _plain_text(
                            tr, "column-organization_name"
                        ),
                        "advertise_date": _plain_text(
                            tr, "column-advertise_date"
                        ),
                        "close_date": _plain_text(tr, "column-close_date"),
                        "upload_on": _plain_text(tr, "column-upload_on"),
                        "document_urls": document_urls,
                    },
                )
            )
        return listings

    def _row_document_hrefs(self, tr: Tag) -> list[str]:
        """Absolute URLs of this row's tender/correction document links."""
        urls: list[str] = []
        for cell_class in (
            "column-image_location",
            "column-image_correction",
            "column-image_correction2",
        ):
            cell = tr.select_one(f"td.{cell_class}")
            if cell is None:
                continue
            for a in cell.find_all("a", href=True):
                href = a["href"]
                if href.startswith("javascript:") or not href.strip():
                    continue
                absolute = urljoin(f"{self.base_url}/", href)
                if absolute not in urls:
                    urls.append(absolute)
        return urls

    def _ref_url(self, serial: str) -> str:
        """The tender search URL for one serial (the site's own filter)."""
        return (
            f"{self.base_url}{LISTING_PATH}?"
            + urlencode({FILTER_SERIAL_PARAM: serial})
        )

    # ------------------------------------------------------------------
    # normalization
    # ------------------------------------------------------------------

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        """Build the normalized opportunity from one raw listing row."""
        data = raw.raw_data or {}
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=raw.external_id,
            title=data.get("title"),
            organization=data.get("organization"),
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_date(data.get("advertise_date")),
            deadline=_parse_date(data.get("close_date")),
            location=None,
            province=PROVINCE,
            category=None,
            procurement_type=None,  # listing does not state a type
            document_urls=list(data.get("document_urls") or []),
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        """Tender/correction ZIP documents found on the listing row."""
        data = raw.raw_data or {}
        return list(data.get("document_urls") or [])
