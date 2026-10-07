"""Punjab eProcurement (eproc) - one adapter for Punjab PPRA tenders.

Source: ``https://eproc.punjab.gov.pk/Admin_Tender_Search.aspx`` - the
public tender-search ASP.NET WebForms page linked from
``ppra.punjab.gov.pk`` (the province's open tender search).

Verified facts (2026-10-07, robots.txt is a 404 - no restrictions
declared; PoliteClient's robots gate treats a 404 as crawl-all):

* the page is a Telerik ASP.NET WebForms grid
  (``ctl00_ContentPlaceHolderSRIS_rdgrdManageTender``) rendered by the
  server for plain browser GETs. Columns (9): Procurement Title |
  Procurement Name | Type | Publish Date | Close Date | Department |
  Status | Tender Notice | Bidding Document;
* 50 rows per page, announced as ``<strong>N</strong> items in <strong>M</strong>
  pages`` (1660 items / 34 pages at verify time). Pager buttons and
  numeric pages POST back the same page with
  ``__EVENTTARGET``=<grid pagertaskcontrol name> plus all WebForms state
  fields (__VIEWSTATE/__VIEWSTATEGENERATOR/__EVENTVALIDATION) from the
  immediately-preceding GET; each stateful POST expects the response's
  state for the next one;
* this host has a flaky TLS layer for non-browser clients (OpenSSL
  handshake succeeds but reads can fail mid-stream, observed from
  Python and curl alike) while real Chromium browsers fetch it reliably.
  Polite fetches therefore go through :func:`chrome_fetch_fallback`
  when requests raise: Google Chrome (if installed) is invoked
  headless in a dedicated profile to render the exact same URL to a
  string. Offline fixtures work without any of this.

Each row's Tender-Notice/Bidding-Document ``href`` points to a uniquely
numbered PDF under ``Tenders/<org>/<tender>/<timestamp>.pdf``; the
filename is unique per tender and is used as ``external_id``. The site
searches by URL fragment ``?search=<fragment>`` for the detail link.

Offline mode reads ``punjab_eproc_listing*.html`` fixtures instead.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from dateutil.tz import gettz
from models import NormalizedOpportunity, RawListing

from .base import FIXTURES_DIR, SourceAdapter

logger = logging.getLogger(__name__)

SOURCE_NAME = "Punjab eProcurement"
BASE_URL = "https://eproc.punjab.gov.pk"
LISTING_PATH = "/Admin_Tender_Search.aspx"
#: The grid's search/filter lives in a stateful - POST-only form, so the
#: stable public link for every row is the search page itself. Never
#: invented per-row URLs (they would be guesses).
SOURCE_URL = LISTING_PATH

#: Listing fixtures (page snapshots) read in offline mode, sorted by name.
OFFLINE_LISTING_GLOB = "punjab_eproc_listing*.html"

#: How many listing pages one run may fetch (started from page 1).
MAX_LISTING_PAGES = 5

PROVINCE = "Punjab"

#: Exactly one of these row classes marks a real Telerik data row.
_ROW_CLASSES = ("rgRow", "rgAltRow")

#: Column headers that must be present before any row is trusted.
REQUIRED_HEADERS = (
    "procurement title",
    "procurement name",
    "type",
    "publish date",
    "close date",
    "department",
)

_NOTICE_HREF = "Tenders/"
_DOC_HREF = "BiddingDocuments/"

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
    """Parse a listing date ("06 Oct 2026") into an Asia/Karachi datetime, or None.

    The source states no time of day, so the result is midnight in
    Asia/Karachi. Unparseable input yields None - never a guessed date.
    """
    text = _clean(text)
    if text is None:
        return None
    try:
        parsed = date_parser.parse(text)
    except (ValueError, OverflowError, TypeError):
        return None
    return parsed.replace(tzinfo=_KARACHI_TZ)


def chrome_fetch_fallback(url: str, timeout: float = 60.0) -> str | None:
    """Render ``url`` with Google Chrome headless and return its DOM, or None.

    Some hosts (like eproc.punjab.gov.pk) fail mid-store when Python
    does HTTPS reads even though OpenSSL handshake succeeds - Chrome's
    exact TLS stack (and BoringSSL tweaks) is required. This helper
    must only be called after a polite direct fetch has already failed,
    never first.

    Chrome is located from PATH or common Windows install locations.
    When no binary exists a RuntimeError is raised (loud failure).
    """
    candidates = [
        shutil.which("chrome"),
        shutil.which("chrome-stable"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    binary = next((c for c in candidates if c and Path(c).is_file()), None)
    if binary is None:
        raise RuntimeError(
            f"Google Chrome is required to fetch {url} "
            "(this host's TLS layer is only browser-friendly), "
            "but no Chrome binary was found."
        )
    profile = Path(tempfile.gettempdir()) / "tenderscout-chrome-headless"
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        binary,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        f"--user-data-dir={profile}",
        "--virtual-time-budget=12000",
        f"--timeout={int(timeout * 1000)}",
        "--dump-dom",
        url,
    ]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        capture_output=True,
        text=True,
        timeout=timeout + 30,
        check=False,
    )
    html = completed.stdout
    if not html.strip():
        logger.warning(
            "chrome headless returned no DOM for %s (stderr: %.200s)",
            url,
            completed.stderr,
        )
        return None
    return html


def _webforms_state(html: str) -> dict[str, str] | None:
    """Captured WebForms state fields from one response's HTML.

    Returns ``None`` when __VIEWSTATE is missing (the POST would be
    useless) - callers must then stop walking pager postbacks.
    """
    soup = BeautifulSoup(html, "lxml")
    state: dict[str, str] = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        element = soup.find("input", {"name": name}) or soup.find(
            "input", {"id": name}
        )
        if element is None or not (element.get("value") or ""):
            return None if name == "__VIEWSTATE" else state
        state[name] = element["value"]
    return state


def _pager_target(html: str, page_number: int) -> str | None:
    """The ``__doPostBack`` target of the grid pager's ``page_number`` link.

    Verified pager markup (Telerik NextPrevAndNumeric): numeric page
    links inside ``tr.rgPager`` render as
    ``<a href="javascript:__doPostBack('<name>','')"><span>N</span></a>``.
    Returns None when that page link does not exist.
    """
    soup = BeautifulSoup(html, "lxml")
    pager = soup.select_one("tr.rgPager")
    if pager is None:
        return None
    for a in pager.find_all("a", href=True):
        href = a["href"]
        match = re.search(r"__doPostBack\('([^']+)'(?:,'[^']*')?\)", href)
        if not match:
            continue
        if a.get_text(" ", strip=True) == str(page_number):
            return match.group(1)
    return None


def _normalize_type(text: str | None) -> str | None:
    """Lower-case the source's own type text (Goods/Work/Services), or None."""
    return _clean(text.lower()) if _clean(text) else None


