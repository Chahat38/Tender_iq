"""The crawl compliance gate: no source runs without passing it.

Gate rules (spec): a source may only run when

    active = true  AND  terms_checked = true  AND
    requires_authentication = false

and ``robots_allowed`` must not be an explicit ``False`` (an unknown
robots.txt is recorded as NULL and left to PoliteClient, which refuses
to fetch when it cannot verify the rules).

Violations raise :class:`ComplianceError` with every reason listed -
the collector refuses loudly instead of quietly skipping or, worse,
crawling something it has no business crawling.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Reasons a source may not run, in report order.
INACTIVE = "inactive (active=false)"
TERMS_UNCHECKED = "terms of use not verified (terms_checked=false)"
REQUIRES_AUTH = "requires authentication - TenderScout never logs in"
ROBOTS_DISALLOWED = "robots.txt disallows crawling"


class ComplianceError(RuntimeError):
    """Raised when a source fails the compliance gate.

    The message always starts with a loud refusal so it is obvious in
    CI logs that this is a policy stop, not a transient error.
    """


def violations(source: Mapping[str, Any]) -> list[str]:
    """Return every reason this source must not be crawled.

    An empty list means the source passes. ``robots_allowed is None``
    (unknown/unfetchable robots.txt) is NOT a violation here:
    :class:`~http_client.PoliteClient` already refuses per-request when
    it cannot verify the rules, and the compliance check records the
    value for review.
    """
    reasons: list[str] = []
    if not source.get("active"):
        reasons.append(INACTIVE)
    if not source.get("terms_checked"):
        reasons.append(TERMS_UNCHECKED)
    if source.get("requires_authentication"):
        reasons.append(REQUIRES_AUTH)
    if source.get("robots_allowed") is False:
        reasons.append(ROBOTS_DISALLOWED)
    return reasons


def passes(source: Mapping[str, Any]) -> bool:
    """True when the source may be crawled."""
    return not violations(source)


def enforce_compliance(source: Mapping[str, Any]) -> None:
    """Raise :class:`ComplianceError` unless the source passes the gate."""
    reasons = violations(source)
    if reasons:
        name = source.get("name", "<unknown source>")
        raise ComplianceError(
            "REFUSING TO CRAWL "
            f"{name!r} - compliance gate failed:\n"
            + "\n".join(f"  * {reason}" for reason in reasons)
            + "\nFix the source row (see scripts/compliance_check.py) or the "
            "registry entry in collector/registry.py; never bypass this gate."
        )


def record_robots_status(
    status: bool | None,
) -> str:
    """Human-readable robots verdict for reports."""
    if status is True:
        return "robots=allow"
    if status is False:
        return "robots=DISALLOW"
    return "robots=unknown"
