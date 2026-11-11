"""One replayable daily Sheet selection and PI intake, before browser acquisition."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tools.selection_handoff import (MAX_SHEET_SELECTION, _pool_lock, _read_pool, apply_import_ack,
                                     normalize_sources, publish_selection, write_pool)
from tools.sheet_selection_agent import _complete, select_from_pool
from tools.sheet_snapshot import download_snapshots, fetch_public_xlsx
from tools.spaiauto.intake import start_intake
from tools.spaiauto.storage import atomic_json, bangkok_date, digest, read_json
from tools.spaiauto.workflow import publish


def run_daily(*, root: Path, day: str, db_path: Path, fetch=fetch_public_xlsx,
              complete=_complete, candidate_limit: int | None = None,
              inbox_root: Path | None = None, limit: int = 15, acquire: bool = False) -> dict:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Day must use YYYY-MM-DD")
    root = root.resolve()
    snapshots = download_snapshots(root / day, fetch=fetch)
    sources = list(snapshots.items())
    pool = root / "candidate_pool.csv"
    with _pool_lock(pool):
        rows = normalize_sources(sources, pool if pool.exists() else None, expected_sheet_ids=tuple(snapshots))
        write_pool(pool, rows)
        atomic_json(pool.with_name(pool.stem + ".sources.json"), {"sources": [
            {"sheet_id": sheet_id, "file": f"sources/{sheet_id}_{path.name}",
             "source_path": str(path.resolve()), "sha256": digest(path)}
            for sheet_id, path in sources]})

    proposal = root / day / "selection.json"
    if proposal.exists():
        selection = read_json(proposal)
    else:
        selection = select_from_pool(rows, complete=complete, candidate_limit=candidate_limit, limit=limit)
        selection["requested_limit"] = limit
        atomic_json(proposal, selection)
    output = _process_selection(selection, root=root, pool=pool, day=day, db_path=db_path,
                                inbox_root=inbox_root, acquire=acquire)
    remaining = MAX_SHEET_SELECTION - output["selected_count"]
    if limit != MAX_SHEET_SELECTION or not remaining or selection.get("requested_limit") == MAX_SHEET_SELECTION:
        return output

    # A same-day move from a smaller limit can append one immutable selection.
    supplement_path = proposal.with_name("selection-supplement.json")
    with _pool_lock(supplement_path):
        if supplement_path.is_file():
            supplement = read_json(supplement_path)
        else:
            supplement = select_from_pool(_read_pool(pool), complete=complete,
                                          candidate_limit=candidate_limit, limit=remaining)
            atomic_json(supplement_path, supplement)
        extra = _process_selection(supplement, root=root, pool=pool, day=day, db_path=db_path,
                                   inbox_root=inbox_root, acquire=acquire)
    if not extra["selected_count"]:
        return output
    if not output["selected_count"]:
        return extra
    output["selected_count"] += extra["selected_count"]
    output["run_dirs"] = [output["run_dir"], extra["run_dir"]]
    batches = [*output.get("exchange_batches", []), *extra.get("exchange_batches", [])]
    if batches:
        output["exchange_batches"] = batches
    pending = [*output.get("pending_items", []), *extra.get("pending_items", [])]
    if pending:
        output.update(status="waiting", pending_items=pending,
                      reason=output.get("reason") or extra.get("reason"),
                      next_step=output.get("next_step") or extra.get("next_step"))
    elif batches:
        output["status"] = "success"
    return output


def _process_selection(selection, *, root, pool, day, db_path, inbox_root, acquire):
    selected = selection.get("selected")
    if not isinstance(selected, list):
        raise ValueError("Selection proposal requires a selected array")
    result = publish_selection(pool, root / "selection_batches" / "selected_products.csv",
                               root / "selection_batches", batch_date=day.replace("-", ""),
                               candidate_ids=[item["candidate_id"] for item in selected],
                               limit=max(15, len(selected)),
                               drafts={item["candidate_id"]: item for item in selected})
    if not selected:
        return {"status": "no_eligible_products", "selected_count": 0, "day": day}
    batch = Path(result["batch_dir"])
    run = root / "pi_runs" / result["batch_id"]
    selection_csv = batch / "selected_products.csv"
    state = start_intake(selection_csv, run, handoff=batch / "handoff.json", db_path=db_path)
    apply_import_ack(pool, batch)
    output = {"status": "awaiting_acquisition", "day": day,
              "selected_count": len(selected), "batch_id": result["batch_id"],
              "batch_dir": str(batch), "run_dir": str(run)}
    if inbox_root is None:
        return output

    if state.get("date") != day:
        if state.get("publish_intent"):
            raise ValueError("Published Sheet intake is already bound to a different date")
        state["date"] = day
        atomic_json(run / "intake.json", state)
    output["selection_csv"] = str(selection_csv)
    acquisition_reason = None
    if acquire:
        from tools.spaiauto.scheduled import acquire_batch

        reasons = acquire_batch(run)
        acquisition_reason = "; ".join(reasons) if reasons else None
        state = read_json(run / "intake.json")
    
    pending = [row["name"] for row in state["rows"] if row["status"] not in ("completed", "skipped")]
    if pending or not any(row["status"] == "completed" for row in state["rows"]):
        output.update(status="waiting", pending_items=pending,
                      reason=acquisition_reason or "Đang chờ PI thu thập và duyệt ảnh sản phẩm từ hai Google Sheets.",
                      next_step=f"Tiếp tục Spaiauto intake tại {run}, rồi chạy lại cùng ngày.")
    
    published_batches = []
    for row in state["rows"]:
        if row["status"] == "completed":
            row_id = row["row_id"]
            entry = {"row_id": row_id, "name": row["name"], "candidate_id": row.get("candidate_id"),
                     "selection_batch_id": result["batch_id"]}
            try:
                batch_path = publish(run, inbox_root, eligible_only=True, row_id=row_id)
                published_batches.append({**entry, "batch_path": str(batch_path)})
            except Exception as exc:
                published_batches.append({**entry, "error": str(exc)})
    
    if published_batches:
        output["exchange_batches"] = published_batches
        if not pending:
            output["status"] = "success"
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot two Sheets, select through 9Router, and intake into PI")
    parser.add_argument("--root", type=Path, default=Path("data/pi_sheet_daily"))
    parser.add_argument("--day", default=bangkok_date())
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--candidate-limit", type=int)
    parser.add_argument("--inbox-root", type=Path)
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--acquire", action="store_true", help="Collect and review the next listing with Spaiauto")
    args = parser.parse_args(argv)
    try:
        result = run_daily(root=args.root, day=args.day, db_path=args.db_path,
                           candidate_limit=args.candidate_limit, inbox_root=args.inbox_root,
                           limit=args.limit, acquire=args.acquire)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
