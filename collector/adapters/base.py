"""Abstract base class shared by every TenderScout source adapter.

One adapter class handles exactly one public procurement source. All
adapters normalize into the same :class:`~models.NormalizedOpportunity`
schema.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from http_client import PoliteClient
from models import NormalizedOpportunity, RawListing

if TYPE_CHECKING:
    from storage import Storage

logger = logging.getLogger(__name__)

#: Directory holding saved HTML fixtures used for offline development and
#: tests. Tests and offline runs never touch the live network.
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@dataclass
class RunResult:
    """Summary of one adapter run.

    ``found`` counts every listing processed. ``created``/``updated``
    count storage upserts and ``failed`` counts listings that raised.
    Listings that were already known and unchanged are included in
    ``found`` but not in the other counters.

    When detail enrichment is enabled, ``enriched`` counts opportunities
    whose detail page added data (a ``procurement_type``, documents, or
    any other previously-None field), ``detail_failures`` counts detail
    fetches/parses that failed (isolated; never aborts the run), and
    ``errors`` carries one ``"<external_id> detail: ..."`` entry per
    failure. With enrichment off, both stay 0.
    """

    found: int = 0
    created: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    enriched: int = 0
    detail_failures: int = 0


class SourceAdapter(ABC):
    """Base class for exactly one public procurement source.

    Subclasses set :attr:`source_name` and :attr:`base_url` and implement
    the abstract methods. Missing data is ``None``, never guessed.

    With ``offline=True``, adapters must read from ``collector/fixtures/``
    (see :meth:`read_fixture`) instead of the network; :meth:`get_html`
    refuses to fetch in offline mode.
    """

    source_name: str
    base_url: str

    def __init__(
        self,
        http_client: PoliteClient | None = None,
        offline: bool = False,
    ) -> None:
        """Create the adapter, optionally offline and/or with a custom client."""
        if not getattr(self, "source_name", None):
            raise TypeError(f"{type(self).__name__} must define source_name")
        if not getattr(self, "base_url", None):
            raise TypeError(f"{type(self).__name__} must define base_url")
        self.offline = offline
        self.http = http_client or PoliteClient()

    # ------------------------------------------------------------------
    # required interface
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch_listings(self, since: datetime | None = None) -> list[RawListing]:
        """Return the raw listings currently visible on the source.

        ``since`` may be used by adapters to limit results to items
        published after the given time; adapters that cannot filter
        server-side return everything.
        """

    @abstractmethod
    def normalize(self, raw: RawListing) -> NormalizedOpportunity:
        """Convert one raw listing into the normalized schema."""

    @abstractmethod
    def extract_documents(self, raw: RawListing) -> list[str]:
        """Return absolute URLs of the tender documents for a listing."""

    def fetch_opportunity(self, ref: str) -> RawListing:
        """Fetch a single opportunity by its external reference.

        Default implementation raises NotImplementedError; adapters with
        usable detail pages override this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement fetch_opportunity"
        )

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------

    def read_fixture(self, name: str) -> str:
        """Read a saved fixture file from ``collector/fixtures/``."""
        return (FIXTURES_DIR / name).read_text(encoding="utf-8")

    def get_html(self, url: str) -> str:
        """Fetch a page via PoliteClient; refused in offline mode."""
        if self.offline:
            raise RuntimeError(
                f"{self.source_name} is in offline mode; use read_fixture() "
                "instead of fetching from the network."
            )
        return self.http.get(url).text

    # ------------------------------------------------------------------
    # detail-page enrichment (Stage 7)
    # ------------------------------------------------------------------

    def supports_detail_enrichment(self) -> bool:
        """Return True when the adapter can fetch per-tender detail pages.

        Default False: adapters without :meth:`fetch_opportunity` (or
        whose listing already carries everything) leave it False; the
        run loop skips enrichment for them at zero cost.
        """
        return type(self).fetch_opportunity is not SourceAdapter.fetch_opportunity

    @staticmethod
    def merge_listing_with_detail(
        listing: NormalizedOpportunity, detail: NormalizedOpportunity
    ) -> NormalizedOpportunity:
        """Merge detail-page data into the listing data, filling gaps only.

        Every field of the result comes from the listing unless it is
        empty/None there and the detail page provides a value: the
        listing was collected first and never loses data to a less
        specific detail parse. Exceptions:

        * ``description`` keeps the listing's snippet even when the
          detail parse found no description (see
          :meth:`~ppra_epms.PPRAEpmsAdapter.normalize_detail`);
        * ``document_urls`` are the union (listing order first, then
          detail-only), deduplicated;
        * ``source_url``/``source_name``/``external_id`` are identity
          fields -- the detail variant of the same tender is expected to
          carry exactly the same values, and a mismatch raises
          ``ValueError`` instead of silently rewriting identity.

        Neither argument is modified.
        """
        identity = ("source_url", "source_name", "external_id")
        for name in identity:
            if getattr(listing, name) != getattr(detail, name):
                raise ValueError(
                    f"refusing to merge detail data with mismatched {name}: "
                    f"{getattr(listing, name)!r} != {getattr(detail, name)!r}"
                )
        merged = listing.model_copy(deep=True)
        for name in (
            "title",
            "organization",
            "description",
            "published_at",
            "deadline",
            "location",
            "category",
            "procurement_type",
            "reference_number",
        ):  # every NormalizedOpportunity field except identity + document_urls
            current = getattr(merged, name)
            if current is None or (
                isinstance(current, str) and not current.strip()
            ):
                setattr(merged, name, getattr(detail, name))
        merged.document_urls = list(
            dict.fromkeys([*listing.document_urls, *detail.document_urls])
        )
        return merged

    @classmethod
    def _detail_added_data(
        cls, merged: NormalizedOpportunity, listing: NormalizedOpportunity
    ) -> bool:
        """Return True when merging changed anything the caller can see.

        Only normalized-field values count: a detail page that adds a
        document already known from the listing, or re-states values the
        listing already carries, is not an enrichment.
        """
        return merged.model_dump(exclude={"source_url", "source_name"}) != (
            listing.model_dump(exclude={"source_url", "source_name"})
        )

    def enrich_listing(self, raw: RawListing) -> NormalizedOpportunity | None:
        """Fetch and parse the detail page for one listing raw.

        Returns the detail :class:`NormalizedOpportunity`, or ``None``
        when the adapter does not support detail enrichment. Errors
        propagate to the caller (the run loop isolates them per
        listing).
        """
        if not self.supports_detail_enrichment():
            return None
        detail_raw = self.fetch_opportunity(raw.external_id)
        return self.normalize_detail(detail_raw)

    def run(
        self,
        storage: Storage | None = None,
        since: datetime | None = None,
        *,
        enrich_details: bool = False,
        enrich_limit: int | None = None,
    ) -> RunResult:
        """Fetch, normalize and (optionally) store all current listings.

        With ``enrich_details=True`` and an adapter that supports detail
        pages, every listing also gets its detail page fetched
        (politely, through the same PoliteClient rate limits) and merged
        into the normalized opportunity before the upsert. Detail
        failures are isolated per listing: they are counted in
        ``detail_failures``/``errors`` and never abort the run or
        prevent the listing-level data from being stored.

        ``enrich_limit`` caps how many detail pages are fetched per run
        (e.g. for bounded demo/CI enrichment); listings beyond the cap
        are still stored with listing-level data.

        Other errors are isolated per listing too: one bad listing is
        logged and counted in ``failed``/``errors`` without stopping the
        run. When ``storage`` is given, every normalized opportunity is
        upserted (the source row is created on demand).
        """
        result = RunResult()
        try:
            listings = self.fetch_listings(since=since)
        except Exception as exc:  # noqa: BLE001 - isolate source-level failure
            result.failed = 1
            result.errors.append(f"fetch_listings failed: {exc!r}")
            logger.exception("fetch_listings failed for %s", self.source_name)
            return result

        source_id: int | None = None
        if storage is not None:
            source_id = storage.get_or_create_source(
                self.source_name, type(self).__name__
            )

        enrich = enrich_details and self.supports_detail_enrichment()
        detail_fetches = 0
        for raw in listings:
            result.found += 1
            try:
                normalized = self.normalize(raw)
                documents = self.extract_documents(raw)
                if documents:
                    merged_urls = dict.fromkeys([*normalized.document_urls, *documents])
                    normalized.document_urls = list(merged_urls)
                if enrich:
                    if enrich_limit is None or detail_fetches < enrich_limit:
                        detail_fetches += 1
                        try:
                            detail = self.enrich_listing(raw)
                            if detail is not None:
                                merged = self.merge_listing_with_detail(normalized, detail)
                                if self._detail_added_data(merged, normalized):
                                    result.enriched += 1
                                normalized = merged
                        except Exception as exc:  # noqa: BLE001 - isolated detail failure
                            result.detail_failures += 1
                            result.errors.append(
                                f"{raw.external_id} detail: {exc!r}"
                            )
                            logger.warning(
                                "detail enrichment failed for %s; keeping "
                                "listing-level data",
                                raw.external_id,
                                exc_info=True,
                            )
                if storage is not None and source_id is not None:
                    status = storage.upsert_opportunity(
                        normalized, source_id, raw_content=raw.raw_html
                    )
                    if status == "created":
                        result.created += 1
                    elif status == "updated":
                        result.updated += 1
            except Exception as exc:  # noqa: BLE001 - isolate per-listing failure
                result.failed += 1
                result.errors.append(f"{raw.external_id}: {exc!r}")
                logger.exception("failed to process listing %s", raw.external_id)
        return result
