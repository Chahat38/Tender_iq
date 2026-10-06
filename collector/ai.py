"""AI stage: per-opportunity analysis and per-user relevance matching.

Two layers, both writing the contract tables:

* **Opportunity analysis** (``opportunity_ai``) - category,
  subcategory, keywords, procurement type, requirement snippets and date
  conflicts for one tender. Deterministic by default; when an
  ``OPENAI_API_KEY`` is present an LLM is asked for the same fields as
  structured JSON and every call is journaled in ``analysis_runs``
  (task/provider/model/tokens/duration/status). Any LLM failure falls
  back to the deterministic analysis so the pipeline never stalls.

* **Relevance matching** (``opportunity_matches``) - a 0-100 score per
  (opportunity, user) built from the user's signals
  (``company_capabilities``, ``target_categories``, ``target_regions``)
  with HIGH/MEDIUM/LOW levels and human-readable ``reasons``. The
  scorer is deterministic: same inputs, same score, no API key
  required, which keeps tests honest and production explainable.

Deterministic-first is a deliberate choice (DECISIONS.md): LLM output
can only improve the *analysis* columns; scores the user is judged by
must be reproducible.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from storage_common import utc_now_iso

logger = logging.getLogger(__name__)

#: Bumped whenever prompts or scoring rules change; stored on every row.
PROMPT_VERSION = "2026-10-v1"

TASK_ANALYSIS = "opportunity_analysis"
TASK_MATCH = "relevance_match"

MODEL_DETERMINISTIC = "deterministic-v1"

#: Level boundaries for a 0-100 relevance score.
LEVEL_HIGH = 70
LEVEL_MEDIUM = 40

#: Keyword taxonomy driving the deterministic category classification.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "Health & Medical": (
        "medical", "health", "hospital", "surgical", "pharmaceutical",
        "medicine", "vaccine", "laboratory", "dental", "clinic", "ambulance",
    ),
    "ICT & Telecom": (
        "software", "hardware", "computer", "laptop", "network", "server",
        "internet", "telecom", "system", "data", "digital", "license",
        "cloud", "database", "firewall", "printer", "scanner",
    ),
    "Construction & Civil Works": (
        "construction", "civil", "building", "road", "bridge", "repair",
        "renovation", "infrastructure", "concrete", "structure", "dam",
        "canal", "slope", "blacktop", "survey",
    ),
    "Furniture & Office Supplies": (
        "furniture", "chair", "table", "desk", "stationery", "toner",
        "paper", "cabinet", "sofa", "carpet", "curtain",
    ),
    "Food & Catering": (
        "food", "ration", "catering", "tea", "kitchen", "meal", "flour",
        "sugar", "grocery", "mess", "dining",
    ),
    "Vehicles & Transport": (
        "vehicle", "car", "truck", "bus", "motorcycle", "fuel", "tyre",
        "transport", "logistics", "trailer", "ambulance",
    ),
    "Security & Cleaning": (
        "security", "guard", "cleaning", "cctv", "surveillance",
        "housekeeping", "patrol", "dog", "baton",
    ),
    "Consulting & Services": (
        "consultancy", "consultant", "audit", "feasibility", "training",
        "legal", "translation", "recruitment", "advertising", "event",
        "insurance", "valuation",
    ),
    "Agriculture & Livestock": (
        "agriculture", "seed", "fertilizer", "livestock", "tractor",
        "cattle", "wheat", "irrigation", "poultry", "orchard",
    ),
    "Utilities & Energy": (
        "electricity", "solar", "generator", "diesel", "gas", "water",
        "pump", "transformer", "battery", "ups", "watt", "kilowatt",
        "hvac", "chiller", "air conditioner",
    ),
    "Education & Sports": (
        "school", "college", "university", "education", "sports", "gym",
        "library", "book", "classroom", "scholarship", "laboratory",
    ),
}

STOPWORDS = frozenset(
    """
    a an and are as at be by for from has have in into is it its of on or
    our shall should that the their this to was we were will with you your
    all any not no but if then than there here when which who whom what
    how why where over under about after before during within without
    per via rs pkr pkr. pkr/ pkrrs pkr pkr pkr pkr
    """.split()
)

_PUNCT = re.compile(r"[^a-z0-9]+")

#: Sentence openers that usually mark a requirement/eligibility clause.
_REQUIREMENT_HINTS = re.compile(
    r"\b(must|shall|required|requirement|eligib|qualif|criteria|"
    r"experience|certificate|registration|deadline for|bidder (?:shall|must)|"
    r"firm (?:shall|must))\w*\b",
    re.IGNORECASE,
)

_PROCUREMENT_TYPES = (
    ("eoi", ("expression of interest", "eoi")),
    ("rfp", ("request for proposal", "rfp")),
    ("rfq", ("request for quotation", "rfq")),
    ("pq", ("prequalification", "pre-qualification", "pre qualification")),
    ("single source", ("single source", "direct procurement")),
    ("open tender", ("open tender", "international tender", "national tender")),
    ("tender", ("tender", "bid", "proposal")),
)


@dataclass
class Analysis:
    """The fields stored on ``opportunity_ai`` for one opportunity."""

    category: str | None = None
    subcategory: str | None = None
    keywords: list[str] = field(default_factory=list)
    procurement_type: str | None = None
    requirements: list[str] = field(default_factory=list)
    date_conflicts: list[str] = field(default_factory=list)
    prompt_version: str = PROMPT_VERSION
    provider: str = MODEL_DETERMINISTIC
    model: str = MODEL_DETERMINISTIC


@dataclass
class Match:
    """The fields stored on ``opportunity_matches`` for one pair."""

    relevance_score: int
    level: str
    reasons: list[str]


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------


def tokens(text: str | None) -> list[str]:
    """Lowercase word tokens with punctuation stripped, no stopwords."""
    if not text:
        return []
    return [
        word
        for word in _PUNCT.sub(" ", str(text).lower()).split()
        if word and word not in STOPWORDS and len(word) > 1
    ]


def _sentences(text: str | None) -> list[str]:
    """Split description-ish text into sentence-like chunks."""
    if not text:
        return []
    parts = re.split(r"(?<=[.;\n])\s+|(?<=\n)", str(text))
    return [part.strip() for part in parts if part and part.strip()]


def normalize_procurement_type(value: str | None) -> str | None:
    """Map free text onto the small canonical set of procurement types."""
    if not value:
        return None
    lowered = str(value).lower()
    for canonical, aliases in _PROCUREMENT_TYPES:
        if any(alias in lowered for alias in aliases):
            return canonical
    return str(value).strip() or None


# ---------------------------------------------------------------------------
# opportunity analysis (deterministic core)
# ---------------------------------------------------------------------------


def classify(text: str) -> tuple[str | None, str | None, int]:
    """Score :data:`CATEGORIES` keyword hits against ``text``.

    Returns ``(category, runner_up, best_score)``; ``runner_up`` is only
    set when it reaches 40% of the winning score.
    """
    lowered = text.lower()
    scores = {
        category: sum(lowered.count(keyword) for keyword in keywords)
        for category, keywords in CATEGORIES.items()
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_category, best_score = ranked[0]
    if best_score == 0:
        return None, None, 0
    runner_up, runner_score = ranked[1]
    subcategory = runner_up if runner_score * 100 >= best_score * 40 else None
    return best_category, subcategory, best_score


def date_conflicts(
    deadline: Any,
    published_at: Any,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Human-readable deadline problems for one opportunity."""
    conflicts: list[str] = []
    parsed_deadline = _parse_dt(deadline)
    parsed_published = _parse_dt(published_at)
    now = now or datetime.now(timezone.utc)
    if parsed_deadline is None:
        return conflicts
    if parsed_deadline < now:
        conflicts.append("deadline is already in the past")
    if parsed_published is not None and parsed_deadline < parsed_published:
        conflicts.append("deadline precedes the publication date")
    return conflicts


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def analyze(opportunity: Mapping[str, Any], *, now: datetime | None = None) -> Analysis:
    """Deterministic analysis of one opportunity row/mapping."""
    title = opportunity.get("title") or ""
    description = opportunity.get("description") or ""
    text = f"{title}\n{description}"

    category, subcategory, _ = classify(text)

    word_counts: dict[str, int] = {}
    for token in tokens(text):
        word_counts[token] = word_counts.get(token, 0) + 1
    keywords = sorted(
        word_counts, key=lambda word: (-word_counts[word], -len(word), word)
    )[:12]

    procurement_type = normalize_procurement_type(
        opportunity.get("procurement_type")
    ) or normalize_procurement_type(text)

    requirements = [
        sentence
        for sentence in _sentences(description)
        if _REQUIREMENT_HINTS.search(sentence)
    ][:10]

    return Analysis(
        category=category,
        subcategory=subcategory,
        keywords=keywords,
        procurement_type=procurement_type,
        requirements=requirements,
        date_conflicts=date_conflicts(
            opportunity.get("deadline"), opportunity.get("published_at"), now=now
        ),
    )


