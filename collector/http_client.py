"""Polite HTTP fetching: robots.txt checks, rate limiting and retries.

All network access in TenderScout goes through :class:`PoliteClient`.
It respects robots.txt, keeps a configurable delay between requests,
and retries transient failures with exponential backoff.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable
from urllib import robotparser
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


class RobotsError(RuntimeError):
    """Base class for robots.txt related refusals."""


class RobotsDisallowedError(RobotsError):
    """Raised when robots.txt explicitly disallows the requested URL."""


class RobotsUnavailableError(RobotsError):
    """Raised when robots.txt cannot be fetched.

    TenderScout refuses to crawl in this case: if we cannot verify that
    a URL is allowed, we do not fetch it.
    """


class PoliteClient:
    """A polite HTTP GET client for public tender pages.

    Wraps ``requests.Session`` with:

    * a custom User-Agent identifying TenderScout with a contact address
      (from the ``CONTACT_EMAIL`` environment variable),
    * a configurable minimum delay between requests (default 2.5 s),
    * a 20 s request timeout,
    * exponential backoff with up to 3 attempts on HTTP 429, HTTP 5xx
      and timeouts,
    * a robots.txt check before every ``get()``: disallowed URLs are
      refused with :class:`RobotsDisallowedError`, and URLs whose
      robots.txt cannot be fetched are refused with
      :class:`RobotsUnavailableError`.
    """

    DEFAULT_DELAY_SECONDS = 2.5
    DEFAULT_TIMEOUT_SECONDS = 20.0
    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_BACKOFF_BASE_SECONDS = 2.0
    USER_AGENT_TEMPLATE = "TenderScoutBot/0.1 (contact: {contact})"

    def __init__(
        self,
        delay: float = DEFAULT_DELAY_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        contact_email: str | None = None,
        robots_fetcher: Callable[[str], str | None] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        session: requests.Session | None = None,
    ) -> None:
        """Configure the client.

        ``robots_fetcher`` (optional) takes a robots.txt URL and returns
        its text, or ``None`` when it cannot be read; it exists so tests
        can mock robots.txt. ``sleep_func`` replaces ``time.sleep`` in
        tests. ``session`` allows injecting a preconfigured Session.
        """
        contact = contact_email or os.environ.get("CONTACT_EMAIL") or "unset"
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = self.USER_AGENT_TEMPLATE.format(
            contact=contact
        )
        self.delay = delay
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.robots_fetcher = robots_fetcher or self._default_robots_fetcher
        self.sleep_func = sleep_func
        self._last_request_at: float | None = None
        self._robots_cache: dict[str, robotparser.RobotFileParser | None] = {}

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------

    def _default_robots_fetcher(self, robots_url: str) -> str | None:
        """Fetch robots.txt content, or ``None`` when it cannot be read.

        401/403 are treated as "disallow everything" by convention;
        other 4xx codes mean no robots.txt exists, so crawling is
        allowed; 5xx and network errors return ``None`` (refuse).
        """
        self._respect_delay()
        try:
            response = self.session.get(robots_url, timeout=self.timeout)
        except requests.RequestException:
            return None
        if response.status_code == 200:
            return response.text
        if response.status_code in (401, 403):
            return "User-agent: *\nDisallow: /"
        if 400 <= response.status_code < 500:
            return ""
        return None

    def _robot_parser_for(self, url: str) -> robotparser.RobotFileParser | None:
        """Return a cached parser for the URL's origin.

        Returns ``None`` when robots.txt could not be fetched.
        """
        origin = self._origin(url)
        if origin in self._robots_cache:
            return self._robots_cache[origin]
        robots_url = f"{origin}/robots.txt"
        text = self.robots_fetcher(robots_url)
        if text is None:
            self._robots_cache[origin] = None
            return None
        parser = robotparser.RobotFileParser(robots_url)
        parser.parse(text.splitlines())
        self._robots_cache[origin] = parser
        return parser

    @staticmethod
    def _origin(url: str) -> str:
        """Return ``scheme://netloc`` for ``url``."""
        parts = urlparse(url)
        return f"{parts.scheme}://{parts.netloc}"

    def is_allowed(self, url: str) -> bool:
        """Return True only if robots.txt verifiably allows fetching ``url``."""
        parser = self._robot_parser_for(url)
        if parser is None:
            return False
        return parser.can_fetch(self.session.headers["User-Agent"], url)

    def _ensure_allowed(self, url: str) -> None:
        """Raise unless robots.txt verifiably allows fetching ``url``."""
        parser = self._robot_parser_for(url)
        if parser is None:
            raise RobotsUnavailableError(
                f"Could not fetch robots.txt for {self._origin(url)}; "
                f"refusing to fetch {url}"
            )
        if not parser.can_fetch(self.session.headers["User-Agent"], url):
            raise RobotsDisallowedError(
                f"robots.txt for {self._origin(url)} disallows fetching {url}"
            )

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def _respect_delay(self) -> None:
        """Sleep long enough to keep ``delay`` seconds between requests."""
        if self.delay <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            remaining = self.delay - (now - self._last_request_at)
            if remaining > 0:
                self.sleep_func(remaining)
        self._last_request_at = time.monotonic()

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """GET ``url`` politely.

        Refuses the request up front if robots.txt disallows the URL or
        cannot be fetched. Retries with exponential backoff
        (``backoff_base ** attempt`` seconds) on HTTP 429, HTTP 5xx and
        timeouts, up to ``max_attempts`` total attempts. A final 429/5xx
        raises ``requests.HTTPError``; a final timeout re-raises the
        original exception. Other responses (including 4xx) are returned
        as-is.
        """
        self._ensure_allowed(url)
        timeout = kwargs.pop("timeout", self.timeout)
        last_response: requests.Response | None = None
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._respect_delay()
            try:
                response = self.session.get(url, timeout=timeout, **kwargs)
            except requests.exceptions.Timeout as exc:
                last_response, last_error = None, exc
                reason = f"timeout after {timeout}s"
            else:
                last_response, last_error = response, None
                if response.status_code != 429 and response.status_code < 500:
                    return response
                reason = f"HTTP {response.status_code}"
            if attempt < self.max_attempts:
                backoff = self.backoff_base ** (attempt - 1)
                logger.warning(
                    "GET %s failed (%s) on attempt %d/%d; retrying in %.1fs",
                    url,
                    reason,
                    attempt,
                    self.max_attempts,
                    backoff,
                )
                self.sleep_func(backoff)
        if last_response is not None:
            last_response.raise_for_status()
        assert last_error is not None
        raise last_error
