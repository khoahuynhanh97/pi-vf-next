"""ProductResearchService: orchestrates acquisition → evidence → resolution → media.

Composition only; no acquisition/resolution semantics live here. Each domain
service is called directly (never via MCP). A challenged/blocked source is
preserved and never converts research into ERROR on its own.
"""

from __future__ import annotations

import sys
import hashlib


from media.evidence import ProductEvidenceService
from media.evidence.models import ProductEvidence, ProductEvidenceExtractionResult
from media.models import ImageCandidate, ProductIdentityHint
from media.research_persistence import ResearchPersistenceService
from media.resolution import ProductResolver
from media.resolution.ids import host_of
from media.service import MediaIntelligenceService
from media.source_discovery import SourceCandidate, SourceDiscoveryService
from media.source_discovery.classify import classify_source
from media.web_acquisition.models import WebAcquisitionResult

from .models import (
    ProductResearchResult,
    ResearchProductRequest,
    ResearchStatus,
    ResearchSummary,
    SourceResearchResult,
    is_usable_source_result,
)


class ProductResearchService:
    def __init__(self, *, evidence_service: ProductEvidenceService | None = None, resolver: ProductResolver | None = None, media: MediaIntelligenceService | None = None, discovery: SourceDiscoveryService | None = None, persistence: ResearchPersistenceService | None = None):
        self.evidence_service = evidence_service or ProductEvidenceService()
        self.resolver = resolver or ProductResolver()
        self.media = media or MediaIntelligenceService()
        self.discovery = discovery or SourceDiscoveryService()
        self.persistence = persistence  # optional; None disables auto-save

    def research(self, request: ResearchProductRequest) -> ProductResearchResult:
        print("research.start", file=sys.stderr, flush=True)
        if not request.is_usable:
            print("research.complete status=ERROR", file=sys.stderr, flush=True)
            return ProductResearchResult(
                status=ResearchStatus.ERROR,
                request=request,
                error="ResearchProductRequest requires a url, query, or a usable product identity hint",
            )

        hint = request.product_identity_hint
        if hint is None and request.query:
            hint = ProductIdentityHint(name=request.query, keywords=[request.query])
        discovered_sources: list[SourceCandidate] = []
        if request.discover_sources and hint is not None:
            print("source_discovery.start", file=sys.stderr, flush=True)
            discovered_sources = self.discovery.discover(hint)
            print(f"source_discovery.complete count={len(discovered_sources)}", file=sys.stderr, flush=True)
            self._append_discovered_urls(request, discovered_sources)

        urls = self._collect_urls(request)
        source_results: list[SourceResearchResult] = []
        evidence_list: list[ProductEvidence] = []
        evidence_sources = []

        target = self.discovery.config.target_usable_sources
        max_sources = self.discovery.config.max_sources
        attempted = 0
        usable = 0
        stop_reason = "all_sources_researched"
        print(f"source_acquisition.start urls={len(urls)} target={target} max={max_sources}", file=sys.stderr, flush=True)
        for url in urls:
            if attempted >= max_sources:
                stop_reason = "budget_exhausted"
                break
            source, evidence_result = self._acquire_and_extract(url, request.browser_fallback)
            source.usable = is_usable_source_result(source)
            source_results.append(source)
            attempted += 1
            if source.usable:
                usable += 1
                if evidence_result.evidence is not None:
                    evidence_list.append(evidence_result.evidence)
                    evidence_sources.append(evidence_result.source)
                if usable >= target:
                    stop_reason = "target_reached"
                    break
        print(f"source_acquisition.complete attempted={attempted} usable={usable} reason={stop_reason}", file=sys.stderr, flush=True)

        print(f"evidence.start count={len(evidence_list)}", file=sys.stderr, flush=True)
        print("evidence.complete", file=sys.stderr, flush=True)

        print("resolution.start", file=sys.stderr, flush=True)
        resolution = self.resolver.resolve(evidence_list)
        print(f"resolution.complete resolved={len(resolution.canonical_products)}", file=sys.stderr, flush=True)

        # Media research driven by resolved identity (first resolved product).
        media_assets = []
        media_status = "skipped"
        identity = hint
        seed_candidates = []
        if len(resolution.canonical_products) == 1:
            product = resolution.canonical_products[0]
            identity = ProductIdentityHint(
                name=product.name.value if product.name else "",
                brand=product.brand.value if product.brand else None,
                model=product.model.value if product.model else None,
            )
            seen_images = set()
            for evidence in evidence_list:
                if evidence.evidence_id not in product.supporting_evidence_ids:
                    continue
                source = evidence.source
                for image_url in [*evidence.media.page_image_urls, evidence.media.og_image_url]:
                    if not image_url or image_url in seen_images:
                        continue
                    seen_images.add(image_url)
                    seed_candidates.append(ImageCandidate(
                        candidate_id=hashlib.sha256(image_url.encode()).hexdigest(),
                        discovery_provider="product_evidence",
                        query=identity.name,
                        search_result_rank=len(seed_candidates) + 1,
                        image_url=image_url,
                        source_page_url=source.canonical_url or source.final_url or source.requested_url,
                        host=source.host,
                        title=identity.name,
                        discovered_at=source.observed_at,
                    ))
        if identity is not None and identity.is_usable:
            print("media.start", file=sys.stderr, flush=True)
            media_result = self.media.research(identity, target_assets=request.target_assets, seed_candidates=seed_candidates)
            media_assets = media_result.selected_assets
            media_status = media_result.status.value

        summary = self._summarize(request, source_results, evidence_list, resolution, media_assets, media_status, identity, discovered_sources, attempted, usable, stop_reason)
        status = self._overall_status(source_results, evidence_list, resolution)
        result = ProductResearchResult(
            status=status,
            request=request,
            source_results=source_results,
            product_evidence=evidence_list,
            canonical_products=resolution.canonical_products,
            variants=[variant for product in resolution.canonical_products for variant in product.variants],
            listings=[listing for product in resolution.canonical_products for listing in product.listings],
            media_assets=media_assets,
            unresolved_evidence_ids=resolution.unresolved_evidence_ids,
            conflicts=resolution.conflicts,
            warnings=resolution.warnings,
            summary=summary,
            evidence_sources=evidence_sources,
            discovered_sources=discovered_sources,
        )
        if self.persistence is not None:
            print("persistence.start", file=sys.stderr, flush=True)
            snapshot_id, persistence_status = self.persistence.save(result)
            result.snapshot_id = snapshot_id or None
            result.research_id = self.persistence.research_id_for(result)
            result.persistence_status = persistence_status.get("status")
            print(f"persistence.complete status={result.persistence_status} snapshot_id={result.snapshot_id}", file=sys.stderr, flush=True)
        print(f"research.complete status={status.value}", file=sys.stderr, flush=True)
        return result

    # --- internals -----------------------------------------------------------

    def _append_discovered_urls(self, request: ResearchProductRequest, candidates: list[SourceCandidate]) -> None:
        existing = set(self._collect_urls(request))
        for candidate in candidates:
            if candidate.url not in existing:
                request.additional_urls.append(candidate.url)
                existing.add(candidate.url)

    def _collect_urls(self, request: ResearchProductRequest) -> list[str]:
        urls = []
        if request.url:
            urls.append(request.url)
        urls.extend(request.additional_urls)
        seen = set()
        return [url for url in urls if not (url in seen or seen.add(url))]

    def _acquire_and_extract(self, url: str, browser_fallback: bool):
        acquisition: WebAcquisitionResult = self.evidence_service.acquisition.acquire(url, browser_fallback=browser_fallback)
        observation = acquisition.observations[-1]
        evidence_result: ProductEvidenceExtractionResult = self.evidence_service.extractor.extract(observation, acquisition.content)
        host = observation.domain or host_of(url)
        source_class = classify_source(
            SourceCandidate(candidate_id=url, url=url, host=host),
            getattr(self.discovery, "official_domains", set()),
        ).source_class.value
        source = SourceResearchResult(
            url=url,
            source_class=source_class,
            acquisition_status=observation.status.value,
            acquisition_method=observation.method,
            evidence_status=evidence_result.status.value,
            evidence_id=evidence_result.evidence.evidence_id if evidence_result.evidence else None,
            host=host,
            observed_at=observation.observed_at,
            signals=[signal.value for signal in observation.signals],
            reason=observation.reason,
            unresolved=evidence_result.evidence is None,
        )
        return source, evidence_result

    def _summarize(self, request, source_results, evidence_list, resolution, media_assets, media_status, identity, discovered_sources, attempted, usable, stop_reason) -> ResearchSummary:
        variants = sum(len(product.variants) for product in resolution.canonical_products)
        listings = sum(len(product.listings) for product in resolution.canonical_products)
        research_identity = {}
        if identity is not None:
            research_identity = identity.model_dump(exclude_none=True)
        return ResearchSummary(
            sources=len(source_results),
            discovered_sources=len(discovered_sources),
            usable_evidence=len(evidence_list),
            usable_sources=usable,
            attempted_sources=attempted,
            stop_reason=stop_reason,
            unresolved_evidence=len(resolution.unresolved_evidence_ids),
            canonical_products=len(resolution.canonical_products),
            variants=variants,
            listings=listings,
            media_assets=len(media_assets),
            media_status=media_status,
            research_identity=research_identity,
        )

    def _overall_status(self, source_results, evidence_list, resolution) -> ResearchStatus:
        if not evidence_list:
            return ResearchStatus.NO_PRODUCT_EVIDENCE
        if resolution.unresolved_evidence_ids:
            return ResearchStatus.PARTIAL
        if not resolution.canonical_products:
            return ResearchStatus.NO_PRODUCT_EVIDENCE
        return ResearchStatus.COMPLETED
