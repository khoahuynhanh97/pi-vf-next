"""Per-product Exchange consumer that processes products independently."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import date

logger = logging.getLogger(__name__)


def process_exchange_batch(*, batch_path: Path, db_path: Path, profile_path: Path,
                          owner_user_id: str = 'daily-scheduler', max_ticks: int = 100,
                          provider_mode: str = 'configured') -> dict:
    """Process one Exchange batch product-by-product, isolating failures.
    
    Returns dict with completed, skipped, failed lists.
    """
    from argparse import Namespace
    from video_factory.automation.importer import ExchangeImporter
    from video_factory.automation.creative_cli import cmd_creative_batch
    
    importer = ExchangeImporter(batch_path.parent.parent)
    manifest = importer.load_manifest(batch_path)
    
    results = {
        'batch_id': manifest.batch_id,
        'batch_path': str(batch_path),
        'completed': [],
        'skipped': [],
        'failed': [],
    }
    
    for product in manifest.products:
        product_key = product.product_key
        logger.info("Processing product %s from batch %s", product_key, manifest.batch_id)
        
        try:
            run_id = f"{manifest.batch_id}_{product_key.replace(':', '_')}"
            args = Namespace(
                run_id=run_id,
                date=manifest.production_date,
                product_key=[product_key],
                mode='selected',
                limit=1,
                profile=Path(profile_path),
                owner_user_id=owner_user_id,
                max_ticks=max_ticks,
                db_path=Path(db_path),
                inbox_root=batch_path.parent.parent,
                batch_dir=batch_path,
                qc_level='high',
                disable_keyframe_qc=False,
            )
            exit_code = cmd_creative_batch(args)
            
            if exit_code == 0:
                results['completed'].append({
                    'product_key': product_key,
                    'name': product.name,
                })
                logger.info("Product %s completed successfully", product_key)
            else:
                results['failed'].append({
                    'product_key': product_key,
                    'name': product.name,
                    'reason': f'exit_code_{exit_code}',
                })
                logger.warning("Product %s failed with exit code %d", product_key, exit_code)
                
        except Exception as exc:
            results['failed'].append({
                'product_key': product_key,
                'name': product.name,
                'reason': str(exc)[:200],
            })
            logger.error("Product %s raised exception: %s", product_key, exc, exc_info=True)
    
    return results


def consume_daily_batches(*, inbox_root: Path, target_date: date | str,
                         db_path: Path, profile_path: Path,
                         owner_user_id: str = 'daily-scheduler') -> list[dict]:
    """Find and process all READY batches for a given date.
    
    Returns list of batch results.
    """
    from video_factory.automation.importer import ExchangeImporter
    
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    
    importer = ExchangeImporter(inbox_root)
    day_dir = inbox_root / target_date.isoformat()
    
    if not day_dir.exists():
        logger.info("No Exchange directory for date %s", target_date)
        return []
    
    batch_dirs = [d for d in day_dir.iterdir() if d.is_dir() and (d / "READY").exists()]
    logger.info("Found %d READY batches for %s", len(batch_dirs), target_date)
    
    results = []
    for batch_dir in sorted(batch_dirs):
        logger.info("Processing batch %s", batch_dir.name)
        result = process_exchange_batch(
            batch_path=batch_dir,
            db_path=db_path,
            profile_path=profile_path,
            owner_user_id=owner_user_id,
            provider_mode='configured',
        )
        results.append(result)
    
    return results


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Process Exchange batches product-by-product")
    parser.add_argument("--inbox-root", type=Path, required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--owner-user-id", default="daily-scheduler")
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    
    results = consume_daily_batches(
        inbox_root=args.inbox_root,
        target_date=args.date,
        db_path=args.db_path,
        profile_path=args.profile,
        owner_user_id=args.owner_user_id,
    )
    
    total_completed = sum(len(r['completed']) for r in results)
    total_failed = sum(len(r['failed']) for r in results)
    
    print(json.dumps({
        'status': 'success' if total_failed == 0 else 'partial',
        'batches': len(results),
        'completed': total_completed,
        'failed': total_failed,
        'results': results,
    }, indent=2, ensure_ascii=False))
    
    return 0 if total_failed == 0 else 1


if __name__ == '__main__':
    exit(main())
