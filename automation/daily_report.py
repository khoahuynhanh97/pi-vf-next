"""Summarize existing PI/VF receipts for one production day."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shlex
import sqlite3
from contextlib import closing
from pathlib import Path

from automation.config import PI_DIR, RUNTIME_DIR, VF_DB_PATH, VF_WORKSPACE_DIR
from automation.storage import atomic_json

logger = logging.getLogger(__name__)
LEGACY_TELEGRAM_ENV = PI_DIR / ".env"


def preproduction_summary(artifact: dict) -> dict | None:
    from video_factory.creative.agents.gates import current_preproduction_review

    review = current_preproduction_review(artifact)
    if review is None:
        return None
    evaluator_receipt = getattr(review, "evaluator_receipt", None) or {}
    rubric_version = evaluator_receipt.get("rubric_version", "v1")
    director_config_version = evaluator_receipt.get("director_config_version", "legacy")
    return {
        "verdict": review.verdict, "input_digest": review.input_digest,
        "total_tokens": evaluator_receipt.get("total_tokens"),
        "rubric_version": rubric_version,
        "director_config_version": director_config_version,
        **review.coverage.get("scorecard", {}),
    }


def _recorded_cost(costs, key, amount):
    if type(amount) in (float, int) and math.isfinite(amount) and amount >= 0:
        costs[key] = amount


def _artifact_cost(costs, artifact):
    receipt = artifact.get("evaluator_receipt") or artifact.get("provenance") or {}
    route = receipt.get("route") or receipt.get("route_receipt") or {}
    operation = route.get("attempt_id") or route.get("router_usage_id")
    if operation and route.get("cost_status") == "recorded":
        _recorded_cost(costs, str(operation), route.get("cost_usd"))


def _outcome_summary(outcome):
    from video_factory.workflows.steps import _safe_log_text

    base = {key: outcome.get(key) for key in ("status", "stage", "file_path", "operation_id") if outcome.get(key) is not None}
    base["reason"] = _safe_log_text(outcome.get("reason"), 500)
    return base


def _collect_vf(result, db_path, products, costs):
    from video_factory.workflows.steps import read_pipeline_steps

    run_ids = [result.run_id, *(item["run_id"] for item in result.production_result.get("batches", []))]
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "workflow" not in tables:
            return
        refs = {}
        for run_id in run_ids:
            selection_id = "pv-creative-batch-" + hashlib.sha256(run_id.encode()).hexdigest()[:24]
            saved = conn.execute("SELECT aggregate_json FROM workflow WHERE workflow_id=?", (selection_id,)).fetchone()
            if saved:
                selection = json.loads(saved[0])
                for key in ("prepared_refs", "resume_refs", "production_selected_refs"):
                    refs.update(dict.fromkeys(selection.get(key, [])))
                for item in selection.get("skipped", []):
                    if isinstance(item, dict) and item.get("workflow_id"):
                        refs[item["workflow_id"]] = None
                for item in selection.get("unresolved", []):
                    if isinstance(item, dict) and item.get("workflow_id"):
                        refs[item["workflow_id"]] = None
        outcomes = {}
        for item in result.product_outcomes:
            w_id = item.get("workflow_id")
            if not w_id:
                continue
            if w_id in outcomes:
                prev = outcomes[w_id]
                if prev.get("status") in ("success", "completed", "reused") and prev.get("receipt_path"):
                    if item.get("status") in ("skipped", "failed", "waiting"):
                        continue
            outcomes[w_id] = item
        unmatched = [item for item in result.product_outcomes if not item.get("workflow_id")]
        batch = result.intake_result.get("exchange_batch_dir")
        manifest_path = Path(batch) / "manifest.json" if batch else None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path and manifest_path.is_file() else {}
        by_key = {item["product_key"]: item for item in manifest.get("products", []) if item.get("product_key")}
        for outcome in unmatched:
            source = by_key.get(outcome.get("product_key"))
            row_id = outcome.get("row_id")
            candidate_id = outcome.get("candidate_id") or (source or {}).get("candidate_id")
            product = next((item for item in products if
                            (item.get("candidate_id") == candidate_id if candidate_id
                             else row_id and item.get("row_id") == row_id)), None)
            if product is None and products and outcome.get("stage") in {"pi_intake", "exchange_admission"}:
                continue  # The original PI rows carry the individual acquisition reasons.
            if product is None:
                product = {"name": outcome.get("product_name"), "stages": {}}
                products.append(product)
            product["outcome"] = _outcome_summary(outcome)
        refs.update(dict.fromkeys(outcomes))
        for workflow_id in refs:
            row = conn.execute("SELECT aggregate_json FROM workflow WHERE workflow_id=?", (workflow_id,)).fetchone()
            if row is None:
                continue
            workflow = json.loads(row[0])
            source = workflow.get("source_context") or {}
            row_id = source.get("source_product_id")
            outcome = outcomes.pop(workflow_id, {})
            candidate_id = outcome.get("candidate_id") or (source.get("creative_input") or {}).get("candidate_id")
            product = next((item for item in products if
                            (item.get("candidate_id") == candidate_id if candidate_id
                             else row_id and item.get("row_id") == row_id)), None)
            if product is None:
                product = {"name": source.get("name") or outcome.get("product_name") or workflow_id, "stages": {}}
                products.append(product)
            scene = workflow.get("scene_plan") or {}
            repair_count = 0
            if "workflow_event" in tables:
                for step in read_pipeline_steps(conn, workflow_id):
                    product["stages"][step["step_name"]] = {
                        key: step.get(key) for key in ("status", "error_code", "message", "cost_usd")
                    }
                repair_count += conn.execute(
                    "SELECT COUNT(*) FROM workflow_event WHERE workflow_id=? AND event_type='CreativeStageRecovered'",
                    (workflow_id,),
                ).fetchone()[0]
            if "workflow_qc_retries" in tables:
                repair_count += conn.execute(
                    "SELECT COUNT(*) FROM workflow_qc_retries WHERE workflow_id=? AND "
                    "(retry_type='creative_rewrite' OR action_taken='retry_scene' "
                    "OR (retry_type='four_agent_image_send' AND attempt_number>1))",
                    (workflow_id,),
                ).fetchone()[0]
            product.update(workflow_id=workflow_id, vf_state=workflow.get("state"),
                           preproduction=preproduction_summary(scene), repair_count=repair_count)
            scene_provider_kinds = []
            if {"video_factory_scene_operation", "video_factory_render_batches"} <= tables:
                try:
                    query = (
                        "SELECT s.scene_id, s.provider_kind, s.attempt, s.status "
                        "FROM video_factory_scene_operation AS s "
                        "JOIN video_factory_render_batches AS b "
                        "ON b.render_batch_id = s.render_batch_id "
                        "AND b.project_id = s.project_id "
                        "AND b.owner_user_id = s.owner_user_id "
                        "WHERE b.workflow_id = ? "
                        "AND s.provider_kind IN ('google_flow', 'vertex_ai') "
                        "AND (s.submitted_at IS NOT NULL OR COALESCE(s.provider_operation_id, '') <> '') "
                        "ORDER BY s.scene_order, s.submitted_at, s.attempt, s.operation_id"
                    )
                    for row in conn.execute(query, (workflow_id,)):
                        record = {
                            "scene_id": row[0],
                            "provider_kind": row[1],
                            "attempt": row[2],
                            "status": row[3],
                        }
                        if record not in scene_provider_kinds:
                            scene_provider_kinds.append(record)
                except sqlite3.Error:
                    pass
            product["scene_provider_kinds"] = scene_provider_kinds
            if not outcome and workflow.get("state") == "completed":
                exported = workflow.get("export_result") or {}
                raw_exp = json.loads(exported["payload"]) if isinstance(exported.get("payload"), str) else exported
                mp4_str = str(raw_exp.get("file_path") or raw_exp.get("video_path") or "")
                if mp4_str and Path(mp4_str).is_file():
                    outcome = {
                        "status": "success",
                        "stage": "completed",
                        "file_path": mp4_str,
                        "reason": "Successfully exported and verified decodable MP4 from current run export receipt",
                    }
            if outcome:
                current_outcome = product.get("outcome")
                if current_outcome and current_outcome.get("status") in ("success", "completed", "reused") and current_outcome.get("file_path"):
                    if outcome.get("status") not in ("success", "completed", "reused"):
                        pass
                    else:
                        product["outcome"] = _outcome_summary(outcome)
                else:
                    product["outcome"] = _outcome_summary(outcome)
            if result.provider_mode == "configured":
                if "product_video_operation_attempt" in tables:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(product_video_operation_attempt)")}
                    if {"actual_cost_usd", "cost_status"} <= columns:
                        for attempt in conn.execute(
                            "SELECT * FROM product_video_operation_attempt WHERE workflow_id=? AND cost_status='recorded'",
                            (workflow_id,),
                        ):
                            operation = attempt["provider_operation_id"] or attempt["attempt_id"]
                            _recorded_cost(costs, operation, attempt["actual_cost_usd"])
                render = workflow.get("render_result") or {}
                if isinstance(render.get("payload"), str):
                    render = json.loads(render["payload"])
                for artifact in (workflow.get("creative_brief") or {}, scene, scene.get("scene_prompts") or {},
                                 scene.get("preproduction_review") or {}, render.get("final_review") or {}):
                    _artifact_cost(costs, artifact)
                for folder in ("creative_reviews", "preproduction_reviews"):
                    history = VF_WORKSPACE_DIR / folder / workflow_id
                    for path in history.glob("*/*.json"):
                        try:
                            _artifact_cost(costs, json.loads(path.read_text(encoding="utf-8")))
                        except (OSError, ValueError, TypeError):
                            logger.warning("Could not read critic cost receipt for %s", workflow_id)
        for outcome in outcomes.values():
            products.append({"name": outcome.get("product_name"), "stages": {},
                             "outcome": _outcome_summary(outcome)})


def write_daily_report(result, *, db_path=VF_DB_PATH, root=RUNTIME_DIR / "reports") -> Path:
    from datetime import date
    from video_factory.workflows.steps import _safe_log_text

    day = date.fromisoformat(result.production_date).isoformat()
    path = root / f"{day}.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"date": day, "runs": {}}
    if not isinstance(report, dict) or not isinstance(report.get("runs"), dict) or any(
        not isinstance(run, dict) or not isinstance(run.get("recorded_costs_usd"), dict)
        or "provider_mode" not in run for run in report["runs"].values()
    ):
        raise ValueError(f"Invalid daily report structure: {path}")
    key = f"{result.provider_mode}:{result.run_id}"
    intake = result.intake_result
    intake_dir = intake.get("intake_run_dir") or intake.get("run_dir")
    intake_dirs = intake.get("run_dirs") or ([intake_dir] if intake_dir else [])
    products, costs, errors = [], {}, [_safe_log_text(error, 500) for error in result.inspection_errors]
    for intake_dir in intake_dirs:
        try:
            state = json.loads((Path(intake_dir) / "intake.json").read_text(encoding="utf-8"))
            for row in state["rows"]:
                item = {field: row.get(field) for field in ("row_id", "candidate_id", "name",
                                                            "reason", "reason_code", "retryable")}
                item["reason"] = _safe_log_text(item["reason"], 500)
                item["stages"] = {"PI_SELECTION": {"status": "selected"},
                                  "PI_ACQUISITION": {"status": row.get("status")}}
                products.append(item)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"PI intake report unavailable: {type(exc).__name__}")
    if db_path.is_file():
        try:
            _collect_vf(result, db_path, products, costs)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"VF report unavailable: {type(exc).__name__}")
    previous = report["runs"].get(key, {})
    run = {"status": result.status, "provider_mode": result.provider_mode, "products": products,
           "error": _safe_log_text(result.error.splitlines()[0], 500) if result.error else None,
           "report_errors": errors,
           "recorded_costs_usd": {**previous.get("recorded_costs_usd", {}), **costs}}
    report["runs"][key] = run
    daily_costs = {}
    for item in report["runs"].values():
        daily_costs.update(item["recorded_costs_usd"])
    report["cost"] = {
        "recorded_usd": round(sum(daily_costs.values()), 6),
        "complete": not any(item["provider_mode"] == "configured" for item in report["runs"].values()),
        "scope": "receipt_only: recorded receipts for runs of this production date. Unreported provider spend is excluded.",
    }
    atomic_json(path, report)
    if run != previous and result.provider_mode == "configured" and result.status in {"success", "partial", "failed"}:
        _notify_telegram(path, day, result.status, len(products), report["cost"]["recorded_usd"])
    return path


def _notify_telegram(path, day, status, count, recorded_usd):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat_id):
        keys = ("PRODUCT_SCOUT_TELEGRAM_BOT_TOKEN", "PRODUCT_SCOUT_TELEGRAM_OWNER_ID")
        legacy = {}
        try:
            if LEGACY_TELEGRAM_ENV.is_file():
                for line in LEGACY_TELEGRAM_ENV.read_text(encoding="utf-8-sig").splitlines():
                    key, sep, raw = line.partition("=")
                    if sep and key.strip() in keys:
                        parts = shlex.split(raw, comments=True)
                        if len(parts) == 1:
                            legacy[key.strip()] = parts[0]
        except (OSError, ValueError):
            logger.warning("Could not read legacy PI Telegram configuration")
        token, chat_id = (os.getenv(key, legacy.get(key, "")).strip() for key in keys)
    if not token or not chat_id:
        return
    message = f"PI/VF {day} | {status} | {count} sản phẩm | Chi phí ghi nhận: USD {recorded_usd:.4f}"
    try:
        import httpx

        with path.open("rb") as report:
            response = httpx.post(f"https://api.telegram.org/bot{token}/sendDocument",
                                  data={"chat_id": chat_id, "caption": message},
                                  files={"document": (path.name, report, "application/json")},
                                  timeout=10)
        if response.status_code != 200 or response.json().get("ok") is not True:
            logger.warning("Telegram rejected the PI/VF daily report (HTTP %s)", response.status_code)
    except Exception as exc:
        logger.warning("Telegram daily report delivery failed: %s", type(exc).__name__)


def report_configured_run(result, *, db_path=VF_DB_PATH):
    if result.provider_mode != "configured":
        return
    try:
        path = write_daily_report(result, db_path=db_path)
        print(f"Daily report: {path}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.error("Could not save the PI/VF daily report: %s", exc)
