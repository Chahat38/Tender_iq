"""TenderScout source adapters.

Each module in this package implements exactly one public procurement
source and normalizes into the shared ``NormalizedOpportunity`` schema.
"""

from .base import RunResult, SourceAdapter

__all__ = ["RunResult", "SourceAdapter"]
