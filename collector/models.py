"""Pydantic models shared by every TenderScout source adapter.

Source data is normalized into :class:`NormalizedOpportunity`. Missing
values are always ``None`` -- adapters must never guess, default or
infer data.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

#: Separator used when hashing multi-part content. A control character
#: that cannot appear in ordinary tender text, so joins are unambiguous.
_HASH_SEPARATOR = "\x1f"


def _validate_http_url(value: str) -> str:
    """Raise unless ``value`` is an absolute http(s) URL."""
    if not (value.startswith("https://") or value.startswith("http://")):
        raise ValueError(f"must be an absolute http(s) URL, got: {value!r}")
    return value


class RawListing(BaseModel):
    """A single raw listing exactly as read from a source page.

    ``raw_html`` / ``raw_data`` keep the untouched source payload so the
    original data can always be re-examined without re-crawling.
    """

    external_id: str = Field(min_length=1)
    url: str
    raw_html: str | None = None
    raw_data: dict[str, Any] | None = None

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str) -> str:
        return _validate_http_url(value)


class NormalizedOpportunity(BaseModel):
    """A tender/opportunity normalized to the common adapter schema.

    Every adapter produces instances of this model. Optional content
    fields are ``None`` when the source does not state a value; they are
    never guessed.
    """

    title: str | None = None
    organization: str | None = None
    description: str | None = None
    source_url: str
    source_name: str = Field(min_length=1)
    published_at: datetime | None = None
    deadline: datetime | None = None
    location: str | None = None
    province: str | None = None
    category: str | None = None
    procurement_type: str | None = None
    reference_number: str | None = None
    document_urls: list[str] = Field(default_factory=list)
    external_id: str = Field(min_length=1)

    @field_validator("source_url")
    @classmethod
    def _source_url_is_http(cls, value: str) -> str:
        return _validate_http_url(value)

    def content_hash(self) -> str:
        """Return a stable sha256 fingerprint of the opportunity content.

        The hash covers title, organization, deadline, description and
        document_urls only. ``document_urls`` are sorted first, so the
        hash does not depend on the order links were found on the page.
        Fields that are ``None`` contribute an empty string, and the
        parts are joined with an unambiguous control-character separator.
        """
        parts = [
            self.title or "",
            self.organization or "",
            self.deadline.isoformat() if self.deadline is not None else "",
            self.description or "",
            "\n".join(sorted(self.document_urls)),
        ]
        payload = _HASH_SEPARATOR.join(parts)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
