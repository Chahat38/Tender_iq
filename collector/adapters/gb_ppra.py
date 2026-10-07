"""GB PPRA procurements - one adapter for Gilgit-Baltistan tenders.

Source: ``https://www.gbppra.gov.pk/viewall`` - the public "All
Procurements" list of the GB Public Procurement Regulatory Authority.

Verified facts (2026-10-07, robots.txt is allow-all
``User-agent: * / Disallow:``; PoliteClient's robots gate passes):

* each procurement renders as a server-side ``div.job-post-item`` card
  (no JS rendering) carrying a ``[TSE-YYYYMMDDNNNN]`` reference, a
  title, a notice badge ("Tender Notice", "Invitation to bids", ...),
  the procuring office, published/closing dates, a Notice PDF link and
  a ``View Details`` link to ``/procurementdetails/{id}``;
* pagination is a plain GET ``?page=N`` (10 items per page, newest
  first); ``since`` stops the crawl early once a page has no newer
  published dates left to collect;
* dates render as ``06 Oct, 2026`` and are read as Asia/Karachi time;
* the card has no city field - the office name often embeds one, but
  detail-page parsing (which may expose a clean city) is future work;
* detail pages exist at ``/procurementdetails/{id}`` but are not fetched
  yet (listing-only adapter, same as EPADS).

Offline mode reads ``gb_viewall*.html`` fixtures instead.
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

SOURCE_NAME = "GB PPRA"
BASE_URL = "https://www.gbppra.gov.pk"
LISTING_PATH = "/viewall"

#: Listing fixtures (page snapshots) read in offline mode, sorted by name.
OFFLINE_LISTING_GLOB = "gb_viewall*.html"

#: Reference badge as rendered, e.g. ``[TSE-202610063191]``.
_REF_RE = re.compile(r"^\[([A-Z]+-?\d+)\]$")

#: Detail/opportunity href fragment used to find the View Details link.
_DETAIL_HREF = "/procurementdetails/"

#: Notice PDFs are stored under /storage/proc_files/.
_NOTICE_HREF = "/storage/proc_files/"

#: Safety cap for online pagination (2935 results / 10 per page at
#: verify time; earliest pages have no active-until dates anyway).
MAX_LISTING_PAGES = 30

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
    """Parse a ``06 Oct, 2026`` card date into an Asia/Karachi datetime."""
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


class GbPpraAdapter(SourceAdapter):
    """Adapter for the GB PPRA "All Procurements" listing."""

    source_name = SOURCE_NAME
    base_url = BASE_URL

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return every raw listing row currently open on GB PPRA.

        Offline mode reads each ``gb_viewall*.html`` fixture once (page
        order = filename sort order). Online mode walks the paginated
        list until a page yields no rows or stops adding newer
        publications than ``since``.
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
        for page in range(1, MAX_LISTING_PAGES + 1):
            html = self.get_html(f"{self.base_url}{LISTING_PATH}?page={page}")
            rows = self._parse_listing_page(html)
            if not rows:
                break
            listings.extend(rows)
            if since is not None:
                newest = max(
                    (self._card_published_at(row) for row in rows),
                    default=None,
                )
                if newest is None or newest < since:
                    break
        return listings

    @staticmethod
    def _card_published_at(raw: RawListing) -> datetime | None:
        """The parsed published date of a listing card raw."""
        return _parse_date((raw.raw_data or {}).get("published_text"))

    # ------------------------------------------------------------------
    # listing-page parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        """Parse one page of cards into raw listings (each card -> raw)."""
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("div.job-post-item")
        listings = []
        for card in cards:
            raw = self._card_to_raw(card)
            if raw is not None:
                listings.append(raw)
        return listings

    def _card_to_raw(self, card: Tag) -> RawListing | None:
        """Extract one card into a RawListing, or None to skip."""
        ref_match = None
        for h2 in card.select("h2"):
            ref_match = _REF_RE.match(h2.get_text(" ", strip=True) or "")
            if ref_match:
                break
        if ref_match is None:
            return None
        external_id = ref_match.group(1)

        title = None
        for h2 in card.select("h2"):
            ref = _REF_RE.match(h2.get_text(" ", strip=True) or "")
            if ref is None:
                title = _clean(h2.get_text(" ", strip=True))
                break
        if not title:
            return None

        badge = card.select_one("span.badge")
        notice_type = _clean(badge.get_text(" ", strip=True)) if badge else None
        if notice_type == "\xa0":
            notice_type = None

        organization = None
        for div in card.find_all("div", recursive=True):
            if div.select_one(".fa-university") and not organization:
                link = div.select_one("a")
                organization = _clean(
                    link.get_text(" ", strip=True) if link else div.get_text(" ", strip=True)
                )
        if not organization:
            organization = None

        published_text = closing_text = None
        for div in card.select("div.mr-3"):
            if div.select_one("span[class*=fa-calendar]") is None:
                continue
            classes = " ".join(div.get("class") or [])
            text = div.get_text(" ", strip=True)
            if "text-success" in classes:
                published_text = text
            elif "text-danger" in classes:
                closing_text = text

        notice_pdf = None
        detail_url = f"{self.base_url}{LISTING_PATH}"
        for a in card.find_all("a", href=True):
            href = a["href"]
            if _NOTICE_HREF in href and notice_pdf is None:
                notice_pdf = urljoin(self.base_url, href)
            elif _DETAIL_HREF in href:
                detail_url = urljoin(self.base_url, href)

        return RawListing(
            external_id=external_id,
            url=detail_url,
            raw_data={
                "title": title,
                "notice_type": notice_type,
                "organization": organization,
                "published_text": published_text,
                "closing_text": closing_text,
                "document_urls": [notice_pdf] if notice_pdf else [],
            },
        )

    # ------------------------------------------------------------------
    # normalization
    # ------------------------------------------------------------------

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        """Build the normalized opportunity from one raw card."""
        data = raw.raw_data or {}
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=raw.external_id,
            title=data.get("title"),
            organization=data.get("organization"),
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_date(data.get("published_text")),
            deadline=_parse_date(data.get("closing_text")),
            location=None,
            province=None,  # Gilgit-Baltistan jurisdiction, not a province
            category=None,
            procurement_type=data.get("notice_type"),
            document_urls=list(data.get("document_urls") or []),
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        """The Notice PDF stored under /storage/proc_files/."""
        data = raw.raw_data or {}
        return list(data.get("document_urls") or [])