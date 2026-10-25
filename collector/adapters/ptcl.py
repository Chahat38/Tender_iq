"""PTCL tenders - public notices published by Pakistan Telecommunication.

Source: ``https://ptcl.com.pk/Tenders`` (server-rendered accordion list).

Verified 2026-10-25:

* robots.txt is allow-all apart from internal ASP.NET folders; the
  tenders path is allowed.
* each notice is a ``div.item`` with a date range in
  ``span.h3.green1`` ("From Oct 21, 2026 To Oct 31, 2026"), the title in
  ``p.black`` and a detail link to ``/Home/TenderDetail/?ItemId=N``;
* the detail page adds a direct PDF link ("Click here to download file");
* no login, CAPTCHA or JavaScript wall is involved.

The listing page shows the current notices (one page at verify time), so
this adapter does not paginate.  Detail enrichment is supported and
fetches the PDF link from the detail page.  Offline tests read
``ptcl_tenders*.html`` fixtures.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from dateutil import parser as date_parser
from dateutil.tz import gettz
from models import NormalizedOpportunity, RawListing

from .base import FIXTURES_DIR, SourceAdapter

logger = logging.getLogger(__name__)

SOURCE_NAME = "PTCL Tenders"
BASE_URL = "https://ptcl.com.pk"
LISTING_PATH = "/Tenders"

OFFLINE_LISTING_GLOB = "ptcl_tenders*.html"

_ID_PREFIX = "PTCL-"

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


def _date_range(text: str | None) -> tuple[datetime | None, datetime | None]:
    """Split the PTCL ``From ... To ...`` label into two dates."""
    text = _clean(text)
    if text is None:
        return None, None
    match = re.search(r"\bFrom\s+(.+?)\s+\bTo\s+(.+)$", text, re.I)
    if match is None:
        return None, None
    return _parse_date(match.group(1)), _parse_date(match.group(2))


class PtclAdapter(SourceAdapter):
    """Adapter for the PTCL public tender notices."""

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
        for item in soup.select("div.item"):
            raw = self._item_to_raw(item)
            if raw is not None:
                listings.append(raw)
        return listings

    def _item_to_raw(self, item: Tag) -> RawListing | None:
        anchor = item.select_one('a.file-icons[href*="TenderDetail"]')
        if anchor is None:
            anchor = item.find("a", href=re.compile(r"TenderDetail", re.I))
        if anchor is None or not anchor.get("href"):
            return None

        parsed = urlparse(anchor["href"])
        item_ids = parse_qs(parsed.query).get("ItemId") or []
        if not item_ids or not item_ids[0].isdigit():
            return None
        item_id = item_ids[0]
        external_id = f"{_ID_PREFIX}{item_id}"
        detail_url = f"{self.base_url}/Home/TenderDetail/?ItemId={item_id}"

        date_text = None
        date_span = item.select_one(".faq-head .h3")
        if date_span is not None:
            date_text = _clean(date_span.get_text(" ", strip=True))

        title = None
        title_node = item.select_one("p.black")
        if title_node is not None:
            title = _clean(title_node.get_text(" ", strip=True))
        if title is None:
            # Some historic notices put the title in an h4 instead.
            title_node = item.select_one("h4")
            if title_node is not None:
                title = _clean(title_node.get_text(" ", strip=True))
        if title is None:
            return None

        return RawListing(
            external_id=external_id,
            url=detail_url,
            raw_data={"title": title, "date_text": date_text},
        )

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        data = raw.raw_data or {}
        published_at, deadline = _date_range(data.get("date_text"))
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=None,
            title=_clean(data.get("title")),
            organization="Pakistan Telecommunication Company Limited (PTCL)",
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=published_at,
            deadline=deadline,
            location=None,
            province=None,
            category=None,
            procurement_type=None,
            document_urls=[],
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        return []

    # ------------------------------------------------------------------
    # detail enrichment
    # ------------------------------------------------------------------

    def fetch_opportunity(self, ref: str) -> RawListing:
        if self.offline:
            raise RuntimeError("PTCL detail fetching is unavailable offline")
        item_id = ref.removeprefix(_ID_PREFIX)
        if not item_id.isdigit():
            raise ValueError(f"invalid PTCL external id: {ref!r}")
        url = f"{self.base_url}/Home/TenderDetail/?ItemId={item_id}"
        html = self.get_html(url)
        return RawListing(
            external_id=ref,
            url=url,
            raw_html=html,
            raw_data=self._parse_detail(html),
        )

    @staticmethod
    def _parse_detail(html: str) -> dict[str, object]:
        soup = BeautifulSoup(html, "lxml")
        content = soup.select_one("#contentdiv")
        scope = content if content is not None else soup
        date_text = None
        date_node = scope.find("h4")
        if date_node is not None:
            candidate = _clean(date_node.get_text(" ", strip=True))
            if candidate and candidate.lower().startswith("from "):
                date_text = candidate
        title = None
        for paragraph in scope.find_all("p"):
            if paragraph.find("a", href=True) is not None:
                continue
            text = _clean(paragraph.get_text(" ", strip=True))
            if text:
                title = text
                break
        documents = []
        for anchor in scope.find_all("a", href=True):
            href = urljoin(BASE_URL, anchor["href"])
            if href.lower().endswith(".pdf"):
                documents.append(href)
        return {"title": title, "date_text": date_text, "document_urls": documents}

    def normalize_detail(self, raw: RawListing) -> NormalizedOpportunity:
        data = raw.raw_data or {}
        published_at, deadline = _date_range(data.get("date_text"))
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=None,
            title=_clean(data.get("title")),
            organization="Pakistan Telecommunication Company Limited (PTCL)",
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=published_at,
            deadline=deadline,
            location=None,
            province=None,
            category=None,
            procurement_type=None,
            document_urls=list(data.get("document_urls") or []),
        )
