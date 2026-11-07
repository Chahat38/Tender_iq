"""Adapter for Pakistan Ordnance Factories (POF) tenders.

Official site: https://pof.gov.pk. Every public path there answers HTTP
403 with ``Cf-Mitigated: challenge`` (a Cloudflare managed challenge), so
the site cannot be read by a polite crawler. TenderScout never bypasses a
bot challenge, so pof.gov.pk itself is *not* fetched.

POF does publish its own notices on the federal PPRA EPMS public portal
as "Pakistan Ordnance Factories (POF)" -- the same portal the
:mod:`ppra_epms` adapter already crawls under a verified compliance
entry (public pages, no robots.txt restrictions, no login/CAPTCHA).
This adapter reads that listing through EPMS's public keyword box and
keeps a row only when the organization or title actually names the
factories, so keyword matching can never attribute another agency's
tender to POF.

``external_id`` is the EPMS tender number (e.g. ``TS0000017178E``), the
same stable identifier the PPRA adapter stores, so the two sources
deduplicate exactly and the detail page is reachable with the inherited
:meth:`PPRAEpmsAdapter.fetch_opportunity`.

Dates are interpreted in Asia/Karachi by the inherited parser; the
procurement category, reference number, city and status come from the
listing row exactly as PPRA EPMS renders them.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote_plus

from models import RawListing

from .base import FIXTURES_DIR
from .ppra_epms import (
    BASE_URL,
    DEFAULT_TENDER_TYPE,
    LISTING_PATH,
    TENDER_TYPES,
    PPRAEpmsAdapter,
)

SOURCE_NAME = "POF Tenders"

#: EPMS keyword submitted to the portal's own search box.
KEYWORD = "Pakistan Ordnance Factories"

#: Listing fixtures (page snapshots) read in offline mode.
OFFLINE_LISTING_GLOB = "pof_listing*.html"

#: Case-insensitive markers that identify a POF notice. "Ordnance Factor"
#: covers both "Factory" and "Factories"; it deliberately does not match
#: other ordnance organizations such as "Central Ordnance Depot", which
#: is a Pakistan Army depot, not a POF factory.
POF_MARKERS = ("ordnance factor",)


class PofAdapter(PPRAEpmsAdapter):
    """Pakistan Ordnance Factories notices, collected via PPRA EPMS.

    Everything except the listing query is inherited: the row parser,
    date parsing, normalization, document extraction and detail-page
    enrichment are exactly the ones used for PPRA EPMS.
    """

    source_name = SOURCE_NAME
    base_url = BASE_URL

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def _listing_url(self, page: int, tender_type: str = DEFAULT_TENDER_TYPE) -> str:
        """Return the keyword-filtered listing URL for one page and type."""
        return (
            f"{self.base_url}{LISTING_PATH}"
            f"?keyword={quote_plus(KEYWORD)}&tender_type={tender_type}&page={page}"
        )

    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return POF's own rows from the EPMS public listing.

        Offline mode reads every ``pof_listing*.html`` fixture once and
        applies the same POF filter, so tests exercise the real parsing
        and filtering path without the network.

        Online mode walks the EPMS keyword listing for every tender type
        (Tender Notice, PQ, RFP, EOI) until a page yields no rows.
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
            return [raw for raw in listings if self.is_pof(raw)]

        listings = []
        for tender_type in TENDER_TYPES:
            for page in range(1, self.MAX_LISTING_PAGES + 1):
                html = self.get_html(self._listing_url(page, tender_type))
                rows = self._parse_listing_page(html)
                if not rows:
                    break
                listings.extend(raw for raw in rows if self.is_pof(raw))
        return listings

    # ------------------------------------------------------------------
    # filtering
    # ------------------------------------------------------------------

    @classmethod
    def is_pof(cls, raw: RawListing) -> bool:
        """Return True when a listing row's organization/title names POF."""
        data = raw.raw_data or {}
        text = " ".join(
            part
            for part in (
                str(data.get("organization") or ""),
                str(data.get("title") or ""),
            )
            if part
        ).lower()
        return any(marker in text for marker in POF_MARKERS)