# ---------------------------------------------------------------------------
# opportunity analysis (optional LLM pass)
# ---------------------------------------------------------------------------


def llm_settings() -> dict[str, str] | None:
    """LLM connection settings from the environment, or None when unset."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "base_url": os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        ).rstrip("/"),
        "model": os.environ.get("TENDERSCOUT_LLM_MODEL", "gpt-4o-mini"),
    }


ANALYSIS_PROMPT = """You classify public tender notices for a Pakistani
procurement search engine. Reply with ONE JSON object only, using exactly
these keys:
- "category": one of {categories} (or null)
- "subcategory": a narrower label inside the category (or null)
- "keywords": array of 5-10 lowercase topic words
- "procurement_type": one of "open tender", "rfp", "rfq", "pq", "eoi",
  "single source", "tender", or null
- "requirements": array of short sentences copied from the notice that
  state eligibility or submission requirements (max 8)
- "date_conflicts": array of strings describing deadline inconsistencies
  (empty when none)"""


def analyze_with_llm(
    opportunity: Mapping[str, Any],
    *,
    transport: Any = None,
    settings: dict[str, str] | None = None,
) -> Analysis | None:
    """Ask an LLM for the analysis; None means "use the fallback".

    Every call (success or failure) is journaled in ``analysis_runs`` by
    :func:`run_analysis`, which owns storage. ``transport`` must expose
    ``post(url, json=..., headers=..., timeout=...)`` and defaults to
    ``requests`` (injected in tests).
    """
    settings = settings or llm_settings()
    if settings is None:
        return None
    if transport is None:
        import requests as transport  # local import: requests is a dep anyway

    prompt = ANALYSIS_PROMPT.format(categories=", ".join(sorted(CATEGORIES)))
    payload = {
        "model": settings["model"],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "title": opportunity.get("title"),
                        "description": opportunity.get("description"),
                        "organization": opportunity.get("organization"),
                        "published_at": opportunity.get("published_at"),
                        "deadline": opportunity.get("deadline"),
                        "procurement_type": opportunity.get("procurement_type"),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    response = transport.post(
        f"{settings['base_url']}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {settings['api_key']}"},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    data = json.loads(content)
    analysis = Analysis(
        category=data.get("category"),
        subcategory=data.get("subcategory"),
        keywords=list(data.get("keywords") or [])[:12],
        procurement_type=normalize_procurement_type(data.get("procurement_type")),
        requirements=list(data.get("requirements") or [])[:10],
        date_conflicts=list(data.get("date_conflicts") or []),
        provider="openai",
        model=settings["model"],
    )
    # Journal data for run_analysis: it records tokens/duration.
    analysis.model = settings["model"]
    return analysis


def _llm_call(
    opportunity: Mapping[str, Any],
    *,
    transport: Any = None,
    settings: dict[str, str] | None = None,
) -> tuple[Analysis | None, dict[str, Any]]:
    """Run the LLM analysis and return (analysis, run_telemetry)."""
    started = time.monotonic()
    telemetry: dict[str, Any] = {
        "provider": "openai",
        "model": (settings or {}).get("model", "unknown"),
        "tokens": None,
        "status": "ok",
        "error_message": None,
    }
    try:
        analysis = analyze_with_llm(
            opportunity, transport=transport, settings=settings
        )
    except Exception as exc:  # noqa: BLE001 - any LLM failure falls back
        telemetry["status"] = "error"
        telemetry["error_message"] = f"{type(exc).__name__}: {exc}"[:500]
        analysis = None
    finally:
        telemetry["duration_ms"] = int((time.monotonic() - started) * 1000)
    if analysis is None and telemetry["status"] == "ok":
        telemetry["status"] = "skipped"
    return analysis, telemetry


# ---------------------------------------------------------------------------
# relevance matching (deterministic)
# ---------------------------------------------------------------------------


@dataclass
class UserSignals:
    """Everything the scorer needs about one user."""

    user_id: str
    capabilities: list[tuple[str, list[str]]] = field(default_factory=list)
    categories: set[str] = field(default_factory=set)
    regions: set[str] = field(default_factory=set)
    alert_threshold: int = 60


def load_signals(storage: Any, user_id: str) -> UserSignals:
    """Read the user's profile rows into :class:`UserSignals`."""
    raw = storage.get_user_signals(user_id)
    return UserSignals(
        user_id=user_id,
        capabilities=[
            (item["name"], [str(keyword) for keyword in json.loads(item["keywords"])])
            for item in raw["capabilities"]
        ],
        categories=set(raw["categories"]),
        regions=set(raw["regions"]),
        alert_threshold=int(raw["profile"].get("alert_threshold") or 60)
        if raw.get("profile")
        else 60,
    )


