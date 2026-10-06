"""EPADS federal procurements - one adapter for the national portal.

Source: ``https://epads.gov.pk/`` - the e-Pak Acquisition and Disposal
System run by the federal PPRA ("Open Opportunities (Procurements)").

Verified facts (2026-10-06, robots.txt absent -> no declared rules):

* fully server-rendered results table
  ``table.table.table-sm.table-cb`` with columns:
  ``# | Procurement Title | Procurement Status | Type/Procedure |
  Details`` - no JS rendering needed;
* 100 rows per page, plain GET pagination ``/?page=N`` (15 pages /
  1,436 open procurements at verify time; the ``?jurisdiction=`` query
  param redirects to the bare root, so it is deliberately not used);
* detail pages exist at ``/opportunities/federal/procurements/{id}``;
  the listing itself carries title, department, reference badge,
  published/closing timestamps and procurement type - enough without a
  second fetch per row (detail enrichment is future work);
* reference numbers render as a ``P{id}`` badge (kept verbatim);
* timestamps are full ("Monday, September 14, 2026 02:18 PM") and are
  read as Asia/Karachi time;
* this adapter crawls the federal jurisdiction only - other
  jurisdictions (sindh/kp/gb/ajk) are separate registry decisions.

Offline mode reads ``epads_listing*.html`` fixtures instead.
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

SOURCE_NAME = "EPADS"
BASE_URL = "https://epads.gov.pk"
LISTING_PATH = "/"

#: Listing fixtures (page snapshots) read in offline mode, sorted by name.
OFFLINE_LISTING_GLOB = "epads_listing*.html"

#: Column headers that must be present before any row is trusted.
REQUIRED_HEADERS = (
    "#",
    "procurement title",
    "procurement status",
    "type/procedure",
    "details",
)

#: Reference badge as rendered, e.g. ``P119659``.
_REF_RE = re.compile(r"^P\d+$")

#: Detail/opportunity href fragment used to find title links.
_OPPORTUNITY_HREF = "/procurements/"

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


def _parse_timestamp(text: str | None) -> datetime | None:
    """Parse a listing timestamp into an Asia/Karachi datetime, or None.

    Source format: ``Monday, September 14, 2026 02:18 PM`` (weekday
    prefix, no timezone stated). Naive results are Asia/Karachi - the
    portal serves Pakistan procurement. Unparseable -> None.
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


class EpadsAdapter(SourceAdapter):
    """Adapter for the EPADS federal open-procurements listing."""

    source_name = SOURCE_NAME
    base_url = BASE_URL

    #: Safety cap for online pagination (15 pages at verify time).
    MAX_LISTING_PAGES = 30

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return every raw listing row currently open on EPADS.

        Offline mode reads each ``epads_listing*.html`` fixture once
        (page order = filename sort order). Online mode walks the
        paginated listing until a page yields no rows.

        ``since`` is accepted but not applied: the portal's filter
        parameters have not been verified for date filtering, and
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
            html = self.get_html(f"{self.base_url}/?page={page}")
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
        table = soup.find("table")
        if table is None:
            return []
        headers = [
            th.get_text(" ", strip=True).lower() for th in table.select("thead th")
        ]
        missing = [h for h in REQUIRED_HEADERS if h not in headers]
        if missing:
            raise ValueError(
                f"Unexpected EPADS listing table structure; missing columns: "
                f"{missing}. Refusing to guess column positions."
            )

        listings = []
        for tr in table.select("tbody tr"):
            raw = self._row_to_raw(tr)
            if raw is not None:
                listings.append(raw)
        return listings

    def _row_to_raw(self, tr: Tag) -> RawListing | None:
        """Extract one listing row into a RawListing, or None to skip.

        Values are stored as *source text* in ``raw_data``; parsing
        happens in :meth:`normalize`.
        """
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 5:
            return None
        title_cell, status_cell, type_cell = cells[1], cells[2], cells[3]

        link = title_cell.select_one(f'a[href*="{_OPPORTUNITY_HREF}"]')
        if link is None or not link.get("href"):
            return None
        external_id = link["href"].rstrip("/").rsplit("/", 1)[-1]
        if not external_id.isdigit():
            return None

        # Full title lives in the span's title= attribute; the visible
        # text is truncated with an ellipsis.
        title_span = link.select_one("span[title]")
        title = _clean(title_span.get("title") if title_span else link.get_text())
        if not title:
            return None

        # Department span: the second title-bearing span in the cell.
        spans = title_cell.select("span[title]")
        organization = _clean(spans[1].get_text(" ", strip=True)) if len(spans) > 1 else None

        reference_number = None
        for span in title_cell.find_all("span"):
            text = _clean(span.get_text(strip=True))
            if text and _REF_RE.match(text):
                reference_number = text
                break

        published_text, closing_text = self._date_texts(status_cell)

        type_span = type_cell.select_one("span.badge")
        procedure_span = type_cell.select_one("span.d-block")

        return RawListing(
            external_id=external_id,
            url=urljoin(self.base_url, link["href"]),
            raw_data={
                "title": title,
                "organization": organization,
                "reference_number": reference_number,
                "published_text": published_text,
                "closing_text": closing_text,
                "procurement_type": _clean(type_span.get_text(" ", strip=True))
                if type_span
                else None,
                "procedure": _clean(procedure_span.get_text(" ", strip=True))
                if procedure_span
                else None,
            },
        )

    @staticmethod
    def _date_texts(cell: Tag) -> tuple[str | None, str | None]:
        """Pull the raw Published On / Closing On strings from a cell."""
        published: str | None = None
        closing: str | None = None
        spans = cell.find_all("span")
        for i, span in enumerate(spans):
            label = (span.get_text(" ", strip=True) or "").lower()
            if "published on" in label and i + 1 < len(spans):
                published = _clean(spans[i + 1].get_text(" ", strip=True))
            elif "closing on" in label and i + 1 < len(spans):
                closing = _clean(spans[i + 1].get_text(" ", strip=True))
        return published, closing

    # ------------------------------------------------------------------
    # normalization
    # ------------------------------------------------------------------

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        """Build the normalized opportunity from one raw listing row."""
        data = raw.raw_data or {}
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=data.get("reference_number"),
            title=data.get("title"),
            organization=data.get("organization"),
            # The listing carries no prose description; the procurement
            # procedure text is kept in raw_data for later use rather
            # than stuffed into a field it does not belong to.
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_timestamp(data.get("published_text")),
            deadline=_parse_timestamp(data.get("closing_text")),
            location=None,
            province=None,  # federal jurisdiction, not a province
            category=None,
            procurement_type=data.get("procurement_type"),
            document_urls=[],
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        """No document links on the EPADS listing itself."""
        return []
