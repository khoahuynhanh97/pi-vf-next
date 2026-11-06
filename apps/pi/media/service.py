"""Media Intelligence application service orchestration."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .acquisition.storage import extension_for_format
from .classification import ImageClassifier
from .config import MediaConfig
from .dedup import ImageDeduplicator
from .discovery import DuckDuckGoImageDiscoveryProvider, ImageDiscoveryProvider
from .matching import DeterministicProductImageMatcher
from .models import (
    ImageCandidate,
    ImageSearchQuery,
    MatchStatus,
    MediaAsset,
    MediaResearchResult,
    ProductIdentityHint,
    ProductImageAssessment,
    Rejection,
    ResearchStatus,
    SourceClass,
    UsagePolicy,
)
from .selection import ProductAssetSelector
from .validation import TechnicalImageValidator


class MediaIntelligenceService:
    """High-level product media research.

    Orchestrates discovery -> acquisition -> technical validation ->
    deduplication -> product match -> classification -> selection.
    """

    def __init__(
        self,
        config: MediaConfig | None = None,
        *,
        provider: ImageDiscoveryProvider | None = None,
        downloader=None,
        store=None,
        validator=None,
        deduplicator=None,
        matcher=None,
        classifier=None,
        selector=None,
    ):
        self.config = config or MediaConfig()
        self.provider = provider or DuckDuckGoImageDiscoveryProvider(max_results=self.config.max_search_results)
        if downloader is None:
            from .acquisition import ImageDownloader

            downloader = ImageDownloader(max_bytes=self.config.max_image_bytes, timeout_seconds=self.config.download_timeout_seconds)
        self.downloader = downloader
        if store is None:
            from .acquisition import ContentAddressedImageStore

            store = ContentAddressedImageStore(self.config)
        self.store = store
        self.validator = validator or TechnicalImageValidator(self.config)
        self.deduplicator = deduplicator or ImageDeduplicator(self.config.phash_duplicate_distance)
        self.matcher = matcher or DeterministicProductImageMatcher(self.config.product_match_review_threshold)
        self.classifier = classifier or ImageClassifier()
        self.selector = selector or ProductAssetSelector(self.config.target_selected_assets)
        self.official_domains = self._load_official_domains()

    def _load_official_domains(self) -> set[str]:
        path = self.config.official_domains_path or (Path(__file__).parent.parent / "product_scout" / "official_domains.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            domains = [item for values in data.values() for item in values]
        except (OSError, ValueError):
            domains = []
        return {str(item).casefold().removeprefix("www.") for item in domains}

    def research(self, hint: ProductIdentityHint, *, target_assets: int | None = None, minimum_short_edge: int | None = None, seed_candidates: list[ImageCandidate] | None = None) -> MediaResearchResult:
        import time as _time

        _t0 = _time.monotonic()
        print("media.start", file=sys.stderr, flush=True)
        config = self.config.model_copy(deep=True)
        if target_assets is not None:
            config.target_selected_assets = target_assets
        if minimum_short_edge is not None:
            config.minimum_short_edge = minimum_short_edge
        self.selector.target_assets = config.target_selected_assets
        self.validator.config = config
        if not hint.is_usable:
            return MediaResearchResult(
                status=ResearchStatus.ERROR,
                product=hint,
                error="ProductIdentityHint must provide a name, brand, model, or keyword",
                config=config,
            )

        queries = [] if seed_candidates else self._build_queries(hint, config)
        candidates: list[ImageCandidate] = []
        seen_urls: set[str] = set()
        for candidate in seed_candidates or []:
            if candidate.image_url and candidate.image_url not in seen_urls:
                seen_urls.add(candidate.image_url)
                candidates.append(candidate)
        for query in queries:
            for c in self._discover(query):
                if c.image_url and c.image_url not in seen_urls:
                    seen_urls.add(c.image_url)
                    candidates.append(c)

        # Stably prioritize candidate provenance (official -> marketplace -> review/others)
        candidates.sort(key=lambda c: (self._candidate_priority(c), c.search_result_rank or 999))
        discovered_count = len(candidates)
        print(f"media.discovery.complete count={discovered_count} elapsed={_time.monotonic() - _t0:.1f}s", file=sys.stderr, flush=True)
        if not candidates:
            return MediaResearchResult(status=ResearchStatus.NO_USABLE_ASSETS, product=hint, discovered_count=0, rejections=[], config=config)

        rejections: list[Rejection] = []
        downloaded_count = 0
        total_downloaded_bytes = 0
        max_candidates = config.max_candidates_to_download
        max_bytes = config.max_download_bytes_per_product
        validated = []

        for candidate in candidates:
            if downloaded_count >= max_candidates:
                rejections.append(
                    Rejection(
                        candidate_id=candidate.candidate_id,
                        image_url=candidate.image_url,
                        reason="DOWNLOAD_BUDGET_EXCEEDED",
                        detail=f"Exceeded max candidates download limit ({max_candidates})",
                    )
                )
                break

            if total_downloaded_bytes >= max_bytes:
                rejections.append(
                    Rejection(
                        candidate_id=candidate.candidate_id,
                        image_url=candidate.image_url,
                        reason="BYTE_BUDGET_EXCEEDED",
                        detail=f"Exceeded max download byte budget ({max_bytes} bytes)",
                    )
                )
                break

            try:
                print(f"media.acquisition.start candidate={candidate.candidate_id} url={candidate.image_url}", file=sys.stderr, flush=True)
                _status, payload = self.downloader.download(candidate)
                print(f"media.acquisition.complete candidate={candidate.candidate_id} status={_status} elapsed={_time.monotonic() - _t0:.1f}s", file=sys.stderr, flush=True)
            except Exception as exc:  # noqa: BLE001 - per-candidate failure isolation
                reason = getattr(exc, "reason", "IMAGE_HTTP_STATUS")
                print(f"media.acquisition.complete candidate={candidate.candidate_id} error={reason} elapsed={_time.monotonic() - _t0:.1f}s", file=sys.stderr, flush=True)
                rejections.append(Rejection(candidate_id=candidate.candidate_id, image_url=candidate.image_url, reason=reason, detail=getattr(exc, "detail", None)))
                continue

            downloaded_count += 1
            total_downloaded_bytes += len(payload)

            print(f"media.validation.start candidate={candidate.candidate_id}", file=sys.stderr, flush=True)
            result = self.validator.validate(candidate, payload)
            print(f"media.validation.complete candidate={candidate.candidate_id} status={result.status} elapsed={_time.monotonic() - _t0:.1f}s", file=sys.stderr, flush=True)
            if result.status == "rejected":
                rejections.append(Rejection(candidate_id=candidate.candidate_id, image_url=candidate.image_url, reason=result.reason or "IMAGE_REJECTED"))
                continue
            sha256, local_path = self.store.store(payload, extension_for_format(result.format))
            validated.append(_Validated(candidate, result, local_path, sha256))

            # Early stopping when sufficient distinct valid candidates are acquired
            if len({v.sha256 for v in validated}) >= config.target_selected_assets * 2:
                break

        print(f"media.validation.complete count={len(validated)}", file=sys.stderr, flush=True)
        if not validated:
            status = ResearchStatus.NO_USABLE_ASSETS if discovered_count else ResearchStatus.ERROR
            return MediaResearchResult(status=status, product=hint, discovered_count=discovered_count, downloaded_count=downloaded_count, rejections=rejections, config=config)

        # Deduplicate (annotate duplicate_group_id) then materialize MediaAsset.
        self.deduplicator.group(validated)
        assets: list[MediaAsset] = []
        for item in validated:
            assessment = ProductImageAssessment(
                technical_quality=1.0,
                match_status=MatchStatus.UNKNOWN,
                duplicate_group_id=item.duplicate_group_id,
            )
            match = self.matcher.match(item.candidate, hint, title=item.candidate.title or "")
            assessment.product_match_score = match.product_match_score
            assessment.match_status = match.match_status
            view = self.classifier.classify(width=item.result.width or 0, height=item.result.height or 0, url=item.candidate.image_url)
            assessment.view = view
            provenance, _source_class, _usage = self._provenance(item.candidate)
            assets.append(
                MediaAsset(
                    asset_id=item.sha256,
                    local_path=item.local_path,
                    sha256=item.sha256,
                    phash=item.result.phash or "",
                    width=item.result.width or 0,
                    height=item.result.height or 0,
                    format=item.result.format or "",
                    discovery={
                        "provider": item.candidate.discovery_provider,
                        "query": item.candidate.query,
                        "rank": item.candidate.search_result_rank,
                    },
                    source={
                        "image_url": item.candidate.image_url,
                        "page_url": item.candidate.source_page_url,
                        "host": item.candidate.host,
                    },
                    assessment=assessment,
                    provenance=provenance,
                )
            )

        selected, coverage = self.selector.select(assets)
        coverage["queries_executed"] = len(queries)
        coverage["downloaded_bytes"] = total_downloaded_bytes
        print(f"media.selection.complete count={len(selected)} elapsed={_time.monotonic() - _t0:.1f}s", file=sys.stderr, flush=True)
        status = (
            ResearchStatus.COMPLETED
            if coverage["complete"]
            else ResearchStatus.PARTIAL
            if selected
            else ResearchStatus.NO_USABLE_ASSETS
        )
        return MediaResearchResult(
            status=status,
            product=hint,
            discovered_count=discovered_count,
            downloaded_count=downloaded_count,
            valid_count=len(validated),
            selected_assets=selected,
            rejections=rejections,
            coverage=coverage,
            config=config,
        )

    def _discover(self, query: ImageSearchQuery) -> list[ImageCandidate]:
        try:
            return self.provider.search(query)
        except Exception:
            return []

    def _candidate_priority(self, candidate: ImageCandidate) -> int:
        host = (candidate.host or "").casefold().removeprefix("www.")
        if host and (host in self.official_domains or any(host.endswith("." + domain) for domain in self.official_domains)):
            return 0
        if any(token in host for token in ("shopee", "lazada", "tiki", "bachhoaxanh", "cooponline", "vinmart", "winmart", "hasaki", "guardian")):
            return 1
        return 2

    def _build_queries(self, hint: ProductIdentityHint, config: MediaConfig) -> list[ImageSearchQuery]:
        max_q = max(1, config.max_queries_per_identity)
        query_texts: list[str] = []

        brand_clean = (hint.brand or "").strip()
        model_clean = (hint.model or "").strip()
        name_clean = (hint.name or "").strip()

        # Clean trailing store / review noise suffixes
        noise_re = re.compile(
            r"[-|/]\s*(?:Chính hãng|Giá tốt|Shopee|Lazada|TikTok|Freeship|Sale \d+%?|Giảm \d+%?|Combo|Voucher|"
            r"Shop\s+.*|Chuỗi siêu thị.*|Nhà sách.*|Try and Review.*|Websosanh.*|Bách hóa.*|Foodland.*)$",
            re.IGNORECASE,
        )
        model_clean = noise_re.sub("", model_clean).strip(" -:,|/()")
        name_clean = noise_re.sub("", name_clean).strip(" -:,|/()")

        # 1. Exact SKU / canonical identity query
        parts = []
        if brand_clean:
            parts.append(brand_clean)
        if model_clean:
            if brand_clean and brand_clean.casefold() in model_clean.casefold():
                parts = [model_clean]
            else:
                parts.append(model_clean)
        elif name_clean:
            if brand_clean and brand_clean.casefold() in name_clean.casefold():
                parts = [name_clean]
            else:
                parts.append(name_clean)

        if hint.flavor and hint.flavor.lower() not in " ".join(parts).lower():
            parts.append(hint.flavor)
        if hint.spec and hint.spec.lower() not in " ".join(parts).lower():
            parts.append(hint.spec)
        q1 = " ".join(parts).strip() or name_clean
        if q1:
            query_texts.append(q1)

        # 2. Attribute / packaging front query (only when identity has substance)
        is_substantial = bool(brand_clean or len(q1.split()) >= 2)
        if is_substantial:
            is_food = (hint.category == "food") or bool(hint.flavor) or any(
                w in (hint.name or "").lower() for w in ("bánh", "kẹo", "snack", "nước", "sữa")
            )
            base = q1
            flavor_part = f" {hint.flavor}" if hint.flavor and hint.flavor.lower() not in base.lower() else ""

            if is_food:
                q2 = f"{base}{flavor_part} bao bì gói".strip()
                q3 = f"{base}{flavor_part} mặt sau dinh dưỡng".strip()
            else:
                q2 = f"{base} chính hãng mặt trước".strip()
                q3 = f"{base} chi tiết thực tế".strip()

            if q2 and q2 not in query_texts:
                query_texts.append(q2)
            if q3 and q3 not in query_texts:
                query_texts.append(q3)

        if not query_texts:
            query_texts = [self._build_query(hint, config).query]

        selected_texts = query_texts[:max_q]
        return [
            ImageSearchQuery(
                query=qt,
                initial_results=config.initial_search_results,
                max_results=config.max_search_results,
            )
            for qt in selected_texts
        ]

    @staticmethod
    def _build_query(hint: ProductIdentityHint, config: MediaConfig) -> ImageSearchQuery:
        parts = [part for part in (hint.brand, hint.model, hint.name) if part]
        parts.extend(hint.keywords[:2])
        keyword = " ".join(parts).strip() or hint.name
        return ImageSearchQuery(query=keyword, initial_results=config.initial_search_results, max_results=config.max_search_results)

    def _provenance(self, candidate: ImageCandidate) -> tuple[dict, str, str]:
        host = (candidate.host or "").casefold().removeprefix("www.")
        if host and (host in self.official_domains or any(host.endswith("." + domain) for domain in self.official_domains)):
            source_class = SourceClass.OFFICIAL.value
        elif any(token in host for token in ("shopee", "usercontent", "shop.", "lazada", "tikicdn", "lazcdn", "amazon")):
            source_class = SourceClass.MARKETPLACE.value
        elif any(token in host for token in ("review", "blog", "forum", "vnreview")):
            source_class = SourceClass.REVIEW.value
        else:
            source_class = SourceClass.UNKNOWN.value
        # Web-discovered third-party images default to reference_only unless
        # explicit rights evidence exists (none is collected in this milestone).
        return {"source_class": source_class, "usage_policy": UsagePolicy.REFERENCE_ONLY.value}, source_class, UsagePolicy.REFERENCE_ONLY.value


class _Validated:
    __slots__ = ("candidate", "duplicate_group_id", "local_path", "result", "sha256")

    def __init__(self, candidate, result, local_path: str, sha256: str):
        self.candidate = candidate
        self.result = result
        self.local_path = local_path
        self.sha256 = sha256
        self.duplicate_group_id: str | None = None