def score_match(
    opportunity: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    signals: UserSignals,
) -> Match:
    """Deterministic 0-100 relevance score with reasons.

    Components: capability keyword coverage (0-45), target category hit
    (0-25), target region hit (0-20), source category keyword overlap
    (0-10). Users without any signals score 0 / LOW - the UI explains
    why via ``reasons``.
    """
    reasons: list[str] = []
    score = 0
    haystack = " ".join(
        str(opportunity.get(field_name) or "")
        for field_name in ("title", "description", "organization", "category")
    ).lower()
    if analysis:
        haystack = f"{haystack} {' '.join(_analysis_keywords(analysis))}".lower()

    # 1. capability keywords (0-45)
    all_keywords = [keyword for _, keywords in signals.capabilities for keyword in keywords]
    if all_keywords:
        hits = sorted(
            {
                keyword
                for keyword in all_keywords
                if re.search(rf"\b{re.escape(keyword.lower())}\b", haystack)
            }
        )
        if hits:
            coverage = len(hits) / len(set(k.lower() for k in all_keywords))
            keyword_score = round(10 + 35 * coverage)
            score += min(45, keyword_score)
            reasons.append(f"capability keywords matched: {', '.join(hits[:6])}")

    # 2. target categories (0-25)
    opportunity_category = (
        (analysis or {}).get("category") or opportunity.get("category") or ""
    )
    if signals.categories:
        matched = sorted(
            target
            for target in signals.categories
            if _normalize_label(target) == _normalize_label(opportunity_category)
        )
        if matched:
            score += 25
            reasons.append(f"target category matched: {matched[0]}")

    # 3. target regions (0-20)
    if signals.regions:
        location_text = " ".join(
            str(opportunity.get(name) or "") for name in ("province", "location")
        ).lower()
        matched_regions = sorted(
            region
            for region in signals.regions
            if _normalize_label(region) in location_text
        )
        if matched_regions:
            score += 20
            reasons.append(f"target region matched: {matched_regions[0]}")

    # 4. source category keywords vs taxonomy keywords (0-10)
    if opportunity_category:
        category_words = [
            word for word in _normalize_label(opportunity_category).split()
            if word not in STOPWORDS
        ]
        if category_words and any(word in haystack for word in category_words):
            score += 10
            reasons.append(f"category keywords present: {opportunity_category}")

    score = max(0, min(100, score))
    if score >= LEVEL_HIGH:
        level = "HIGH"
    elif score >= LEVEL_MEDIUM:
        level = "MEDIUM"
    else:
        level = "LOW"
    return Match(relevance_score=score, level=level, reasons=reasons)


