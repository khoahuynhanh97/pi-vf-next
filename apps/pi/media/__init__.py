"""Media Intelligence domain contracts.

This package owns product image discovery, acquisition, technical validation,
product matching, classification, deduplication, and selection. It must never
import the MCP SDK; the MCP adapter lives in the application layer instead.
"""

from .models import (
    ImageCandidate,
    ImageSearchQuery,
    MediaAsset,
    MediaConfig,
    MediaResearchResult,
    ProductIdentityHint,
    ProductImageAssessment,
    Rejection,
)
from .service import MediaIntelligenceService

__all__ = [
    "ImageCandidate",
    "ImageSearchQuery",
    "MediaAsset",
    "MediaConfig",
    "MediaIntelligenceService",
    "MediaResearchResult",
    "ProductIdentityHint",
    "ProductImageAssessment",
    "Rejection",
]
