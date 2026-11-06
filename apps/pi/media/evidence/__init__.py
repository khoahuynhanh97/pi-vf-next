"""Product evidence extraction domain.

Transforms an acquired source (SourceObservation + ExtractedPageContent) into a
typed ProductEvidence record. Every value is source evidence with field-level
provenance; this milestone never resolves truth across sources.
"""

from .extractor import ProductEvidenceExtractor
from .models import (
    EvidenceExtractionStatus,
    EvidenceSource,
    EvidenceValue,
    ProductEvidence,
    ProductEvidenceExtractionResult,
)
from .service import ProductEvidenceService

__all__ = [
    "EvidenceExtractionStatus",
    "EvidenceSource",
    "EvidenceValue",
    "ProductEvidence",
    "ProductEvidenceExtractionResult",
    "ProductEvidenceExtractor",
    "ProductEvidenceService",
]
