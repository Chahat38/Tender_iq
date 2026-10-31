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
    "kppra_active_tenders": SourceSpec(
        key="kppra_active_tenders",
        source_name="KPPRA Active Tenders",
        base_url="http://kppra.gov.pk",
        adapter_type="KppraActiveTendersAdapter",
        active=True,
        # Research 2026-10-06: public server-rendered listing at
        # /kppra/activetenders.php, no login; robots.txt absent (404 ->
        # no restrictions declared); no terms found forbidding polite
        # automated access; per-tender search URL verified by hand.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="every 6h",
    ),
    "epads": SourceSpec(
        key="epads",
        source_name="EPADS",
        base_url="https://epads.gov.pk",
        adapter_type="EpadsAdapter",
        active=True,
        # Research 2026-10-06: public open-opportunities listing
        # (server-rendered, no JS wall); robots.txt absent (404 -> no
        # restrictions declared); site is the PPRA-run national portal
        # with public pages and no stated anti-automation terms.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="every 6h",
    ),
    "ajk_ppra": SourceSpec(
        key="ajk_ppra",
        source_name="AJK PPRA",
        base_url="https://www.ajkppra.gov.pk",
        adapter_type="AjkPpraAdapter",
        active=True,
        # Research 2026-10-07: public "Latest Advertisements" table at
        # /advertisements.php (server-rendered, no JS wall or login);
        # robots.txt absent (404 -> no restrictions declared); no terms
        # forbidding polite automated access found.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="every 6h",
    ),
    "pia": SourceSpec(
        key="pia",
        source_name="PIA Tenders",
        base_url="https://piac.com.pk",
        adapter_type="PiaAdapter",
        active=True,
        # Research 2026-10: public /corporate/business-with-pia/tenders
        # table, no login/CAPTCHA, no pagination; robots.txt is the
        # stock Joomla file whose rules never touch the tenders path,
        # PoliteClient robots gate passes (verified 2026-10-07).
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="every 6h",
    ),
    "pakrail": SourceSpec(
        key="pakrail",
        source_name="Pakistan Railways",
        base_url="https://www.pakrail.gov.pk",
        adapter_type="PakrailAdapter",
        active=True,
        # Research 2026-10: public /Tenders/index.aspx archive, no
        # login/CAPTCHA; robots.txt 404 -> no restrictions declared,
        # PoliteClient robots gate passes (verified 2026-10-07).
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="every 6h",
    ),
    "gb_ppra": SourceSpec(
        key="gb_ppra",
        source_name="GB PPRA",
        base_url="https://www.gbppra.gov.pk",
        adapter_type="GbPpraAdapter",
        active=True,
        # Research 2026-10-07: public "All Procurements" listing at
        # /viewall (server-rendered cards, no JS wall or login);
        # robots.txt is allow-all (User-agent: * / Disallow:); no terms
        # forbidding polite automated access found.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="every 6h",
    ),
    "sindh_sppra": SourceSpec(
        key="sindh_sppra",
        source_name="SPPRA Sindh",
        base_url="http://e.pprasindh.gov.pk",
        adapter_type="SindhSppraAdapter",
        active=True,
        # Research 2026-10-07: public "Tenders List" table at
        # /tenderlst (server-rendered, no login); robots.txt is a bare
        # "Disallow:\n" = allow-all, fetched and verified by hand; no
        # terms forbidding polite automated access found.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="every 6h",
    ),
    "punjab_eproc": SourceSpec(
        key="punjab_eproc",
        source_name="Punjab eProcurement",
        base_url="https://eproc.punjab.gov.pk",
        adapter_type="PunjabEprocAdapter",
        active=True,
        # Research 2026-10-07: public tender-search grid at
        # /Admin_Tender_Search.aspx linked from ppra.punjab.gov.pk; it
        # is a plain browser GET (stateful POST pager), no login or CAPTCHA;
        # robots.txt is a 404 -> no restrictions declared. No terms
        # forbidding polite automated access were found. NOTE: the
        # host's TLS fetch is flaky for non-browser clients; the adapter
        # falls back to Google Chrome headless so the polite crawler's
        # bot UA and rate limits still apply to a rendered fetch.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="every 6h",
    ),
    "ptcl": SourceSpec(
        key="ptcl",
        source_name="PTCL Tenders",
        base_url="https://ptcl.com.pk",
        adapter_type="PtclAdapter",
        active=True,
        # Research 2026-10-25: public /Tenders accordion, no login or
        # CAPTCHA; robots.txt is allow-all for the tenders path; detail
        # pages expose the direct PDF. No anti-automation terms found.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="daily",
    ),
    "ndma": SourceSpec(
        key="ndma",
        source_name="NDMA Tenders",
        base_url="https://ndma.gov.pk",
        adapter_type="NdmaAdapter",
        active=True,
        # Research 2026-10-25: public /tenders cards and ?page=N pager;
        # robots.txt allows everything except /admin and /api; no login
        # or CAPTCHA; links are direct PDFs. No anti-automation terms
        # found.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="daily",
    ),
    "world_bank": SourceSpec(
        key="world_bank",
        source_name="World Bank Procurement Notices",
        base_url="https://projects.worldbank.org",
        adapter_type="WorldBankAdapter",
        active=True,
        # Research 2026-10-25: official public JSON API at
        # search.worldbank.org/api/v2/procnotices; search host robots.txt
        # is 404 (no declared rules), projects host is allow-all outside
        # internal paths; no login or key. Contract awards are excluded.
        terms_checked=True,
        requires_authentication=False,
        crawl_frequency="daily",
    ),
}


