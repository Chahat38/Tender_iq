"""PaperPK Tender Service - public newspaper/procurement tender listings.

Source: ``https://tenderservice.pk/`` (the tender section branded
"PaperPk Tender Service").

Verified 2026-10-25:

* ``tenderservice.pk/robots.txt`` has no wildcard rule; only named
  crawlers (Amazonbot, GPTBot, ClaudeBot, ...) are disallowed.  The
  ``paperpk.com`` robots file also has no wildcard disallow;
* the home page is server-rendered and lists the latest 50 notices as
  ``a#jobLink`` rows with title, newspaper, publication date and a
  detail link;
* detail pages are public and expose company, deadline, industry, city
  and address in a plain table.  A subscription is advertised, but the
  fields collected here are visible without logging in.

PaperPK is an aggregator, not an official procuring entity.  The source
name says so, and only the public listing/detail fields are collected.
No paid/subscription-only content is fetched or bypassed.
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

SOURCE_NAME = "PaperPK Tender Service"
BASE_URL = "https://tenderservice.pk"
LISTING_PATH = "/"

OFFLINE_LISTING_GLOB = "paperpk_list*.html"
OFFLINE_DETAIL_GLOB = "paperpk_detail*.html"

_KARACHI_TZ = gettz("Asia/Karachi")
if _KARACHI_TZ is None:  # pragma: no cover - environment issue
    raise RuntimeError("Cannot resolve Asia/Karachi timezone")

_DATE_HREF_RE = re.compile(r"calendar-search\.php\?date=(\d{4}-\d{2}-\d{2})")


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


def _parse_iso_date(text: str | None) -> datetime | None:
    text = _clean(text)
    if text is None:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return parsed.replace(tzinfo=_KARACHI_TZ)


class PaperpkAdapter(SourceAdapter):
    """Adapter for the public PaperPK/TenderService listing."""

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
        for link in soup.select('a#jobLink[href]'):
            raw = self._row_to_raw(link)
            if raw is not None:
                listings.append(raw)
        return listings

    def _row_to_raw(self, link: Tag) -> RawListing | None:
        title = _clean(link.get_text(" ", strip=True))
        href = (link.get("href") or "").strip()
        if title is None or not href:
            return None
        # Keep the stable numeric ID + slug path as the identifier.  It is
        # also the exact relative URL needed for detail enrichment.
        ref = href.lstrip("/")
        if not re.match(r"^\d+/", ref):
            return None
        row = link.find_parent("tr")
        if row is None:
            return None

        description = None
        snippet = row.find("font", color=re.compile("grey", re.I))
        if snippet is not None:
            description = _clean(snippet.get_text(" ", strip=True))

        published = None
        date_link = row.find("a", href=_DATE_HREF_RE)
        if date_link is not None:
            match = _DATE_HREF_RE.search(date_link.get("href", ""))
            if match:
                published = _parse_iso_date(match.group(1))

        cells = row.find_all("td", recursive=False)
        newspaper = None
        if len(cells) > 1:
            newspaper = _clean(cells[1].get_text(" ", strip=True))

        return RawListing(
            external_id=ref,
            url=urljoin(self.base_url, href),
            raw_data={
                "title": title,
                "description": description,
                "published_at": published,
                "newspaper": newspaper,
            },
        )

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        data = raw.raw_data or {}
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=None,
            title=_clean(data.get("title")),
            organization=None,
            description=_clean(data.get("description")),
            source_url=raw.url,
            source_name=self.source_name,
            published_at=data.get("published_at"),
            deadline=None,
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
            paths = sorted(FIXTURES_DIR.glob(OFFLINE_DETAIL_GLOB))
            if not paths:
                raise FileNotFoundError(
                    f"No detail fixtures matching {OFFLINE_DETAIL_GLOB!r} in "
                    f"{FIXTURES_DIR}"
                )
            html = paths[0].read_text(encoding="utf-8")
            return RawListing(
                external_id=ref,
                url=f"{self.base_url}/{ref.lstrip('/')}",
                raw_html=html,
                raw_data=self._parse_detail(html),
            )
        url = f"{self.base_url}/{ref.lstrip('/')}"
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
        values: dict[str, Tag] = {}
        for row in soup.select("table tr"):
            header = row.find("th")
            value = row.find("td")
            if header is None or value is None:
                continue
            label = _clean(header.get_text(" ", strip=True))
            if label:
                values[label.lower()] = value

        def text(label: str) -> str | None:
            node = values.get(label)
            return _clean(node.get_text(" ", strip=True)) if node is not None else None

        city = None
        address = values.get("address")
        if address is not None:
            city_link = address.find("a", href=re.compile(r"/Tender/city/", re.I))
            if city_link is not None:
                city = _clean(city_link.get_text(" ", strip=True))

        documents = []
        doc_node = values.get("documents")
        if doc_node is not None:
            for anchor in doc_node.find_all("a", href=True):
                href = urljoin(BASE_URL, anchor["href"])
                if href.lower().endswith((".pdf", ".doc", ".docx")):
                    documents.append(href)

        return {
            "company": text("company name"),
            "deadline_text": text("deadline to apply"),
            "industry": text("industry"),
            "tender_id": text("tender id"),
            "city": city,
            "documents": documents,
        }

    def normalize_detail(self, raw: RawListing) -> NormalizedOpportunity:
        data = raw.raw_data or {}
        industry = _clean(data.get("industry"))
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=_clean(data.get("tender_id")),
            title=None,
            organization=_clean(data.get("company")),
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=None,
            deadline=_parse_date(data.get("deadline_text")),
            location=_clean(data.get("city")),
            province=None,
            category=industry,
            procurement_type=None,
            document_urls=list(data.get("documents") or []),
        )
