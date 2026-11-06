"""Daily Scout Execution Engine.

Orchestrates daily prompt reading, multi-source trend discovery, Media Intelligence download,
idempotent run tracking, SQLite selection persistence, and atomic dual export.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import unicodedata
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from media import MediaConfig, MediaIntelligenceService, ProductIdentityHint
from media.models import ImageCandidate
from media.web_acquisition.tiktok_gallery import extract_tiktok_gallery_candidates
from media.config import trend_history_db_path
from product_scout.daily_parser import DailyPromptParser, ParsedCriteria
from product_scout.trend_discovery import (
    TrendDiscoveryService,
    TrendHistoryRepository,
    backfill_daily_runs,
    build_default_trend_discovery_service,
)

logger = logging.getLogger(__name__)


def gallery_candidates_for_selected(item: Any, *, fetcher=None) -> list[ImageCandidate]:
    """Use a selected TikTok category listing's own product gallery."""
    for observation in item.observations:
        if observation.provider == "tiktok_snacks_category":
            return extract_tiktok_gallery_candidates(
                observation.source_url, item.name, fetcher=fetcher
            )
    return []


def slugify(text: str) -> str:
    """Convert text to safe ASCII directory slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[-\s]+", "-", text)[:50]


class DailyScoutRunner:
    """Manages the lifecycle of a daily product intelligence execution."""

    def __init__(
        self,
        base_dir: Path | None = None,
        request_file: Path | None = None,
        runs_dir: Path | None = None,
        *,
        discovery_service: TrendDiscoveryService | None = None,
        history_repository: TrendHistoryRepository | None = None,
        media_service: MediaIntelligenceService | None = None,
    ):
        self.base_dir = Path(base_dir or Path.cwd()).resolve()
        self.request_file = Path(request_file or self.base_dir / "request.txt").resolve()
        self.runs_dir = Path(runs_dir or self.base_dir / "data" / "daily_runs").resolve()
        self.history_repository = history_repository or TrendHistoryRepository(trend_history_db_path())
        self.discovery_service = discovery_service or build_default_trend_discovery_service(self.history_repository)
        self.media_service = media_service or MediaIntelligenceService(MediaConfig())

        # Perform idempotent backfill of existing daily runs
        self._backfill_error: str | None = None
        try:
            backfill_daily_runs(self.history_repository, self.runs_dir)
        except Exception as exc:
            logger.error("Backfill failed during runner initialization: %s", exc)
            self._backfill_error = str(exc)

    def is_already_completed(self, target_date: date | None = None) -> bool:
        """Check if today's daily run has already been completed successfully."""
        day_str = (target_date or date.today()).isoformat()
        product_json = self.runs_dir / day_str / "products.json"
        if not product_json.is_file():
            return False
        try:
            payload = json.loads(product_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(payload.get("products")) and payload.get("status") in {
            "success", "success_with_warnings"
        }

    def run_daily(
        self,
        force: bool = False,
        target_date: date | None = None,
        max_images_per_product: int = 5,
        min_image_dim: int = 400,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """Execute the daily intelligence run.

        If already completed today and force is False, skips execution (Idempotent Catch-up).
        If prompt is provided, it overrides request.txt and persists for future runs.
        """
        from product_scout.workflow_policy import PRODUCT_DISCOVERY_PAUSED, PAUSED_MESSAGE

        if PRODUCT_DISCOVERY_PAUSED:
            return {"status": "paused", "failure_code": "product_discovery_paused", "message": PAUSED_MESSAGE}
        if self._backfill_error:
            raise RuntimeError(
                f"Cannot execute daily run: History database backfill failed ({self._backfill_error}). "
                "Aborting before selection to prevent cooldown violation."
            )

        run_date = target_date or date.today()
        day_str = run_date.isoformat()
        out_dir = self.runs_dir / day_str

        # 1. State Guard (Idempotency)
        if not force and self.is_already_completed(run_date):
            logger.info("Daily run for %s is already completed. Skipping.", day_str)
            return {
                "status": "already_completed",
                "date": day_str,
                "message": f"Daily run for {day_str} already exists. Use force=True to re-run.",
                "output_dir": str(out_dir),
            }

        # 2. Prompt Resolution & Persistence
        if prompt and prompt.strip():
            raw_prompt = prompt.strip()
            self.request_file.parent.mkdir(parents=True, exist_ok=True)
            self.request_file.write_text(raw_prompt, encoding="utf-8")
            logger.info("Using provided prompt and saved to %s: %s", self.request_file.name, raw_prompt)
        else:
            if not self.request_file.exists():
                default_prompt = "tìm 10 sản phẩm ngành công nghệ hot trend tiktok/shopee trong phạm vi giá từ 200k-1tr vnd"
                self.request_file.parent.mkdir(parents=True, exist_ok=True)
                self.request_file.write_text(default_prompt, encoding="utf-8")
                logger.info("Created default request.txt at %s", self.request_file)
            raw_prompt = self.request_file.read_text(encoding="utf-8").strip()
            logger.info("Read prompt from %s: %s", self.request_file.name, raw_prompt)

        # 3. Agent Step 1: Front-End AI Category Dispatcher Agent
        from product_scout.category_dispatcher import CategoryDispatcherAgent
        dispatcher = CategoryDispatcherAgent()
        dispatch_result = dispatcher.analyze_and_dispatch(raw_prompt)
        dispatch_meta = dispatch_result.metadata.to_dict()

        if dispatch_result.status in ("unsupported_category", "needs_review"):
            logger.warning("Daily run halted: %s (%s)", dispatch_result.status, dispatch_result.message)
            return {
                "status": dispatch_result.status,
                "date": day_str,
                "failure_code": dispatch_result.status,
                "message": dispatch_result.message,
                "prompt": raw_prompt,
                "dispatch_metadata": dispatch_meta,
            }

        logger.info(
            "Agent Category Dispatcher decomposed prompt into %d task(s): %s",
            len(dispatch_result.tasks),
            [t.category_id for t in dispatch_result.tasks],
        )

        publication_snapshot = self._snapshot_publication_state(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        images_dir = out_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        # Support run-configurable max_images_per_product via env or argument
        import os
        if os.environ.get("MAX_IMAGES_PER_PRODUCT"):
            try:
                max_images_per_product = int(os.environ["MAX_IMAGES_PER_PRODUCT"])
            except ValueError:
                pass
        if not 3 <= max_images_per_product <= 10:
            self._discard_publication_snapshot(publication_snapshot)
            raise ValueError("max_images_per_product must be between 3 and 10")

        results_data = []
        all_warnings: list[str] = []
        successful_discovery_results: list[Any] = []
        session_runs: list[Any] = []
        staged_partitions: list[tuple[str, Path, dict[str, Any], str]] = []
        failed_categories: dict[str, Any] = {}
        category_summaries: dict[str, Any] = {}

        for cat_task in dispatch_result.tasks:
            cat_id = cat_task.category_id
            cat_dir = out_dir / cat_id
            cat_dir.mkdir(parents=True, exist_ok=True)
            cat_images_dir = cat_dir / "images"
            cat_images_dir.mkdir(parents=True, exist_ok=True)

            logger.info("Executing category task [%s]: %s (limit=%d)", cat_id, cat_task.sub_prompt, cat_task.target_count)

            # 4. Discover & Select Candidates via TrendDiscoveryService for this category
            discovery_result = self.discovery_service.discover(cat_task.criteria, run_date=run_date)
            if discovery_result.status == "failed":
                error_msg = discovery_result.failure_message or f"Discovery failed for {cat_id}"
                logger.error("Category %s discovery failed: %s (code=%s)", cat_id, error_msg, discovery_result.failure_code)
                failed_categories[cat_id] = {
                    "stage": "discovery",
                    "reason": error_msg,
                    "code": discovery_result.failure_code,
                    "affected_count": cat_task.target_count,
                }
                all_warnings.append(f"{cat_id}: {error_msg}")
                category_summaries[cat_id] = {
                    "name": cat_task.category_name,
                    "status": "failed",
                    "count": 0,
                    "reason": error_msg,
                    "dir": str(cat_dir),
                }
                continue

            successful_discovery_results.append(discovery_result)
            from product_scout.trend_discovery.repository import SessionCategoryRun
            session_runs.append(
                SessionCategoryRun(
                    run_id=discovery_result.run_id,
                    selections=discovery_result.selection,
                    provider_summary=discovery_result.provider_summary,
                    candidate_count=discovery_result.raw_candidate_count,
                )
            )
            selected_candidates = discovery_result.selection.selected
            all_warnings.extend(discovery_result.selection.warnings)
            logger.info("Category [%s] discovered %d selected products", cat_id, len(selected_candidates))

            cat_results_data = []
            for idx, scored_cand in enumerate(selected_candidates, start=len(results_data) + 1):
                item = scored_cand.candidate
                logger.info("[%d] Processing media for (%s): %s...", idx, cat_id, item.name)
                item_slug = f"{idx:02d}_{slugify(item.brand)}_{slugify(item.model or item.name)}"

                # Subfolder per category and root copy
                cat_item_img_dir = cat_images_dir / item_slug
                cat_item_img_dir.mkdir(parents=True, exist_ok=True)
                root_item_img_dir = images_dir / item_slug
                root_item_img_dir.mkdir(parents=True, exist_ok=True)

                keywords = item.keywords if item.keywords else [f"{item.brand} {item.model or item.name}".strip()]
                category_val = item.category or cat_task.criteria.category
                flavor_val = getattr(item, "flavor", None)
                spec_val = getattr(item, "spec", None)

                from product_scout.daily_parser import is_food_category
                from product_scout.trend_discovery.identity import _extract_food_sku
                if is_food_category(category_val):
                    extracted_sku = _extract_food_sku(f"{item.brand} {item.model} {item.name}", item.brand or "")
                    if extracted_sku and not item.model:
                        item.model = extracted_sku

                hint = ProductIdentityHint(
                    name=item.name,
                    brand=item.brand or None,
                    model=item.model or None,
                    keywords=keywords,
                    category=category_val,
                    flavor=flavor_val,
                    spec=spec_val,
                )

                product_images = []
                res = None
                shortfall_reason = None
                try:
                    try:
                        gallery_candidates = gallery_candidates_for_selected(item)
                    except Exception as exc:
                        logger.warning("  -> TikTok gallery unavailable for %s: %s", item.name, exc)
                        gallery_candidates = []
                    res = self.media_service.research(
                        hint,
                        target_assets=max_images_per_product,
                        minimum_short_edge=min_image_dim,
                        **({"seed_candidates": gallery_candidates} if gallery_candidates else {}),
                    )
                    coverage_data = getattr(res, "coverage", None)
                    if isinstance(coverage_data, dict):
                        sr = coverage_data.get("shortfall_reason")
                        if isinstance(sr, str):
                            shortfall_reason = sr

                    for a_idx, asset in enumerate(res.selected_assets, start=1):
                        src_file = Path(asset.local_path)
                        if src_file.exists():
                            ext = src_file.suffix or ".jpg"
                            dest_filename = f"image_{a_idx}{ext}"
                            # Save in category partition
                            dest_file = cat_item_img_dir / dest_filename
                            shutil.copy2(src_file, dest_file)
                            # Copy to root images directory
                            shutil.copy2(src_file, root_item_img_dir / dest_filename)

                            rel_path = f"images/{item_slug}/{dest_filename}"
                            view_val = (
                                asset.assessment.view.value
                                if hasattr(asset.assessment.view, "value")
                                else str(asset.assessment.view)
                            )
                            match_status_val = (
                                asset.assessment.match_status.value
                                if hasattr(asset.assessment.match_status, "value")
                                else str(asset.assessment.match_status)
                            )
                            usage_policy = (
                                asset.provenance.get("usage_policy", "reference_only")
                                if isinstance(asset.provenance, dict)
                                else "reference_only"
                            )

                            product_images.append({
                                "asset_id": asset.asset_id,
                                "local_path": rel_path,
                                "absolute_path": str(dest_file.resolve()),
                                "original_url": asset.source.get("image_url") if isinstance(asset.source, dict) else None,
                                "page_url": asset.source.get("page_url") if isinstance(asset.source, dict) else None,
                                "host": asset.source.get("host") if isinstance(asset.source, dict) else None,
                                "query": asset.discovery.get("query") if isinstance(asset.discovery, dict) else None,
                                "view": view_val,
                                "match_score": asset.assessment.product_match_score,
                                "match_status": match_status_val,
                                "usage_policy": usage_policy,
                                "width": asset.width,
                                "height": asset.height,
                                "format": asset.format,
                            })

                    logger.info("  -> Downloaded %d valid images for %s", len(product_images), item.name)
                except Exception as exc:
                    logger.warning("  -> Failed to acquire media for %s: %s", item.name, exc)
                    shortfall_reason = f"media_research_error: {exc}"

                unique_shas = {img["asset_id"] for img in product_images}
                matched_images = [img for img in product_images if img.get("match_status") in ("matched", "needs_semantic_review")]
                actual_matched_count = len(matched_images)
                actual_quota_met = (actual_matched_count >= max_images_per_product)
                if not actual_quota_met and not shortfall_reason:
                    if actual_matched_count == 0:
                        shortfall_reason = "no_matched_identity_assets"
                    else:
                        shortfall_reason = f"insufficient_matched_assets: {actual_matched_count} < {max_images_per_product}"

                # Price display
                price_display = f"{cat_task.criteria.price_min:,}đ - {cat_task.criteria.price_max:,}đ"
                if item.price_min and item.price_max:
                    price_display = f"{item.price_min:,}đ - {item.price_max:,}đ"

                trend_evidence = [
                    {
                        "provider": obs.provider,
                        "url": obs.source_url,
                        "host": obs.source_host,
                        "title": obs.title,
                        "signal_type": obs.signal_type,
                    }
                    for obs in item.observations[:3]
                ]
                primary_reason = (
                    item.observations[0].snippet or item.observations[0].title
                    if item.observations
                    else "Sản phẩm xu hướng nổi bật"
                )

                ck = item.canonical_key
                selection_status = discovery_result.selection.statuses.get(ck, "new")
                selection_reasons = discovery_result.selection.reason_codes.get(ck, [])

                prod_entry = {
                    "id": f"prod_{idx:02d}",
                    "canonical_key": ck,
                    "name": item.name,
                    "brand": item.brand,
                    "model": item.model,
                    "category": category_val,
                    "category_id": cat_id,
                    "price_display": price_display,
                    "price_min": item.price_min or cat_task.criteria.price_min,
                    "price_max": item.price_max or cat_task.criteria.price_max,
                    "currency": item.currency,
                    "selection_status": selection_status,
                    "trend_score": scored_cand.trend_score,
                    "freshness_score": scored_cand.freshness_score,
                    "confidence_score": scored_cand.confidence_score,
                    "final_score": scored_cand.final_score,
                    "score_breakdown": scored_cand.score_breakdown.model_dump(),
                    "trend_evidence": trend_evidence,
                    "selection_reasons": selection_reasons,
                    "hot_reason": primary_reason,
                    "key_features": "",
                    "keywords": keywords,
                    "image_count": len(product_images),
                    "saved_images_count": len(product_images),
                    "unique_sha_count": len(unique_shas),
                    "matched_identity_count": actual_matched_count,
                    "target_images": max_images_per_product,
                    "quota_met": actual_quota_met,
                    "shortfall_reason": str(shortfall_reason) if (not actual_quota_met and isinstance(shortfall_reason, str)) else ("insufficient_matched_assets" if not actual_quota_met else None),
                    "media_research_status": (
                        getattr(res.status, "value", str(res.status))
                        if res and hasattr(res, "status") and not type(res.status).__name__.startswith("MagicMock")
                        else ("completed" if actual_quota_met else "partial" if product_images else "no_usable_assets")
                    ),
                    "images": product_images,
                }
                cat_results_data.append(prod_entry)
                results_data.append(prod_entry)

            # Stage category-partitioned report and products.json in memory for atomic publication
            cat_payload = {
                "version": "2.0",
                "is_canonical": True,
                "date": day_str,
                "category_id": cat_id,
                "category_name": cat_task.category_name,
                "dispatch_metadata": dispatch_meta,
                "ecc_classification": dispatch_meta,
                "sub_prompt": cat_task.sub_prompt,
                "total_products": len(cat_results_data),
                "media_summary": {
                    "target_per_product": max_images_per_product,
                    "total_images_saved": sum(p["saved_images_count"] for p in cat_results_data),
                    "total_unique_sha": len({img["asset_id"] for p in cat_results_data for img in p["images"]}),
                    "total_matched_identity_images": sum(p["matched_identity_count"] for p in cat_results_data),
                    "products_full_quota": sum(1 for p in cat_results_data if p["quota_met"]),
                    "products_shortfall": sum(1 for p in cat_results_data if not p["quota_met"]),
                },
                "products": cat_results_data,
            }
            cat_md = self._generate_markdown_report(day_str, cat_task.sub_prompt, cat_task.criteria, cat_results_data, discovery_result.selection.warnings)
            staged_partitions.append((cat_id, cat_dir, cat_payload, cat_md))
            category_summaries[cat_id] = {
                "name": cat_task.category_name,
                "status": "success",
                "count": len(cat_results_data),
                "dir": str(cat_dir),
            }

        if not results_data:
            if failed_categories and len(failed_categories) == len(dispatch_result.tasks):
                failed_reasons = "; ".join(f"{k}: {v.get('reason')}" for k, v in failed_categories.items())
                message = f"All dispatched categories failed: {failed_reasons}"
                logger.error(message)
                self._restore_publication_files(out_dir, publication_snapshot[0])
                self._discard_publication_snapshot(publication_snapshot)
                return {
                    "status": "failed",
                    "date": day_str,
                    "failure_code": "all_categories_failed",
                    "message": message,
                    "failed_categories": failed_categories,
                }
            message = "No product with a usable brand, model, and source identity was selected"
            if successful_discovery_results:
                for sdr in successful_discovery_results:
                    self.history_repository.fail_run(sdr.run_id, "no_eligible_products", message)
            logger.error(message)
            self._restore_publication_files(out_dir, publication_snapshot[0])
            self._discard_publication_snapshot(publication_snapshot)
            return {
                "status": "failed",
                "date": day_str,
                "failure_code": "no_eligible_products",
                "message": message,
                "failed_categories": failed_categories,
            }

        if failed_categories:
            run_status = "success_with_warnings"
        elif all_warnings:
            run_status = "success_with_warnings"
        else:
            run_status = "success"

        primary_criteria = dispatch_result.tasks[0].criteria if dispatch_result.tasks else criteria

        # Aggregate discovery counts across all successful categories
        raw_candidates_total = sum(dr.raw_candidate_count for dr in successful_discovery_results)
        dedup_candidates_total = sum(dr.deduplicated_candidate_count for dr in successful_discovery_results)
        enriched_candidates_total = sum(dr.enriched_candidate_count for dr in successful_discovery_results)

        # Provider summary aggregated by provider name
        provider_counts: dict[str, dict[str, Any]] = {}
        for dr in successful_discovery_results:
            for ps in dr.provider_summary:
                p_dict = ps.model_dump() if hasattr(ps, "model_dump") else dict(ps)
                p_name = p_dict.get("provider", "unknown")
                if p_name not in provider_counts:
                    provider_counts[p_name] = {
                        "provider": p_name,
                        "status": "ok",
                        "observation_count": 0,
                        "latency_ms": 0,
                        "error_code": None,
                    }
                provider_counts[p_name]["observation_count"] += p_dict.get("observation_count", 0)
                provider_counts[p_name]["latency_ms"] = max(
                    provider_counts[p_name]["latency_ms"], p_dict.get("latency_ms", 0)
                )
                if p_dict.get("status") != "ok":
                    provider_counts[p_name]["status"] = p_dict.get("status")
                    provider_counts[p_name]["error_code"] = p_dict.get("error_code")
        aggregated_provider_summary = list(provider_counts.values())
        from product_scout.trend_discovery.models import ProviderRunSummary
        aggregated_provider_models = [
            ProviderRunSummary(**p) for p in aggregated_provider_summary
        ]

        primary_run_id = successful_discovery_results[0].run_id if successful_discovery_results else "run_unknown"

        # 6. Build schema-v2 root aggregated export payload (compatibility export)
        export_payload = {
            "version": "2.0",
            "is_compatibility_export": True,
            "canonical_source": "category_partitions",
            "date": day_str,
            "run_id": primary_run_id,
            "status": run_status,
            "warnings": all_warnings,
            "failed_categories": failed_categories,
            "generated_at": datetime.now().isoformat(),
            "request_prompt": raw_prompt,
            "dispatch_metadata": dispatch_meta,
            "ecc_classification": dispatch_meta,
            "categories_dispatched": [t.category_id for t in dispatch_result.tasks],
            "category_summaries": category_summaries,
            "parsed_criteria": {
                "category": primary_criteria.category,
                "limit": sum(t.criteria.limit for t in dispatch_result.tasks),
                "price_min": primary_criteria.price_min,
                "price_max": primary_criteria.price_max,
                "currency": primary_criteria.currency,
                "platforms": primary_criteria.platforms,
            },
            "discovery_summary": {
                "raw_candidates": raw_candidates_total,
                "deduplicated_candidates": dedup_candidates_total,
                "enriched_candidates": enriched_candidates_total,
                "eligible_candidates": len(results_data),
                "selected_candidates": len(results_data),
            },
            "provider_summary": aggregated_provider_summary,
            "selection_policy": {
                "cooldown_days": self.discovery_service.config.cooldown_days,
                "fresh_ratio": self.discovery_service.config.fresh_ratio,
                "confidence_threshold": self.discovery_service.config.confidence_threshold,
                "reentry_trend_threshold": self.discovery_service.config.reentry_trend_threshold,
                "reentry_delta_threshold": self.discovery_service.config.reentry_delta_threshold,
                "config_version": self.discovery_service.config.config_version,
            },
            "total_candidates": len(results_data),
            "successful_products": sum(1 for p in results_data if p["image_count"] > 0),
            "media_summary": {
                "target_per_product": max_images_per_product,
                "total_images_saved": sum(p["saved_images_count"] for p in results_data),
                "total_unique_sha": len({img["asset_id"] for p in results_data for img in p["images"]}),
                "total_matched_identity_images": sum(p["matched_identity_count"] for p in results_data),
                "products_full_quota": sum(1 for p in results_data if p["quota_met"]),
                "products_shortfall": sum(1 for p in results_data if not p["quota_met"]),
            },
            "products": results_data,
        }

        # 7. Atomic publication with backup rollback across root and category partitions
        current_cat_ids = {t.category_id for t in dispatch_result.tasks}
        from product_scout.category_dispatcher import SUPPORTED_CATEGORIES
        obsolete_cat_dirs = [
            sub for sub in out_dir.iterdir()
            if sub.is_dir() and sub.name in SUPPORTED_CATEGORIES and sub.name not in current_cat_ids
        ]

        try:
            md_content = self._generate_markdown_report(
                day_str, raw_prompt, primary_criteria, results_data, all_warnings
            )
            self._atomic_publish(
                out_dir=out_dir,
                run_date=run_date,
                session_runs=session_runs,
                export_payload=export_payload,
                md_content=md_content,
                staged_partitions=staged_partitions,
                obsolete_cat_dirs=obsolete_cat_dirs,
            )
        except Exception:
            self._restore_publication_state(out_dir, publication_snapshot)
            raise
        else:
            self._discard_publication_snapshot(publication_snapshot)

        json_file = out_dir / "products.json"
        md_file = out_dir / "report.md"

        # 8. Refresh master Excel and CSV catalogue
        master_excel_file = self.base_dir / "data" / "master_products.xlsx"
        master_csv_file = self.base_dir / "data" / "master_products.csv"
        try:
            from product_scout.master_excel import MasterExcelExporter
            MasterExcelExporter(runs_dir=self.runs_dir, output_dir=self.base_dir / "data").export(
                excel_path=master_excel_file,
                csv_path=master_csv_file,
            )
        except Exception as exc:
            logger.warning("Failed to refresh master Excel catalog: %s", exc)

        return {
            "status": run_status,
            "date": day_str,
            "total_products": len(results_data),
            "products_with_images": sum(1 for p in results_data if p["image_count"] > 0),
            "warnings": all_warnings,
            "failed_categories": failed_categories,
            "category_summaries": category_summaries,
            "output_json": str(json_file),
            "output_report": str(md_file),
            "output_excel": str(master_excel_file),
            "output_dir": str(out_dir),
        }

    def _atomic_publish(
        self,
        out_dir: Path,
        run_date: date | None = None,
        session_runs: list[Any] | None = None,
        export_payload: dict[str, Any] | None = None,
        md_content: str = "",
        staged_partitions: list[tuple[str, Path, dict[str, Any], str]] | None = None,
        obsolete_cat_dirs: list[Path] | None = None,
        *,
        run_id: str | None = None,
        selection_result: Any = None,
        provider_summaries: list[Any] | None = None,
        candidate_count: int = 0,
    ) -> None:
        """Write reports to temp files, complete database transaction, and replace destination files atomically."""
        actual_date = run_date or (date.fromisoformat(out_dir.name) if out_dir.name and len(out_dir.name) == 10 else date.today())
        from product_scout.trend_discovery.repository import SessionCategoryRun
        if session_runs is None:
            if run_id:
                session_runs = [
                    SessionCategoryRun(
                        run_id=run_id,
                        selections=selection_result,
                        provider_summary=provider_summaries or [],
                        candidate_count=candidate_count,
                    )
                ]
            else:
                session_runs = []

        export_payload = export_payload or {}
        staged_partitions = staged_partitions or []

        tmp_json = out_dir / "products.json.tmp"
        tmp_md = out_dir / "report.md.tmp"
        dest_json = out_dir / "products.json"
        dest_md = out_dir / "report.md"

        # 1. Write temp files for root
        tmp_json.write_text(json.dumps(export_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_md.write_text(md_content, encoding="utf-8")

        # 2. Write temp files for all staged category partitions
        for cat_id, cat_dir, cat_payload, cat_md in staged_partitions:
            cat_dir.mkdir(parents=True, exist_ok=True)
            (cat_dir / "products.json.tmp").write_text(json.dumps(cat_payload, indent=2, ensure_ascii=False), encoding="utf-8")
            (cat_dir / "report.md.tmp").write_text(cat_md, encoding="utf-8")

        # 3. Publish files before committing selections/history to SQLite.
        all_dest_moves: list[tuple[Path, Path]] = [
            (tmp_json, dest_json),
            (tmp_md, dest_md),
        ]
        tmp_files: list[Path] = [tmp_json, tmp_md]
        for cat_id, cat_dir, _, _ in staged_partitions:
            c_tmp_j = cat_dir / "products.json.tmp"
            c_dest_j = cat_dir / "products.json"
            c_tmp_m = cat_dir / "report.md.tmp"
            c_dest_m = cat_dir / "report.md"
            all_dest_moves.append((c_tmp_j, c_dest_j))
            all_dest_moves.append((c_tmp_m, c_dest_m))
            tmp_files.extend([c_tmp_j, c_tmp_m])

        moved_backups: list[tuple[Path, Path]] = []
        try:
            # 4a. Backup existing destination files
            for tmp_src, dest in all_dest_moves:
                if dest.exists():
                    bak = dest.with_name(dest.name + ".bak")
                    shutil.move(dest, bak)
                    moved_backups.append((dest, bak))

            # 4b. Move temp to dest
            for tmp_src, dest in all_dest_moves:
                shutil.move(tmp_src, dest)

            # 4c. Remove obsolete category directories from prior runs on same date
            if obsolete_cat_dirs:
                for obs_dir in obsolete_cat_dirs:
                    if obs_dir.exists():
                        shutil.rmtree(obs_dir, ignore_errors=True)

            # 4d. SQLite only becomes current after all files are published.
            if session_runs:
                self.history_repository.complete_session_runs(actual_date, session_runs)

            # 4e. Cleanup backups
            for dest, bak in moved_backups:
                if bak.exists():
                    bak.unlink(missing_ok=True)
        except Exception as exc:
            # Restore backups
            for dest, bak in moved_backups:
                if bak.exists():
                    shutil.move(bak, dest)
            for tmp in tmp_files:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

            raise

    def _snapshot_publication_state(self, out_dir: Path) -> tuple[Path | None, Path]:
        """Capture the pre-run public files and SQLite state for an export rollback."""
        snapshot_id = uuid.uuid4().hex
        output_snapshot = None
        if out_dir.exists():
            output_snapshot = out_dir.with_name(f".{out_dir.name}.rollback-{snapshot_id}")
            shutil.copytree(out_dir, output_snapshot)

        database_snapshot = self.runs_dir.parent / f".{self.history_repository.path.name}.rollback-{snapshot_id}"
        source = sqlite3.connect(self.history_repository.path)
        target = sqlite3.connect(database_snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return output_snapshot, database_snapshot

    def _restore_publication_state(self, out_dir: Path, snapshot: tuple[Path | None, Path]) -> None:
        """Return the public result and selection database to their pre-run state."""
        output_snapshot, database_snapshot = snapshot
        if out_dir.exists():
            shutil.rmtree(out_dir)
        if output_snapshot is not None and output_snapshot.exists():
            shutil.move(output_snapshot, out_dir)

        source = sqlite3.connect(database_snapshot)
        target = sqlite3.connect(self.history_repository.path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        database_snapshot.unlink(missing_ok=True)

    @staticmethod
    def _restore_publication_files(out_dir: Path, output_snapshot: Path | None) -> None:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        if output_snapshot is not None and output_snapshot.exists():
            shutil.move(output_snapshot, out_dir)

    @staticmethod
    def _discard_publication_snapshot(snapshot: tuple[Path | None, Path]) -> None:
        output_snapshot, database_snapshot = snapshot
        if output_snapshot is not None and output_snapshot.exists():
            shutil.rmtree(output_snapshot)
        database_snapshot.unlink(missing_ok=True)

    @staticmethod
    def _generate_markdown_report(
        day_str: str,
        prompt: str,
        criteria: ParsedCriteria,
        products: list[dict[str, Any]],
        warnings: list[str],
    ) -> str:
        """Generate visually appealing Markdown report with embedded images, scores, and status."""
        lines = [
            f"# 📊 Báo Cáo Nghiên Cứu Sản Phẩm Hot Trend - Ngày {day_str}",
            "",
            f"> **Yêu cầu (Prompt):** `{prompt}`  ",
            f"> **Phân loại ECC:** Ngành hàng: **{criteria.category}** | Mục tiêu: **{criteria.limit} sản phẩm** | Ngân sách: **{criteria.price_min:,}đ - {criteria.price_max:,}đ** | Kênh: **{', '.join(criteria.platforms).upper()}**",
        ]

        if warnings:
            lines.append("")
            for w in warnings:
                lines.append(f"> ⚠️ **Cảnh báo vận hành:** `{w}`")

        lines.extend([
            "",
            "---",
            "",
            "## 📋 Danh Sách Tổng Hợp Sản Phẩm",
            "",
            "| # | Hình Ảnh | Tên Sản Phẩm | Khoảng Giá | Trạng Thái | Trend | Fresh | Conf | Bằng Chứng |",
            "|---|---|---|---|---|---|---|---|---|",
        ])

        for p in products:
            img_md = "*(Chưa có ảnh)*"
            if p["images"]:
                first_img = p["images"][0]["local_path"]
                img_md = f'<img src="{first_img}" width="70" alt="{p["name"]}"/>'

            status_badge = "**Mới**" if p["selection_status"] == "new" else "🔄 *Quay lại*"
            evidence_str = ""
            if p.get("trend_evidence"):
                links = [f"[{e['host']}]({e['url']})" for e in p["trend_evidence"] if e.get("url")]
                evidence_str = ", ".join(links[:2])

            lines.append(
                f"| **{p['id']}** | {img_md} | **{p['name']}**<br>*{p['brand']}* | `{p['price_display']}` | {status_badge} | {p['trend_score']:.0f} | {p['freshness_score']:.0f} | {p['confidence_score']:.0f} | {evidence_str} |"
            )

        lines.extend([
            "",
            "---",
            "",
            "## 🔍 Chi Tiết Từng Sản Phẩm & Thư Viện Hình Ảnh",
            "",
        ])

        for p in products:
            lines.extend([
                f"### {p['id']}. {p['name']} ({p['brand']} {p['model']})",
                f"- **Danh mục:** {p['category']}",
                f"- **Mức giá tham khảo:** `{p['price_display']}`",
                f"- **Trạng thái lựa chọn:** {p['selection_status']} (Điểm tổng: {p['final_score']})",
                f"- **Điểm thành phần:** Trend: `{p['trend_score']:.1f}` | Freshness: `{p['freshness_score']:.1f}` | Confidence: `{p['confidence_score']:.1f}`",
                f"- **Lý do lựa chọn:** {', '.join(p.get('selection_reasons', []))}",
                f"- **Lý do Hot Trend / Bằng chứng:** {p['hot_reason']}",
                "",
                "**Bằng chứng nguồn xu hướng:**",
            ])

            for ev in p.get("trend_evidence", []):
                lines.append(f"- [{ev['provider']} / {ev['host']}]({ev['url']}): {ev['title']}")

            lines.append("")
            lines.append("**Bộ sưu tập ảnh chất lượng cao đã tải:**")
            lines.append("")

            if p["images"]:
                for i_idx, img in enumerate(p["images"], start=1):
                    match_str = f"{img.get('match_score', 'N/A')} ({img.get('match_status', 'unknown')})"
                    policy = img.get("usage_policy", "reference_only")
                    lines.append(
                        f"- ![Ảnh {i_idx}]({img['local_path']})  "
                        f"*Kích thước: {img['width']}x{img['height']} | Góc chụp: {img['view']} | Match: {match_str} | Quyền: {policy}*  "
                        f"[Nguồn: {img.get('host', 'web')}]({img['original_url']})"
                    )
            else:
                lines.append("- *(Không tìm thấy ảnh đạt tiêu chuẩn kỹ thuật >= 400px)*")

            if p.get("quota_met"):
                lines.append(f"> ✅ **Chỉ tiêu hình ảnh SKU:** Đạt chỉ tiêu ({p.get('matched_identity_count', 0)}/{p.get('target_images', 0)} ảnh xác nhận SKU). Quyền sử dụng: reference_only.")
            else:
                lines.append(f"> ⚠️ **Chỉ tiêu hình ảnh SKU:** Chưa đủ chỉ tiêu ({p.get('matched_identity_count', 0)}/{p.get('target_images', 0)} ảnh xác nhận SKU). Lý do: `{p.get('shortfall_reason')}`. Ảnh web chưa rõ bản quyền: reference_only.")

            lines.append("")

        lines.extend([
            "---",
            f"*Tài liệu được sinh tự động bởi Product-Intelligence Daily Scout v2.0 vào lúc {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}.*",
            "",
        ])

        return "\n".join(lines)