@dataclass(frozen=True)
class CoverageSpec:
    """One requested source in the public coverage matrix.

    ``source_name`` is set for entries that map to a registered
    (collectable) source.  Blocked/login-only entries have no source row
    and therefore never appear as a zero-result source filter.
    """

    key: str
    requested_name: str
    source_name: str | None
    status: str
    url: str | None
    note: str


#: Status vocabulary used by ``/coverage`` and the Sources page.
COVERAGE_STATUSES = ("Active", "Login Required", "Restricted", "Failed", "Not Tested")

#: The user's requested list, kept separate from SOURCE_REGISTRY because
#: blocked/login-only entries must not be scheduled or pass the compliance
#: gate.  ``Active`` entries point at the actual registered source row.
SOURCE_CATALOG: tuple[CoverageSpec, ...] = (
    CoverageSpec(
        key="zong",
        requested_name="Zong",
        source_name=None,
        status="Login Required",
        url="https://www.zong.com.pk/procurement",
        note="Public page is informational; Zong uses a vendor registration "
        "portal and no public tender listing was found.",
    ),
    CoverageSpec(
        key="jazz",
        requested_name="Jazz",
        source_name=None,
        status="Login Required",
        url="https://s2pservices.jazz.com.pk/",
        note="Jazz uses a vendor portal (login); no public tender listing was "
        "found on jazz.com.pk.",
    ),
    CoverageSpec(
        key="ptcl",
        requested_name="PTCL",
        source_name="PTCL Tenders",
        status="Active",
        url="https://ptcl.com.pk/Tenders",
        note="Public tender notices with direct PDFs.",
    ),
    CoverageSpec(
        key="ndc",
        requested_name="NDC",
        source_name=None,
        status="Not Tested",
        url=None,
        note="Identified as National Defence Complex (NDC). No verified public "
        "procurement domain/page was found; defence notices appear through "
        "DGDP/MoDP channels instead.",
    ),
    CoverageSpec(
        key="c4i",
        requested_name="C4I",
        source_name=None,
        status="Not Tested",
        url=None,
        note="C4I directorate/organization could not be tied to a verified "
        "public procurement domain. No crawlable page was found.",
    ),
    CoverageSpec(
        key="pof",
        requested_name="POF",
        source_name=None,
        status="Restricted",
        url="https://pof.gov.pk/",
        note="Every public path returns a Cloudflare 403 challenge. "
        "TenderScout does not bypass bot challenges, so no adapter is "
        "registered.",
    ),
    CoverageSpec(
        key="ndma",
        requested_name="NDMA",
        source_name="NDMA Tenders",
        status="Active",
        url="https://ndma.gov.pk/tenders",
        note="Public tender cards and direct PDFs; evaluation reports are "
        "skipped.",
    ),
    CoverageSpec(
        key="world_bank",
        requested_name="World Bank",
        source_name="World Bank Procurement Notices",
        status="Active",
        url="https://projects.worldbank.org/en/projects-operations/procurement",
        note="Official public JSON API; contract awards and expired notices "
        "are excluded.",
    ),
    CoverageSpec(
        key="undp",
        requested_name="UNDP Pakistan",
        source_name=None,
        status="Restricted",
        url="https://www.undp.org/pakistan/procurement",
        note="Both UNDP notice hosts disallow all user agents (robots "
        "Disallow: /). Manual CSV/HTML import is the approved route.",
    ),
    CoverageSpec(
        key="kppra",
        requested_name="KPPRA",
        source_name="KPPRA Active Tenders",
        status="Active",
        url="http://kppra.gov.pk/kppra/activetenders.php",
        note="Already integrated.",
    ),
    CoverageSpec(
        key="ppppra",
        requested_name="PPPPRA",
        source_name="Punjab eProcurement",
        status="Active",
        url="https://eproc.punjab.gov.pk",
        note="The requested 'PPPPRA' appears to mean Punjab PPRA; its public "
        "notices are collected through Punjab eProcurement.",
    ),
    CoverageSpec(
        key="federal_ppra",
        requested_name="Federal PPRA",
        source_name="PPRA EPMS",
        status="Active",
        url="https://epms.ppra.gov.pk",
        note="Already integrated through the PPRA EPMS public pages.",
    ),
)


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
