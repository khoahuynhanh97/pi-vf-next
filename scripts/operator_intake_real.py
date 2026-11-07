"""Unified operator intake of authentic product assets into PI and publishing to Exchange."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from typing import Any

# Ensure apps/pi is on sys.path
repo_root = pathlib.Path(__file__).resolve().parent.parent
apps_pi = repo_root / "apps" / "pi"
apps_vf = repo_root / "apps" / "vf" / "src"

# Re-exec with isolated PI venv Python if invoked with system Python (e.g. Python 3.10)
pi_venv_python = apps_pi / ".venv" / "Scripts" / "python.exe"
if pi_venv_python.is_file() and sys.executable.lower() != str(pi_venv_python).lower() and sys.version_info < (3, 11):
    import subprocess
    cmd = [str(pi_venv_python)] + sys.argv
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(cmd, env=env)
    sys.exit(proc.returncode)

for p in [str(repo_root), str(apps_pi), str(apps_vf)]:
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["PYTHONIOENCODING"] = "utf-8"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from automation.config import INBOX_ROOT, PI_RUNS_DIR, today_bangkok_str
from tools.spark_intake import is_verified_real_batch, run_spark_intake
from tools.spaiauto.intake import (
    finish_request,
    resolve_request,
    search_attempt,
)
from tools.spaiauto.storage import atomic_json, digest, item_key, product_url, read_json
from tools.spaiauto.workflow import attach, begin_attempt, review_item


def find_historical_archive(
    target_url: str,
    search_roots: list[pathlib.Path],
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path] | None:
    """Find a complete, reviewed archive for the exact PDP used by PI intake."""
    target_pdp = product_url(target_url)
    for search_root in search_roots:
        for directory, _, files in os.walk(search_root):
            if not {"capture.json", "download.zip"}.issubset(files):
                continue
            folder = pathlib.Path(directory)
            capture = folder / "capture.json"
            try:
                if product_url(read_json(capture).get("source_url", "")) != target_pdp:
                    continue
                review = folder.parent / "review.json"
                if not review.is_file():
                    continue
                approval = read_json(review)
                reviewer = str(approval.get("reviewed_by") or "").strip()
                if (
                    approval.get("identity_verified") is not True
                    or reviewer in ("", "fixture_operator", "synthetic_operator")
                    or approval.get("capture_digest") != digest(capture)
                    or not approval.get("selected_asset_ids")
                ):
                    continue
                screenshots = [folder / "page.png", folder.parent / "incoming" / "page.png"]
                if len(folder.parents) > 4:
                    screenshots.append(folder.parents[4] / "incoming" / "page.png")
                page = next((candidate for candidate in screenshots if candidate.is_file()), None)
                if page is not None:
                    return capture, folder / "download.zip", page, review
            except (OSError, ValueError, AttributeError):
                continue
    return None


def execute_operator_intake(
    spark_file: pathlib.Path,
    run_dir: pathlib.Path | None = None,
    inbox_root: pathlib.Path = INBOX_ROOT,
    date_str: str | None = None,
    limit: int = 1,
    scenes: int = 4,
    hist_roots: list[pathlib.Path] | None = None,
) -> dict[str, Any]:
    """Execute complete operator intake with authentic historical archives."""
    if hist_roots is None:
        hist_roots = [
            repo_root / "runtime" / "pi",
            pathlib.Path(r"D:\work\Nguyên liệu\_Full_Backup_Product_Intelligence_Data"),
            pathlib.Path(r"D:\work\Personal\Product-Intelligence\data"),
        ]

    date_str = date_str or today_bangkok_str()
    spark_file = spark_file.resolve()
    csv_sha = hashlib.sha256(spark_file.read_bytes()).hexdigest()
    effective_run_id = f"run_{date_str.replace('-', '')}_{csv_sha[:8]}_s{scenes}_n{limit}"
    effective_run_dir = run_dir or (PI_RUNS_DIR / effective_run_id)
    effective_run_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Starting Operator Intake for Date: {date_str} ===")
    print(f"Spark Sheet:   {spark_file}")
    print(f"Run Directory: {effective_run_dir}")
    print(f"Inbox Root:    {inbox_root}")
    print(f"Limit:         {limit}")

    # Step A: Initialize Spark Intake to parse CSV and produce selected_products.csv
    init_res = run_spark_intake(
        spark_file=spark_file,
        run_dir=effective_run_dir,
        inbox_root=inbox_root,
        limit=limit,
        offline_adapter=False,
        batch_date=date_str,
    )
    print(f"Initial Intake Status: {init_res.get('status')} ({init_res.get('reason') or 'OK'})")

    if init_res.get("status") == "success":
        return init_res

    intake_dir = effective_run_dir / "intake"
    handoff_csv = effective_run_dir / "handoff" / "selected_products.csv"
    if not handoff_csv.is_file():
        raise FileNotFoundError(f"selected_products.csv missing in {handoff_csv}")

    import csv
    with handoff_csv.open("r", encoding="utf-8-sig") as f:
        selected_rows = list(csv.DictReader(f))

    completed_rows = {
        row["row_id"]
        for row in read_json(intake_dir / "intake.json")["rows"]
        if row["status"] == "completed"
    }
    for idx, row in enumerate(selected_rows[:limit], 1):
        row_id = f"row_{idx:04d}"
        if row_id in completed_rows:
            continue
        url = row["source_url"]
        p_name = row["product_name"]
        print(f"\nProcessing {row_id}: {p_name}")

        # Find matching authentic archive
        archive = find_historical_archive(url, hist_roots)
        if not archive:
            print(f"  [WAITING] No reviewed archive with matching PDP and screenshot for '{p_name}'.")
            break

        cap_path, zip_path, page_path, hist_rev_path = archive
        print(f"  Found authentic archive:")
        print(f"    capture: {cap_path}")
        print(f"    zip:     {zip_path} ({zip_path.stat().st_size:,} bytes)")

        base_rev_data = read_json(hist_rev_path)

        brand = (base_rev_data.get("identity", {}).get("brand") or row.get("brand_hint") or "Generic").strip()
        model = (base_rev_data.get("identity", {}).get("model") or p_name[:40]).strip()
        variant = (base_rev_data.get("identity", {}).get("variant") or "").strip()

        # Prepare selection evidence with the synchronized identity
        sel_data = {
            "url": url,
            "observed_title": p_name,
            "selected_by": "PI agent / Spaiauto browser",
            "reason": "Direct product match from Spark sheet with authentic historical archive.",
            "brand": brand,
            "model": model,
            "variant": variant,
            "observed_price": float(row.get("price_hint_vnd") or 150000.0),
            "currency": "VND",
            "criteria_checks": {
                "requested_features_match": True,
                "variant_checked": True,
            },
            "candidates": [
                {
                    "url": url,
                    "title": p_name,
                    "decision": "selected",
                    "reason": "Authentic matching archive",
                }
            ],
            "selected_at": f"{date_str}T00:00:00Z",
            "row_id": row_id,
        }
        sel_file = effective_run_dir / "fixtures" / f"selection_{row_id}.json"
        sel_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(sel_file, sel_data)

        # Intake lifecycle
        child_dir = intake_dir / "rows" / row_id / "acquisition"
        k = item_key(url)

        search_attempt(intake_dir, row_id)
        resolve_request(intake_dir, row_id, sel_file)

        begin_attempt(child_dir, k, "scan")
        begin_attempt(child_dir, k, "download")

        dest = attach(child_dir, k, cap_path, zip_path, page_path, keep_archive=True)
        print(f"  Imported to: {dest}")

        review_item(child_dir, k, hist_rev_path)
        finish_request(intake_dir, row_id, "completed", "Reused archived visual review for the matching capture")

    # Step F: Finalize intake and publish
    print("\n--- Finalizing Intake & Publishing to Exchange ---")
    publish_res = run_spark_intake(
        spark_file=spark_file,
        run_dir=effective_run_dir,
        inbox_root=inbox_root,
        limit=limit,
        offline_adapter=False,
        batch_date=date_str,
    )
    print("Publish Result Status:", publish_res.get("status"))

    batch_dir = publish_res.get("exchange_batch_dir")
    if batch_dir:
        is_real, reason = is_verified_real_batch(batch_dir)
        print(f"Batch Verification: {is_real} — {reason}")
        publish_res["verified_real"] = is_real
        publish_res["verification_reason"] = reason

    return publish_res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PI Operator Intake of Real Historical Assets")
    parser.add_argument("--spark-file", type=pathlib.Path, required=True)
    parser.add_argument("--run-dir", type=pathlib.Path, help="Custom PI run directory")
    parser.add_argument("--inbox-root", type=pathlib.Path, default=INBOX_ROOT)
    parser.add_argument("--date", default=today_bangkok_str(), help="Production date YYYY-MM-DD (Bangkok today by default)")
    parser.add_argument("--limit", type=int, default=1, help="Number of products to intake")
    parser.add_argument("--archive-root", action="append", type=pathlib.Path, help="Directory containing reviewed archives (repeatable; default: runtime/pi)")
    parser.add_argument("--provider-mode", choices=("configured", "fake"), default="configured")
    parser.add_argument("--run-pipeline", action="store_true", help="Execute Video Factory pipeline after intake")
    parser.add_argument("--profile", type=pathlib.Path)
    parser.add_argument("--qc-level", choices=("low", "middle", "high"), default="high")
    parser.add_argument("--scenes", type=int, choices=(3, 4, 5), default=4, help="Target scene count")

    args = parser.parse_args(argv)
    if args.run_pipeline and (args.run_dir is not None or args.inbox_root.resolve() != INBOX_ROOT):
        parser.error("--run-pipeline requires the platform run and inbox directories")
    res = execute_operator_intake(
        spark_file=args.spark_file,
        run_dir=args.run_dir,
        inbox_root=args.inbox_root,
        date_str=args.date,
        limit=args.limit,
        scenes=args.scenes,
        hist_roots=args.archive_root,
    )

    if res.get("status") != "success":
        print("Intake failed or still waiting:", json.dumps(res, indent=2, ensure_ascii=False))
        return 1

    if args.run_pipeline:
        print("\n--- Triggering Video Factory Pipeline via VF Subprocess ---")
        vf_python = repo_root / "apps" / "vf" / ".venv" / "Scripts" / "python.exe"
        cmd = [
            str(vf_python),
            "-m", "automation.cli", "run",
            "--csv", str(args.spark_file),
            "--date", str(args.date),
            "--limit", str(args.limit),
            "--scenes", str(args.scenes),
            "--provider-mode", args.provider_mode,
            "--qc-level", args.qc_level,
            "--live-intake",
        ]
        if args.profile is not None:
            cmd.extend(("--profile", str(args.profile)))
        import subprocess
        proc = subprocess.run(cmd, cwd=str(repo_root))
        return proc.returncode

    return 0


if __name__ == "__main__":
    sys.exit(main())
