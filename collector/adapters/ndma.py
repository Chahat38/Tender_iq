"""NDMA tenders - public notices of the National Disaster Management Authority.

Source: ``https://ndma.gov.pk/tenders`` (server-rendered cards).

Verified 2026-10-25:

* robots.txt disallows only ``/admin`` and ``/api``; the tenders path is
  explicitly outside those rules;
* the page shows nine ``div.tender-card`` blocks per page and paginates
  with a plain ``?page=N`` link (34 pages at verify time);
* a card carries the title, a submission date, an opening date/time and
  an Open/Closed badge, and links directly to the PDF in
  ``/storage/tenders/<Month><Year>/...pdf``;
* no login or CAPTCHA is involved.

Evaluation reports are deliberately skipped: they are results of a
completed procurement, not a tender opportunity.  Items are otherwise
kept even when their deadline has passed; the collector's normal
``--prune-expired`` run removes expired rows.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from dateutil.tz import gettz
from models import NormalizedOpportunity, RawListing

from .base import FIXTURES_DIR, SourceAdapter

logger = logging.getLogger(__name__)

SOURCE_NAME = "NDMA Tenders"
BASE_URL = "https://ndma.gov.pk"
LISTING_PATH = "/tenders"

OFFLINE_LISTING_GLOB = "ndma_tenders*.html"

MAX_LISTING_PAGES = 40
_ID_PREFIX = "NDMA-"

#: "Evaluation Report", "Technical Evaluation Report", "Final Evaluation
#: Report" are procurement results, not notices.
_EXCLUDED_TITLE_RE = re.compile(r"\bevaluation\s+report\b", re.I)

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


def _pdf_external_id(url: str) -> str | None:
    path = urlparse(url).path
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return f"{_ID_PREFIX}{stem}" if stem else None


class NdmaAdapter(SourceAdapter):
    """Adapter for NDMA's public tender notices."""

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

        listings = self._parse_listing_page(
            self.get_html(f"{self.base_url}{LISTING_PATH}")
        )
        for page in range(2, MAX_LISTING_PAGES + 1):
            rows = self._parse_listing_page(
                self.get_html(f"{self.base_url}{LISTING_PATH}?page={page}")
            )
            if not rows:
                break
            listings.extend(rows)
        return listings

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        soup = BeautifulSoup(html, "lxml")
        listings: list[RawListing] = []
        for card in soup.select("div.tender-card"):
            raw = self._card_to_raw(card)
            if raw is not None:
                listings.append(raw)
        return listings

    def _card_to_raw(self, card: Tag) -> RawListing | None:
        title_link = card.select_one(".tender-card__title a[href]")
        if title_link is None or not title_link.get("href"):
            return None
        title = _clean(title_link.get_text(" ", strip=True))
        if title is None or _EXCLUDED_TITLE_RE.search(title):
            return None

        url = title_link["href"].strip()
        if url.startswith("//"):
            url = f"https:{url}"
        if not url.startswith("http"):
            url = f"{self.base_url}/{url.lstrip('/')}"
        external_id = _pdf_external_id(url)
        if external_id is None:
            return None

        submission = card.find(attrs={"data-label": "Submission Date"})
        opening = card.find(attrs={"data-label": "Opening Date"})
        status = card.select_one(".tender-badge")
        return RawListing(
            external_id=external_id,
            url=url,
            raw_data={
                "title": title,
                "submission_text": (
                    _clean(submission.get_text(" ", strip=True))
                    if submission is not None
                    else None
                ),
                "opening_text": (
                    _clean(opening.get_text(" ", strip=True))
                    if opening is not None
                    else None
                ),
                "status": (
                    _clean(status.get_text(" ", strip=True))
                    if status is not None
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
            organization="National Disaster Management Authority (NDMA)",
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            # The card has no publication date; submission/opening dates are
            # deadlines, never guessed to be publication dates.
            published_at=None,
            deadline=_parse_date(data.get("submission_text")),
            location=None,
            province=None,
            category=None,
            procurement_type=None,
            document_urls=[raw.url],
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        return [raw.url] if raw.url.lower().endswith(".pdf") else []
