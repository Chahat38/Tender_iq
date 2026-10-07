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
* ``GET /opportunities`` -- newest-first page of opportunities,
* ``POST /opportunities/query`` -- same, with a JSON body for
  tri-state deadline filtering,
* ``GET /opportunities/{opportunity_id}`` -- one opportunity with
  documents and update history,
* ``GET /opportunities/{opportunity_id}/analysis`` -- the AI analysis
  of one opportunity,
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
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Union

from fastapi import FastAPI, HTTPException, Path as FastAPIPath, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

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
    deadline_before: datetime | None = Field(
        None,
        description="Only rows with a deadline strictly before this ISO datetime.",
    )
    deadline_after: Union[Literal[False], datetime, None] = Field(
        None,
        description="false hides rows without a deadline; null keeps them; "
        "a datetime keeps only later deadlines.",
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
            )
            total = storage.count_opportunities(
                source_id=source_id,
                location=location,
                deadline_before=deadline_before,
                deadline_after=deadline_after,
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
                "opportunities": "/opportunities",
                "opportunity_query": "POST /opportunities/query",
                "opportunity": "/opportunities/{opportunity_id}",
                "opportunity_analysis": "/opportunities/{opportunity_id}/analysis",
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
        deadline_before: datetime | None = Query(
            None,
            description="Only rows with a deadline strictly before this datetime.",
        ),
        deadline_after: datetime | None = Query(
            None,
            description="Only rows with a strictly later deadline; rows without "
            "a deadline then drop out (use POST /opportunities/query to keep them).",
        ),
    ) -> OpportunityPage:
        """Newest-first page of opportunities (insertion/crawl order)."""
        return _query_opportunities(
            limit=limit,
            offset=offset,
            source_id=source_id,
            location=location,
            deadline_before=deadline_before,
            deadline_after=(
                deadline_after if deadline_after is not None else NULL_DEADLINE_FILTER
            ),
        )

    @app.get("/locations", response_model=list[str], tags=["opportunities"])
    def list_locations() -> list[str]:
        """Distinct location strings currently in the database (a facet
        for the city filter; values are opaque scraped text)."""
        with data_lock:
            return _get_storage().list_distinct_locations()

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
            deadline_before=body.deadline_before,
            deadline_after=deadline_after,
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
    ) -> SearchPage:
        """Full-text search (FTS5 bm25-ranked; lower rank = better).

        Postgres-backed deployments (``DATABASE_URL``) have no FTS5;
        the route falls back to :meth:`PostgresStorage.search` with the
        same strict query contract (every word must match, the last
        word matches as a prefix).
        """
        storage = _get_storage()
        if isinstance(storage, PostgresStorage):
            with data_lock:
                total = storage.search_count(q)
                results = storage.search(q, limit=limit, offset=offset)
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
                results = index.search(q, limit=limit, offset=offset)
                total = index.count(q)
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
