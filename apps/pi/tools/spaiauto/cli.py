"""Preferred Spaiauto entry point. Browser acquisition is performed by an agent."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile

from .storage import read_json
from .videos import attach_video
from .intake import (finish_request, intake_summary, next_request, resolve_request,
                     search_attempt, search_failed, start_intake)
from .workflow import (attach, begin_attempt, export_run, fail_attempt, initialize,
                       publish, review_item, skip_item)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="File-first PI: Excel/CSV products and source links -> browser acquisition -> resources.json. Catalog discovery is paused.")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Start/resume PI from a product list, table or file")
    start.add_argument("--file", type=Path, required=True)
    start.add_argument("--target-images", type=int, default=5)
    start.add_argument("--run-dir", type=Path)
    start.add_argument("--handoff", type=Path, help="Validate an immutable pi_selection READY snapshot")
    start.add_argument("--db-path", type=Path, help="SQLite PI snapshot database for handoff intake")
    for name in ("next", "search", "search-fail", "resolve", "finish", "summary"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", type=Path, required=True)
        if name in ("search", "search-fail", "resolve", "finish"):
            command.add_argument("--row", required=True)
        if name in ("search-fail", "finish"):
            command.add_argument("--reason", required=True)
        if name == "search-fail":
            command.add_argument("--evidence-file", type=Path)
        if name == "resolve":
            command.add_argument("--selection-file", type=Path, required=True)
        if name == "finish":
            command.add_argument("--status", choices=("completed", "needs_review", "skipped"), required=True)
            command.add_argument("--after-review", action="store_true", help="Complete a needs_review row after the batch and its child review are finished")
    init = commands.add_parser("init", help="Create/resume a batch of product links for browser acquisition")
    init.add_argument("--url", action="append", default=[])
    init.add_argument("--urls-file", type=Path)
    init.add_argument("--target-images", type=int, default=5)
    init.add_argument("--run-dir", type=Path, default=None)
    for name in ("status", "begin", "fail", "skip", "attach", "attach-video", "review", "export", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", type=Path, required=True)
        if name in ("begin", "fail", "skip", "attach", "attach-video", "review"):
            command.add_argument("--item", required=True)
        if name == "begin":
            command.add_argument("--stage", choices=("scan", "download"), required=True)
        if name in ("fail", "skip"):
            command.add_argument("--reason", required=True)
        if name == "attach":
            command.add_argument("--capture", type=Path, required=True)
            command.add_argument("--zip", type=Path, required=True)
            command.add_argument("--screenshot", type=Path, required=True)
        if name == "review":
            command.add_argument("--review-file", type=Path, required=True)
        if name == "attach-video":
            command.add_argument("--file", type=Path, required=True)
            command.add_argument("--source-url", required=True)
        if name == "publish":
            command.add_argument("--inbox-root", type=Path, required=True)
            command.add_argument("--batch-id")
            command.add_argument("--eligible-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            default_run = (Path(__file__).resolve().parents[2] / "data" / "pi_runs" /
                           (read_json(args.handoff)["batch_id"] if args.handoff else
                            f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"))
            run_dir = (args.run_dir or default_run).resolve()
            state = start_intake(args.file, run_dir, args.target_images, handoff=args.handoff, db_path=args.db_path)
            result = {"run_dir": str(run_dir), "total_rows": len(state["rows"]), "next": next_request(run_dir),
                      "input_issues": [{"row_id": row["row_id"], "warnings": row["parse_warnings"]}
                                       for row in state["rows"] if row.get("parse_warnings")],
                      "duplicate_rows": [{"row_id": row["row_id"], "duplicate_of": row["duplicate_of"]}
                                         for row in state["rows"] if row.get("duplicate_of")]}
        elif args.command in ("next", "search", "search-fail", "resolve", "finish", "summary"):
            run_dir = args.run_dir.resolve()
            if args.command == "next":
                result = {"next": next_request(run_dir)}
            elif args.command == "search":
                result = search_attempt(run_dir, args.row)
            elif args.command == "search-fail":
                result = search_failed(run_dir, args.row, args.reason, args.evidence_file)
            elif args.command == "resolve":
                result = resolve_request(run_dir, args.row, args.selection_file)
            elif args.command == "finish":
                result = finish_request(run_dir, args.row, args.status, args.reason, after_review=args.after_review)
            else:
                report = intake_summary(run_dir)
                result = {"summary": str(run_dir / "summary.json"), "report": str(run_dir / "README.md"),
                          "resources_json": str(run_dir / "resources.json"),
                          "total_rows": report["total_rows"], "counts": report["counts"]}
        elif args.command == "init":
            urls = args.url[:]
            if args.urls_file:
                urls.extend(line.strip() for line in args.urls_file.read_text(encoding="utf-8-sig").splitlines()
                            if line.strip() and not line.lstrip().startswith("#"))
            run_dir = args.run_dir or (Path(__file__).resolve().parents[2] / "data" / "spaiauto_runs" /
                       f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}")
            result = {"run_dir": str(run_dir.resolve()), "state": initialize(run_dir.resolve(), urls, args.target_images)}
        else:
            run_dir = args.run_dir.resolve()
            if args.command == "status":
                state = read_json(run_dir / "state.json")
                result = {"run_dir": str(run_dir), "items": state["items"],
                          "next_items": [k for k, item in state["items"].items()
                                         if item["status"] not in ("imported", "reviewed", "skipped")],
                          "review_items": [k for k, item in state["items"].items()
                                           if item["status"] == "imported"]}
            elif args.command == "begin":
                result = begin_attempt(run_dir, args.item, args.stage)
            elif args.command == "fail":
                result = fail_attempt(run_dir, args.item, args.reason)
            elif args.command == "skip":
                result = skip_item(run_dir, args.item, args.reason)
            elif args.command == "attach":
                result = {"import_dir": str(attach(run_dir, args.item, args.capture, args.zip, args.screenshot))}
            elif args.command == "review":
                result = review_item(run_dir, args.item, args.review_file)
            elif args.command == "attach-video":
                result = attach_video(run_dir, args.item, args.file, args.source_url)
            elif args.command == "export":
                payload = export_run(run_dir)
                result = {"products_json": str(run_dir / "products.json"),
                          "resources_json": str(run_dir / "resources.json"),
                          "report": str(run_dir / "README.md"), "products": payload["total_products"],
                          "needs_review": sum(p["status"] == "needs_review" for p in payload["products"]),
                          "issues": payload["issues"]}
            else:
                result = {"exchange_batch": str(publish(run_dir, args.inbox_root, args.batch_id, args.eligible_only))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, BadZipFile) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