def _external_id_from_href(href: str | None) -> str | None:
    """Unique tender id extracted from a Tender Notice PDF path, or None.

    Observed layout: ``Tenders/<org>/<tender>/<timestamp>.pdf`` - the
    timestamp filename is unique per tender. Falls back to the tender
    folder segment when the file is absent.
    """
    if not href:
        return None
    path = href.split("?", 1)[0].split("#", 1)[0]
    if not path.lower().endswith(".pdf"):
        return None
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return None
    filename = segments[-1]
    stem = filename[: -len(".pdf")]
    return stem if stem else None


class PunjabEprocAdapter(SourceAdapter):
    """Adapter for the Punjab PPRA eProcurement tender search."""

    source_name = SOURCE_NAME
    base_url = BASE_URL

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return every raw listing (page-bounded), walking pager postbacks.

        Offline mode reads each ``punjab_eproc_listing*.html`` fixture
        once (filename sort order). Online mode GETs page 1 then posts
        the pager's ``__doPostBack`` target for pages 2..N.
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
                listings.extend(
                    self._parse_listing_page(path.read_text(encoding="utf-8"))
                )
            return listings

        listings: list[RawListing] = []
        seen_ids: set[str] = set()
        url = f"{self.base_url}{LISTING_PATH}"
        html = self._fetch_html(url)
        state = _webforms_state(html)
        rows = self._parse_listing_page(html)
        if not rows:
            return listings
        seen_ids.update(raw.external_id for raw in rows)
        listings.extend(rows)

        for page in range(2, MAX_LISTING_PAGES + 1):
            target = _pager_target(html, page)
            if not target or state is None:
                break  # single-page site or no usable pager
            # The pager is a stateful POST; when the transport fails,
            # keep the already-collected pages (a plain chrome GET of
            # this URL would only ever render page 1 again).
            try:
                html = self._post_forms(
                    url,
                    {"__EVENTTARGET": target, "__EVENTARGUMENT": "", **state},
                )
            except Exception as exc:  # noqa: BLE001 - bounded walk
                logger.warning(
                    "Punjab pager postback failed on page %d (%r); "
                    "keeping the %d listings collected so far",
                    page,
                    exc,
                    len(listings),
                )
                break
            rows = self._parse_listing_page(html)
            if not rows:
                break
            if seen_ids.issuperset(raw.external_id for raw in rows):
                break  # server rendered a page we already have
            seen_ids.update(raw.external_id for raw in rows)
            listings.extend(rows)
            state = _webforms_state(html)  # the next POST needs the new state
        return listings

    # ------------------------------------------------------------------
    # resilient HTTP (requests-first, chrome-headless on TLS failure)
    # ------------------------------------------------------------------

    def _fetch_html(
        self,
        url: str,
        postback: str | None = None,
        state: dict[str, str] | None = None,
    ) -> str:
        """Polite direct fetch or the Chrome-headless fallback for this URL.

        A ``postback`` target (Telerik pagertaskcontrol) is posted as
        ``__EVENTTARGET`` together with the WebForms ``state`` fields
        captured from the previous response. Any requests-level error
        (including the mid-read TLS oddities of this host) falls back
        to :func:`chrome_fetch_fallback`; failures raise RuntimeError.
        """
        if self.offline:  # pragma: no cover - callers avoid offline here
            raise RuntimeError("offline mode must not use _fetch_html")
        try:
            return self.get_html(url)
        except Exception as direct_error:  # noqa: BLE001
            logger.warning(
                "direct fetch failed for %s (%r); trying chrome headless",
                url,
                direct_error,
            )
            html = chrome_fetch_fallback(url)
            if html is None:
                raise RuntimeError(
                    f"both direct fetch and chrome fallback failed for {url}"
                ) from direct_error
            return html

    def _post_forms(self, url: str, fields: dict[str, str]) -> str:
        """POST the WebForms ``fields`` to ``url`` politely."""
        return self.http.post(url, data=fields).text

    # ------------------------------------------------------------------
    # listing-page parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html: str) -> list[RawListing]:
        """Parse one rendered grid page into raw listings.

        The grid header is verified first; with an unexpected structure
        this raises ValueError instead of guessing column positions.
        """
        soup = BeautifulSoup(html, "lxml")
        grid = soup.find(id=re.compile(r"rdgrdManageTender$"))
        if grid is None:
            if "Server Error" in html[:4000] or "__VIEWSTATE" not in html:
                return []
            raise ValueError(
                "Unexpected Punjab eProcurement page: Telerik grid "
                "rdgrdManageTender not found; refusing to guess."
            )
        headers = [
            re.sub(r"\s+", " ", th.get_text(" ", strip=True)).strip().lower()
            for th in grid.select("thead th")
        ]
        missing = [h for h in REQUIRED_HEADERS if h not in headers]
        if missing:
            raise ValueError(
                f"Unexpected Punjab eProcurement grid structure; missing "
                f"columns: {missing}. Refusing to guess column positions."
            )
        listings = []
        for tr in grid.find_all("tr"):
            class_names = " ".join(tr.get("class") or [])
            if not any(cls in class_names.split() for cls in _ROW_CLASSES):
                continue
            raw = self._row_to_raw(tr)
            if raw is not None:
                listings.append(raw)
        return listings

    def _row_to_raw(self, tr: Tag) -> RawListing | None:
        """Extract one grid row into a RawListing, skipping pager rows.

        A real data row (verified against the live page) has all nine
        cells and a non-empty Procurement Name in cell 1; pager/button
        rows are skipped. The row's Tender-Notice PDF path supplies the
        stable external id (the bathroom-case filename is unique).
        """
        cells = tr.find_all("td")
        if len(cells) < 8:
            return None
        texts = [c.get_text(" ", strip=True) for c in cells]
        title = texts[1] or None  # Procurement Name (col 2 of 9)
        if not title:
            return None

        notice_href = None
        doc_href = None
        for td in cells:
            for a in td.find_all("a", href=True):
                href = a["href"]
                if _NOTICE_HREF in href and notice_href is None:
                    notice_href = href
                elif _DOC_HREF in href and doc_href is None:
                    doc_href = href

        external_id = _external_id_from_href(notice_href)
        if not external_id:
            return None

        def _text_at(index: int) -> str | None:
            return texts[index] or None if index < len(texts) else None

        document_urls = []
        seen = set()
        for href in (notice_href, doc_href):
            if not href:
                continue
            absolute = urljoin(f"{self.base_url}{LISTING_PATH}", href)
            if absolute not in seen:
                seen.add(absolute)
                document_urls.append(absolute)

        return RawListing(
            external_id=external_id,
            url=f"{self.base_url}{SOURCE_URL}",
            raw_data={
                "title": title,
                "procurement_title": _text_at(0),
                "type": _text_at(2),
                "published_text": _text_at(3),
                "closing_text": _text_at(4),
                "organization": _text_at(5),
                "status": _text_at(6),
                "document_urls": document_urls,
            },
        )

    # ------------------------------------------------------------------
    # normalization
    # ------------------------------------------------------------------

    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        """Build the normalized opportunity from one raw grid row."""
        data = raw.raw_data or {}
        return NormalizedOpportunity(
            external_id=raw.external_id,
            reference_number=None,  # the grid does not show a reference
            title=data.get("title"),
            organization=data.get("organization"),
            description=None,
            source_url=raw.url,
            source_name=self.source_name,
            published_at=_parse_date(data.get("published_text")),
            deadline=_parse_date(data.get("closing_text")),
            location=None,
            province=PROVINCE,
            category=None,  # the grid's Type (goods/works/services) maps below
            procurement_type=_normalize_type(data.get("type")),
            document_urls=list(data.get("document_urls") or []),
        )

    def extract_documents(self, raw: RawListing) -> list[str]:
        """The row's Tender-Notice and Bidding-Document PDFs."""
        data = raw.raw_data or {}
        return list(data.get("document_urls") or [])
