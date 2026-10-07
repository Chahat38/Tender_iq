"""Read-only HTTP API over the TenderScout database (Stage 6).

FastAPI application exposing the collected opportunities as JSON. The
API performs only SELECT-style reads through :mod:`storage` and
:mod:`search`; there are no write endpoints, so the collector CLI and
cron workflow remain the only writers.

Run it from the repository root::

    python collector/api.py --port 8000
    # equivalent: python -m uvicorn api:app --app-dir collector

Endpoints:

* ``GET /`` -- service descriptor with endpoint links,
* ``GET /health`` -- liveness probe with coarse row counts,
* ``GET /sources`` -- registered procurement sources,
* ``GET /locations`` -- distinct locations for the city filter,
* ``GET /categories`` -- distinct categories for the category filter,
* ``GET /provinces`` -- distinct provinces for the province filter,
* ``GET /procurement_types`` -- distinct procurement types for the
  filter,
* ``GET /opportunities`` -- newest-first page of opportunities
  (non-duplicate rows by default; ``sort`` can page by closing deadline),
* ``POST /opportunities/query`` -- same, with a JSON body for
  tri-state deadline filtering,
* ``GET /opportunities/{opportunity_id}`` -- one opportunity with
  documents and update history,
* ``GET /opportunities/{opportunity_id}/duplicates`` -- the same notice
  as found on other sources,
* ``GET /opportunities/{opportunity_id}/analysis`` -- the AI analysis
  of one opportunity,
* ``POST /ai/{analyze,dedup,rfp}`` -- optional LLM assistance
  (OpenAI-compatible, Grok works) for summaries, duplicate
  verification and RFP/bid document drafts,
* ``GET /notifications`` -- "new tenders" and "closing soon" buckets,
* ``GET /search?q=...`` -- FTS5 full-text search (same strict query
  translation and bm25 ranking as the CLI),
* ``GET /alerts?user_id=...`` -- user-facing alerts (newest first),
* ``GET /matches?user_id=...`` -- per-user relevance matches.

Errors: unknown ids answer 404, invalid parameters answer 422, a
SQLite build without FTS5 makes ``/search`` answer 503 (every other
endpoint keeps working), database failures answer 500 without leaking
SQL or file paths, and exceeding the per-IP budget answers 429 with a
``Retry-After`` header (``TENDERSCOUT_RATE_LIMIT`` requests per
minute, default 300, 0 disables). Every response carries
``X-Content-Type-Options: nosniff``, ``X-Frame-Options: DENY`` and
``Referrer-Policy: no-referrer``. Internal columns (``raw_content``,
``storage_path``, dedup columns) never appear in responses.

The database path defaults to ``$TENDERSCOUT_DB_PATH`` or
``collector/tenderscout.db`` (resolved relative to this file, so the
server can be started from any working directory).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Union

from fastapi import FastAPI, HTTPException, Path as FastAPIPath, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import ai_endpoints  # noqa: E402
from ai import Analysis, analyze  # noqa: E402
from search import SearchIndex  # noqa: E402
from storage import NULL_DEADLINE_FILTER, Storage  # noqa: E402
from storage_postgres import PostgresStorage  # noqa: E402

logger = logging.getLogger(__name__)

#: Hard cap on rows per page, shared with the FTS5 search layer so both
#: read paths behave the same way.
_API_LIMIT_MAX = 200


class FtsUnavailableError(RuntimeError):
    """The SQLite build has no FTS5 support; ``/search`` answers 503."""


# ----------------------------------------------------------------------
# response models
# ----------------------------------------------------------------------


class SourceOut(BaseModel):
    """One registered procurement source."""

    id: int
    name: str
    adapter_type: str
    active: int
    robots_allowed: int | None = None
    terms_checked: int | None = None
    requires_authentication: int | None = None
    crawl_frequency: str | None = None
    last_crawled_at: str | None = None


class OpportunityOut(BaseModel):
    """Source-recorded fields of one opportunity.

    ``raw_content`` and the internal dedup columns (``is_primary``,
    ``duplicate_group_id``) are deliberately not exposed. Datetime-ish
    columns are returned as ISO-8601 strings exactly as stored.
    """

    id: int
    source_id: int
    external_id: str
    reference_number: str | None = None
    title: str | None = None
    organization: str | None = None
    description: str | None = None
    source_url: str
    published_at: str | None = None
    deadline: str | None = None
    location: str | None = None
    category: str | None = None
    procurement_type: str | None = None
    content_hash: str
    first_seen_at: str
    last_seen_at: str
    last_verified_at: str


class DocumentOut(BaseModel):
    """One document attached to an opportunity."""

    id: int
    url: str
    filename: str | None = None


class UpdateOut(BaseModel):
    """One recorded field change of an opportunity."""

    id: int
    field: str
    old_value: str | None = None
    new_value: str | None = None
    detected_at: str
    update_type: str


class OpportunityDetailOut(OpportunityOut):
    """One opportunity plus its documents and update history."""

    documents: list[DocumentOut] = Field(default_factory=list)
    updates: list[UpdateOut] = Field(default_factory=list)


class DuplicateOut(BaseModel):
    """A sibling listing of the same tender notice on another source."""

    id: int
    source_id: int
    source_name: str | None = None
    title: str | None = None
    organization: str | None = None
    deadline: str | None = None
    source_url: str | None = None
    is_primary: bool = False


class NotificationsOut(BaseModel):
    """Notification buckets the UI turns into alert cards."""

    generated_at: str
    window_days: int
    new_tenders: list[OpportunityOut]
    new_total: int
    closing_soon: list[OpportunityOut]
    closing_total: int


class AiDedupRequest(BaseModel):
    """Candidate duplicate group for the /ai/dedup verifier."""

    opportunity_ids: list[int] = Field(
        ...,
        min_length=2,
        max_length=10,
        description="Two or more listings believed to be the same notice.",
    )

    @field_validator("opportunity_ids")
    @classmethod
    def _positive_ids(cls, value: list[int]) -> list[int]:
        if any(opportunity_id < 1 for opportunity_id in value):
            raise ValueError("opportunity ids must be positive")
        return value


class AiRfpRequest(BaseModel):
    """Inputs for the bid/RFP document draft."""

    opportunity_id: int = Field(..., ge=1)
    company_name: str = Field("", max_length=200)
    capabilities: list[str] = Field(default_factory=list, max_length=20)


class AnalysisOut(BaseModel):
    """AI analysis of one opportunity (``opportunity_ai`` row).

    List columns are stored as JSON text by the collector and decoded
    here; malformed JSON answers an empty list instead of a 500.
    """

    opportunity_id: int
    category: str | None = None
    subcategory: str | None = None
    keywords: list[str] = Field(default_factory=list)
    procurement_type: str | None = None
    requirements: list[str] = Field(default_factory=list)
    date_conflicts: list[str] = Field(default_factory=list)
    prompt_version: str | None = None
    created_at: str

    @field_validator("keywords", "requirements", "date_conflicts", mode="before")
    @classmethod
    def _decode_json_list(cls, value: Any) -> list[Any]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return []
            return parsed if isinstance(parsed, list) else []
        return list(value or [])


class AlertOut(BaseModel):
    """One user-facing alert (``alerts`` row)."""

    id: str
    user_id: str
    opportunity_id: int
    type: str
    title: str
    body: str | None = None
    channel: str = "in_app"
    created_at: str
    read_at: str | None = None


class MatchOut(BaseModel):
    """One (opportunity, user) relevance score."""

    id: str
    opportunity_id: int
    user_id: str
    relevance_score: int
    level: str
    reasons: list[str] = Field(default_factory=list)
    prompt_version: str | None = None
    model: str | None = None
    created_at: str
    updated_at: str

    @field_validator("reasons", mode="before")
    @classmethod
    def _decode_reasons(cls, value: Any) -> list[Any]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return []
            return parsed if isinstance(parsed, list) else []
        return list(value or [])


class OpportunityPage(BaseModel):
    """One page of opportunities with paging metadata."""

    total: int = Field(
        description="Number of opportunities matching the filters "
        "(independent of limit/offset)."
    )
    limit: int
    offset: int
    opportunities: list[OpportunityOut]


class SearchHitOut(BaseModel):
    """One ranked full-text search hit (bm25; lower rank = better)."""

    opportunity_id: int
    external_id: str
    source_id: int
    title: str | None = None
    organization: str | None = None
    snippet: str | None = None
    deadline: str | None = None
    location: str | None = None
    category: str | None = None
    procurement_type: str | None = None
    reference_number: str | None = None
    source_url: str
    rank: float


class SearchPage(BaseModel):
    """One page of search hits with paging metadata."""

    query: str
    total: int = Field(
        description="Total matches for the query (independent of limit/offset)."
    )
    limit: int
    offset: int
    results: list[SearchHitOut]


class OpportunityFilter(BaseModel):
    """JSON body for ``POST /opportunities/query``.

    ``deadline_after`` is tri-state, which a GET query string cannot
    express: ``null`` (default) applies no lower bound and keeps rows
    without a deadline, ``false`` hides rows without a deadline, and a
    datetime keeps only rows with a strictly later deadline.
    """

    limit: int = Field(20, ge=0, le=_API_LIMIT_MAX)
    offset: int = Field(0, ge=0)
    source_id: int | None = Field(None, ge=1)
    location: str | None = Field(
        None,
        max_length=120,
        description="Keep rows whose location contains this text "
        "(case-insensitive substring; scraped locations are free text).",
    )
    category: str | None = Field(
        None,
        max_length=120,
        description="Keep rows whose stored category equals this value "
        "case-insensitively (use /categories for valid values).",
    )
    organization: str | None = Field(
        None,
        max_length=160,
        description="Keep rows whose organization contains this text "
        "(case-insensitive substring).",
    )
    province: str | None = Field(
        None,
        max_length=60,
        description="Keep rows whose stored province equals this value "
        "case-insensitively (use /provinces for valid values).",
    )
    deadline_from: str | None = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Only deadlines on or after this date (YYYY-MM-DD, "
        "inclusive on the deadline's Pakistan-local date; undated rows drop out).",
    )
    deadline_to: str | None = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Only deadlines on or before this date (YYYY-MM-DD, "
        "inclusive; undated rows drop out).",
    )
    deadline_before: datetime | None = Field(
        None,
        description="Only rows with a deadline strictly before this ISO datetime.",
    )
    deadline_after: Union[Literal[False], datetime, None] = Field(
        None,
        description="false hides rows without a deadline; null keeps them; "
        "a datetime keeps only later deadlines.",
    )
    procurement_type: str | None = Field(
        None,
        max_length=80,
        description="Keep rows whose stored procurement type equals this "
        "value case-insensitively (use /procurement_types for valid values).",
    )
    sort: Literal["newest", "closing"] = Field(
        "newest",
        description="newest = insertion order (default); "
        "closing = soonest deadline first, undated rows last.",
    )
    include_duplicates: bool = Field(
        False,
        description="By default one row per tender notice (the same notice "
        "posted on several sources collapses to the primary listing). "
        "Set true to include every duplicate row.",
    )

    @field_validator("deadline_after")
    @classmethod
    def _reject_true(cls, value: Any) -> Any:
        """Reject ``true`` explicitly (it would otherwise read as a datetime)."""
        if value is True:
            raise ValueError("deadline_after must be a datetime, false or null")
        return value


def _default_db_path() -> str:
    """Database path from the environment, or the collector default."""
    return os.environ.get(
        "TENDERSCOUT_DB_PATH",
        str(Path(__file__).resolve().parent / "tenderscout.db"),
    )


def _deterministic_summary(row: Mapping[str, Any], analysis: Analysis) -> str:
    """One-sentence fallback summary (used when no LLM is configured)."""
    title = row.get("title") or "A tender notice"
    org = row.get("organization") or "an undisclosed buyer"
    category = analysis.category or row.get("category")
    parts = [title, f"issued by {org}"]
    if category:
        parts.append(f"in the {category} field")
    if row.get("deadline"):
        parts.append(f"closes {str(row['deadline'])[:10]}")
    if row.get("location"):
        parts.append(f"at {row['location']}")
    return f"{', '.join(parts)}."


def create_app(db_path: str | Path | None = None) -> FastAPI:
    """Build the read-only API application for one database.

    ``db_path=None`` uses ``$TENDERSCOUT_DB_PATH`` or
    ``collector/tenderscout.db``. The storage handle is opened when the
    ASGI lifespan starts and closed when it ends; requests arriving
    without a running lifespan (as in tests) initialize it lazily on
    first use. The handle is shared across request threads
    (``check_same_thread=False``) and every read is serialized through
    ``data_lock``.
    """
    # init_lock guards one-time initialization (storage handle, search
    # index); data_lock serializes reads on the shared SQLite connection.
    init_lock = threading.Lock()
    data_lock = threading.Lock()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "storage", None) is None:
            with init_lock:
                if getattr(app.state, "storage", None) is None:
                    app.state.storage = Storage(
                        app.state.db_path or _default_db_path(),
                        check_same_thread=False,
                    )
        storage = app.state.storage
        logger.info("TenderScout API serving %s", storage.db_path)
        try:
            yield
        finally:
            app.state.search_index = None
            if getattr(app.state, "storage", None) is not None:
                app.state.storage.close()
                app.state.storage = None
            logger.info("TenderScout API shut down")

    app = FastAPI(
        title="TenderScout AI API",
        description="Read-only JSON API over the collected tender database.",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.db_path = db_path
    app.state.storage = None
    app.state.search_index = None
    app.state.rate_state = {}

    @app.exception_handler(FtsUnavailableError)
    async def _fts_unavailable_handler(
        request: Any, exc: FtsUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": f"Full-text search unavailable: {exc}"},
        )

    @app.exception_handler(sqlite3.DatabaseError)
    async def _database_error_handler(
        request: Any, exc: sqlite3.DatabaseError
    ) -> JSONResponse:
        logger.exception("database error serving %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal database error."},
        )

    def _rate_limit_settings() -> tuple[int, int]:
        """(max_requests, window_seconds); 0 disables the limiter."""
        raw = os.environ.get("TENDERSCOUT_RATE_LIMIT", "300")
        try:
            max_requests = int(raw)
        except ValueError:
            max_requests = 300
        return max_requests, 60

    @app.middleware("http")
    async def _hardening_middleware(request: Any, call_next: Any) -> Any:
        """Security headers on every response + per-IP rate limiting.

        The limiter is a fixed 60-second window keyed by client IP
        (``TENDERSCOUT_RATE_LIMIT`` requests per window, default 300,
        0 disables it) - enough to stop scrapers without a reverse
        proxy, and it never touches the read paths themselves.
        """
        max_requests, window = _rate_limit_settings()
        if max_requests > 0:
            ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            state = app.state.rate_state
            if len(state) > 10_000:  # prune stale buckets opportunistically
                app.state.rate_state = {
                    key: bucket
                    for key, bucket in state.items()
                    if now - bucket[0] < window
                }
                state = app.state.rate_state
            bucket = state.get(ip)
            if bucket is None or now - bucket[0] >= window:
                state[ip] = [now, 1]
            else:
                bucket[1] += 1
                if bucket[1] > max_requests:
                    retry_after = int(window - (now - bucket[0])) + 1
                    response: Any = JSONResponse(
                        status_code=429,
                        content={"detail": "rate limit exceeded"},
                        headers={"Retry-After": str(retry_after)},
                    )
                    response.headers["X-Content-Type-Options"] = "nosniff"
                    response.headers["X-Frame-Options"] = "DENY"
                    return response
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def _get_storage() -> Storage:
        """Return the shared Storage handle, initializing it lazily."""
        storage = getattr(app.state, "storage", None)
        if storage is None:
            with init_lock:
                storage = getattr(app.state, "storage", None)
                if storage is None:
                    storage = Storage(
                        app.state.db_path or _default_db_path(),
                        check_same_thread=False,
                    )
                    app.state.storage = storage
        return storage

    def _get_index() -> SearchIndex:
        """Return the shared FTS5 index, creating it on first use.

        Raises :class:`FtsUnavailableError` (-> HTTP 503) when the
        SQLite build has no FTS5 support.
        """
        index = getattr(app.state, "search_index", None)
        if index is None:
            with init_lock:
                index = getattr(app.state, "search_index", None)
                if index is None:
                    try:
                        index = SearchIndex(_get_storage())
                    except RuntimeError as exc:
                        raise FtsUnavailableError(str(exc)) from exc
                    app.state.search_index = index
        return index

    def _query_opportunities(
        *,
        limit: int,
        offset: int,
        source_id: int | None,
        location: str | None,
        deadline_before: datetime | None,
        deadline_after: Any,
        category: str | None = None,
        organization: str | None = None,
        province: str | None = None,
        deadline_from: str | None = None,
        deadline_to: str | None = None,
        procurement_type: str | None = None,
        sort: str = "newest",
        include_duplicates: bool = False,
    ) -> OpportunityPage:
        """Shared read path for GET /opportunities and the POST filter."""
        with data_lock:
            storage = _get_storage()
            rows = storage.list_opportunities(
                limit=limit,
                offset=offset,
                source_id=source_id,
                location=location,
                deadline_before=deadline_before,
                deadline_after=deadline_after,
                category=category,
                organization=organization,
                province=province,
                deadline_from=deadline_from,
                deadline_to=deadline_to,
                primary_only=not include_duplicates,
                procurement_type=procurement_type,
                sort=sort,
            )
            total = storage.count_opportunities(
                source_id=source_id,
                location=location,
                deadline_before=deadline_before,
                deadline_after=deadline_after,
                category=category,
                organization=organization,
                province=province,
                deadline_from=deadline_from,
                deadline_to=deadline_to,
                primary_only=not include_duplicates,
                procurement_type=procurement_type,
            )
        return OpportunityPage(
            total=total,
            limit=limit,
            offset=offset,
            opportunities=[OpportunityOut(**row) for row in rows],
        )

    @app.get("/", tags=["meta"])
    def root() -> dict[str, Any]:
        """Service descriptor with links to the read-only endpoints."""
        return {
            "name": "TenderScout AI API",
            "version": "0.1.0",
            "mode": "read-only",
            "endpoints": {
                "health": "/health",
                "sources": "/sources",
                "locations": "/locations",
                "categories": "/categories",
                "provinces": "/provinces",
                "procurement_types": "/procurement_types",
                "opportunities": "/opportunities",
                "opportunity_query": "POST /opportunities/query",
                "opportunity": "/opportunities/{opportunity_id}",
                "opportunity_duplicates": "/opportunities/{opportunity_id}/duplicates",
                "opportunity_analysis": "/opportunities/{opportunity_id}/analysis",
                "ai_analyze": "POST /ai/analyze/{opportunity_id}",
                "ai_dedup": "POST /ai/dedup",
                "ai_rfp": "POST /ai/rfp",
                "notifications": "/notifications",
                "search": "/search?q=QUERY",
                "alerts": "/alerts?user_id=USER_ID",
                "matches": "/matches?user_id=USER_ID",
                "docs": "/docs",
            },
        }

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, Any]:
        """Liveness probe with coarse row counts (no paths, no SQL)."""
        with data_lock:
            storage = _get_storage()
            return {
                "status": "ok",
                "opportunities": storage.count_opportunities(),
                "sources": len(storage.list_sources()),
            }

    @app.get("/sources", response_model=list[SourceOut], tags=["sources"])
    def list_sources() -> list[SourceOut]:
        """All registered procurement sources."""
        with data_lock:
            rows = _get_storage().list_sources()
        return [SourceOut(**row) for row in rows]

    @app.get(
        "/opportunities", response_model=OpportunityPage, tags=["opportunities"]
    )
    def list_opportunities(
        limit: int = Query(
            20, ge=0, le=_API_LIMIT_MAX, description="Page size (0 returns no rows)."
        ),
        offset: int = Query(0, ge=0),
        source_id: int | None = Query(None, ge=1),
        location: str | None = Query(
            None,
            max_length=120,
            description="Keep rows whose location contains this text "
            "(case-insensitive substring; scraped locations are free text).",
        ),
        category: str | None = Query(
            None,
            max_length=120,
            description="Keep rows whose stored category equals this value "
            "case-insensitively (use /categories for valid values).",
        ),
        organization: str | None = Query(
            None,
            max_length=160,
            description="Keep rows whose organization contains this text "
            "(case-insensitive substring).",
        ),
        province: str | None = Query(
            None,
            max_length=60,
            description="Keep rows whose stored province equals this value "
            "case-insensitively (use /provinces for valid values).",
        ),
        deadline_from: str | None = Query(
            None,
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="Only deadlines on or after this date (YYYY-MM-DD, "
            "inclusive on the deadline's Pakistan-local date; undated rows drop out).",
        ),
        deadline_to: str | None = Query(
            None,
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="Only deadlines on or before this date (YYYY-MM-DD, "
            "inclusive; undated rows drop out).",
        ),
        deadline_before: datetime | None = Query(
            None,
            description="Only rows with a deadline strictly before this datetime.",
        ),
        deadline_after: datetime | None = Query(
            None,
            description="Only rows with a strictly later deadline; rows without "
            "a deadline then drop out (use POST /opportunities/query to keep them).",
        ),
        procurement_type: str | None = Query(
            None,
            max_length=80,
            description="Keep rows whose stored procurement type equals this "
            "value case-insensitively (use /procurement_types for valid values).",
        ),
        sort: Literal["newest", "closing"] = Query(
            "newest",
            description="newest = insertion order (default); "
            "closing = soonest deadline first, undated rows last.",
        ),
        include_duplicates: bool = Query(
            False,
            description="By default one row per tender notice (the same notice "
            "posted on several sources collapses to the primary listing). Set "
            "true to include every duplicate row.",
        ),
    ) -> OpportunityPage:
        """Page of tenders, one row per notice by default.

        ``sort=closing`` orders by soonest deadline (undated rows
        last); ``include_duplicates=true`` is the internal/admin view
        that shows the same notice as reported by every source.
        """
        return _query_opportunities(
            limit=limit,
            offset=offset,
            source_id=source_id,
            location=location,
            category=category,
            organization=organization,
            province=province,
            deadline_from=deadline_from,
            deadline_to=deadline_to,
            deadline_before=deadline_before,
            deadline_after=(
                deadline_after if deadline_after is not None else NULL_DEADLINE_FILTER
            ),
            procurement_type=procurement_type,
            sort=sort,
            include_duplicates=include_duplicates,
        )

    @app.get("/locations", response_model=list[str], tags=["opportunities"])
    def list_locations() -> list[str]:
        """Distinct location strings currently in the database (a facet
        for the city filter; values are opaque scraped text)."""
        with data_lock:
            return _get_storage().list_distinct_locations()

    @app.get("/categories", response_model=list[str], tags=["opportunities"])
    def list_categories() -> list[str]:
        """Distinct stored categories (facet for the category filter;
        values are opaque scraped labels, lowercase)."""
        with data_lock:
            return _get_storage().list_distinct_categories()

    @app.get("/provinces", response_model=list[str], tags=["opportunities"])
    def list_provinces() -> list[str]:
        """Distinct stored provinces (facet for the province filter;
        adapter-written, lowercase)."""
        with data_lock:
            return _get_storage().list_distinct_provinces()

    @app.get("/procurement_types", response_model=list[str], tags=["opportunities"])
    def list_procurement_types() -> list[str]:
        """Distinct stored procurement types, most common first (facet
        for the procurement_type filter; values are scraped labels)."""
        with data_lock:
            return _get_storage().list_distinct_procurement_types()

    @app.post(
        "/opportunities/query",
        response_model=OpportunityPage,
        tags=["opportunities"],
    )
    def query_opportunities(body: OpportunityFilter) -> OpportunityPage:
        """Filter opportunities with a JSON body.

        Identical to ``GET /opportunities`` but ``deadline_after`` is
        tri-state (see :class:`OpportunityFilter`).
        """
        if body.deadline_after is None:
            deadline_after: Any = NULL_DEADLINE_FILTER
        elif body.deadline_after is False:
            deadline_after = None  # hide rows without a deadline
        else:
            deadline_after = body.deadline_after
        return _query_opportunities(
            limit=body.limit,
            offset=body.offset,
            source_id=body.source_id,
            location=body.location,
            category=body.category,
            organization=body.organization,
            province=body.province,
            deadline_from=body.deadline_from,
            deadline_to=body.deadline_to,
            deadline_before=body.deadline_before,
            deadline_after=deadline_after,
            procurement_type=body.procurement_type,
            sort=body.sort,
            include_duplicates=body.include_duplicates,
        )

    @app.get(
        "/opportunities/{opportunity_id}",
        response_model=OpportunityDetailOut,
        tags=["opportunities"],
    )
    def get_opportunity(
        opportunity_id: int = FastAPIPath(..., ge=1),
    ) -> OpportunityDetailOut:
        """One opportunity with its documents and update history."""
        with data_lock:
            storage = _get_storage()
            row = storage.get_opportunity(opportunity_id)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"opportunity {opportunity_id} not found",
                )
            documents = storage.get_documents(opportunity_id)
            updates = storage.get_updates(opportunity_id)
        return OpportunityDetailOut(
            **row,
            documents=[DocumentOut(**doc) for doc in documents],
            updates=[UpdateOut(**upd) for upd in updates],
        )

    @app.get(
        "/opportunities/{opportunity_id}/analysis",
        response_model=AnalysisOut,
        tags=["opportunities"],
    )
    def get_opportunity_analysis(
        opportunity_id: int = FastAPIPath(..., ge=1),
    ) -> AnalysisOut:
        """AI analysis of one opportunity (404 until the worker ran)."""
        with data_lock:
            storage = _get_storage()
            if storage.get_opportunity(opportunity_id) is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"opportunity {opportunity_id} not found",
                )
            analysis = storage.get_opportunity_ai(opportunity_id)
            if analysis is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"no analysis for opportunity {opportunity_id} yet "
                        "(run the collector's AI pass)"
                    ),
                )
        return AnalysisOut(**analysis)

    @app.get(
        "/opportunities/{opportunity_id}/duplicates",
        response_model=list[DuplicateOut],
        tags=["opportunities"],
    )
    def get_opportunity_duplicates(
        opportunity_id: int = FastAPIPath(..., ge=1),
    ) -> list[DuplicateOut]:
        """The same tender notice as published by other sources.

        The dedup pass groups notices it believes are identical (same
        reference number/title across sources); ``/opportunities``
        shows the primary row only, this endpoint surfaces the group.
        Answers an empty list for ungrouped rows.
        """
        with data_lock:
            storage = _get_storage()
            if storage.get_opportunity(opportunity_id) is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"opportunity {opportunity_id} not found",
                )
            rows = storage.list_duplicates(opportunity_id)
        return [DuplicateOut(**row) for row in rows]

    @app.get("/alerts", response_model=list[AlertOut], tags=["alerts"])
    def list_alerts(
        user_id: str = Query(
            ..., min_length=1, max_length=128, description="Alert owner."
        ),
        unread_only: bool = Query(False, description="Only unread alerts."),
        limit: int = Query(50, ge=1, le=_API_LIMIT_MAX),
    ) -> list[AlertOut]:
        """Newest alerts for one user (read-only; mark-read is external)."""
        with data_lock:
            rows = _get_storage().list_alerts(
                user_id, unread_only=unread_only, limit=limit
            )
        return [AlertOut(**row) for row in rows]

    @app.get("/matches", response_model=list[MatchOut], tags=["matches"])
    def list_matches(
        user_id: str = Query(..., min_length=1, max_length=128),
        min_score: int = Query(0, ge=0, le=100),
        limit: int = Query(50, ge=1, le=_API_LIMIT_MAX),
    ) -> list[MatchOut]:
        """Best relevance matches for one user, score-descending."""
        with data_lock:
            rows = _get_storage().list_matches(
                user_id, min_score=min_score, limit=limit
            )
        return [MatchOut(**row) for row in rows]

    @app.get("/search", response_model=SearchPage, tags=["search"])
    def search(
        q: str = Query(
            ...,
            min_length=1,
            max_length=256,
            description="Free text: every word must match, the last word "
            "matches as a prefix. Punctuation-only queries match nothing.",
        ),
        limit: int = Query(20, ge=0, le=_API_LIMIT_MAX),
        offset: int = Query(0, ge=0),
        include_duplicates: bool = Query(
            False,
            description="By default one hit per tender notice (duplicate "
            "rows collapse to their primary listing). Set true to include "
            "every duplicate row.",
        ),
    ) -> SearchPage:
        """Full-text search (FTS5 bm25-ranked; lower rank = better).

        Postgres-backed deployments (``DATABASE_URL``) have no FTS5;
        the route falls back to :meth:`PostgresStorage.search` with the
        same strict query contract (every word must match, the last
        word matches as a prefix).
        """
        primary_only = not include_duplicates
        storage = _get_storage()
        if isinstance(storage, PostgresStorage):
            with data_lock:
                total = storage.search_count(q, primary_only=primary_only)
                results = storage.search(
                    q, limit=limit, offset=offset, primary_only=primary_only
                )
            return SearchPage(
                query=q,
                total=total,
                limit=limit,
                offset=offset,
                results=[SearchHitOut(**vars(hit)) for hit in results],
            )
        index = _get_index()
        try:
            with data_lock:
                results = index.search(
                    q, limit=limit, offset=offset, primary_only=primary_only
                )
                total = index.count(q, primary_only=primary_only)
        except ValueError as exc:
            # search() raises ValueError only for negative paging args,
            # which the ge=0 query constraints already prevent; kept as
            # a safety net so a programming slip never becomes a 500.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return SearchPage(
            query=q,
            total=total,
            limit=limit,
            offset=offset,
            results=[SearchHitOut(**vars(hit)) for hit in results],
        )

    @app.get("/notifications", response_model=NotificationsOut, tags=["notifications"])
    def notifications(
        window_days: int = Query(
            7,
            ge=1,
            le=90,
            description="Look-back/look-ahead window in days. The UI calls "
            "this for 1/3/7/30-day buckets and picks its own thresholds.",
        ),
        limit: int = Query(50, ge=1, le=_API_LIMIT_MAX),
    ) -> NotificationsOut:
        """"New tenders" and "closing soon" buckets.

        ``new_tenders`` are primary rows first published within the
        window (newest first); ``closing_soon`` are primary rows whose
        deadline falls within the window (soonest first). The UI uses
        these for the notifications page and the browser Notification
        API. Only primary rows appear - duplicates are never notified.
        """
        with data_lock:
            storage = _get_storage()
            new_rows = storage.list_new_tenders(window_days, limit=limit)
            closing = storage.list_closing_soon(window_days, limit=limit)
        now_iso = datetime.now(timezone.utc).isoformat()
        return NotificationsOut(
            generated_at=now_iso,
            window_days=window_days,
            new_tenders=[OpportunityOut(**row) for row in new_rows],
            new_total=len(new_rows),
            closing_soon=[OpportunityOut(**row) for row in closing],
            closing_total=len(closing),
        )

    @app.post("/ai/analyze/{opportunity_id}", tags=["ai"])
    def ai_analyze(
        opportunity_id: int = FastAPIPath(..., ge=1),
    ) -> dict[str, Any]:
        """Analyze one tender: deterministic classification, plus an LLM
        prose summary when a key is configured.

        Unlike ``GET /opportunities/{id}/analysis`` this never 404s on
        an unrun pipeline - the deterministic rules run on the spot.
        LLM failures degrade gracefully to the deterministic answer.
        """
        with data_lock:
            storage = _get_storage()
            row = storage.get_opportunity(opportunity_id)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"opportunity {opportunity_id} not found",
                )
            analysis = analyze(row)
            llm_on = ai_endpoints.llm_available()
            suggestion = None
            if llm_on:
                try:
                    suggestion = ai_endpoints.analyze_suggestion(row, analysis)
                except Exception as exc:  # noqa: BLE001 - degrade, never 500
                    logger.warning("LLM analyze failed for %s: %s", opportunity_id, exc)
        if suggestion is not None:
            summary = suggestion["summary"]
            fit_hints = suggestion["fit_hints"]
            red_flags = suggestion["red_flags"]
            provider = "llm"
            model = (ai_endpoints.llm_settings() or {}).get("model")
        else:
            summary = _deterministic_summary(row, analysis)
            fit_hints = []
            red_flags = list(analysis.date_conflicts)
            provider = "deterministic"
            model = "deterministic-v1"
        return {
            "opportunity_id": opportunity_id,
            "llm_available": llm_on,
            "provider": provider,
            "model": model,
            "prompt_version": ai_endpoints.AI_PROMPT_VERSION,
            "category": analysis.category,
            "subcategory": analysis.subcategory,
            "keywords": analysis.keywords,
            "procurement_type": analysis.procurement_type,
            "requirements": analysis.requirements,
            "date_conflicts": analysis.date_conflicts,
            "summary": summary,
            "fit_hints": fit_hints,
            "red_flags": red_flags,
        }

    @app.post("/ai/dedup", tags=["ai"])
    def ai_dedup(body: AiDedupRequest) -> dict[str, Any]:
        """Verify a candidate duplicate group is really one tender.

        With an LLM the model compares the listings and suggests
        canonical fields; without one, a deterministic reference/title
        scorer answers. Unknown ids are skipped; fewer than two known
        listings is a 422.
        """
        with data_lock:
            storage = _get_storage()
            rows = [
                storage.get_opportunity(opportunity_id)
                for opportunity_id in dict.fromkeys(body.opportunity_ids)
            ]
        rows = [row for row in rows if row is not None]
        if len(rows) < 2:
            raise HTTPException(
                status_code=422,
                detail="need at least two known opportunities to compare",
            )
        llm_on = ai_endpoints.llm_available()
        verdict: dict[str, Any]
        if llm_on:
            try:
                verdict = ai_endpoints.dedup_verify(rows)
            except Exception as exc:  # noqa: BLE001 - degrade to the scorer
                logger.warning("LLM dedup failed (%s); using deterministic", exc)
                verdict = ai_endpoints.dedup_verify_deterministic(rows)
        else:
            verdict = ai_endpoints.dedup_verify_deterministic(rows)
        return {
            "llm_available": llm_on,
            "same_tender": verdict["same_tender"],
            "confidence": verdict["confidence"],
            "reason": verdict["reason"],
            "canonical": verdict["canonical"],
            "listings": [
                {
                    "opportunity_id": row["id"],
                    "source_name": row.get("source_name"),
                    "reference_number": row.get("reference_number"),
                    "title": row.get("title"),
                    "organization": row.get("organization"),
                    "deadline": row.get("deadline"),
                    "source_url": row.get("source_url"),
                }
                for row in rows
            ],
        }

    @app.post("/ai/rfp", tags=["ai"])
    def ai_rfp(body: AiRfpRequest) -> dict[str, Any]:
        """Draft a bid/RFP document for one tender.

        This genuinely needs an LLM (a key must be configured); without
        one it answers 503. The result is a draft - the bidder remains
        responsible for the tender's exact terms.
        """
        if not ai_endpoints.llm_available():
            raise HTTPException(
                status_code=503,
                detail=(
                    "RFP drafting needs an LLM API key (OPENAI_API_KEY; "
                    "Grok via OPENAI_BASE_URL=https://api.x.ai/v1)"
                ),
            )
        with data_lock:
            storage = _get_storage()
            row = storage.get_opportunity(body.opportunity_id)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"opportunity {body.opportunity_id} not found",
                )
        try:
            return ai_endpoints.rfp_draft(
                row,
                body.company_name,
                [str(item) for item in body.capabilities],
            )
        except Exception as exc:  # noqa: BLE001 - LLM errors are a 502
            logger.error("LLM RFP draft failed: %s", exc)
            raise HTTPException(
                status_code=502,
                detail=f"LLM request failed: {type(exc).__name__}",
            ) from exc

    return app


#: ASGI application for ``python -m uvicorn api:app --app-dir collector``.
app = create_app()


def _serve() -> None:
    """Run the API server directly: ``python collector/api.py``."""
    import argparse

    parser = argparse.ArgumentParser(description="TenderScout read-only API server")
    parser.add_argument(
        "--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="bind port (default: 8000)"
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "SQLite database path (default: $TENDERSCOUT_DB_PATH or "
            "collector/tenderscout.db)"
        ),
    )
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(db_path=args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    _serve()
