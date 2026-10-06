"""Adapter for Pakistan's federal PPRA EPMS tender portal.

Source: https://epms.ppra.gov.pk (EPMS, run by the Public Procurement
Regulatory Authority). Public pages only: the "Active Tenders" listing
and per-tender detail pages. No login, no CAPTCHA bypass; all requests
go through PoliteClient (robots.txt, delays, retries). The site has no
robots.txt (404), which our client treats as "no restrictions declared".

external_id choice: the site's own tender number (e.g. TS0000015263E).
It is unique per tender, stable across corrigenda, and is the slug of
the detail-page URL. The agency-issued reference number (e.g. P89833)
can be missing or a placeholder on the site, so it is stored separately
in ``reference_number``.

Timestamps are interpreted in Asia/Karachi (Pakistan Standard Time,
UTC+05:00) per the project convention; unparseable dates become None.

Listing rows carry no document links -- documents exist only on detail
pages (see :meth:`fetch_opportunity` / :meth:`extract_documents`).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser, tz
from models import NormalizedOpportunity, RawListing

from .base import FIXTURES_DIR, SourceAdapter

logger = logging.getLogger(__name__)

SOURCE_NAME = "PPRA EPMS"
BASE_URL = "https://epms.ppra.gov.pk"
LISTING_PATH = "/public/tenders/active-tenders"
DETAIL_PATH_TEMPLATE = "/public/tenders/tender-details/{external_id}"

#: tender_type values for the site's own filter, verified against the
#: live portal's filter box (2026-10-06): 1 = Tender Notice,
#: 2 = Pre-qualification (PQ), 3 = Request for Proposal (RFP),
#: 4 = Expression of Interest (EOI). Online runs walk all four so PQ,
#: RFP and EOI notices are collected too, not just tender notices.
TENDER_TYPES = ("1", "2", "3", "4")

#: The single type used when one is requested explicitly.
DEFAULT_TENDER_TYPE = TENDER_TYPES[0]

#: Listing fixtures (page snapshots) read in offline mode, sorted by name.
OFFLINE_LISTING_GLOB = "ppra_listing*.html"
#: Detail-page fixtures scanned by offline fetch_opportunity().
OFFLINE_DETAIL_GLOB = "ppra_detail*.html"

KARACHI_TZ = tz.gettz("Asia/Karachi")
if KARACHI_TZ is None:
    raise RuntimeError(
        "Cannot resolve the Asia/Karachi timezone on this system; "
        "TenderScout refuses to guess timezone offsets."
    )

#: Values the site displays in its reference-number field when an agency
#: did not provide one. These are tender types, not reference numbers, so
#: normalize() stores None instead of copying the placeholder.
_REFERENCE_PLACEHOLDERS = {
    "Tender Notice",
    "Pre-qualification Notice (PQ)",
    "Request for Proposal (RFP)",
    "Expression of Interest (EOI)",
    "Invitation for E-Bid",
}


def _clean(value: object) -> str | None:
    """Strip whitespace; return None for empty results."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_pakistan_datetime(text: str | None) -> datetime | None:
    """Parse a site date string into an Asia/Karachi datetime, or None.

    The site writes dates like ``Oct 21, 2026 11:00 AM`` (listing) or
    ``October 21, 2026 at 11:00 AM`` (detail pages). Naive results are
    assumed to be Asia/Karachi. Unparseable input yields None -- never a
    guessed date.
    """
    text = _clean(text)
    if text is None:
        return None
    try:
        parsed = date_parser.parse(text.replace(" at ", " "))
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KARACHI_TZ)
    return parsed


def _city_from_location_text(text: str | None) -> str | None:
    """Reduce ``Lahore - Pakistan`` to ``Lahore`` (cosmetic cleanup only)."""
    text = _clean(text)
    if text is None:
        return None
    city = text.split(" - ")[0].strip()
    city = city.rstrip(".").strip()
    return city or None


