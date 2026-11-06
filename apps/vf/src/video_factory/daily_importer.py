"""Pragmatic importer for Product-Intelligence daily ``products.json`` runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from video_factory.handlers import start_product_video_workflow
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.repository import WorkflowRepository


DEFAULT_SOURCE_ROOT = Path(r"D:\work\hermes-data\product-intelligence-data\daily_runs")
FALLBACK_SOURCE_ROOT = Path(r"D:\work\Personal\Product-Intelligence\data\daily_runs")


def get_default_source_root() -> Path:
    try:
        repo_root = Path(__file__).resolve().parents[4]
        for p in [repo_root / "runtime" / "pi" / "runs", repo_root / "apps" / "pi" / "data" / "daily_runs"]:
            if p.exists():
                return p
    except IndexError:
        pass
    workspace_parent = Path(__file__).resolve().parents[3]
    candidates = [
        DEFAULT_SOURCE_ROOT,
        Path(r"Z:\work\hermes-data\product-intelligence-data\daily_runs"),
        workspace_parent / "hermes-data" / "product-intelligence-data" / "daily_runs",
        FALLBACK_SOURCE_ROOT,
        Path(r"Z:\work\Personal\Product-Intelligence\data\daily_runs"),
        workspace_parent / "Personal" / "Product-Intelligence" / "data" / "daily_runs",
    ]
    for c in candidates:
        if c.exists():
            return c
    return DEFAULT_SOURCE_ROOT

from video_factory.automation.repository import (
    DailyImportRepository,
)

_AMBIGUOUS_VARIANT = re.compile(r"\b\d+\s*[a-zA-Z]*\s*/\s*\d+", re.IGNORECASE)


@dataclass(frozen=True)
class ImportReport:
    source_date: str
    source_path: str
    created_product_names: tuple[str, ...]
    skipped_existing: int
    skipped_invalid: int
    errors: tuple[str, ...]


def _normalized(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = raw.encode("ascii", "ignore").decode("ascii").lower()
    return "-".join(re.findall(r"[a-z0-9]+", ascii_value))


def _product_key(product: dict[str, Any]) -> str:
    brand = _normalized(product.get("brand"))
    model = _normalized(product.get("model"))
    if not brand or not model:
        raise ValueError("missing brand or model")
    seed = f"{brand}|{model}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _latest_products_file(source_root: Path, target_date: date | None) -> Path:
    if target_date is not None:
        candidate = source_root / target_date.isoformat() / "products.json"
        if not candidate.is_file():
            raise FileNotFoundError(f"Daily Product-Intelligence output not found: {candidate}")
        return candidate

    candidates = sorted(source_root.glob("????-??-??/products.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No products.json found beneath {source_root}")
    return candidates[0]


def _valid_images(product: dict[str, Any], batch_dir: Path) -> list[dict[str, Any]]:
    batch_root = batch_dir.resolve()
    accepted: list[dict[str, Any]] = []
    for raw in product.get("images") or []:
        try:
            path = Path(str(raw.get("absolute_path") or "")).resolve()
            path.relative_to(batch_root)
            if float(raw.get("match_score", 0.0)) < 0.5:
                continue
            if not path.is_file() or path.is_symlink():
                continue
            with Image.open(path) as image:
                image.verify()
            accepted.append(
                {
                    "asset_id": str(raw.get("asset_id") or ""),
                    "absolute_path": str(path),
                    "original_url": raw.get("original_url"),
                    "match_score": float(raw.get("match_score", 0.0)),
                    "width": raw.get("width"),
                    "height": raw.get("height"),
                }
            )
        except (OSError, TypeError, ValueError):
            continue
    return accepted


def _validate_product(product: dict[str, Any], batch_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    name = str(product.get("name") or "").strip()
    if not name:
        raise ValueError("missing product name")
    if _AMBIGUOUS_VARIANT.search(name):
        raise ValueError("ambiguous product variant")
    key = _product_key(product)
    images = _valid_images(product, batch_dir)
    if len(images) < 1:
        raise ValueError("no technically readable candidate image")
    return key, images


def _ensure_ledger(connection: sqlite3.Connection) -> None:
    DailyImportRepository(connection)


def import_daily_products(
    *,
    source_root: Path,
    connection: sqlite3.Connection,
    daily_limit: int = 3,
    owner_user_id: str = "local-owner",
    target_date: date | None = None,
) -> ImportReport:
    if not 1 <= daily_limit <= 5:
        raise ValueError("daily_limit must be between 1 and 5")
    source_file = _latest_products_file(Path(source_root), target_date)
    raw = json.loads(source_file.read_text(encoding="utf-8"))
    products = raw.get("products")
    if not isinstance(products, list):
        raise ValueError("products.json must contain a products list")
    source_date = str(raw.get("date") or source_file.parent.name)
    _ensure_ledger(connection)
    repo = WorkflowRepository(connection)

    started_rows = connection.execute(
        "SELECT workflow_id FROM daily_product_video_import WHERE source_date = ? AND status = 'started'",
        (source_date,),
    ).fetchall()
    already_started_today = sum(
        1 for row in started_rows if repo.get_workflow(row["workflow_id"]) is not None
    )
    remaining_capacity = max(0, daily_limit - already_started_today)
    if remaining_capacity == 0:
        return ImportReport(
            source_date=source_date,
            source_path=str(source_file.resolve()),
            created_product_names=(),
            skipped_existing=already_started_today,
            skipped_invalid=0,
            errors=(),
        )

    created: list[str] = []
    skipped_existing = 0
    skipped_invalid = 0
    errors: list[str] = []

    for product in products:
        if len(created) >= remaining_capacity:
            break
        try:
            product_key, images = _validate_product(product, source_file.parent)
        except (TypeError, ValueError) as exc:
            skipped_invalid += 1
            errors.append(f"{product.get('id', 'unknown')}: {exc}")
            continue

        workflow_id = f"pv-daily-{product_key[:16]}"
        existing = connection.execute(
            "SELECT workflow_id, status FROM daily_product_video_import WHERE product_key = ?",
            (product_key,),
        ).fetchone()
        if existing is not None and repo.get_workflow(existing["workflow_id"]) is not None:
            if existing["status"] != "started":
                with connection:
                    connection.execute(
                        "UPDATE daily_product_video_import SET status='started', last_error=NULL, updated_at=? WHERE product_key=?",
                        (datetime.now(timezone.utc).isoformat(), product_key),
                    )
            skipped_existing += 1
            continue

        now = datetime.now(timezone.utc).isoformat()
        with connection:
            connection.execute(
                """
                INSERT INTO daily_product_video_import (
                    product_key, canonical_product_key, source_date, source_batch_id,
                    source_product_id, source_path, workflow_id, status, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, ?, ?)
                ON CONFLICT(product_key) DO UPDATE SET
                    canonical_product_key=excluded.canonical_product_key,
                    source_date=excluded.source_date,
                    source_batch_id=excluded.source_batch_id,
                    source_product_id=excluded.source_product_id,
                    source_path=excluded.source_path,
                    workflow_id=excluded.workflow_id,
                    status='reserved',
                    last_error=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    product_key,
                    f"{_normalized(product.get('brand'))}:{_normalized(product.get('model'))}",
                    source_date,
                    source_date,
                    str(product.get("id") or ""),
                    str(source_file),
                    workflow_id,
                    now,
                    now,
                ),
            )

        exact_identity = f"{product['brand']} {product['model']}".strip()
        keywords = list(dict.fromkeys([exact_identity, str(product["name"]), *(product.get("keywords") or [])]))
        source_context = {
            "source": "product_intelligence_daily_runs",
            "source_date": source_date,
            "source_file": str(source_file.resolve()),
            "source_product_id": product.get("id"),
            "product_key": product_key,
            "brand": product.get("brand"),
            "model": product.get("model"),
            "hot_reason": product.get("hot_reason"),
            "key_features": product.get("key_features"),
            "images": images,
        }
        try:
            start_product_video_workflow(
                workflow_repository=repo,
                workflow_id=workflow_id,
                product_name=str(product["name"]),
                keywords=keywords,
                pdp_url=product.get("pdp_url"),
                owner_user_id=owner_user_id,
                idempotency_key=f"daily-import:{product_key}",
                project_id=f"proj-{workflow_id}",
                source_context=source_context,
            )
        except Exception as exc:
            if repo.get_workflow(workflow_id) is None:
                with connection:
                    connection.execute(
                        "UPDATE daily_product_video_import SET status='error', last_error=?, updated_at=? WHERE product_key=?",
                        (str(exc), datetime.now(timezone.utc).isoformat(), product_key),
                    )
                errors.append(f"{product.get('id', 'unknown')}: {exc}")
                continue

        with connection:
            connection.execute(
                "UPDATE daily_product_video_import SET status='started', last_error=NULL, updated_at=? WHERE product_key=?",
                (datetime.now(timezone.utc).isoformat(), product_key),
            )
        created.append(str(product["name"]))

    return ImportReport(
        source_date=source_date,
        source_path=str(source_file.resolve()),
        created_product_names=tuple(created),
        skipped_existing=skipped_existing,
        skipped_invalid=skipped_invalid,
        errors=tuple(errors),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Import Product-Intelligence daily products into Hermes video workflows")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--owner-user-id", default="local-owner")
    parser.add_argument("--date", dest="target_date", default=None)
    args = parser.parse_args(argv)

    if args.db_path is None:
        from video_factory.constants import get_hermes_home

        db_path = get_hermes_home() / "hermes.db"
    else:
        db_path = args.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    parsed_date = date.fromisoformat(args.target_date) if args.target_date else None
    connection = connect(db_path)
    try:
        report = import_daily_products(
            source_root=args.source_root,
            connection=connection,
            daily_limit=args.limit,
            owner_user_id=args.owner_user_id,
            target_date=parsed_date,
        )
    finally:
        connection.close()

    print(json.dumps({
        "source_date": report.source_date,
        "source_path": report.source_path,
        "created": list(report.created_product_names),
        "skipped_existing": report.skipped_existing,
        "skipped_invalid": report.skipped_invalid,
        "errors": list(report.errors),
    }, ensure_ascii=False, indent=2))
    return 0 if not report.errors or report.created_product_names else 1


if __name__ == "__main__":
    raise SystemExit(main())
