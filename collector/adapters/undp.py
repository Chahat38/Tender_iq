"""UNDP Procurement Notices adapter.

Source: https://procurement-notices.undp.org/
Also embedded by: https://www.undp.org/pakistan/procurement

**Status: implemented but deliberately NOT registered.** In October 2026
the notices host's ``robots.txt`` returned ``User-Agent: * / Disallow: /``,
so the project's compliance gate (and :class:`http_client.PoliteClient`)
forbids fetching it. The Pakistan page on ``www.undp.org`` does not publish
the table itself: it loads it from
``https://public-components.undp.org/?comp=proc_notices&cty_id_c=PAK``,
whose ``robots.txt`` is also ``Disallow: /``. The main site only links to
these hosts, so there is no crawlable ``www.undp.org`` path to the notices.
The parser is kept, tested offline and ready to wire into
``main.ADAPTERS`` / ``registry.SOURCE_REGISTRY`` if UNDP ever publishes a
crawlable feed or grants API access. Do not bypass the gate to enable it.

The global listing is a server-rendered table (no login, no CAPTCHA).
TenderScout tracks Pakistani procurement, so only notices whose
``UNDP Office/Country`` cell says ``PAKISTAN`` are kept.

Deadlines on the site are stated in New York time, e.g.
``01-Nov-26 03:00 AM (New York time)``. They are converted to
Asia/Karachi before normalization, the same timezone the rest of the
collector uses for naive deadlines.
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

SOURCE_NAME = "UNDP"
BASE_URL = "https://procurement-notices.undp.org"
LISTING_PATH = "/"

OFFLINE_LISTING_GLOB = "undp_list*.html"

#: Country filter: the app tracks Pakistani tenders only. The UNDP
#: listing mixes every country office, so non-Pakistan rows are skipped.
TARGET_COUNTRY = "pakistan"

_KARACHI_TZ = gettz("Asia/Karachi")
_NEW_YORK_TZ = gettz("America/New_York")

if _KARACHI_TZ is None or _NEW_YORK_TZ is None:  # pragma: no cover - env issue
    raise RuntimeError("Cannot resolve Asia/Karachi or America/New_York timezone")


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(text: str | None) -> datetime | None:
    """Parse a UNDP date cell into an Asia/Karachi datetime.

    The listing renders ``<nobr>01-Nov-26<br>03:00 AM (New York
    time)</nobr>``. ``get_text`` turns the ``<br>`` into whitespace; the
    explicit "(New York time)" marker selects the timezone. Bare dates
    (the Posted column) are date-only and are attached to Karachi.
    """
    text = _clean(text)
    if text is None:
        return None
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    lowered = text.lower()
    source_tz = _NEW_YORK_TZ if "new york" in lowered else _KARACHI_TZ
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    try:
        parsed = date_parser.parse(text, dayfirst=False)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=source_tz)
    try:
        return parsed.astimezone(_KARACHI_TZ)
    except (ValueError, OverflowError):  # pragma: no cover - defensive
        return parsed


class UndpAdapter(SourceAdapter):
    source_name = SOURCE_NAME
    base_url = BASE_URL

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        if self.offline:
            paths = sorted(FIXTURES_DIR.glob(OFFLINE_LISTING_GLOB))
            if not paths:
                raise FileNotFoundError(
                    f"No listing fixtures matching {OFFLINE_LISTING_GLOB!r} in {FIXTURES_DIR}"
                )
            listings: list[RawListing] = []
            for path in paths:
                listings.extend(
                    self._parse_listing_page(path.read_text(encoding="utf-8"))
                )
            return listings
        html = self.get_html(f"{self.base_url}{LISTING_PATH}")
        return self._parse_listing_page(html)

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        soup = BeautifulSoup(html, "lxml")
        listings: list[RawListing] = []
        selector = (
            'a.vacanciesTableLink[href*="view_negotiation.cfm"], '
            'a.vacanciesTableLink[href*="view_notice.cfm"]'
        )
        for link in soup.select(selector):
            raw = self._row_to_raw(link)
            if raw is not None:
                listings.append(raw)
        return listings

    def _row_to_raw(self, link: Tag) -> RawListing | None:
        href = link.get("href")
        if not href:
            return None
        full_url = urljoin(self.base_url, href)
        match = re.search(r"(?:nego_id|notice_id)=(\d+)", href)
        if not match:
            return None
        external_id = match.group(1)

        cells = link.select(".vacanciesTable__cell")
        if not cells:
            return None

        values: dict[str, str] = {}
        for cell in cells:
            label_el = cell.select_one(".vacanciesTable__cell__label")
            label = _clean(label_el.get_text()) if label_el else None
            span = cell.find("span")
            if not span or not label:
                continue
            text = re.sub(r"\s+", " ", span.get_text(" ", strip=True)).strip()
            if text:
                values[label.lower()] = text

        title = values.get("title")
        office = values.get("undp office/country")
        if not title or not office:
            return None
        # Keep Pakistani notices only (see module docstring).
        if TARGET_COUNTRY not in office.lower():
            return None

        return RawListing(
            external_id=external_id,
            url=full_url,
            raw_data={
                "title": title,
                "reference_number": values.get("ref no"),
                "office": office,
                "process": values.get("process"),
                "deadline_text": values.get("deadline"),
                "posted_text": values.get("posted"),
            },
        )

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        data = raw.raw_data or {}
        process = _clean(data.get("process"))
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=_clean(data.get("reference_number")),
            title=_clean(data.get("title")),
            organization="UNDP Pakistan",
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_date(data.get("posted_text")),
            deadline=_parse_date(data.get("deadline_text")),
            location="Pakistan",
            province=None,
            category=None,
            sector=None,
            department="UNDP",
            procurement_type=process,
            document_urls=[],
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        return []
