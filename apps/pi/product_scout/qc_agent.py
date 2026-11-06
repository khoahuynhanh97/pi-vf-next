"""Product Intelligence Quality Control (QC) Agent.

Automated auditing, scoring, and re-scraping orchestration for scraped products.
Evaluates identity completeness, deduplication, price validity, media asset integrity,
and provenance reliability.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image

logger = logging.getLogger("qc_agent")


class QCStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class QCFailureReason(str, Enum):
    NO_IMAGES = "NO_IMAGES"
    INSUFFICIENT_IMAGES = "INSUFFICIENT_IMAGES"
    IMAGE_FILE_MISSING_OR_CORRUPT = "IMAGE_FILE_MISSING_OR_CORRUPT"
    MISSING_PRICE = "MISSING_PRICE"
    DUPLICATE_PRODUCT = "DUPLICATE_PRODUCT"
    JUNK_OR_BLOG_TITLE = "JUNK_OR_BLOG_TITLE"
    MISSING_BRAND_OR_MODEL = "MISSING_BRAND_OR_MODEL"
    NO_PROVENANCE_SOURCE = "NO_PROVENANCE_SOURCE"


@dataclass
class ImageCheckResult:
    path: str
    exists: bool = False
    decodable: bool = False
    width: int = 0
    height: int = 0
    format: str = ""
    sha256: str = ""
    min_edge_met: bool = False
    error: str | None = None


@dataclass
class ProductQCAudit:
    product_id: str
    canonical_key: str
    name: str
    brand: str
    model: str
    category: str
    crawl_date: str
    price_display: str
    price_min: int | None
    price_max: int | None
    currency: str
    source_url: str
    source_host: str
    image_count: int
    images: list[dict[str, Any]] = field(default_factory=list)
    image_checks: list[ImageCheckResult] = field(default_factory=list)
    qc_status: QCStatus = QCStatus.PASSED
    failure_reasons: list[str] = field(default_factory=list)
    qc_score: float = 100.0
    audit_notes: str = ""
    is_duplicate: bool = False
    duplicate_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        res = asdict(self)
        res["qc_status"] = self.qc_status.value
        return res


class QCAgent:
    """Quality Control evaluator and orchestrator for Product Intelligence."""

    JUNK_TITLE_PATTERNS = [
        re.compile(r"^(?:bài viết|tin tức|kinh nghiệm|chia sẻ|hướng dẫn|top \d+|đánh giá|review|so sánh)\b", re.IGNORECASE),
        re.compile(r"\b(?:hiện nay nổi bật nhờ vào|có tốt không|mua ở đâu uy tín|những lưu ý khi)\b", re.IGNORECASE),
        re.compile(r"\b(?:chế độ bảo trì|trang web đang được bảo trì)\b", re.IGNORECASE),
    ]

    def __init__(self, base_dir: Path | None = None, min_image_dim: int = 400, target_image_count: int = 3):
        self.base_dir = Path(base_dir or Path.cwd()).resolve()
        self.min_image_dim = min_image_dim
        self.target_image_count = target_image_count

    def audit_image_file(self, file_path: Path | str, run_dir: Path | None = None) -> ImageCheckResult:
        p = Path(file_path)
        candidates = []
        if p.is_absolute():
            candidates.append(p)
        if run_dir:
            candidates.append(run_dir / p)
            candidates.append(run_dir / "food_snack" / p)
        candidates.append(self.base_dir / p)
        candidates.append(self.base_dir / "data" / p)

        actual_file: Path | None = None
        for cand in candidates:
            if cand.is_file():
                actual_file = cand
                break

        if not actual_file:
            return ImageCheckResult(path=str(file_path), exists=False, error="File does not exist")

        try:
            raw_bytes = actual_file.read_bytes()
            if len(raw_bytes) == 0:
                return ImageCheckResult(path=str(file_path), exists=True, error="Empty file")

            sha = hashlib.sha256(raw_bytes).hexdigest()
            with Image.open(actual_file) as img:
                img.verify()
            with Image.open(actual_file) as img:
                w, h = img.size
                fmt = img.format or actual_file.suffix.lstrip(".").upper()
                min_edge_met = min(w, h) >= self.min_image_dim
                return ImageCheckResult(
                    path=str(file_path),
                    exists=True,
                    decodable=True,
                    width=w,
                    height=h,
                    format=fmt,
                    sha256=sha,
                    min_edge_met=min_edge_met,
                )
        except Exception as exc:
            return ImageCheckResult(path=str(file_path), exists=True, decodable=False, error=str(exc))

    def evaluate_product(
        self,
        product: dict[str, Any],
        crawl_date: str = "",
        seen_keys: dict[str, str] | None = None,
        run_dir: Path | None = None,
    ) -> ProductQCAudit:
        pid = str(product.get("id") or product.get("product_id") or "")
        name = str(product.get("name") or "").strip()
        brand = str(product.get("brand") or "").strip()
        model = str(product.get("model") or "").strip()
        category = str(product.get("category") or "").strip()
        canonical_key = str(product.get("canonical_key") or f"{brand}|{model or name}").strip()

        # Price parsing
        price_disp = str(product.get("price_display") or "").strip()
        p_min = product.get("price_min")
        p_max = product.get("price_max")
        try:
            p_min = int(p_min) if p_min is not None else None
        except (ValueError, TypeError):
            p_min = None
        try:
            p_max = int(p_max) if p_max is not None else None
        except (ValueError, TypeError):
            p_max = None

        currency = str(product.get("currency") or "VND")

        # Provenance source
        source_url = ""
        source_host = ""
        evidence_list = product.get("trend_evidence") or product.get("sources") or []
        if isinstance(evidence_list, list) and evidence_list:
            first_ev = evidence_list[0]
            if isinstance(first_ev, dict):
                source_url = first_ev.get("url", "")
                source_host = first_ev.get("host", first_ev.get("provider", ""))
        if not source_url and product.get("evidence_sources"):
            source_url = str(product.get("evidence_sources"))
        if not source_url and product.get("source_url"):
            source_url = str(product.get("source_url"))
            source_host = urlsplit(source_url).netloc.removeprefix("www.")

        # Images
        raw_images = product.get("images") or []
        img_checks = []
        valid_img_count = 0

        for img_entry in raw_images:
            local_p = ""
            if isinstance(img_entry, dict):
                local_p = img_entry.get("local_path") or img_entry.get("absolute_path") or ""
            elif isinstance(img_entry, str):
                local_p = img_entry

            if local_p:
                check = self.audit_image_file(local_p, run_dir=run_dir)
                img_checks.append(check)
                if check.exists and check.decodable and check.min_edge_met:
                    valid_img_count += 1

        failures: list[str] = []
        deductions = 0.0

        # Check 1: Identity & Junk Titles
        if not name:
            failures.append(QCFailureReason.MISSING_BRAND_OR_MODEL.value)
            deductions += 40.0
        for pat in self.JUNK_TITLE_PATTERNS:
            if pat.search(name) or pat.search(model):
                failures.append(QCFailureReason.JUNK_OR_BLOG_TITLE.value)
                deductions += 40.0
                break

        if brand.lower() in ("unknown", "", "none"):
            failures.append(QCFailureReason.MISSING_BRAND_OR_MODEL.value)
            deductions += 20.0

        # Check 2: Deduplication within run or catalog
        is_dup = False
        dup_of = None
        if seen_keys is not None:
            # Normalize key for loose deduplication
            norm_key = re.sub(r"[^a-z0-9]+", "", canonical_key.lower())
            if norm_key in seen_keys:
                is_dup = True
                dup_of = seen_keys[norm_key]
                failures.append(QCFailureReason.DUPLICATE_PRODUCT.value)
                deductions += 30.0
            else:
                seen_keys[norm_key] = pid or name

        # Check 3: Pricing
        is_price_zero = (
            price_disp in ("0đ - 0đ", "", "0", "None", "0đ")
            or (p_min is not None and p_min <= 0 and (p_max is None or p_max <= 0))
        )
        if is_price_zero or (p_min is None and not price_disp):
            failures.append(QCFailureReason.MISSING_PRICE.value)
            deductions += 30.0

        # Check 4: Media Integrity
        if not raw_images or len(img_checks) == 0:
            failures.append(QCFailureReason.NO_IMAGES.value)
            deductions += 40.0
        elif valid_img_count == 0:
            failures.append(QCFailureReason.IMAGE_FILE_MISSING_OR_CORRUPT.value)
            deductions += 35.0
        elif valid_img_count < 2:
            failures.append(QCFailureReason.INSUFFICIENT_IMAGES.value)
            deductions += 15.0

        # Check 5: Provenance
        if not source_url:
            failures.append(QCFailureReason.NO_PROVENANCE_SOURCE.value)
            deductions += 15.0

        qc_score = max(0.0, 100.0 - deductions)
        qc_status = QCStatus.PASSED if not failures else QCStatus.FAILED

        notes = []
        if failures:
            notes.append(f"Issues: {', '.join(failures)}")
        if is_dup:
            notes.append(f"Duplicate of {dup_of}")
        notes.append(f"Images verified: {valid_img_count}/{len(raw_images)}")

        return ProductQCAudit(
            product_id=pid,
            canonical_key=canonical_key,
            name=name,
            brand=brand,
            model=model,
            category=category,
            crawl_date=crawl_date,
            price_display=price_disp,
            price_min=p_min,
            price_max=p_max,
            currency=currency,
            source_url=source_url,
            source_host=source_host,
            image_count=valid_img_count,
            images=raw_images,
            image_checks=img_checks,
            qc_status=qc_status,
            failure_reasons=failures,
            qc_score=qc_score,
            audit_notes="; ".join(notes),
            is_duplicate=is_dup,
            duplicate_of=dup_of,
        )

    def audit_daily_run(self, run_dir: Path | str) -> list[ProductQCAudit]:
        r_dir = Path(run_dir).resolve()
        prod_json = r_dir / "products.json"
        if not prod_json.is_file():
            return []

        data = json.loads(prod_json.read_text(encoding="utf-8"))
        crawl_date = data.get("date", r_dir.name)
        products = data.get("products", [])

        audits = []
        seen_keys: dict[str, str] = {}
        for p in products:
            audit = self.evaluate_product(p, crawl_date=crawl_date, seen_keys=seen_keys, run_dir=r_dir)
            audits.append(audit)
        return audits

    def audit_all_runs(self, runs_dir: Path | str | None = None) -> list[ProductQCAudit]:
        r_dir = Path(runs_dir or self.base_dir / "data" / "daily_runs").resolve()
        day_dirs = sorted([d for d in r_dir.iterdir() if d.is_dir()])
        all_audits = []
        for d in day_dirs:
            audits = self.audit_daily_run(d)
            all_audits.extend(audits)
        return all_audits
