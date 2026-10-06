"""KPPRA active tenders - one adapter for the Khyber Pakhtunkhwa PPRA.

Source: ``http://kppra.gov.pk/kppra/activetenders.php`` - the KP Public
Procurement Regulatory Authority's public "Active Tenders" listing.

Verified facts (2026-10-06, robots.txt absent -> no declared rules):

* server-rendered table ``table.custom-table`` with columns:
  Tender No | Tender Description | Procurement Entity |
  Date of Advertisement | Closing Date | Tender/EOI |
  Bidding Documents | Action;
* 25 rows per page, pagination is plain GET
  ``activetenders.php/?&sort=&order=&&p=N`` (225 records at verify time);
* there is NO per-tender detail page - the "Action" cell opens a modal
  fed by ``js/staff_tender_operations.js``, so listing-level data is all
  we take. Each tender's stable link is the site's own search URL
  ``activetenders.php?tender_ref=<no>&search=Search`` (verified to
  filter down to that one tender);
* dates are ``dd-Mmm-yyyy`` with no time of day (closing date stored at
  00:00 Asia/Karachi - the source states no time, and we do not invent
  one);
* the tender/EOI column may hold a direct tender-PDF download link
  (``staff/force_download.php?file=...``) or the text "Can be obtained
  from the P.E office";
* each data row is followed by a hidden corrigendum row
  (``tr[style*="display:none"]``); hidden rows are skipped - corrigendum
  parsing is future work and must not silently corrupt the listing.

Offline mode reads ``kppra_listing*.html`` fixtures instead.
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

SOURCE_NAME = "KPPRA Active Tenders"
BASE_URL = "http://kppra.gov.pk"
#: ``<base href="/kppra/">`` on every page - relative hrefs resolve here.
ORIGIN_PATH = "/kppra/"
LISTING_PATH = "/kppra/activetenders.php"
#: Verbatim pager pattern observed in the page's tfoot.
PAGE_PATH_TEMPLATE = "/kppra/activetenders.php/?&sort=&order=&&p={page}"
#: The site's own search form (GET + ``search=Search``) filtered to a
#: single tender - verified 2026-10-06 ("Showing 1 - 1 of 1").
REF_URL_TEMPLATE = "/kppra/activetenders.php?tender_ref={ref}&search=Search"

#: Listing fixtures (page snapshots) read in offline mode, sorted by name.
OFFLINE_LISTING_GLOB = "kppra_listing*.html"

PROVINCE = "Khyber Pakhtunkhwa"

#: Column headers that must be present before any row is trusted.
REQUIRED_HEADERS = (
    "tender no",
    "tender description",
    "procurement entity",
    "date of advertisement",
    "closing date",
)

_KARACHI_TZ = gettz("Asia/Karachi")
if _KARACHI_TZ is None:
    raise RuntimeError(
        "Cannot resolve the Asia/Karachi timezone on this system; "
        "TenderScout refuses to guess timezone offsets."
    )

#: Date format observed on the page, e.g. ``06-Oct-2026``.
_DATE_FORMAT = "%d-%b-%Y"

#: A tender number as rendered (digits only; the form's maxlength is 6
#: but historic numbers can be longer).
_REF_RE = re.compile(r"^\d{1,12}$")


def _clean(value: object) -> str | None:
    """Strip whitespace; return None for empty results."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(text: str | None) -> datetime | None:
    """Parse ``dd-Mmm-yyyy`` into an Asia/Karachi datetime, or None.

    The source states no time of day, so the result is midnight in
    Asia/Karachi. Unparseable input yields None - never a guessed date.
    """
    text = _clean(text)
    if text is None:
        return None
    try:
        parsed = datetime.strptime(text, _DATE_FORMAT)
    except ValueError:
        try:
            parsed = date_parser.parse(text)
        except (ValueError, OverflowError, TypeError):
            return None
    return parsed.replace(tzinfo=_KARACHI_TZ)


