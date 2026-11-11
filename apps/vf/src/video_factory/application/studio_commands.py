"""Fixed Studio-to-command adapter. Reuse durable handlers; never spawn a shell."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from video_factory.workflows.repository import WorkflowRepository
from video_factory.workflows.steps import _safe_log_text, read_pipeline_steps

JOB_STEPS = {
    "product_video.research": "INPUT_ADMISSION", "product_video.resource_pack": "IMAGE_QC",
    "product_video.creative_brief": "CREATIVE_BRIEF", "product_video.scene_plan": "SCENE_PLAN",
    "product_video.storyboard": "KEYFRAME_GENERATION", "product_video.render": "VIDEO_RENDER",
    "product_video.export": "EXPORT",
}
STEPS = ["INPUT_ADMISSION", "IMAGE_QC", "PRODUCT_LOCK", "CREATIVE_BRIEF", "SCENE_PLAN",
         "KEYFRAME_GENERATION", "KEYFRAME_QC", "VIDEO_RENDER", "VIDEO_QC", "EXPORT"]
PRESEND_CODES = (
    "GOOGLE_FLOW_NEW_PROJECT_SELECTOR_NOT_UNIQUE", "GOOGLE_FLOW_AUTH_REQUIRED",
    "GOOGLE_FLOW_AUTH_UNVERIFIED", "GOOGLE_FLOW_SETTINGS_PANEL_UNVERIFIED",
    "GOOGLE_FLOW_SETTINGS_SELECTOR_NOT_UNIQUE", "GOOGLE_FLOW_IMAGE_MODE_SELECTOR_NOT_UNIQUE",
    "GOOGLE_FLOW_IMAGE_MODE_UNVERIFIED", "GOOGLE_FLOW_916_SELECTOR_NOT_UNIQUE",
    "GOOGLE_FLOW_916_UNVERIFIED", "GOOGLE_FLOW_SETTINGS_SAVE_SELECTOR_NOT_UNIQUE",
    "GOOGLE_FLOW_REFERENCE_ASSET_NOT_UNIQUE",
    "FLOW_UPLOAD_CONSENT_REQUIRED",
)


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _loaded(conn: sqlite3.Connection, workflow_id: str) -> tuple[dict, int]:
    loaded = WorkflowRepository(conn).get_workflow(workflow_id)
    if loaded is None:
        raise KeyError("Workflow không tồn tại")
    if loaded[0].get("owner_user_id") != "local-owner":
        raise PermissionError("Workflow thuộc người dùng khác")
    return loaded


def _latest_job(conn: sqlite3.Connection, workflow_id: str) -> dict:
    row = conn.execute("SELECT * FROM workflow_outbox WHERE workflow_id=? ORDER BY outbox_id DESC LIMIT 1", (workflow_id,)).fetchone()
    if not row:
        return {}
    result = dict(row)
    try:
        message = json.loads(row["message"])
        if not isinstance(message, dict):
            return {**result, "job_name": None}
        payload = message.get("payload") or message
        result["job_name"] = payload.get("job_name") if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        result["job_name"] = None
    return result


def quarantine_context(conn: sqlite3.Connection, workflow_id: str) -> dict | None:
    job = _latest_job(conn, workflow_id)
    if job.get("state") != "quarantined":
        return None
    return {"quarantined": True, "step": JOB_STEPS.get(job.get("job_name"), "Pipeline"),
            "job_name": job.get("job_name"), "outbox_id": job["outbox_id"],
            "reason": _safe_log_text(str(job.get("last_error") or "Job đã dừng; kiểm tra nguyên nhân trước khi tiếp tục."), 1000),
            "attempt_count": job.get("attempt_count"), "max_attempts": job.get("max_attempts"),
            "next_action": "Sửa nguyên nhân lỗi, sau đó mở “Chạy lại từ bước…” để chọn command phục hồi đúng bước.",
            "recovery_available": False}


def execution_context(conn: sqlite3.Connection, workflow_id: str) -> dict:
    job = _latest_job(conn, workflow_id)
    return {"state": job.get("state"), "job_name": job.get("job_name"),
            "step": JOB_STEPS.get(job.get("job_name")), "attempt_count": job.get("attempt_count"),
            "updated_at": job.get("updated_at"), "outbox_id": job.get("outbox_id")}


def _events(conn: sqlite3.Connection, workflow_id: str) -> list[dict]:
    events = []
    for row in conn.execute("SELECT event_type,payload_json FROM workflow_event WHERE workflow_id=? AND event_type LIKE 'StudioCommand%' ORDER BY rowid", (workflow_id,)):
        try:
            events.append({"type": row["event_type"], **json.loads(row["payload_json"])})
        except (TypeError, ValueError):
            continue
    return events


def _receipt(conn: sqlite3.Connection, workflow_id: str, request_id: str) -> dict | None:
    events = [e for e in _events(conn, workflow_id) if e.get("request_id") == request_id]
    if not events:
        return None
    return {"started": events[0], "finished": next((e for e in reversed(events) if e["type"] in {"StudioCommandFinished", "StudioCommandRejected"}), None)}


def _guard(conn: sqlite3.Connection, workflow_id: str, agg: dict, *, allow_pending: bool = False) -> str:
    states = ("leased",) if allow_pending else ("leased", "pending")
    rows = conn.execute("SELECT state FROM workflow_outbox WHERE workflow_id=? AND state IN ('pending','leased')", (workflow_id,)).fetchall()
    if any(r["state"] in states for r in rows):
        return "Workflow đang có job chạy hoặc chờ xử lý; không tạo yêu cầu trùng."
    events = _events(conn, workflow_id)
    finished = {e.get("request_id") for e in events if e["type"] in {"StudioCommandFinished", "StudioCommandRejected"}}
    if any(e["type"] == "StudioCommandStarted" and e.get("request_id") not in finished for e in events):
        return "Lệnh Studio trước chưa rõ kết quả; cần đối soát trước khi chạy tiếp."
    for table in ("product_video_operation_attempt", "workflow_qc_retries", "video_factory_keyframe_operation"):
        if _table(conn, table) and conn.execute(
            f"SELECT 1 FROM {table} WHERE workflow_id=? AND status IN ('reserved','claimed','sent','technical_unknown','unknown','submitting','project_created','submitted','running') LIMIT 1", (workflow_id,),
        ).fetchone():
            return "Còn operation/QC đang chạy hoặc chưa rõ kết quả; đối soát đúng operation trước khi gửi lại."
    if _table(conn, "video_factory_scene_operation"):
        batch = (agg.get("render_result") or {}).get("render_id")
        if batch and conn.execute("SELECT 1 FROM video_factory_scene_operation WHERE render_batch_id=? AND status IN ('submitted','running','unknown','technical_unknown','submitting','reserved','claimed','sent') LIMIT 1", (batch,)).fetchone():
            return "Còn scene đang chạy hoặc chưa rõ kết quả; không gửi lại render."
    if _table(conn, "product_video_runtime_control"):
        from video_factory.workflows.runtime_control import is_shared_infra_blocker

        pause = conn.execute("SELECT paused,code,workflow_id FROM product_video_runtime_control WHERE state_id=1").fetchone()
        if pause and pause["paused"] and (
            is_shared_infra_blocker(pause["code"])
            or not pause["workflow_id"] or pause["workflow_id"] == workflow_id
        ):
            if pause["code"] == "QC_OUTCOME_UNCERTAIN":
                return "Kết quả QC chưa được xác nhận (QC_OUTCOME_UNCERTAIN). Cần đối soát yêu cầu QC hiện hữu trước khi tiếp tục; chưa thể chạy lại an toàn."
            return f"Pipeline đang tạm dừng ({pause['code']}); cần xử lý và pipeline-resume trước."
    return ""


def workflow_actions(conn: sqlite3.Connection, workflow_id: str) -> dict:
    agg, version = _loaded(conn, workflow_id)
    job = _latest_job(conn, workflow_id)
    guard = _guard(conn, workflow_id, agg)
    options: list[dict] = []

    def add(key: str, steps: list[str], label: str, command: str, effect: str, **params: Any) -> None:
        action_guard = _guard(conn, workflow_id, agg, allow_pending=True) if key.startswith("run-pending:") else guard
        options.append({"id": key, "steps": steps, "label": label, "command": command,
                        "effect": effect, "enabled": not action_guard, "reason": action_guard, "params": params})

    state = agg.get("state")
    stage = agg.get("operator_stage")
    if job.get("state") == "pending" and job.get("job_name") in JOB_STEPS:
        add(f"run-pending:{job['outbox_id']}:{job.get('attempt_count', 0)}", [JOB_STEPS[job["job_name"]]], "Chạy job đang chờ", "creative-batch (selection)", "Tiếp tục job đã có trong outbox; không tạo job mới.")
    if state == "WaitingForOperator" and stage in {"direction", "script", "storyboard"} and agg.get("operator_action") in {"creative_review", "operator_review", "provider_reconciliation"}:
        step, prerequisite = {"direction": ("CREATIVE_BRIEF", "locked_pack"), "script": ("SCENE_PLAN", "creative_brief"), "storyboard": ("KEYFRAME_GENERATION", "scene_plan")}[stage]
        if agg.get(prerequisite):
            add("recover-creative", [step], "Chạy lại bước sáng tạo đang bị chặn", "recover-creative", "Dùng checkpoint đã duyệt; lưu recovery event, giữ giới hạn retry và lịch sử cũ.")
    if state == "WaitingForOperator" and stage == "keyframe" and (agg.get("keyframe_recovery_plan") or {}).get("frames") and agg.get("scene_plan") and agg.get("storyboard"):
        add("recover-keyframe-qc", ["KEYFRAME_QC", "KEYFRAME_GENERATION"], "Chạy lại keyframe theo recovery plan", "resume_storyboard_after_qc_authorization", "Tạo lại các frame trong recovery plan đã lưu rồi kiểm tra QC; giữ lịch sử các frame cũ.")
    if job.get("state") == "quarantined" and job.get("job_name") == "product_video.storyboard" and _table(conn, "video_factory_keyframe_operation"):
        for row in conn.execute("SELECT operation_id,frame_id,status,error_code,error_message,provider_operation_id,output_path,provider_metadata_json FROM video_factory_keyframe_operation WHERE workflow_id=?", (workflow_id,)):
            try:
                meta = json.loads(row["provider_metadata_json"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            if row["status"] == "failed" and row["error_code"] == "GOOGLE_FLOW_KEYFRAME_FAILED" and meta.get("send_attempted") is False and not row["provider_operation_id"] and not row["output_path"] and any(code in str(row["error_message"] or "") and code in str(job.get("last_error") or "") for code in PRESEND_CODES):
                add(f"recover-keyframe:{row['operation_id']}", ["KEYFRAME_GENERATION"], f"Chạy lại keyframe {row['frame_id']} (chưa Send)", "recover-keyframe", "Phục hồi đúng operation có bằng chứng chưa Send; tái sử dụng các frame đã hoàn tất và giữ ledger.", operation_id=row["operation_id"])
    if state == "WaitingForOperator" and stage == "video" and agg.get("operator_action") in {"qc_retry_authorization", "qc_authorization"}:
        report_id = agg.get("operator_qc_report_id")
        row = conn.execute("SELECT findings_json FROM product_video_qc_reports WHERE report_id=?", (report_id,)).fetchone() if report_id and _table(conn, "product_video_qc_reports") else None
        if row:
            try:
                failures = [f for f in json.loads(row[0]).get("visual_scenes", []) if f.get("verdict") == "fail"]
            except (TypeError, ValueError):
                failures = []
            if len(failures) == 1 and isinstance(failures[0].get("scene_number"), int):
                order = failures[0]["scene_number"]
                add(f"retry-scene:{order}", ["VIDEO_RENDER", "VIDEO_QC"], f"Render lại cảnh {order} bị QC fail", "scene/re-render", "Tạo lượt render mới cho đúng cảnh fail; giữ clip/lượt cũ và các cảnh còn lại. QC phải chạy lại trên kết quả mới.", scene_order=order)
    gate = agg.get("pending_approval_gate")
    if gate:
        gate_steps = {"AwaitingProductLock": ["PRODUCT_LOCK"], "AwaitingBriefApproval": ["CREATIVE_BRIEF", "SCENE_PLAN"], "AwaitingSceneApproval": ["SCENE_PLAN", "KEYFRAME_GENERATION"], "AwaitingStoryboardApproval": ["KEYFRAME_QC", "VIDEO_RENDER"], "AwaitingFinalApproval": ["VIDEO_QC", "EXPORT"]}
        if gate in gate_steps:
            add("approve", gate_steps[gate], "Duyệt gate hiện tại và tiếp tục", "approve", "Dùng artifact hiện tại qua gate được backend kiểm tra; không xóa hay tạo lại artifact đã có.")
    if job.get("state") == "quarantined" and job.get("job_name") in JOB_STEPS:
        step = JOB_STEPS[job["job_name"]]
        journal = next((s for s in read_pipeline_steps(conn, workflow_id) if s["step_name"] == step), {})
        before_send = journal.get("cost_status") == "not_sent" and not journal.get("operation_id")
        registry_failure = "cannot be dispatched on registry" in str(job.get("last_error") or "")
        if (job["job_name"] == "product_video.export" or before_send or registry_failure) and int(job.get("attempt_count") or 0) < int(job.get("max_attempts") or 0):
            add(f"retry-job:{job['outbox_id']}:{job.get('attempt_count', 0)}", [step], "Chạy lại job đã dừng", "requeue_quarantined (preserve_attempts)", "Xếp lại đúng message cũ; giữ số lần đã thử, checkpoint, lịch sử và giới hạn retry.", outbox_id=job["outbox_id"])
        if job["job_name"] == "product_video.creative_brief":
            from video_factory.application.operator_handler import (
                inspect_quarantined_creative_brief,
            )

            inspection = inspect_quarantined_creative_brief(conn, workflow_id=workflow_id)
            if inspection.get("recovery_available") and inspection.get("qc_level", "middle") == "middle":
                add("resume-brief", ["CREATIVE_BRIEF"], "Tiếp tục brief sau khi sửa ảnh nguồn", "retry-quarantined", "Xác minh lại ảnh/manifest/SHA và xếp lại đúng job brief; giữ số lần thử.")
    reasons = {step: guard or "Bước này không có command phục hồi hợp lệ ở trạng thái hiện tại; cần checkpoint/job tương ứng." for step in STEPS}
    reasons["ALL"] = "Lệnh reset hiện xóa workflow/lịch sử và không cho dùng sau Product Lock. Không dùng reset cho workflow đang sản xuất."
    if not any("KEYFRAME_GENERATION" in o["steps"] for o in options) and job.get("job_name") == "product_video.storyboard":
        reasons["KEYFRAME_GENERATION"] = guard or "Chưa có operation keyframe với bằng chứng chưa Send hoặc recovery plan hợp lệ. Cần đối soát operation đã gửi; không tạo request mới chỉ từ thông báo lỗi."
    if state in {"skipped", "WaitingForOperator"} and agg.get("failure_code") in {"IMAGE_QC_FAILED", "EXCHANGE_IMAGE_QC_FAILED", "PRODUCT_IMAGE_QC_REJECTED"}:
        reasons["IMAGE_QC"] = "Thay ảnh mẫu trong trang sản phẩm rồi xác nhận để xếp lại QC ảnh nguồn."
    return {"ok": True, "workflow_id": workflow_id, "version": version, "state": state, "options": options, "unavailable_reasons": reasons}


def prepare_runner(conn: sqlite3.Connection, workflow_id: str) -> dict:
    """Validate the same selected-product execution path used by Studio Start."""
    from video_factory.automation.importer import (
        ExchangeImporter,
        get_default_exchange_inbox,
    )

    agg, _ = _loaded(conn, workflow_id)
    source = agg.get("source_context") or {}
    ledger = conn.execute("SELECT * FROM daily_product_video_import WHERE workflow_id=?", (workflow_id,)).fetchone() if _table(conn, "daily_product_video_import") else None
    key = source.get("product_key") or agg.get("product_key") or (ledger["product_key"] if ledger else None)
    target_date = source.get("source_date") or (ledger["source_date"] if ledger else None)
    saved_batch = source.get("source_batch_id") or (ledger["source_batch_id"] if ledger else None)
    if not key or not target_date or not saved_batch:
        raise ValueError("Thiếu product_key/ngày nguồn/batch đã lưu; không chạy nhầm sang batch READY khác.")
    inbox = Path(os.environ.get("HERMES_EXCHANGE_INBOX") or get_default_exchange_inbox()).resolve()
    importer = ExchangeImporter(inbox)
    manifest = importer.load_manifest(importer.find_latest_ready_batch(date.fromisoformat(str(target_date))))
    if manifest.batch_id != saved_batch or not any(p.product_key == key for p in manifest.products):
        raise ValueError("READY batch hiện tại không khớp checkpoint của workflow; không chạy batch khác.")
    db_path = next((Path(r["file"]).resolve() for r in conn.execute("PRAGMA database_list") if r["name"] == "main" and r["file"]), None)
    if db_path is None:
        raise ValueError("Runner Studio cần database lưu trên đĩa")
    profile = Path(os.environ.get("VF_CREATIVE_RUN_CONFIG") or Path(__file__).resolve().parents[3] / "config/creative_run_profile.json").resolve()
    from video_factory.creative.agents.contracts import BackendProfile

    try:
        BackendProfile.from_dict(json.loads(profile.read_text(encoding="utf-8"))["profile"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Creative profile không hợp lệ hoặc không đọc được: {profile}") from exc
    policy = agg.get("creative_policy") or {}
    return {"db_path": db_path, "inbox_root": inbox, "profile_path": profile, "product_keys": [key], "target_date": str(target_date),
            "owner_user_id": "local-owner", "limit": None, "qc_level": policy.get("qc_level") or "middle",
            "disable_keyframe_qc": bool(policy.get("disable_keyframe_qc", False))}


def _event(conn: sqlite3.Connection, workflow_id: str, request_id: str, kind: str, payload: dict) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute("INSERT INTO workflow_event(event_id,workflow_id,event_type,timestamp,payload_json) VALUES(?,?,?,?,?)",
                 (f"studio_{request_id}_{kind}", workflow_id, f"StudioCommand{kind}", now, json.dumps({"request_id": request_id, **payload}, ensure_ascii=False)))


def command_receipt(conn: sqlite3.Connection, workflow_id: str, payload: dict) -> dict | None:
    _loaded(conn, workflow_id)
    receipt = _receipt(conn, workflow_id, payload["request_id"])
    if receipt is None:
        return None
    if receipt["started"].get("fingerprint") != _fingerprint(payload):
        raise ValueError("request_id đã dùng cho nội dung khác")
    final = receipt["finished"]
    if not final:
        raise ValueError("Lệnh trước chưa rõ kết quả; không tự gửi lại")
    if final["type"] == "StudioCommandRejected":
        raise ValueError(final["error"])
    return {**final["result"], "reused": True}


def command_status(conn: sqlite3.Connection, workflow_id: str, request_id: str) -> dict:
    _loaded(conn, workflow_id)
    receipt = _receipt(conn, workflow_id, request_id)
    if receipt is None:
        return {"state": "not_found", "request_id": request_id}
    final = receipt["finished"]
    if final is None:
        return {"state": "unresolved", "request_id": request_id}
    return {"state": "accepted" if final["type"] == "StudioCommandFinished" else "rejected",
            "request_id": request_id, "result": final.get("result"), "error": final.get("error")}


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def execute_command(conn: sqlite3.Connection, workflow_id: str, payload: dict) -> dict:
    """Fence/record a manual intent, then call exactly one existing command."""
    from video_factory.application import operator_handler as handlers

    receipt = command_receipt(conn, workflow_id, payload)
    if receipt:
        return receipt
    conn.execute("BEGIN IMMEDIATE")
    try:
        _agg, version = _loaded(conn, workflow_id)
        if version != payload["expected_version"]:
            raise ValueError("Workflow đã thay đổi; làm mới trước khi chạy lại")
        actions = workflow_actions(conn, workflow_id)
        selected = next((a for a in actions["options"] if a["id"] == payload["action_id"] and a["enabled"]), None)
        if not selected:
            raise ValueError("Thao tác không còn hợp lệ; làm mới để xem command hiện có")
        _event(conn, workflow_id, payload["request_id"], "Started", {"fingerprint": _fingerprint(payload), "action_id": selected["id"], "expected_version": version, "note": payload["note"]})
        conn.execute("INSERT INTO processed_command(workflow_id,command_id) VALUES(?,?)", (workflow_id, f"studio:{payload['request_id']}"))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    try:
        key = selected["id"]
        params = selected["params"]
        if key == "recover-creative":
            result = handlers.recover_creative_stage(conn, workflow_id=workflow_id, owner_user_id="local-owner", note=payload["note"], expected_version=version)
        elif key == "resume-brief":
            result = handlers.resume_quarantined_creative_brief(conn, workflow_id=workflow_id, owner_user_id="local-owner", expected_version=version)
        elif key.startswith("recover-keyframe:"):
            result = handlers.recover_unknown_keyframe_submission(conn, workflow_id=workflow_id, owner_user_id="local-owner", operation_id=params["operation_id"], expected_version=version)
        elif key == "recover-keyframe-qc":
            result = handlers.resume_storyboard_after_qc_authorization(conn, workflow_id=workflow_id, owner_user_id="local-owner", expected_version=version)
        elif key.startswith("retry-scene:"):
            from video_factory.integrations.scene_renderer import (
                SceneRenderOperationStore,
            )
            result = handlers.resume_video_scene_after_qc_failure(conn, SceneRenderOperationStore(conn), workflow_id=workflow_id, owner_user_id="local-owner", scene_order=params["scene_order"], expected_version=version)
        elif key == "approve":
            result = handlers.approve_and_advance_workflow(conn, target_id=workflow_id, owner_user_id="local-owner", expected_version=version)
        elif key.startswith("retry-job:"):
            from video_factory.workflows.outbox_repository import OutboxRepository
            with conn:
                if not OutboxRepository(conn).requeue_quarantined_in_transaction(params["outbox_id"], preserve_attempts=True):
                    raise ValueError("Job đã đổi hoặc hết số lần thử; không reset bộ đếm")
            result = {"outbox_id": params["outbox_id"], "status": "requeued"}
        elif key.startswith("run-pending:"):
            result = {"status": "pending_job_preserved"}
        else:
            raise ValueError("Command chưa có handler được cho phép")
    except (ValueError, KeyError) as exc:
        with conn:
            _event(conn, workflow_id, payload["request_id"], "Rejected", {"error": str(exc)})
        raise
    result = {"ok": True, "workflow_id": workflow_id, "action_id": key, "command": selected["command"], "status": "accepted", "reused": False, "result": result}
    with conn:
        _event(conn, workflow_id, payload["request_id"], "Finished", {"result": result})
    return result
