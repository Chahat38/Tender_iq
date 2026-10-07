"""Optional LLM assistance endpoints (Grok/OpenAI-compatible).

Everything here is *assistance*, never a hard dependency:

* ``POST /ai/analyze`` always answers from the deterministic analysis
  (the same rules that already write ``opportunity_ai``); when an
  OpenAI-compatible key is configured (xAI Grok works via
  ``OPENAI_BASE_URL=https://api.x.ai/v1``) a concise prose summary is
  added on top.
* ``POST /ai/dedup`` verifies that a group of listings are the same
  tender notice. Without an LLM it falls back to token/reference
  comparisons; with one, an LLM makes the call and suggests canonical
  fields.
* ``POST /ai/rfp`` drafts a structured bid/RFP document from an
  opportunity. This one genuinely needs an LLM (503 without it).

The chat transport is ``requests`` (injected in tests) and every LLM
call uses the OpenAI chat-completions wire format with
``response_format=json_object``, matching ``collector/ai.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ai import PROMPT_VERSION, Analysis, llm_settings

logger = logging.getLogger(__name__)

#: Long descriptions are truncated before ever reaching the LLM.
_MAX_LLM_TEXT = 4_000
#: Prompt marker so the LLM can signal "analysis only" when needed.
AI_PROMPT_VERSION = f"ai-assist-v1/{PROMPT_VERSION}"


def llm_available() -> bool:
    """True when an OpenAI-compatible key is configured."""
    return llm_settings() is not None


def _clamp(text: Any, limit: int = _MAX_LLM_TEXT) -> str:
    """Truncate to ``limit`` characters, never mid-word when possible."""
    value = str(text or "")
    if len(value) <= limit:
        return value
    cut = value[:limit]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return cut + " …"


def _chat_json(
    system_prompt: str,
    user_payload: dict[str, Any],
    *,
    transport: Any = None,
    timeout: int = 90,
) -> dict[str, Any]:
    """One OpenAI-compatible chat call demanding a JSON object.

    Raises whatever the HTTP layer raises; callers translate failures
    into 502 responses so a flaky/tired model never 500s the API.
    """
    settings = llm_settings()
    assert settings is not None, "callers must check llm_available()"
    if transport is None:
        import requests as transport  # local import: requests is a dep anyway

    response = transport.post(
        f"{settings['base_url']}/chat/completions",
        json={
            "model": settings["model"],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
        },
        headers={"Authorization": f"Bearer {settings['api_key']}"},
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("LLM did not return a JSON object")
    return data


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

ANALYZE_SYSTEM = """You are TenderScout, an assistant for Pakistani public
procurement. The tender details, the deterministic classification and the
eligibility/requirement sentences are provided. Reply with ONE JSON object
with exactly these keys:
- "summary": 2-4 plain-English sentences describing what is being procured,
  by whom, and (when present) the key dates, written for a supplier deciding
  whether to pursue this tender.
- "fit_hints": array of 3-6 short tips on how a Pakistani vendor could
  qualify (eligibility, documents, fee, registration) - only from the data.
- "red_flags": array of short strings (empty when none) calling out
  deadline/date problems or obviously incomplete notices."""

#: The deterministic analysis result reshaped into an LLM-friendly view.
_ANALYZE_VIEW_KEYS = (
    "title",
    "organization",
    "description",
    "reference_number",
    "published_at",
    "deadline",
    "source_url",
    "category",
    "procurement_type",
    "requirements",
)


def _opportunity_view(opportunity: dict[str, Any]) -> dict[str, Any]:
    """The subset of a row the LLM (and the frontend) should see."""
    return {
        key: opportunity.get(key)
        for key in _ANALYZE_VIEW_KEYS
        if opportunity.get(key) is not None
    }


def analyze_suggestion(
    opportunity: dict[str, Any],
    analysis: Analysis,
    *,
    transport: Any = None,
) -> dict[str, Any]:
    """LLM prose around the deterministic analysis; safe to call hot."""
    view = _opportunity_view(opportunity)
    view["analysis"] = {
        "category": analysis.category,
        "subcategory": analysis.subcategory,
        "keywords": analysis.keywords,
        "procurement_type": analysis.procurement_type,
        "requirements": analysis.requirements,
        "date_conflicts": analysis.date_conflicts,
    }
    data = _chat_json(ANALYZE_SYSTEM, view, transport=transport)
    return {
        "summary": str(data.get("summary") or "").strip(),
        "fit_hints": [str(item) for item in (data.get("fit_hints") or []) if item][:6],
        "red_flags": [str(item) for item in (data.get("red_flags") or []) if item][:6],
    }


# ---------------------------------------------------------------------------
# dedup verification
# ---------------------------------------------------------------------------

DEDUP_SYSTEM = """You verify whether several procurement listings are the
SAME tender notice (published by one source then mirrored or reposted by
others). Answer with ONE JSON object:
- "same_tender": boolean
- "confidence": number 0-1
- "reason": one short sentence in English explaining the decision from the
  data (same reference number, same title/org with trivial rewordings, or
  genuinely different tenders).