class KppraActiveTendersAdapter(SourceAdapter):
    """Adapter for the KPPRA public active-tenders listing."""

    source_name = SOURCE_NAME
    base_url = BASE_URL

    #: Safety cap for online pagination (9 pages at verify time).
    MAX_LISTING_PAGES = 40

    #: Column labels of the results table, in page order.
    COLUMNS = (
        "tender_no",
        "description",
        "organization",
        "advertised",
        "closing",
        "tender_eoi",
        "bidding_documents",
        "action",
    )

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return every raw listing row on the active-tenders page.

        Offline mode reads each ``kppra_listing*.html`` fixture once
        (page order = filename sort order). Online mode walks the
        paginated listing until a page yields no rows.

        ``since`` is accepted but not applied: the site's date-filter
        parameters (``fromDate``/``toDate``) have not been verified, and
        TenderScout does not guess query parameters.
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
        for page in range(1, self.MAX_LISTING_PAGES + 1):
            html = self.get_html(f"{self.base_url}{PAGE_PATH_TEMPLATE.format(page=page)}")
            rows = self._parse_listing_page(html)
            if not rows:
                break
            listings.extend(rows)
        return listings

    # ------------------------------------------------------------------
    # listing-page parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        """Parse one listing page into raw listings (row -> raw_data).

        The results table's headers are verified first; with an
        unexpected table structure this raises ValueError instead of
        guessing column positions.
        """
        soup = BeautifulSoup(html, "lxml")
        table = self._find_results_table(soup)
        if table is None:
            return []
        headers = [
            th.get_text(" ", strip=True).lower() for th in table.select("thead th")
        ]
        if not all(header in headers for header in REQUIRED_HEADERS):
            raise ValueError(
                "Unexpected KPPRA listing table structure; missing columns: "
                f"{[h for h in REQUIRED_HEADERS if h not in headers]}. "
                "Refusing to guess column positions."
            )

        listings = []
        for tr in table.find_all("tr"):
            raw = self._row_to_raw(tr, table)
            if raw is not None:
                listings.append(raw)
        return listings

    @staticmethod
    def _find_results_table(soup: BeautifulSoup) -> Tag | None:
        """Return the first table that has a thead, or None.

        The page also carries modal tables without thead (corrigendum
        and description tables); those never match.
        """
        for table in soup.find_all("table"):
            if table.find("thead") is not None:
                return table
        return None

    def _row_to_raw(self, tr: Tag, table: Tag) -> RawListing | None:
        """Extract one data row into a RawListing, or None to skip.

        Hidden rows (the paired corrigendum rows), header rows and rows
        belonging to nested tables are skipped. Values are stored as
        *source text* in ``raw_data``; parsing happens in
        :meth:`normalize`.
        """
        # Row belongs to a nested table (inside a hidden corrigendum row)?
        if tr.find_parent("table") is not table:
            return None
        # Hidden row?
        style = (tr.get("style") or "").replace(" ", "").lower()
        if "display:none" in style:
            return None
        cells = tr.find_all("td", recursive=False)
        if len(cells) < len(self.COLUMNS):
            return None

        values = {
            key: _clean(cells[i].get_text(" ", strip=True))
            for i, key in enumerate(self.COLUMNS)
        }
        tender_no = values["tender_no"]
        if not tender_no or not _REF_RE.match(tender_no):
            return None
        if not values["description"]:
            return None

        return RawListing(
            external_id=tender_no,
            url=f"{self.base_url}{REF_URL_TEMPLATE.format(ref=tender_no)}",
            raw_data={
                **values,
                "document_hrefs": self._document_hrefs(cells[5]),
            },
        )

    @staticmethod
    def _document_hrefs(cell: Tag) -> list[str]:
        """Absolute document hrefs from the Tender/EOI cell."""
        hrefs = []
        for anchor in cell.find_all("a", href=True):
            href = urljoin(f"{BASE_URL}{ORIGIN_PATH}", anchor["href"])
            if href.startswith("http"):
                hrefs.append(href)
        return hrefs

    # ------------------------------------------------------------------
    # normalization
    # ------------------------------------------------------------------

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        """Build the normalized opportunity from one raw listing row."""
        data = raw.raw_data or {}
        tender_no = raw.external_id
        return NormalizedOpportunity(
            external_id=tender_no,
            reference_number=tender_no,
            title=data.get("description"),
            organization=data.get("organization"),
            # The listing has no prose description beyond the title.
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_date(data.get("advertised")),
            deadline=_parse_date(data.get("closing")),
            location=None,
            province=PROVINCE,
            category=None,
            procurement_type=None,
            document_urls=list(data.get("document_hrefs") or []),
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        """Document URLs already collected during listing parse."""
        return list((raw.raw_data or {}).get("document_hrefs") or [])