class PPRAEpmsAdapter(SourceAdapter):
    """One adapter for the PPRA EPMS public tender portal.

    Implements :class:`~base.SourceAdapter` for the "Active Tenders"
    listing and the per-tender detail pages. In offline mode it reads
    saved fixtures from ``collector/fixtures/`` instead of the network
    (``ppra_listing*.html`` for listings, ``ppra_detail*.html`` when a
    detail page is requested via :meth:`fetch_opportunity`).
    """

    source_name = SOURCE_NAME
    base_url = BASE_URL

    #: Safety cap for online pagination (the site had 37 pages of
    #: Tender Notices when this adapter was written).
    MAX_LISTING_PAGES = 200

    #: Column headers that must be present in the listing table before
    #: any row is trusted (matched case-insensitively).
    REQUIRED_HEADERS = ("tender details", "organization details", "advertised", "closing")

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return the raw listing rows currently visible on the portal.

        Offline mode reads every ``ppra_listing*.html`` fixture once
        (page order = filename sort order); fixtures capture the
        Tender Notice listing, so the type matrix only applies online.

        Online mode walks every tender type (Tender Notice, PQ, RFP,
        EOI - see :data:`TENDER_TYPES`) and, per type, the paginated
        listing until a page yields no rows.

        ``since`` is accepted but not applied: the public listing's
        server-side date-filter parameters have not been verified yet,
        and TenderScout does not guess query parameters. All visible
        rows are returned.
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
        for tender_type in TENDER_TYPES:
            for page in range(1, self.MAX_LISTING_PAGES + 1):
                html = self.get_html(self._listing_url(page, tender_type))
                rows = self._parse_listing_page(html)
                if not rows:
                    break
                listings.extend(rows)
        return listings

    def fetch_opportunity(self, ref: str) -> RawListing:
        """Return the detail-page raw listing for one tender number.

        Offline mode scans ``ppra_detail*.html`` fixtures for the
        tender number; raises FileNotFoundError when no fixture
        contains it.
        """
        url = urljoin(self.base_url, DETAIL_PATH_TEMPLATE.format(external_id=ref))
        if self.offline:
            for path in sorted(FIXTURES_DIR.glob(OFFLINE_DETAIL_GLOB)):
                html = path.read_text(encoding="utf-8")
                if f">{ref}<" in html:
                    return RawListing(external_id=ref, url=url, raw_html=html)
            raise FileNotFoundError(
                f"No detail fixture contains tender {ref!r}; save its detail "
                "page with scripts/save_fixture.py first."
            )
        return RawListing(external_id=ref, url=url, raw_html=self.get_html(url))

    # ------------------------------------------------------------------
    # listing-page parsing
    # ------------------------------------------------------------------

    def _listing_url(self, page: int, tender_type: str = DEFAULT_TENDER_TYPE) -> str:
        """Return the listing URL for one 1-based page and tender type."""
        return (
            f"{self.base_url}{LISTING_PATH}"
            f"?keyword=&tender_type={tender_type}&page={page}"
        )

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        """Parse one listing page into raw listings (row -> raw_data).

        The table headers are verified first; with an unexpected table
        structure this raises ValueError instead of guessing columns.
        """
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        if table is None:
            return []
        headers = [
            th.get_text(" ", strip=True).lower() for th in table.select("thead th")
        ]
        index = {
            "details": self._column_index(headers, "tender details"),
            "org": self._column_index(headers, "organization details"),
            "advertised": self._column_index(headers, "advertised"),
            "closing": self._column_index(headers, "closing"),
            "actions": self._column_index(headers, "actions"),
        }
        missing = [
            key for key in self.REQUIRED_HEADERS if index.get(self._key_for(key), -1) < 0
        ]
        if missing:
            raise ValueError(
                f"Unexpected PPRA listing table structure; missing columns: {missing}. "
                "Refusing to guess column positions."
            )
        listings = []
        for tr in table.select("tbody tr"):
            raw = self._row_to_raw(tr, index)
            if raw is not None:
                listings.append(raw)
        return listings

    @staticmethod
    def _key_for(header: str) -> str:
        """Map a REQUIRED_HEADERS entry to its index-dict key."""
        return {
            "tender details": "details",
            "organization details": "org",
            "advertised": "advertised",
            "closing": "closing",
        }[header]

    @staticmethod
    def _column_index(headers: list[str], *candidates: str) -> int:
        """Find a column index by exact then substring header match."""
        for candidate in candidates:
            for i, header in enumerate(headers):
                if header == candidate:
                    return i
        for candidate in candidates:
            for i, header in enumerate(headers):
                if candidate in header:
                    return i
        return -1

    def _row_to_raw(self, tr: Tag, index: dict[str, int]) -> RawListing | None:
        """Extract one listing row into a RawListing (page-verified columns).

        The parsed values go into ``raw_data`` as *source text*; date
        parsing happens in :meth:`normalize` so the raw payload stays an
        unmodified record of what the page said.
        """
        cells = tr.find_all("td")
        tender_no_el = tr.select_one("td.tender-no")
        external_id = tender_no_el.get_text(strip=True) if tender_no_el else ""
        if not external_id:
            return None
        detail_cell = cells[index["details"]]
        title_el = detail_cell.find("strong")
        if title_el is None:
            return None

        # Description snippets: small.text-muted.d-block (may be empty).
        snippets = [
            s.get_text(" ", strip=True)
            for s in detail_cell.select("small.text-muted.d-block")
        ]
        description = next((s for s in snippets if s), "")

        # Chips: one badge WITHOUT text-muted = sector/category; muted
        # badges = reference number, then the ministry (which carries the
        # org icon and is also present in the organization cell).
        category = None
        reference = None
        for chip in detail_cell.select("small.badge"):
            classes = chip.get("class") or []
            text = chip.get_text(" ", strip=True)
            if "text-muted" in classes:
                if chip.find("i", class_=re.compile(r"^ri-")):
                    continue  # ministry chip (duplicated in the org cell)
                if reference is None and text and text not in _REFERENCE_PLACEHOLDERS:
                    reference = text
            elif category is None and text:
                category = text

        # Organization cell: ministry (<small>), office (span.tender-org),
        # city (<small>), each led by an empty decorative <i> icon.
        org_cell = cells[index["org"]]
        parts = org_cell.select("span, small")
        organization = parts[0].get_text(" ", strip=True) if parts else ""
        office_el = org_cell.select_one("span.tender-org")
        office = office_el.get_text(" ", strip=True) if office_el else ""
        city = parts[-1].get_text(" ", strip=True) if parts else ""

        return RawListing(
            external_id=external_id,
            url=self._detail_url_from_row(tr, index, external_id),
            raw_html=str(tr),
            raw_data={
                "title": title_el.get_text(" ", strip=True),
                "description": description,
                "category": category,
                "reference": reference,
                "organization": organization,
                "office": office,
                "location": city,
                "published_text": cells[index["advertised"]].get_text(" ", strip=True),
                "deadline_text": cells[index["closing"]].get_text(" ", strip=True),
            },
        )

    def _detail_url_from_row(
        self, tr: Tag, index: dict[str, int], external_id: str
    ) -> str:
        """Return the absolute detail-page URL for a row.

        Prefers the row's own tender-details link; falls back to the
        site's stable URL pattern built from the tender number.
        """
        if index.get("actions", -1) >= 0:
            cells = tr.find_all("td")
            if index["actions"] < len(cells):
                for a in cells[index["actions"]].find_all("a", href=True):
                    if "tender-details/" in a["href"]:
                        return urljoin(self.base_url, a["href"])
        return urljoin(
            self.base_url, DETAIL_PATH_TEMPLATE.format(external_id=external_id)
        )

    # ------------------------------------------------------------------
    # normalization
    # ------------------------------------------------------------------

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        """Normalize one listing row into the common schema.

        Dates are parsed with dateutil and localized to Asia/Karachi;
        unparseable dates become None. Strings are stripped; missing
        values stay None. The listing does not state Goods/Services/
        Works (that lives on detail pages), so ``procurement_type`` is
        None here, and so are ``document_urls`` (see extract_documents).
        """
        data = raw.raw_data or {}
        title = _clean(data.get("title"))
        if not title:
            raise ValueError(f"listing {raw.external_id} has no title")
        return NormalizedOpportunity(
            title=title,
            organization=_clean(data.get("organization")),
            description=_clean(data.get("description")),
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_pakistan_datetime(data.get("published_text")),
            deadline=_parse_pakistan_datetime(data.get("deadline_text")),
            location=_city_from_location_text(data.get("location")),
            category=_clean(data.get("category")),
            procurement_type=None,
            reference_number=_clean(data.get("reference")),
            document_urls=[],
            external_id=raw.external_id,
        )

    def normalize_detail(self, raw: RawListing) -> NormalizedOpportunity:
        """Normalize a detail-page raw (from :meth:`fetch_opportunity`).

        Detail pages expose more labeled fields than the listing, so
        this fills ``procurement_type``, ``location`` (city),
        ``reference_number`` and ``document_urls`` in addition to the
        listing-level fields. Labels are matched by the site's own
        ``detail-label`` texts; missing labels stay None.
        """
        soup = BeautifulSoup(raw.raw_html or "", "lxml")
        values: dict[str, str] = {}
        for item in soup.select(".list-group-item"):
            label_el = item.select_one(".detail-label")
            value_el = item.select_one(".detail-value") or item.select_one(
                ".flex-grow-1"
            )
            if label_el is None or value_el is None:
                continue
            label = label_el.get_text(" ", strip=True).rstrip(":").lower()
            values[label] = value_el.get_text(" ", strip=True)

        title_el = soup.find("h1")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            raise ValueError(f"detail page {raw.external_id} has no title")

        procurement_type = _clean(values.get("procurement category"))
        return NormalizedOpportunity(
            title=title,
            organization=_clean(values.get("organization name")),
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_pakistan_datetime(values.get("advertisement date")),
            deadline=_parse_pakistan_datetime(values.get("closing date & time")),
            location=_city_from_location_text(values.get("city")),
            category=_clean(values.get("sector")),
            procurement_type=procurement_type.lower() if procurement_type else None,
            reference_number=_clean(
                values.get("tender no / reference no / tender inquiry no")
            ),
            document_urls=self.extract_documents(raw),
            external_id=raw.external_id,
        )

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------

    def extract_documents(self, raw: RawListing) -> list[str]:
        """Return absolute document URLs found in the raw HTML.

        Listing rows contain no document links (documents live on detail
        pages), so listing raws yield []. Detail-page raws yield the
        site's ``/pdf?file=...`` download links (Tender Document and
        Advertisement), made absolute; the file name is recoverable by
        base64-decoding the ``file`` parameter.
        """
        if not raw.raw_html:
            return []
        soup = BeautifulSoup(raw.raw_html, "lxml")
        urls: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/pdf?" in href:
                urls.append(urljoin(self.base_url, href))
        return list(dict.fromkeys(urls))
