"""World Bank procurement notices for Pakistan - official JSON API.

Source: ``https://search.worldbank.org/api/v2/procnotices`` with
``project_ctry_code_exact=PK``.  The public Projects & Operations site
uses this same API; no login or key is required.

Verified 2026-10-25:

* ``search.worldbank.org/robots.txt`` returns 404 (no declared rules);
  ``projects.worldbank.org/robots.txt`` is allow-all apart from internal
  paths, and the public detail pages live under an allowed path;
* each notice has a stable ``id`` (``OP...``), a notice type, publication
  date, submission deadline, agency, project, reference number and HTML
  notice text;
* contract awards are excluded.  Only notices with a real submission
  deadline on or after today are collected, so the app does not present
  an award as an open tender.

The API returns newest first.  We inspect the newest 300 notices (three
100-row pages), which is enough to cover the current Pakistan pipeline
without walking the full archive of ~10,000 historic awards.
"""

from __future__ import annotations

import html as html_module
import json
import logging
import re
from datetime import date, datetime, time
from urllib.parse import urlencode

from dateutil import parser as date_parser
from dateutil.tz import gettz
from models import NormalizedOpportunity, RawListing

from .base import FIXTURES_DIR, SourceAdapter

logger = logging.getLogger(__name__)

SOURCE_NAME = "World Bank Procurement Notices"
BASE_URL = "https://projects.worldbank.org"
API_URL = "https://search.worldbank.org/api/v2/procnotices"

OFFLINE_LISTING_GLOB = "worldbank_procnotices*.json"

API_ROWS = 100
MAX_API_PAGES = 3

_KARACHI_TZ = gettz("Asia/Karachi")
if _KARACHI_TZ is None:  # pragma: no cover - environment issue
    raise RuntimeError("Cannot resolve Asia/Karachi timezone")

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_DOC_HREF_RE = re.compile(
    r"""href=["']([^"']+\.(?:pdf|docx?|xlsx?)(?:\?[^"']*)?)["']""", re.I
)


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _plain_text(value: object) -> str | None:
    text = _clean(value)
    if text is None:
        return None
    text = html_module.unescape(text)
    text = _TAG_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text or None


def _parse_notice_date(text: object) -> datetime | None:
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


def _parse_deadline(text: object) -> datetime | None:
    """Parse the API's ISO submission deadline as a Pakistan-local date."""
    text = _clean(text)
    if text is None:
        return None
    try:
        parsed = date_parser.isoparse(text)
    except (ValueError, OverflowError, TypeError):
        return None
    # The API sends a date at 00:00:00Z; the source does not state a time.
    # Keep the calendar date the World Bank meant, at Pakistan midnight.
    return datetime.combine(parsed.date(), time.min, tzinfo=_KARACHI_TZ)


def _detail_url(notice_id: str) -> str:
    return f"{BASE_URL}/en/projects-operations/procurement-detail/{notice_id}"


class WorldBankAdapter(SourceAdapter):
    """Adapter for the World Bank's official procurement-notices API."""

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
                payload = json.loads(path.read_text(encoding="utf-8"))
                listings.extend(self._parse_payload(payload))
            return listings

        listings: list[RawListing] = []
        today = datetime.now(_KARACHI_TZ).date()
        for page in range(MAX_API_PAGES):
            params = {
                "format": "json",
                "rows": API_ROWS,
                "os": page * API_ROWS,
                "fl": "*",
                "project_ctry_code_exact": "PK",
            }
            response = self.http.get(f"{API_URL}?{urlencode(params)}")
            payload = response.json()
            batch = payload.get("procnotices") or []
            listings.extend(self._parse_payload(payload, today=today))
            total = int(payload.get("total") or 0)
            if len(batch) < API_ROWS or (page + 1) * API_ROWS >= total:
                break
        return listings

    def _parse_payload(
        self, payload: dict[str, object], *, today: date | None = None
    ) -> list[RawListing]:
        raw_notices = payload.get("procnotices") or []
        if not isinstance(raw_notices, list):
            return []
        listings: list[RawListing] = []
        for notice in raw_notices:
            if not isinstance(notice, dict):
                continue
            raw = self._notice_to_raw(notice, today=today)
            if raw is not None:
                listings.append(raw)
        return listings

    def _notice_to_raw(
        self, notice: dict[str, object], *, today: date | None = None
    ) -> RawListing | None:
        notice_type = _clean(notice.get("notice_type"))
        if notice_type is not None and notice_type.lower() == "contract award":
            return None

        deadline = _parse_deadline(notice.get("submission_deadline_date"))
        if deadline is None:
            return None
        if today is None:
            today = datetime.now(_KARACHI_TZ).date()
        if deadline.date() < today:
            return None

        notice_id = _clean(notice.get("id"))
        if notice_id is None:
            return None
        title = (
            _clean(notice.get("noticetitle"))
            or _clean(notice.get("bid_description"))
            or _clean(notice.get("project_name"))
        )
        if title is None:
            return None

        return RawListing(
            external_id=f"WB-{notice_id}",
            url=_detail_url(notice_id),
            raw_data={"notice": notice},
        )

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        notice = (raw.raw_data or {}).get("notice") or {}
        if not isinstance(notice, dict):
            notice = {}
        notice_text = _clean(notice.get("notice_text"))
        documents = []
        if notice_text:
            for href in _DOC_HREF_RE.findall(notice_text):
                href = html_module.unescape(href)
                if href.startswith("http"):
                    documents.append(href)
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=_clean(notice.get("bid_reference_no")),
            title=(
                _clean(notice.get("noticetitle"))
                or _clean(notice.get("bid_description"))
                or _clean(notice.get("project_name"))
            ),
            organization=_clean(notice.get("agency_name")),
            description=_plain_text(notice_text),
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_notice_date(notice.get("noticedate")),
            deadline=_parse_deadline(notice.get("submission_deadline_date")),
            location=None,
            province=None,
            category=_clean(notice.get("procurement_group_desc")),
            procurement_type=_clean(notice.get("notice_type")),
            document_urls=list(dict.fromkeys(documents)),
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        return []
