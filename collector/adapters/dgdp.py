"""DGDP downloads - public tender, auction and corrigendum documents.

Source: ``https://dgdp.gov.pk/downloads.php`` (the "Tenders" download
page of the Directorate General of Defence Procurement).

Verified 2026-10-25:

* robots.txt returns 404, so no restriction is declared;
* the page is server-rendered and lists ``div.portfolio-item`` cards,
  each with a title, a date and a direct ``scripts/forcedown.php?file=``
  document link;
* no login or CAPTCHA is involved.

This is a document archive rather than a live opportunity table: the
cards do not state deadlines.  TenderScout therefore records the title,
publication date, direct document URL and source, and leaves ``deadline``
as ``None`` instead of inventing one.  Every card on this page is a
tender/auction/corrigendum document; policy documents live elsewhere on
the site.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import unquote_plus, urljoin

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from dateutil.tz import gettz
from models import NormalizedOpportunity, RawListing

from .base import FIXTURES_DIR, SourceAdapter

logger = logging.getLogger(__name__)

SOURCE_NAME = "DGDP Tenders"
BASE_URL = "https://dgdp.gov.pk"
LISTING_PATH = "/downloads.php"

OFFLINE_LISTING_GLOB = "dgdp_downloads*.html"

_KARACHI_TZ = gettz("Asia/Karachi")
if _KARACHI_TZ is None:  # pragma: no cover - environment issue
    raise RuntimeError("Cannot resolve Asia/Karachi timezone")


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(text: str | None) -> datetime | None:
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


def _external_id(href: str) -> str:
    """Stable ID from the downloaded filename (or the path as fallback)."""
    candidate = ""
    if "file=" in href:
        candidate = href.split("file=", 1)[1]
    if not candidate:
        candidate = href.rsplit("/", 1)[-1]
    candidate = unquote_plus(candidate)
    candidate = re.sub(r"[^A-Za-z0-9._ -]+", "-", candidate).strip()
    return f"DGDP-{candidate or 'document'}"


class DgdpAdapter(SourceAdapter):
    """Adapter for DGDP's public tender document archive."""

    source_name = SOURCE_NAME
    base_url = BASE_URL

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
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
        return self._parse_listing_page(self.get_html(f"{self.base_url}{LISTING_PATH}"))

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        soup = BeautifulSoup(html, "lxml")
        listings: list[RawListing] = []
        for item in soup.select("div.portfolio-item"):
            raw = self._item_to_raw(item)
            if raw is not None:
                listings.append(raw)
        return listings

    def _item_to_raw(self, item: Tag) -> RawListing | None:
        title_node = item.select_one("h4")
        link = item.select_one("a.preview[href]")
        if title_node is None or link is None:
            return None
        title = _clean(title_node.get_text(" ", strip=True))
        if title is None:
            return None
        href = link["href"].strip()
        url = urljoin(f"{self.base_url}{LISTING_PATH}", href)
        date_node = item.select_one("p")
        return RawListing(
            external_id=_external_id(href),
            url=url,
            raw_data={
                "title": title,
                "date_text": (
                    _clean(date_node.get_text(" ", strip=True))
                    if date_node is not None
                    else None
                ),
            },
        )

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        data = raw.raw_data or {}
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=None,
            title=_clean(data.get("title")),
            organization="Directorate General of Defence Procurement (DGDP)",
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_date(data.get("date_text")),
            deadline=None,
            location=None,
            province=None,
            category=None,
            procurement_type=None,
            document_urls=[raw.url],
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        return [raw.url]