def _analysis_keywords(analysis: Mapping[str, Any]) -> list[str]:
    raw = analysis.get("keywords")
    if isinstance(raw, str):
        try:
            return list(json.loads(raw))
        except ValueError:
            return []
    return list(raw or [])


def _normalize_label(value: Any) -> str:
    return _PUNCT.sub(" ", str(value or "").lower()).strip()


# ---------------------------------------------------------------------------
# batch runners (storage-facing)
# ---------------------------------------------------------------------------


def run_analysis(
    storage: Any,
    *,
    limit: int = 500,
    use_llm: bool | None = None,
    transport: Any = None,
) -> dict[str, int]:
    """Analyze every opportunity missing an ``opportunity_ai`` row.

    Returns ``{"analyzed", "llm", "deterministic", "failed"}``. Each
    LLM attempt (including fallbacks) is journaled in ``analysis_runs``.
    """
    if use_llm is None:
        use_llm = llm_settings() is not None
    report = {"analyzed": 0, "llm": 0, "deterministic": 0, "failed": 0}
    for opportunity in storage.list_opportunities_needing_ai(limit):
        started = time.monotonic()
        analysis: Analysis | None = None
        telemetry: dict[str, Any] | None = None
        if use_llm:
            analysis, telemetry = _llm_call(
                opportunity, transport=transport, settings=llm_settings()
            )
        if analysis is None:
            analysis = analyze(opportunity)
            report["deterministic"] += 1
        else:
            report["llm"] += 1
        storage.upsert_opportunity_ai(
            int(opportunity["id"]),
            category=analysis.category,
            subcategory=analysis.subcategory,
            keywords=json.dumps(analysis.keywords),
            procurement_type=analysis.procurement_type,
            requirements=json.dumps(analysis.requirements),
            date_conflicts=json.dumps(analysis.date_conflicts),
            prompt_version=PROMPT_VERSION,
            created_at=utc_now_iso(),
        )
        if telemetry is not None:
            if telemetry["status"] != "ok":
                report["failed"] += 1
            storage.insert_analysis_run(
                TASK_ANALYSIS,
                provider=telemetry["provider"],
                model=telemetry["model"],
                prompt_version=PROMPT_VERSION,
                duration_ms=telemetry["duration_ms"],
                tokens=telemetry["tokens"],
                status=telemetry["status"],
                error_message=telemetry["error_message"],
                opportunity_id=int(opportunity["id"]),
                created_at=utc_now_iso(),
            )
        elif analysis.provider == MODEL_DETERMINISTIC:
            storage.insert_analysis_run(
                TASK_ANALYSIS,
                provider=MODEL_DETERMINISTIC,
                model=MODEL_DETERMINISTIC,
                prompt_version=PROMPT_VERSION,
                duration_ms=int((time.monotonic() - started) * 1000),
                tokens=0,
                status="ok",
                opportunity_id=int(opportunity["id"]),
                created_at=utc_now_iso(),
            )
        report["analyzed"] += 1
    return report