- "canonical": object with the best values to keep:
  {"title", "organization", "deadline", "source_url"}; "source_url" is the
  most authoritative (government domain preferred), others are the most
  complete/clean values. Strings can be empty when not known."""


def dedup_verify(
    listings: list[dict[str, Any]],
    *,
    transport: Any = None,
) -> dict[str, Any]:
    """LLM verdict for one candidate duplicate group."""
    payload = {
        "listings": [
            {
                "source": item.get("source_name") or f"source {item.get('source_id')}",
                "reference_number": item.get("reference_number"),
                "title": _clamp(item.get("title"), 600),
                "organization": _clamp(item.get("organization"), 300),
                "deadline": item.get("deadline"),
                "source_url": item.get("source_url"),
            }
            for item in listings
        ]
    }
    data = _chat_json(DEDUP_SYSTEM, payload, transport=transport)
    canonical = data.get("canonical") or {}
    return {
        "same_tender": bool(data.get("same_tender")),
        "confidence": float(data.get("confidence") or 0.0),
        "reason": str(data.get("reason") or "").strip(),
        "canonical": {
            "title": str(canonical.get("title") or ""),
            "organization": str(canonical.get("organization") or ""),
            "deadline": str(canonical.get("deadline") or ""),
            "source_url": str(canonical.get("source_url") or ""),
        },
    }


def _normalize(value: Any) -> str:
    return " ".join((str(value or "").lower().split()))


def dedup_verify_deterministic(
    listings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rule-based verdict used when no LLM is configured.

    An exact reference-number match across the listings is treated as
    proof; failing that, the strongest pairwise word-token overlap of
    the titles (>= 60% of the shorter title) counts as the same notice.
    """
    titles = [item.get("title") for item in listings if item.get("title")]
    refs = [
        _normalize(item.get("reference_number"))
        for item in listings
        if _normalize(item.get("reference_number"))
    ]
    same_by_ref = len(refs) >= 2 and len({ref for ref in refs}) == 1

    overlap = 0.0
    token_sets = [set(_normalize(value).split()) for value in titles]
    if len(token_sets) >= 2:
        for i in range(len(token_sets)):
            for j in range(i + 1, len(token_sets)):
                short_set, long_set = sorted(
                    (token_sets[i], token_sets[j]), key=len
                )
                if short_set:
                    pair = len(short_set & long_set) / len(short_set)
                    overlap = max(overlap, pair)

    same = same_by_ref or overlap >= 0.6
    if same_by_ref:
        reason = "same reference number across listings"
    elif overlap >= 0.6:
        reason = "titles overlap strongly across listings"
    else:
        reason = "no reference match and titles do not overlap strongly"
    priority = ["https://", "http://"]
    return {
        "same_tender": same,
        "confidence": 0.95 if same_by_ref else 0.78 if overlap >= 0.6 else 0.4,
        "reason": reason,
        "canonical": {
            "title": max((value or "" for value in titles), key=len, default=""),
            "organization": max(
                (item.get("organization") or "" for item in listings),
                key=len,
                default="",
            ),
            "deadline": next(
                (item.get("deadline") for item in listings if item.get("deadline")),
                "",
            ),
            "source_url": next(
                (
                    item.get("source_url")
                    for item in listings
                    if (item.get("source_url") or "").startswith(
                        tuple(priority)
                    )
                ),
                next(
                    (
                        item.get("source_url")
                        for item in listings
                        if item.get("source_url")
                    ),
                    "",
                ),
            ),
        },
    }


# ---------------------------------------------------------------------------
# RFP / bid document draft
# ---------------------------------------------------------------------------

RFP_SYSTEM = """You draft a bid document (RFP submission) for a Pakistani
tender, as an experienced public-procurement consultant. Reply with ONE
JSON object with exactly these keys:
- "document_title"
- "introduction": 1-2 paragraphs framing the bidder's company against the
  tender.
- "eligibility": bullet list (array of short strings) covering the
  documents/credentials usually required in Pakistan (PEC/registration
  where relevant, income tax, sales tax, NTN, audited accounts, bid
  security/earnest money, performance guarantee).
- "technical_approach": array of section drafts (""-prefixed) the bidder
  should flesh out.
- "pricing_note": 1 paragraph on how to quote (taxes, currency, validity,
  conditional discounts).
- "submission_checklist": array of items to attach when submitting.
- "coverage_disclaimer": one sentence noting this is a draft and the bidder
  must follow the exact terms of the tender document.
Use the tender details provided; when a field is unknown say so instead of
inventing figures."""


def rfp_draft(
    opportunity: dict[str, Any],
    company_name: str,
    capabilities: list[str],
    *,
    transport: Any = None,
) -> dict[str, Any]:
    """Structured bid-document draft (LLM required)."""
    payload = {
        "company_name": company_name or "the bidding company",
        "capabilities": capabilities,
        "tender": {
            "title": _clamp(opportunity.get("title"), 600),
            "organization": _clamp(opportunity.get("organization"), 300),
            "description": _clamp(opportunity.get("description"), _MAX_LLM_TEXT),
            "reference_number": opportunity.get("reference_number"),
            "procurement_type": opportunity.get("procurement_type"),
            "deadline": opportunity.get("deadline"),
            "source_url": opportunity.get("source_url"),
            "location": opportunity.get("location"),
        },
    }
    data = _chat_json(RFP_SYSTEM, payload, transport=transport)
    return {
        "document_title": str(data.get("document_title") or ""),
        "introduction": str(data.get("introduction") or ""),
        "eligibility": [str(i) for i in (data.get("eligibility") or []) if i][:12],
        "technical_approach": [
            str(i) for i in (data.get("technical_approach") or []) if i
        ][:8],
        "pricing_note": str(data.get("pricing_note") or ""),
        "submission_checklist": [
            str(i) for i in (data.get("submission_checklist") or []) if i
        ][:15],
        "coverage_disclaimer": str(data.get("coverage_disclaimer") or ""),
        "provider": "llm",
        "model": llm_settings()["model"] if llm_settings() else None,
    }


__all__ = [
    "AI_PROMPT_VERSION",
    "analyze_suggestion",
    "dedup_verify",
    "dedup_verify_deterministic",
    "llm_available",
    "rfp_draft",
]