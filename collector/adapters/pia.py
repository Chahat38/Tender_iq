"""Pakistan International Airlines tenders - one adapter.

Source: ``https://piac.com.pk/corporate/business-with-pia/tenders`` - the
public "Tenders" table of PIA's corporate site (the airline's own
listing; many PIA tenders also flow through PPRA EPMS/EPADS and are
already covered there).

Verified facts (2026-10-07, robots.txt is the standard Joomla file with
no rule touching the tenders path; PoliteClient's robots gate passes):

* the whole current tender set renders as one server-side ``table``
  (no JS rendering, no working pagination - ``?start=N`` returns the
  same rows), with one ``tr`` per tender carrying a numeric sequential
  number, the title, publish date, closing date (ISO ``2026-10-05``),
  the procuring department and a ``View`` link to
  ``/corporate/business-with-pia/tenders/{id}``;
* external_id is ``PIA-<id>`` taken from the view link so it survives
  renumbering of the first column;
* the listing carries no city, category or procurement type - the rows
  are PIA-HQ (Karachi) centred but departments may embed their station;
  detail pages are not fetched (listing-only, same as EPADS).

Offline mode reads ``pia_tenders*.html`` fixtures instead.
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

SOURCE_NAME = "PIA Tenders"
BASE_URL = "https://piac.com.pk"
LISTING_PATH = "/corporate/business-with-pia/tenders"

#: Listing fixtures (page snapshots) read in offline mode, sorted by name.
OFFLINE_LISTING_GLOB = "pia_tenders*.html"

#: View-link fragment that carries the tender id.
_DETAIL_HREF = "/corporate/business-with-pia/tenders/"

#: External-id prefix so PIA ids never collide with other sources.
_ID_PREFIX = "PIA-"

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
    """Parse an ISO ``2026-10-05`` cell date into Asia/Karachi time."""
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


class PiaAdapter(SourceAdapter):
    """Adapter for the PIA tenders table."""

    source_name = SOURCE_NAME
    base_url = BASE_URL

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return the raw listing rows currently on PIA's single table.

        Offline mode reads each ``pia_tenders*.html`` fixture once.
        Online mode fetches the one listing page (the site has no
        working pagination).
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

        html = self.get_html(f"{self.base_url}{LISTING_PATH}")
        return self._parse_listing_page(html)

    # ------------------------------------------------------------------
    # listing-page parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        """Parse the tenders table into raw listings (row -> raw)."""
        soup = BeautifulSoup(html, "lxml")
        listings = []
        for row in soup.select("table tbody tr"):
            raw = self._row_to_raw(row)
            if raw is not None:
                listings.append(raw)
        return listings

    def _row_to_raw(self, row: Tag) -> RawListing | None:
        """Extract one table row into a RawListing, or None to skip."""
        link = None
        for cell in row.find_all("td"):
            a = cell.find("a", href=True)
            if a is not None and _DETAIL_HREF in a["href"]:
                link = a
                break
        if link is None:
            return None
        match = re.search(r"/(\d+)$", link["href"].rstrip("/"))
        if match is None:
            return None
        external_id = f"{_ID_PREFIX}{match.group(1)}"
        detail_url = urljoin(self.base_url, link["href"])

        cells = row.find_all("td")
        if len(cells) < 5:
            return None
        title = _clean(cells[1].get_text(" ", strip=True))
        if not title:
            return None
        published_text = _clean(cells[2].get_text(" ", strip=True))
        closing_text = _clean(cells[3].get_text(" ", strip=True))
        organization = _clean(cells[4].get_text(" ", strip=True))

        return RawListing(
            external_id=external_id,
            url=detail_url,
            raw_data={
                "title": title,
                "organization": organization,
                "published_text": published_text,
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
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_date(data.get("published_text")),
            deadline=_parse_date(data.get("closing_text")),
            location=None,
            province=None,  # airline HQ in Karachi; station not listed
            category=None,
            procurement_type=None,
            document_urls=list(data.get("document_urls") or []),
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        """Listing rows expose no direct document files yet."""
        return []