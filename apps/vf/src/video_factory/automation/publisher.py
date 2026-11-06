"""Daily Product-Intelligence to Exchange Inbox Publisher with Gatekeeper Logic."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

from video_factory.storage_infra.sqlite import connect
from .importer import DEFAULT_EXCHANGE_INBOX, publish_daily_run_to_exchange
from .repository import DailyImportRepository


DEFAULT_SOURCE_ROOT = Path(r"D:\work\hermes-data\product-intelligence-data\daily_runs")
FALLBACK_SOURCE_ROOT = Path(r"D:\work\Personal\Product-Intelligence\data\daily_runs")


def get_default_source_root() -> Path:
    try:
        repo_root = Path(__file__).resolve().parents[5]
        for p in [repo_root / "runtime" / "pi" / "runs", repo_root / "apps" / "pi" / "data" / "daily_runs"]:
            if p.exists():
                return p
    except IndexError:
        pass
    if DEFAULT_SOURCE_ROOT.exists():
        return DEFAULT_SOURCE_ROOT
    if FALLBACK_SOURCE_ROOT.exists():
        return FALLBACK_SOURCE_ROOT
    return DEFAULT_SOURCE_ROOT


def publish_daily_batch(
    *,
    source_root: Path,
    inbox_root: Path,
    connection: sqlite3.Connection,
    cooldown_days: int = 30,
    max_products: int = 5,
    target_date: date | None = None,
) -> dict[str, Any]:
    """Publish Product-Intelligence daily run to exchange inbox with gatekeeper validation.
    
    Returns:
        Report dict with batch_dir, production_date, admitted_count, rejected_count, errors.
    """
    # Find latest or specific date
    if target_date is not None:
        candidate = source_root / target_date.isoformat()
        if not candidate.is_dir():
            raise FileNotFoundError(f"Daily run not found: {candidate}")
        source_run_dir = candidate
    else:
        candidates = sorted(source_root.glob("????-??-??"), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"No daily runs found in {source_root}")
        source_run_dir = candidates[0]

    products_file = source_run_dir / "products.json"
    if not products_file.is_file():
        raise FileNotFoundError(f"products.json not found in {source_run_dir}")

    # Publish with gatekeeper
    try:
        batch_dir = publish_daily_run_to_exchange(
            source_run_dir=source_run_dir,
            inbox_root=inbox_root,
            batch_id=None,
            master_catalog_conn=connection,
            cooldown_days=cooldown_days,
            max_products=max_products,
        )

        # Read published manifest to get stats
        manifest_file = batch_dir / "manifest.json"
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        admitted_count = len(manifest_data.get("products") or [])

        # Count rejections from master catalog
        raw_data = json.loads(products_file.read_text(encoding="utf-8"))
        total_count = len(raw_data.get("products") or [])
        rejected_count = total_count - admitted_count

        return {
            "succeeded": True,
            "batch_dir": str(batch_dir),
            "production_date": manifest_data.get("production_date"),
            "batch_id": manifest_data.get("batch_id"),
            "total_products": total_count,
            "admitted": admitted_count,
            "rejected": rejected_count,
            "ready": (batch_dir / "READY").is_file(),
        }

    except Exception as exc:
        return {
            "succeeded": False,
            "error": str(exc),
            "source_dir": str(source_run_dir),
        }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for daily batch publisher."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description="Publish Product-Intelligence daily run to exchange inbox with gatekeeper validation"
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=DEFAULT_SOURCE_ROOT,
        help="Product-Intelligence daily_runs directory",
    )
    parser.add_argument(
        "--inbox-root",
        type=Path,
        default=DEFAULT_EXCHANGE_INBOX,
        help="Exchange inbox root directory",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="SQLite database path (defaults to HERMES_HOME/hermes.db)",
    )
    parser.add_argument(
        "--cooldown-days",
        type=int,
        default=30,
        help="Days to block recently produced products",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum products per batch",
    )
    parser.add_argument(
        "--date",
        dest="target_date",
        default=None,
        help="Specific date to publish (YYYY-MM-DD)",
    )

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
        # Ensure master catalog schema
        DailyImportRepository(connection)

        report = publish_daily_batch(
            source_root=args.source_root,
            inbox_root=args.inbox_root,
            connection=connection,
            cooldown_days=args.cooldown_days,
            max_products=args.limit,
            target_date=parsed_date,
        )
    finally:
        connection.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("succeeded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
