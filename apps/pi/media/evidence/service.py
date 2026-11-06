"""ProductEvidenceService: composes acquisition + evidence extraction."""

from __future__ import annotations

from media.web_acquisition import AcquisitionService
from media.web_acquisition.models import WebAcquisitionResult

from .extractor import ProductEvidenceExtractor
from .models import ProductEvidenceExtractionResult


class ProductEvidenceService:
    """High-level service: acquire a source, then extract product evidence."""

    def __init__(self, *, acquisition: AcquisitionService | None = None, extractor: ProductEvidenceExtractor | None = None):
        self.acquisition = acquisition or AcquisitionService()
        self.extractor = extractor or ProductEvidenceExtractor()

    def research_url(self, url: str, *, browser_fallback: bool = True) -> tuple[WebAcquisitionResult, ProductEvidenceExtractionResult]:
        acquisition_result = self.acquisition.acquire(url, browser_fallback=browser_fallback)
        observation = acquisition_result.observations[-1]
        evidence_result = self.extractor.extract(observation, acquisition_result.content)
        return acquisition_result, evidence_result