def run_matching(
    storage: Any,
    user_id: str,
    *,
    limit: int = 500,
) -> dict[str, int]:
    """Score (and re-score) matches for one user.

    A row is re-scored when it has no match yet or the opportunity
    changed after the match was written. Returns
    ``{"scored", "high", "medium", "low", "analyzed"}``.
    """
    signals = load_signals(storage, user_id)
    report = {"scored": 0, "high": 0, "medium": 0, "low": 0, "analyzed": 0}
    started = time.monotonic()
    for row in storage.list_opportunities_for_matching(user_id, limit):
        opportunity = storage.get_opportunity(int(row["id"]))
        if opportunity is None:
            continue
        analysis_row = storage.get_opportunity_ai(int(row["id"]))
        if analysis_row is None:
            analysis = analyze(opportunity)
            storage.upsert_opportunity_ai(
                int(row["id"]),
                category=analysis.category,
                subcategory=analysis.subcategory,
                keywords=json.dumps(analysis.keywords),
                procurement_type=analysis.procurement_type,
                requirements=json.dumps(analysis.requirements),
                date_conflicts=json.dumps(analysis.date_conflicts),
                prompt_version=PROMPT_VERSION,
                created_at=utc_now_iso(),
            )
            analysis_row = analysis.__dict__
            report["analyzed"] += 1
        match = score_match(opportunity, analysis_row, signals)
        now = utc_now_iso()
        storage.upsert_opportunity_match(
            int(row["id"]),
            user_id,
            relevance_score=match.relevance_score,
            level=match.level,
            reasons=json.dumps(match.reasons),
            prompt_version=PROMPT_VERSION,
            model=MODEL_DETERMINISTIC,
            created_at=now,
            updated_at=now,
        )
        report["scored"] += 1
        report[match.level.lower()] += 1
    storage.insert_analysis_run(
        TASK_MATCH,
        provider=MODEL_DETERMINISTIC,
        model=MODEL_DETERMINISTIC,
        prompt_version=PROMPT_VERSION,
        duration_ms=int((time.monotonic() - started) * 1000),
        tokens=0,
        status="ok",
        user_id=user_id,
        created_at=utc_now_iso(),
    )
    return report


def score_threshold_level(score: int) -> str:
    """Map a score onto its level (shared with API/alerts code)."""
    if score >= LEVEL_HIGH:
        return "HIGH"
    if score >= LEVEL_MEDIUM:
        return "MEDIUM"
    return "LOW"


def list_matches(
    storage: Any, user_id: str, *, min_score: int = 0, limit: int = 50
) -> Sequence[Mapping[str, Any]]:
    """Best matches first for one user (thin wrapper for API/alerts)."""
    return storage.list_matches(user_id, min_score=min_score, limit=limit)
