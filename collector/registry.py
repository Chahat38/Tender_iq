"""Version-controlled registry of crawlable sources and their
compliance facts.

The registry is the reviewable record of *why* a source may be crawled:
whether a human verified its terms, whether it needs authentication and
how often it should be polled. :func:`register_source` writes those
facts onto the source row before every run, and the compliance gate
(:mod:`compliance`) refuses to run a source whose row does not say
``active AND terms_checked AND NOT requires_authentication``.

Adding a source here is a deliberate, reviewable act - a source that is
not registered (or is registered with ``terms_checked=False``) cannot be
collected, which is exactly the "fail loudly" behaviour the spec asks
for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from storage import Storage


@dataclass(frozen=True)
class SourceSpec:
    """One registered source and its compliance facts."""

    key: str
    source_name: str
    base_url: str
    adapter_type: str
    active: bool = True
    #: True only after a human checked the site's terms/robots stance
    #: (see docs/SOURCES.md notes in DECISIONS.md).
    terms_checked: bool = False
    requires_authentication: bool = False
    crawl_frequency: str = "every 6h"


#: Keys match ``main.ADAPTERS``. Facts are entered from the research in
#: DECISIONS.md; ``terms_checked`` stays False until the source has been
#: researched and accepted (a False value blocks collection loudly).
SOURCE_REGISTRY: dict[str, SourceSpec] = {
    "ppra_epms": SourceSpec(
        key="ppra_epms",
        source_name="PPRA EPMS",
        base_url="https://epms.ppra.gov.pk",
        adapter_type="PPRAEpmsAdapter",
        active=True,
        # Research 2026-10: public /public/... pages, no login/CAPTCHA;
        # epms.ppra.gov.pk has no robots.txt (404 -> no restrictions
        # declared) and ppra.gov.pk/robots.txt is allow-all. No terms
        # forbidding automated access found.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="every 6h",
    ),
}


def spec_for(adapter_key: str) -> SourceSpec | None:
    """Return the registry spec for an adapter key, or None."""
    return SOURCE_REGISTRY.get(adapter_key)


def register_source(storage: Storage, spec: SourceSpec) -> int:
    """Insert or refresh the source row for a registry entry.

    The registry owns the compliance columns (``active``,
    ``terms_checked``, ``requires_authentication``, ``base_url``,
    ``crawl_frequency``); runtime columns (``last_crawled_at``,
    ``last_status``, ``consecutive_failures``, ``robots_allowed``) are
    never touched here - they belong to the runs themselves.
    """
    return storage.register_source(
        name=spec.source_name,
        adapter_type=spec.adapter_type,
        base_url=spec.base_url,
        active=spec.active,
        terms_checked=spec.terms_checked,
        requires_authentication=spec.requires_authentication,
        crawl_frequency=spec.crawl_frequency,
    )


def source_flags(row: dict[str, Any]) -> dict[str, bool]:
    """Normalise a source row's flag columns to plain booleans."""
    return {
        "active": bool(row.get("active")),
        "terms_checked": bool(row.get("terms_checked")),
        "requires_authentication": bool(row.get("requires_authentication")),
        "robots_allowed": row.get("robots_allowed"),
    }
