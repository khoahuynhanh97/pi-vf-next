"""Source discovery contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel


class SourceClass(StrEnum):
    OFFICIAL = "official"
    MARKETPLACE = "marketplace"
    RETAILER = "retailer"
    REVIEW = "review"
    UNKNOWN = "unknown"


class SourceCandidate(BaseModel):
    candidate_id: str
    url: str
    title: str = ""
    snippet: str = ""
    host: str = ""
    query: str = ""
    discovery_provider: str = ""
    rank: int = 0
    source_class: SourceClass = SourceClass.UNKNOWN
    relevance: str = "unknown"
    relevance_reason: str = ""
    discovered_at: str = ""


class SourceDiscoveryQuery(BaseModel):
    query: str
    max_results: int = 15


class SourceDiscoveryProvider(Protocol):
    """Finds candidate source URLs for a product research seed.

    A provider is a discovery mechanism only; search ranking is never treated
    as product or source authority.
    """

    name: str

    def search(self, query: SourceDiscoveryQuery) -> list[SourceCandidate]:
        ...
