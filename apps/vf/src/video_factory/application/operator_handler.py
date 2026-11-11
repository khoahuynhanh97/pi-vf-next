from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable

from dataclasses import dataclass, field

@dataclass
class OperatorCommand:
    command_type: str
    target_id: str
    owner_user_id: str = "default_owner"
    payload: dict[str, Any] = field(default_factory=dict)
    expected_version: int | None = None
    idempotency_key: str | None = None

from video_factory.integrations.ports import OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS
from video_factory.integrations.scene_renderer import SceneRenderOperationStore
from video_factory.workflow import (
    HITL_WAITING_STATES,
    ProductVideoWorkflowState,
    transition,
)
from video_factory.handlers import start_product_video_workflow
from video_factory.qc.image_qc import compute_artifact_digest
from video_factory.qc.fingerprint import (
    IMAGE_POLICY_VERSION,
    KEYFRAME_POLICY_VERSION,
    VIDEO_POLICY_VERSION,
    minimum_pass_score,
)
from video_factory.qc.models import QCStage, QCVerdict
from video_factory.qc.normalization import _media_manifest
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.video_qc import compute_file_sha256
from video_factory.workflows.approvals import ApprovalService, ApprovalStatus
from video_factory.workflows.envelope import build_envelope
from video_factory.workflows.repository import WorkflowRepository


def inspect_quarantined_creative_brief(
    connection: sqlite3.Connection, *, workflow_id: str, inbox_root: str | Path | None = None,
) -> dict[str, Any]:
    """Allow only the known pre-generation reference-path failure to resume in place."""
    from datetime import date
    from video_factory.automation.importer import ExchangeImporter, get_default_exchange_inbox

    def unavailable(reason: str, **details: Any) -> dict[str, Any]:
        return {"quarantined": True, "recovery_available": False, "reason": reason, **details}

    loaded = WorkflowRepository(connection).get_workflow(workflow_id)
    if loaded is None:
        return unavailable("Không tìm thấy workflow.")
    aggregate, version = loaded
    rows = connection.execute(
        "SELECT outbox_id, message, state, attempt_count, max_attempts, last_error, "
        "lease_token, lease_owner, lease_expires_at FROM workflow_outbox "
        "WHERE workflow_id=? ORDER BY outbox_id DESC", (workflow_id,),
    ).fetchall()
    if not rows:
        return {"quarantined": False, "recovery_available": False, "reason": "Không có job outbox."}
    job = rows[0]
    try:
        message = json.loads(job["message"])
        payload = message.get("payload") or {}
    except (TypeError, json.JSONDecodeError):
        return unavailable("Job bị lỗi định dạng, cần kiểm tra thủ công.", outbox_id=job["outbox_id"])
    job_name = str(payload.get("job_name") or "")
    message_id = str(message.get("message_id") or "")
    base = {"outbox_id": job["outbox_id"], "message_id": message_id, "job_name": job_name,
            "attempt_count": job["attempt_count"], "max_attempts": job["max_attempts"],
            "step": "CREATIVE_BRIEF", "error": str(job["last_error"] or "")}

    recovery_events = []
    for event in connection.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='CreativeStageRecovered'",
        (workflow_id,),
    ):
        try:
            data = json.loads(event[0])
            if data.get("outbox_id") == job["outbox_id"]:
                recovery_events.append(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    if job["state"] == "pending" and recovery_events:
        return {"quarantined": True, "recovery_available": False, "status": "already_queued",
                "reason": "Job đã được đưa tiếp tục; đang chờ worker xử lý.", **base}
    if job["state"] == "leased" or job["lease_token"] or job["lease_owner"]:
        return unavailable("Job đang được worker giữ lease; không gửi lại.", status="running", **base)
    if aggregate.get("state") != ProductVideoWorkflowState.CREATING_BRIEF.value:
        return unavailable("Workflow không còn ở bước tạo brief.", **base)
    if job["state"] != "quarantined" or job_name != "product_video.creative_brief":
        return {"quarantined": False, "recovery_available": False,
                "reason": "Job không thuộc trường hợp có thể tiếp tục.", **base}
    if int(job["attempt_count"] or 0) >= int(job["max_attempts"] or 0):
        return unavailable("Job đã hết số lần thử được phép.", **base)
    if any(row["state"] in {"pending", "leased"} for row in rows[1:]):
        return unavailable("Workflow đang có job khác chờ hoặc chạy.", **base)
    if payload.get("workflow_id") != workflow_id or message.get("workflow_id") != workflow_id:
        return unavailable("Workflow ID trong job không khớp.", **base)
    if (payload.get("project_id") != aggregate.get("project_id")
            or payload.get("owner_user_id") != aggregate.get("owner_user_id")):
        return unavailable("Project hoặc owner trong job không khớp workflow.", **base)
    binding_error = "MISSING_PROVIDER_CONFIG: active capability binding unavailable for video.brief.create"
    binding_failure = binding_error in str(job["last_error"] or "")
    if binding_failure:
        from video_factory.capabilities.snapshots import CapabilitySnapshotRepository
        from video_factory.mcp_adapters import _resolve_snapshot_execution_wire
        snapshot = CapabilitySnapshotRepository(connection).get_active()
        if snapshot is None or not _resolve_snapshot_execution_wire(snapshot, "video.brief.create")[0]:
            return unavailable("Provider binding tạo brief chưa được sửa.", **base)
    elif "UNTRUSTED_REFERENCE_SOURCE" not in str(job["last_error"] or ""):
        return unavailable("Nguyên nhân quarantine không phải lỗi reference đã xử lý.", **base)

    step = None
    for event in connection.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='PipelineStep' "
        "ORDER BY timestamp DESC", (workflow_id,),
    ):
        try:
            data = json.loads(event[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if data.get("step_name") == "CREATIVE_BRIEF" and data.get("operation_id") == message_id:
            step = data
            break
    if (not step or step.get("status") != "pending_operator"
            or step.get("error_code") != ("BINDING_UNAVAILABLE" if binding_failure else "TOOL_OPERATION_TERMINAL_FAILED")
            or (binding_error if binding_failure else "UNTRUSTED_REFERENCE_SOURCE") not in str(step.get("message") or "")
            or step.get("cost_status") != "not_applicable"):
        return unavailable("Thiếu bằng chứng lỗi CREATIVE_BRIEF đã kết thúc trước khi gửi provider.", **base)

    context = (aggregate.get("research_result") or {}).get("source_context") or {}
    pack_wrapper = aggregate.get("locked_pack") or {}
    pack = pack_wrapper.get("resource_pack", pack_wrapper)
    if pack_wrapper.get("pack_status") not in (None, "locked") or not pack.get("resource_pack_id"):
        return unavailable("Không tìm thấy Product Lock checkpoint hợp lệ.", **base)
    receipt = aggregate.get("latest_approval_receipt") or {}
    request_id = str(receipt.get("approval_request_id") or "")
    approval = connection.execute(
        "SELECT status, payload_json FROM approval_request WHERE request_id=?", (request_id,),
    ).fetchone() if request_id else None
    if not approval or approval["status"] != "approved":
        return unavailable("Product Lock chưa có approval receipt đã chấp nhận.", **base)
    try:
        approval_payload = json.loads(approval["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        approval_payload = {}
    if (approval_payload.get("workflow_id") != workflow_id
            or approval_payload.get("project_id") != aggregate.get("project_id")
            or approval_payload.get("owner_user_id") != aggregate.get("owner_user_id")
            or approval_payload.get("gate") != "AwaitingProductLock"
            or receipt.get("gate") != "AwaitingProductLock"
            or receipt.get("project_id") != aggregate.get("project_id")
            or receipt.get("owner_user_id") != aggregate.get("owner_user_id")
            or receipt.get("artifact_id") != pack.get("resource_pack_id")
            or int(receipt.get("artifact_version") or 0) != int(pack.get("version") or 0)):
        return unavailable("Product Lock request/receipt không khớp workflow hoặc artifact.", **base)
    approval_pack = approval_payload.get("artifact_ref") or {}
    approval_pack = approval_pack.get("resource_pack", approval_pack)
    if (approval_pack.get("resource_pack_id") != pack.get("resource_pack_id")
            or int(approval_pack.get("version") or 0) != int(pack.get("version") or 0)):
        return unavailable("Product Lock approval không xác nhận đúng resource pack.", **base)
    pending_approvals = connection.execute(
        "SELECT COUNT(*) FROM approval_request WHERE status='pending' "
        "AND json_extract(payload_json, '$.workflow_id')=?", (workflow_id,),
    ).fetchone()[0]
    if pending_approvals:
        return unavailable("Workflow còn approval request chưa được giải quyết.", **base)

    for table in ("product_video_operation_attempt", "video_factory_keyframe_operation"):
        try:
            if connection.execute(f"SELECT 1 FROM {table} WHERE workflow_id=? LIMIT 1", (workflow_id,)).fetchone():
                return unavailable("Workflow đã có operation generation; cần đối soát checkpoint trước khi tiếp tục.", **base)
        except sqlite3.OperationalError:
            pass
    try:
        if connection.execute(
            "SELECT 1 FROM video_factory_scene_operation WHERE project_id=? LIMIT 1",
            (aggregate.get("project_id"),),
        ).fetchone():
            return unavailable("Project đã có scene operation; cần đối soát checkpoint trước khi tiếp tục.", **base)
    except sqlite3.OperationalError:
        pass

    inbox = Path(inbox_root or os.environ.get("HERMES_EXCHANGE_INBOX") or get_default_exchange_inbox()).resolve()
    source_date = str(context.get("source_date") or "")
    source_batch_id = str(context.get("source_batch_id") or "")
    source_file_raw = str(context.get("source_file") or "")
    if not source_date or not source_batch_id or not source_file_raw:
        return unavailable("Thiếu binding PI batch/source manifest.", **base)
    source_file = Path(source_file_raw).resolve()
    try:
        batch_dir = ExchangeImporter(inbox).find_latest_ready_batch(date.fromisoformat(source_date))
        manifest = ExchangeImporter(inbox).load_manifest(batch_dir)
    except Exception as exc:
        return unavailable(f"Không đọc được READY batch đã pin: {exc}", **base)
    if (manifest.batch_id != source_batch_id or batch_dir.name != source_batch_id
            or source_file != (batch_dir / "manifest.json").resolve()):
        return unavailable("READY batch hiện tại không khớp batch đã pin; không đổi batch tự động.", **base)
    product_key = str(context.get("product_key") or "")
    canonical_key = str(context.get("canonical_product_key") or "")
    product = next((item for item in manifest.products if item.product_key == product_key
                    and item.canonical_product_key == canonical_key), None)
    if (not product or product.brand != context.get("brand") or product.model != context.get("model")
            or product.variant != context.get("variant")):
        return unavailable("Danh tính sản phẩm trong READY không khớp Product Lock đã pin.", **base)
    manifest_images = {item.asset_id: item.relative_path for item in product.images}
    source_images = {str(item.get("asset_id") or ""): item for item in context.get("images", [])
                     if isinstance(item, dict)}
    payload_pack = ((payload.get("args") or [None, {}])[1] or {}).get("resource_pack") or {}
    if payload_pack.get("resource_pack_id") != pack.get("resource_pack_id"):
        return unavailable("CREATIVE_BRIEF job không mang Product Lock artifact đã xác nhận.", **base)
    expected_assets = pack.get("assets") or []
    if not expected_assets:
        return unavailable("Product Lock không có reference ảnh.", **base)
    verified_shas = []
    for asset in expected_assets:
        asset_id = str(asset.get("asset_id") or "")
        source = source_images.get(asset_id)
        relative_path = manifest_images.get(asset_id)
        if not source or not relative_path or source.get("relative_path") != relative_path:
            return unavailable("Reference trong Product Lock không khớp manifest PI.", **base)
        path = Path(str(source.get("absolute_path") or "")).resolve()
        try:
            path.relative_to(inbox)
        except ValueError:
            return unavailable("Ảnh nguồn nằm ngoài inbox PI đang cấu hình.", **base)
        expected_sha = str(source.get("sha256") or asset.get("sha256") or "").removeprefix("sha256:").lower()
        if (not path.is_file() or not expected_sha
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha
                or str(asset.get("sha256") or "").removeprefix("sha256:").lower() != expected_sha):
            return unavailable("Ảnh nguồn thiếu hoặc SHA không còn khớp; thay ảnh rồi kiểm tra lại.", **base)
        payload_assets = {str(item.get("asset_id") or ""): item for item in payload_pack.get("assets", [])}
        payload_asset = payload_assets.get(asset_id) or {}
        if (str(payload_asset.get("sha256") or "").removeprefix("sha256:").lower() != expected_sha
                or Path(str(payload_asset.get("absolute_path") or payload_asset.get("local_path") or "")).resolve() != path):
            return unavailable("Reference trong job không khớp SHA/path đã pin.", **base)
        verified_shas.append(expected_sha)

    return {"quarantined": True, "recovery_available": True,
            "reason": "Ảnh nguồn hiện nằm trong inbox đã cấu hình và khớp manifest/SHA; có thể tiếp tục job hiện có.",
            "status": "quarantined", "workflow_id": workflow_id, "workflow_version": version,
            "project_id": aggregate.get("project_id"), "product_key": product_key,
            "target_date": manifest.production_date, "batch_id": manifest.batch_id,
            "qc_level": (aggregate.get("creative_policy") or {}).get("qc_level", "middle"),
            "reference_sha256s": verified_shas, **base}


def resume_quarantined_creative_brief(
    connection: sqlite3.Connection, *, workflow_id: str, inbox_root: str | Path | None = None,
    owner_user_id: str = "local-owner",
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Audit and requeue the same quarantined CREATIVE_BRIEF message without resetting attempts."""
    from uuid import uuid4
    from video_factory.workflows.outbox_repository import OutboxRepository

    inspection = inspect_quarantined_creative_brief(connection, workflow_id=workflow_id, inbox_root=inbox_root)
    if inspection.get("status") == "already_queued":
        return {"status": "already_queued", **inspection}
    if not inspection.get("recovery_available"):
        raise ValueError(str(inspection.get("reason") or "Workflow cannot be resumed safely."))
    loaded = WorkflowRepository(connection).get_workflow(workflow_id)
    if loaded is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = loaded
    if expected_version is not None and version != expected_version:
        raise ValueError("Workflow đã thay đổi trước khi phục hồi brief")
    outbox_id = int(inspection["outbox_id"])
    previous_error = str(connection.execute(
        "SELECT last_error FROM workflow_outbox WHERE outbox_id=?", (outbox_id,),
    ).fetchone()[0] or "")
    now = datetime.now(timezone.utc).isoformat()
    token = f"{outbox_id}_{inspection['attempt_count']}"
    with connection:
        WorkflowRepository(connection).commit_workflow_in_transaction(
            workflow_id, json.dumps(aggregate, ensure_ascii=False), [{
                "event_id": f"evt_creative_recovery_{uuid4().hex}",
                "command_id": f"resume_quarantined_brief:{workflow_id}:{token}",
                "event_type": "CreativeStageRecovered", "timestamp": now,
                "payload": {
                    "stage": "direction", "job_name": inspection["job_name"],
                    "outbox_id": outbox_id, "message_id": inspection["message_id"],
                    "previous_state": "quarantined", "previous_error": previous_error,
                    "reason": "Provider binding/reference corrected; READY input and SHA revalidated",
                    "attempt_count_before": inspection["attempt_count"],
                    "attempt_count_preserved": True,
                    "reference_sha256s": inspection["reference_sha256s"],
                    "batch_id": inspection["batch_id"],
                    "operator_id": owner_user_id,
                },
            }], expected_version=version,
        )
        if not OutboxRepository(connection).requeue_quarantined_in_transaction(
            outbox_id, preserve_attempts=True,
        ):
            raise ValueError("Quarantined job changed before it could be resumed; refresh Studio and retry.")
    return {"status": "recovered", "workflow_id": workflow_id, "outbox_id": outbox_id,
            "message_id": inspection["message_id"], "workflow_version": version + 1,
            "attempt_count": inspection["attempt_count"], "product_key": inspection["product_key"],
            "target_date": inspection["target_date"], "batch_id": inspection["batch_id"],
            "qc_level": inspection["qc_level"]}


def resume_video_scene_after_qc_failure(
    connection: sqlite3.Connection,
    scene_store: SceneRenderOperationStore,
    *,
    workflow_id: str,
    owner_user_id: str,
    scene_order: int,
    provider_kind: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Retry one QC-failed scene and resume the approved render batch."""
    repo = WorkflowRepository(connection)
    loaded = repo.get_workflow(workflow_id)
    if loaded is None:
        raise KeyError(f"Workflow {workflow_id!r} not found")
    aggregate, version = loaded
    if expected_version is not None and version != expected_version:
        raise ValueError("Workflow đã thay đổi trước khi phục hồi scene")
    if aggregate.get("owner_user_id") != owner_user_id:
        raise ValueError("Workflow belongs to another owner")
    if (
        aggregate.get("state") != ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
        or aggregate.get("operator_stage") != QCStage.VIDEO.value
        or aggregate.get("operator_action") not in {"qc_retry_authorization", "qc_authorization"}
    ):
        raise ValueError("Workflow is not waiting for a failed Video QC scene retry")
    report_id = str(aggregate.get("operator_qc_report_id") or "")
    report = QCReportRepository(connection).get_report(report_id)
    render_result = aggregate.get("render_result") or {}
    batch_id = str(render_result.get("render_id") or "")
    if (
        not report
        or report.stage != QCStage.VIDEO
        or report.verdict != QCVerdict.FAIL
        or report.artifact_digest != str(render_result.get("sha256") or "").removeprefix("sha256:")
        or not batch_id
    ):
        raise ValueError("Failed Video QC report is not bound to the current render")
    failures = [
        item for item in report.findings.get("visual_scenes", [])
        if isinstance(item, dict) and item.get("verdict") == "fail"
    ]
    if len(failures) != 1 or failures[0].get("scene_number") != scene_order:
        raise ValueError("Requested scene does not uniquely match the failed Video QC scene")
    rows = scene_store.connection.execute(
        "SELECT * FROM video_factory_scene_operation WHERE render_batch_id = ? AND scene_order = ? "
        "ORDER BY attempt DESC",
        (batch_id, scene_order),
    ).fetchall()
    if not rows:
        raise ValueError("Failed scene operation is missing")
    old_operation = dict(rows[0])
    if old_operation["status"] != "completed":
        raise ValueError("Failed scene has no completed clip to retry")
    storyboard = aggregate.get("storyboard")
    approval = aggregate.get("latest_approval_receipt")
    authorization = aggregate.get("qc_authorization")
    if (
        not isinstance(storyboard, dict)
        or not isinstance(approval, dict)
        or approval.get("gate") != ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
        or not isinstance(authorization, dict)
    ):
        raise ValueError("Approved storyboard or scoped authorization is missing")
    retry = scene_store.retry_qc_failed_scene(
        old_operation["operation_id"], qc_report_id=report_id,
        provider_kind=provider_kind,
    )
    next_aggregate = dict(aggregate)
    next_aggregate["state"] = transition(
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
        ProductVideoWorkflowState.RENDERING,
    ).value
    next_aggregate["active_render_job_id"] = batch_id
    next_aggregate["video_retry_report_id"] = report_id
    for key in ("operator_action", "operator_stage", "operator_reason", "operator_qc_report_id"):
        next_aggregate.pop(key, None)
    outbox = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_product_video.render_qc_retry_{workflow_id}_a{retry['attempt']}_v{version + 1}",
        payload={
            "job_name": "product_video.render",
            "workflow_id": workflow_id,
            "project_id": aggregate.get("project_id"),
            "args": [storyboard],
            "kwargs": {
                "owner_user_id": owner_user_id,
                "project_id": aggregate.get("project_id"),
                "approval_receipt": approval,
                "qc_authorization": authorization,
                "qc_max_cost_usd": aggregate.get("qc_max_cost_usd"),
                "render_params": {
                    "active_job_id": batch_id,
                    **({"provider_kind": provider_kind} if provider_kind else {}),
                },
            },
            "owner_user_id": owner_user_id,
        },
    )
    with connection:
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=[{
                "event_id": f"evt_video_qc_retry_{workflow_id}_v{version + 1}",
                "command_id": f"cmd_video_qc_retry_{workflow_id}_v{version + 1}",
                "event_type": "VideoSceneRetryAuthorized",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"qc_report_id": report_id, "scene_order": scene_order, "operation_id": retry["operation_id"]},
            }],
            outbox_message=outbox,
            expected_version=version,
        )
    return {"workflow_id": workflow_id, "scene_operation_id": retry["operation_id"], "render_batch_id": batch_id}


def rebuild_storyboard_after_video_qc_failure(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str,
    frame_feedback: dict[str, str],
) -> dict[str, Any]:
    """Regenerate specified keyframes after a video failure traces to approved anchors."""
    repo = WorkflowRepository(connection)
    loaded = repo.get_workflow(workflow_id)
    if loaded is None:
        raise KeyError(f"Workflow {workflow_id!r} not found")
    aggregate, version = loaded
    if aggregate.get("owner_user_id") != owner_user_id:
        raise ValueError("Workflow belongs to another owner")
    if (
        aggregate.get("state") != ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
        or aggregate.get("operator_stage") != QCStage.VIDEO.value
        or aggregate.get("operator_action") not in {"qc_retry_authorization", "qc_authorization"}
    ):
        raise ValueError("Workflow is not waiting for Video QC recovery")
    render = aggregate.get("render_result") or {}
    report = QCReportRepository(connection).get_latest_report(
        workflow_id, QCStage.VIDEO,
        expected_artifact_digest=str(render.get("sha256") or "").removeprefix("sha256:"),
    )
    if report is None or report.verdict != QCVerdict.FAIL:
        raise ValueError("Current render has no failed Video QC report")
    storyboard = aggregate.get("storyboard")
    scene_plan = aggregate.get("scene_plan")
    if not isinstance(storyboard, dict) or not isinstance(scene_plan, dict):
        raise ValueError("Approved storyboard and Scene Plan are required")
    approved_ids = {
        str(frame.get("frame_id")) for frame in storyboard.get("generated_keyframes", [])
        if isinstance(frame, dict)
    }
    if not frame_feedback or any(
        frame_id not in approved_ids or not frame_id.endswith("-end") or not feedback.strip()
        for frame_id, feedback in frame_feedback.items()
    ):
        raise ValueError("Recovery requires specific approved END frames and feedback")
    attempt = int(aggregate.get("keyframe_recovery_attempt", 0) or 0) + 1
    recovery_plan = {
        "frames": [
            {"frame_id": frame_id, "scene_id": frame_id.removesuffix("-end"),
             "actions": ["regenerate_keyframe"], "feedback": feedback}
            for frame_id, feedback in sorted(frame_feedback.items())
        ],
        "source_video_qc_report_id": report.report_id,
    }
    next_aggregate = dict(aggregate)
    next_aggregate["state"] = transition(
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
        ProductVideoWorkflowState.CREATING_STORYBOARD,
    ).value
    next_aggregate["keyframe_recovery_plan"] = recovery_plan
    next_aggregate["keyframe_recovery_attempt"] = attempt
    for key in ("operator_action", "operator_stage", "operator_reason", "operator_qc_report_id"):
        next_aggregate.pop(key, None)
    auth = aggregate.get("qc_authorization")
    if not isinstance(auth, dict):
        raise ValueError("Scoped QC authorization is required")
    outbox = build_envelope(
        workflow_id=workflow_id, topic="job.dispatch",
        idempotency_key=f"idemp_product_video.storyboard_video_recovery_{workflow_id}_a{attempt}_v{version + 1}",
        payload={
            "job_name": "product_video.storyboard", "workflow_id": workflow_id,
            "project_id": aggregate.get("project_id"), "args": [scene_plan],
            "kwargs": {
                "project_id": aggregate.get("project_id"), "owner_user_id": owner_user_id,
                "qc_authorization": auth, "qc_max_cost_usd": aggregate.get("qc_max_cost_usd"),
                "recovery_plan": recovery_plan, "recovery_attempt": attempt,
                "previous_storyboard": storyboard,
            },
            "owner_user_id": owner_user_id,
        },
    )
    with connection:
        repo.commit_workflow(
            workflow_id=workflow_id, aggregate_json=json.dumps(next_aggregate),
            events=[{
                "event_id": f"evt_video_anchor_recovery_{workflow_id}_v{version + 1}",
                "command_id": f"cmd_video_anchor_recovery_{workflow_id}_v{version + 1}",
                "event_type": "StoryboardRecoveryFromVideoQC",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"qc_report_id": report.report_id, "frame_ids": sorted(frame_feedback)},
            }],
            outbox_message=outbox, expected_version=version,
        )
    return {"workflow_id": workflow_id, "recovery_attempt": attempt, "frame_ids": sorted(frame_feedback)}


def _file_url_to_path(value: str) -> Path | None:
    if not value.startswith("file://"):
        return None
    raw = value.removeprefix("file://")
    if raw.startswith("/") and len(raw) > 3 and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


def _current_qc_digest(stage: QCStage, artifact: dict[str, Any]) -> str:
    if stage == QCStage.IMAGE:
        raw = artifact.get("resource_pack", artifact)
        return compute_artifact_digest(raw)
    if stage == QCStage.KEYFRAME:
        raw = dict(artifact)
        raw.pop("keyframe_qc_receipt", None)
        return str(raw.get("package_digest") or compute_artifact_digest(raw))

    sha = artifact.get("sha256") or artifact.get("digest") or artifact.get("artifact_digest")
    if sha:
        return str(sha).removeprefix("sha256:")
    url = artifact.get("video_url") or artifact.get("final_video_url") or artifact.get("file_path")
    if isinstance(url, str):
        path = _file_url_to_path(url) if url.startswith("file://") else Path(url)
        digest = compute_file_sha256(path)
        if digest:
            return digest
    return compute_artifact_digest(artifact)


def _require_current_pass_qc(
    connection: sqlite3.Connection,
    workflow_id: str,
    stage: QCStage,
    artifact: dict[str, Any],
    provider_mode: str | None = None,
) -> None:
    has_workflow_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow'"
    ).fetchone()
    workflow_row = (connection.execute(
        "SELECT aggregate_json FROM workflow WHERE workflow_id=?", (workflow_id,)
    ).fetchone() if has_workflow_table else None)
    if workflow_row and stage in {QCStage.IMAGE, QCStage.KEYFRAME, QCStage.VIDEO}:
        policy = json.loads(workflow_row[0]).get("creative_policy") or {}
        if policy.get("qc_level", "middle") == "low":
            return
    artifact_digest = _current_qc_digest(stage, artifact)
    report = QCReportRepository(connection).get_latest_report(
        workflow_id,
        stage,
        expected_artifact_digest=artifact_digest,
    )
    if report is None:
        raise ValueError(
            f"QC report required before approval: no current {stage.value} QC report "
            f"matches artifact digest {artifact_digest}"
        )
    reuses_final_review = (
        stage == QCStage.VIDEO
        and report.findings.get("content_evidence_source") == "creative_final_review"
    )
    technical_only_final = (
        stage == QCStage.VIDEO and report.findings.get("technical_only_final") is True
        and (report.findings.get("technical_specs") or {}).get("technical_pass") is True
    )
    if report.verdict != QCVerdict.PASS or (
        report.score < minimum_pass_score(stage) and not (reuses_final_review or technical_only_final)
    ):
        raise ValueError(
            f"QC report required before approval: latest {stage.value} QC report "
            f"{report.report_id} is {report.verdict.value} with score {report.score} "
            f"(minimum {minimum_pass_score(stage)} PASS required)"
        )

    # Historical seeded reports remain readable for offline recovery fixtures,
    # but they cannot unlock a real artifact with media or a render output.
    legacy_fixture = (
        report.findings.get("seeded_for") and not report.findings.get("automatic")
    ) or (
        report.evaluator_name in {"test", "test_seeded_qc"}
        and not (artifact.get("assets") or artifact.get("media_assets") or artifact.get("product_references"))
    )
    if legacy_fixture:
        return
    allowed_modes = {"configured", provider_mode} if provider_mode else {"configured"}
    if report.provider_mode not in allowed_modes or report.findings.get("automatic") is False:
        raise ValueError("Configured automatic QC report is required; fake/manual summaries cannot unlock a gate")
    expected_policy = {
        QCStage.IMAGE: IMAGE_POLICY_VERSION,
        QCStage.KEYFRAME: KEYFRAME_POLICY_VERSION,
        QCStage.VIDEO: VIDEO_POLICY_VERSION,
    }[stage]
    if report.policy_version != expected_policy:
        raise ValueError(f"QC report {report.report_id} uses stale policy {report.policy_version!r}")

    if stage == QCStage.VIDEO and (reuses_final_review or technical_only_final):
        from video_factory.qc.validation import require_current_pass_report
        require_current_pass_report(
            QCReportRepository(connection), workflow_id=workflow_id, stage=stage,
            artifact=artifact, owner_user_id=report.owner_user_id,
            project_id=report.project_id, provider_mode="configured",
        )

    if stage == QCStage.IMAGE:
        raw = artifact.get("resource_pack", artifact) if isinstance(artifact, dict) else {}
        if report.provider_mode == "configured":
            from video_factory.qc.validation import require_current_pass_report
            require_current_pass_report(
                QCReportRepository(connection), workflow_id=workflow_id, stage=stage,
                artifact=raw, owner_user_id=report.owner_user_id,
                project_id=report.project_id, provider_mode="configured",
            )
        manifest = {item["asset_id"]: item for item in _media_manifest(raw)}
        items = report.findings.get("items", [])
        eligible = [item for item in items if isinstance(item, dict) and item.get("eligible")]
        if not eligible:
            raise ValueError("Image QC requires at least one usable reference")
        if len(eligible) != int(report.findings.get("eligible_count", -1)):
            raise ValueError("Image QC eligible manifest count is inconsistent")
        for item in eligible:
            current = manifest.get(str(item.get("asset_id")))
            item_digest = item.get("digest") or item.get("sha256")
            if not current or not current.get("eligible") or current.get("sha256") != item_digest:
                raise ValueError("Image QC reference manifest is stale or no longer eligible")
            if item.get("verdict") != QCVerdict.PASS.value or item.get("hard_failure"):
                raise ValueError("Image QC contains an ineligible or failed reference")
    elif stage == QCStage.KEYFRAME:
        frames = artifact.get("generated_keyframes", [])
        items = report.findings.get("items", [])
        if not frames or len(items) != len(frames):
            raise ValueError("Keyframe QC must cover every generated keyframe")
        if report.score < minimum_pass_score(QCStage.KEYFRAME) or any(
            item.get("verdict") != QCVerdict.PASS.value
            or item.get("hard_failure")
            or int(item.get("score", 0)) < minimum_pass_score(QCStage.KEYFRAME)
            for item in items
            if isinstance(item, dict)
        ):
            raise ValueError("Keyframe QC contains a failed or unknown frame, or score is below 60")
    else:
        findings = report.findings
        tech = findings.get("technical_specs", {})
        if findings.get("technical_only_final") is True:
            if not tech.get("technical_pass"):
                raise ValueError("Technical-only final QC must contain a current technical PASS")
            return
        coverage = findings.get("sample_coverage", {})
        scenes = findings.get("visual_scenes", [])
        expected_samples = int(coverage.get("expected_samples", 0))
        samples_per_scene = int(coverage.get("samples_per_scene", 0))
        if not tech.get("technical_pass") or not coverage.get("coverage_valid") or len(scenes) == 0:
            raise ValueError("Video QC requires technical PASS and complete scene coverage")
        if (
            samples_per_scene < 1
            or expected_samples != len(scenes) * samples_per_scene
            or int(coverage.get("total_samples", -1)) != expected_samples
        ):
            raise ValueError("Video QC sample coverage is incomplete")
        if (not reuses_final_review and report.score < 80) or any(
            scene.get("verdict") != QCVerdict.PASS.value
            or scene.get("hard_failure")
            or (not reuses_final_review and int(scene.get("score", 0)) < 80)
            or int(scene.get("samples_extracted", 0)) != samples_per_scene
            for scene in scenes
            if isinstance(scene, dict)
        ):
            raise ValueError("Video QC contains a failed, unknown, or incompletely sampled scene, or score is below 80")


def approve_and_advance_workflow(
    connection: sqlite3.Connection,
    target_id: str,
    owner_user_id: str = "local-owner",
    payload: dict[str, Any] | None = None,
    expected_version: int | None = None,
    provider_mode: str | None = None,
) -> dict[str, Any]:
    """Approve an approval request AND advance the workflow state and outbox queue atomically."""
    effective_provider_mode = provider_mode or (payload.get("provider_mode") if isinstance(payload, dict) else None)
    approval_service = ApprovalService(connection)
    repo = WorkflowRepository(connection)

    # 1. Resolve workflow & approval request
    try:
        req = approval_service.get_request(target_id)
        is_approval_req = True
    except KeyError:
        is_approval_req = False

    if is_approval_req:
        wf_id = req.payload.get("workflow_id") or target_id
        wf_data = repo.get_workflow(wf_id)
        if wf_data is None:
            raise KeyError(f"Workflow '{wf_id}' not found")
        agg, current_version = wf_data
    else:
        wf_data = repo.get_workflow(target_id)
        if wf_data is None:
            raise KeyError(f"Approval request or workflow '{target_id}' not found")
        agg, current_version = wf_data
        wf_id = target_id
        req_id = agg.get("pending_approval_id")
        if not req_id:
            raise KeyError(f"No pending approval for workflow '{wf_id}'")
        req = approval_service.get_request(req_id)

    # 2. Check workflow version if expected_version is provided
    if expected_version is not None and expected_version != current_version:
        raise ValueError(f"Stale workflow version: expected {expected_version}, current {current_version}")

    # Check approval request matches aggregate's pending request
    if req.request_id != agg.get("pending_approval_id"):
        raise ValueError("Approval request ID does not match aggregate pending_approval_id")

    # Check owner matching
    req_owner = req.payload.get("owner_user_id") or req.payload.get("owner_id")
    if not req_owner:
        raise ValueError("Approval request missing owner_user_id")
    if req_owner != owner_user_id:
        raise ValueError(f"Owner mismatch: approval request belongs to '{req_owner}', not '{owner_user_id}'")
    agg_owner = agg.get("owner_user_id") or agg.get("owner_id")
    if agg_owner and req_owner != agg_owner:
        raise ValueError(f"Owner mismatch: approval request belongs to '{req_owner}', aggregate belongs to '{agg_owner}'")

    # Check project matching
    req_project = req.payload.get("project_id")
    agg_project = agg.get("project_id")
    if not req_project:
        raise ValueError("Approval request missing project_id")
    if agg_project and req_project != agg_project:
        raise ValueError(f"Project mismatch: approval request has '{req_project}', expected '{agg_project}'")

    # Check artifact ID and version matching
    latest_artifact = agg.get("latest_artifact")
    if isinstance(latest_artifact, dict):
        expected_art_id = (
            latest_artifact.get("artifact_id")
            or latest_artifact.get("brief_id")
            or latest_artifact.get("scene_plan_id")
            or latest_artifact.get("storyboard_id")
            or latest_artifact.get("render_id")
            or latest_artifact.get("export_id")
            or latest_artifact.get("resource_pack_id")
            or latest_artifact.get("id")
        )
        expected_art_ver = latest_artifact.get("version") or latest_artifact.get("resource_pack_version")

        req_art_id = req.payload.get("artifact_id")
        req_art_ver = req.payload.get("artifact_version")

        if expected_art_id:
            if not req_art_id:
                raise ValueError(f"Approval request missing artifact_id (expected '{expected_art_id}')")
            if req_art_id != expected_art_id:
                raise ValueError(f"Artifact ID mismatch: approval request has '{req_art_id}', expected '{expected_art_id}'")
        if expected_art_ver is not None:
            if req_art_ver is None:
                raise ValueError(f"Approval request missing artifact_version (expected {expected_art_ver})")
            if req_art_ver != expected_art_ver:
                raise ValueError(f"Artifact version mismatch: approval request has {req_art_ver}, expected {expected_art_ver}")

    # 3. Check HITL waiting state
    curr_enum = ProductVideoWorkflowState(agg.get("state"))
    if curr_enum not in HITL_WAITING_STATES:
        raise ValueError(f"Workflow {wf_id} state {curr_enum.value} is not in HITL_WAITING_STATES")

    # 4. Validate ALL 5 bindings before any mutation
    if req.request_id != agg.get("pending_approval_id"):
        raise ValueError("Approval request ID does not match aggregate pending_approval_id")

    if req.payload.get("workflow_id") != wf_id:
        raise ValueError("Approval request workflow_id mismatch")

    req_gate = req.payload.get("gate")
    agg_gate = agg.get("pending_approval_gate")
    if req_gate != agg_gate:
        raise ValueError("Approval request gate mismatch")

    if req.payload.get("workflow_version") != current_version:
        pr1_auto_preparation = (
            payload
            and payload.get("automation_actor") == "creative_auto_policy"
            and (agg.get("creative_policy") or {}).get("creative_mode") == "four_agent"
            and (agg.get("creative_policy") or {}).get("preparation_only") is True
        )
        if not (payload and payload.get("automation_actor") == "creative_auto_policy"
                and (agg.get("production_run_id") or pr1_auto_preparation)):
            raise ValueError("Approval request workflow_version mismatch")

    if req.status != ApprovalStatus.PENDING:
        raise ValueError(f"Approval request {req.request_id} is already {req.status.value}")

    # 5. Determine next state & next job
    if curr_enum == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK:
        next_enum = ProductVideoWorkflowState.LOCKING_RESOURCE_PACK
        job_name = "product_video.lock"
        gate_name = "AwaitingProductLock"
    elif curr_enum == ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL:
        next_enum = ProductVideoWorkflowState.CREATING_SCENE_PLAN
        job_name = "product_video.scene_plan"
        gate_name = "AwaitingBriefApproval"
    elif curr_enum == ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL:
        next_enum = ProductVideoWorkflowState.CREATING_STORYBOARD
        job_name = "product_video.storyboard"
        gate_name = "AwaitingSceneApproval"
    elif curr_enum == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL:
        next_enum = ProductVideoWorkflowState.RENDERING
        job_name = "product_video.render"
        gate_name = "AwaitingStoryboardApproval"
    elif curr_enum == ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL:
        next_enum = ProductVideoWorkflowState.EXPORTING
        job_name = "product_video.export"
        gate_name = "AwaitingFinalApproval"
    else:
        raise ValueError(f"Unhandled HITL waiting state: {curr_enum}")

    if curr_enum == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK:
        if not isinstance(latest_artifact, dict):
            raise ValueError("QC report required before approval: latest resource-pack artifact is missing")
        _require_current_pass_qc(connection, wf_id, QCStage.IMAGE, latest_artifact, provider_mode=effective_provider_mode)
    elif curr_enum == ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL:
        if not isinstance(latest_artifact, dict):
            raise ValueError("QC report required before approval: latest render artifact is missing")
        if effective_provider_mode != "fake" or latest_artifact.get("video_url") or latest_artifact.get("file_path"):
            _require_current_pass_qc(connection, wf_id, QCStage.VIDEO, latest_artifact, provider_mode=effective_provider_mode)
        if latest_artifact.get('creative_mode') == 'four_agent':
            from video_factory.creative.agents.video_review import require_final_pass
            require_final_pass(latest_artifact)
    elif curr_enum == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL:
        if not isinstance(latest_artifact, dict):
            raise ValueError("QC report required before approval: storyboard package is missing")
        if effective_provider_mode != "fake" or latest_artifact.get("generated_keyframes"):
            _require_current_pass_qc(connection, wf_id, QCStage.KEYFRAME, latest_artifact, provider_mode=effective_provider_mode)

    args = [agg.get("latest_artifact")]

    # 6. Validate transition
    next_validated = transition(curr_enum, next_enum)

    # 7. SINGLE ATOMIC SQLITE TRANSACTION
    effective_project_id = req_project or agg_project
    if not effective_project_id:
        raise ValueError("Cannot determine project_id for workflow advancement")

    receipt_dict = {
        "approval_request_id": req.request_id,
        "project_id": effective_project_id,
        "gate": gate_name,
        "artifact_id": req.payload.get("artifact_id"),
        "artifact_version": req.payload.get("artifact_version"),
        "workflow_version": req.payload.get("workflow_version"),
        "owner_user_id": owner_user_id,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        'assessment_actor': 'creative_auto_policy' if (payload or {}).get('automation_actor') == 'creative_auto_policy' else 'operator',
        'human_review': 'not_performed' if (payload or {}).get('automation_actor') == 'creative_auto_policy' else 'not_recorded',
    }

    outbox_payload = {
        "job_name": job_name,
        "workflow_id": wf_id,
        "project_id": effective_project_id,
        "args": args,
        "kwargs": {
            "approval_receipt": receipt_dict,
            "approval_request_id": req.request_id,
            "project_id": effective_project_id,
            "owner_user_id": owner_user_id,
        },
        "owner_user_id": owner_user_id,
        "approval_request_id": req.request_id,
    }
    if curr_enum == ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL:
        previous_storyboard = agg.get("storyboard")
        if isinstance(previous_storyboard, dict):
            outbox_payload["kwargs"]["previous_storyboard"] = dict(previous_storyboard)
    # Preserve the explicit QC authorization across human-gated transitions;
    # it remains workflow-scoped and is revalidated by ProductVideoQCService.
    qc_authorization = (payload or {}).get("qc_authorization") or agg.get("qc_authorization")
    is_four_agent = (
        agg.get("creative_mode") == "four_agent"
        or agg.get("creative_policy", {}).get("creative_mode") == "four_agent"
        or (payload or {}).get("creative_mode") == "four_agent"
    )
    if not is_four_agent and not qc_authorization and (payload or {}).get("authorize_cost"):
        import uuid
        from datetime import timedelta
        qc_authorization = {
            "run_id": f"auth-auto-{uuid.uuid4().hex[:12]}",
            "scope": ["image", "video", "product_video"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "campaign_id": "camp-prod-video",
            "campaign_max_cost_usd": 100.0,
            "max_cost_usd": 100.0,
            "workflow_max_cost_usd": 100.0,
        }
    qc_max_cost_usd = (payload or {}).get("qc_max_cost_usd")
    if qc_max_cost_usd is None:
        qc_max_cost_usd = agg.get("qc_max_cost_usd")
    if isinstance(qc_authorization, dict):
        outbox_payload["kwargs"]["qc_authorization"] = dict(qc_authorization)
    if qc_max_cost_usd is not None:
        outbox_payload["kwargs"]["qc_max_cost_usd"] = qc_max_cost_usd
    render_provider_kind = str((payload or {}).get("render_provider_kind") or "").strip()
    if render_provider_kind:
        if curr_enum != ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL:
            raise ValueError("render_provider_kind is only valid at Storyboard approval")
        if render_provider_kind not in OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS:
            raise ValueError("render_provider_kind must be google_flow or vertex_ai")
        outbox_payload["kwargs"]["render_params"] = {"provider_kind": render_provider_kind}
    if req.payload.get("artifact_id"):
        outbox_payload["artifact_id"] = req.payload.get("artifact_id")
    if req.payload.get("artifact_version") is not None:
        outbox_payload["artifact_version"] = req.payload.get("artifact_version")
    if req.payload.get("gate"):
        outbox_payload["gate"] = req.payload.get("gate")

    outbox_msg = build_envelope(
        workflow_id=wf_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_{job_name}_{wf_id}_v{current_version + 1}",
        payload=outbox_payload,
    )

    next_agg = dict(agg)
    next_agg["state"] = next_validated.value
    next_agg["project_id"] = effective_project_id
    next_agg["latest_approval_receipt"] = {
        "approval_request_id": req.request_id,
        "project_id": effective_project_id,
        "gate": gate_name,
        "artifact_id": req.payload.get("artifact_id"),
        "artifact_version": req.payload.get("artifact_version"),
        "workflow_version": req.payload.get("workflow_version"),
        "owner_user_id": owner_user_id,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(qc_authorization, dict):
        next_agg["qc_authorization"] = dict(qc_authorization)
    if qc_max_cost_usd is not None:
        next_agg["qc_max_cost_usd"] = qc_max_cost_usd
    if render_provider_kind:
        next_agg["selected_render_provider"] = render_provider_kind
    next_agg.pop("pending_approval_id", None)
    next_agg.pop("pending_approval_gate", None)

    with connection:
        cur = connection.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "UPDATE approval_request SET status = ?, updated_at = ? WHERE request_id = ?",
            (ApprovalStatus.APPROVED.value, now_iso, req.request_id),
        )
        repo.commit_workflow(
            workflow_id=wf_id,
            aggregate_json=json.dumps(next_agg),
            events=[
                {
                    "event_id": f"evt_appr_{req.request_id}",
                    "command_id": f"cmd_appr_{req.request_id}",
                    "event_type": "ApprovalGranted",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": {"gate": gate_name, "request_id": req.request_id},
                }
            ],
            outbox_message=outbox_msg,
            expected_version=current_version,
        )

    return {
        "request_id": req.request_id,
        "status": "approved",
        "workflow_id": wf_id,
        "new_state": next_validated.value,
        "enqueued_job": job_name,
    }


def revise_brief_and_request_approval(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str,
    revised_brief: dict[str, Any],
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Replace the pending Gate 2 artifact and issue a new human approval.

    The caller persists the new Video Factory brief before invoking this
    coordinator.  This function owns the Hermes-side transition so the prior
    approval cannot approve a superseded brief.
    """
    repo = WorkflowRepository(connection)
    approval_service = ApprovalService(connection)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, current_version = workflow
    if expected_version is not None and expected_version != current_version:
        raise ValueError(f"Stale workflow version: expected {expected_version}, current {current_version}")
    if aggregate.get("state") != ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value:
        raise ValueError("Creative Brief can only be revised while awaiting Gate 2 approval")
    if aggregate.get("pending_approval_gate") != ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value:
        raise ValueError("Workflow is not waiting for a Creative Brief approval")

    previous_request_id = aggregate.get("pending_approval_id")
    if not isinstance(previous_request_id, str) or not previous_request_id:
        raise ValueError("Workflow is missing its pending Creative Brief approval")
    previous_request = approval_service.get_request(previous_request_id)
    if previous_request.status != ApprovalStatus.PENDING:
        raise ValueError("Pending Creative Brief approval is no longer pending")
    if previous_request.payload.get("workflow_id") != workflow_id:
        raise ValueError("Pending Creative Brief approval belongs to another workflow")
    if previous_request.payload.get("owner_user_id") != owner_user_id:
        raise ValueError("Pending Creative Brief approval belongs to another owner")

    project_id = aggregate.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("Workflow is missing project_id")
    if previous_request.payload.get("project_id") != project_id:
        raise ValueError("Pending Creative Brief approval belongs to another project")

    artifact_id = revised_brief.get("artifact_id") or revised_brief.get("brief_id")
    artifact_version = revised_brief.get("version")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("Revised Creative Brief is missing brief_id")
    if not isinstance(artifact_version, int) or artifact_version < 1:
        raise ValueError("Revised Creative Brief is missing a positive version")

    previous_brief = aggregate.get("creative_brief") or aggregate.get("latest_artifact")
    now = datetime.now(timezone.utc).isoformat()
    next_aggregate = dict(aggregate)
    next_aggregate["creative_brief"] = dict(revised_brief)
    next_aggregate["latest_artifact"] = dict(revised_brief)
    revisions = list(next_aggregate.get("creative_brief_revisions") or [])
    if isinstance(previous_brief, dict):
        revisions.append({
            "artifact": previous_brief,
            "superseded_at": now,
            "superseded_approval_id": previous_request_id,
        })
    next_aggregate["creative_brief_revisions"] = revisions

    approval_payload = {
        "workflow_id": workflow_id,
        "project_id": project_id,
        "gate": ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value,
        "workflow_version": current_version + 1,
        "artifact_ref": dict(revised_brief),
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "owner_user_id": owner_user_id,
        "supersedes_approval_id": previous_request_id,
    }

    with connection:
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE approval_request SET status = ?, updated_at = ? WHERE request_id = ? AND status = ?",
            (ApprovalStatus.REJECTED.value, now, previous_request_id, ApprovalStatus.PENDING.value),
        )
        if cursor.rowcount != 1:
            raise ValueError("Pending Creative Brief approval changed while being revised")
        new_request_id = approval_service.submit_request(approval_payload, cursor=cursor)
        next_aggregate["pending_approval_id"] = new_request_id
        next_aggregate["pending_approval_gate"] = ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=[{
                "event_id": f"evt_brief_revised_{workflow_id}_v{current_version + 1}",
                "command_id": f"cmd_brief_revised_{workflow_id}_v{current_version + 1}",
                "event_type": "CreativeBriefRevised",
                "timestamp": now,
                "payload": {
                    "previous_approval_id": previous_request_id,
                    "new_approval_id": new_request_id,
                    "artifact_id": artifact_id,
                    "artifact_version": artifact_version,
                },
            }],
            expected_version=current_version,
        )

    return {
        "workflow_id": workflow_id,
        "previous_request_id": previous_request_id,
        "request_id": new_request_id,
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "state": ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value,
    }


def revise_scene_plan_and_request_approval(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str,
    revised_plan: dict[str, Any],
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Replace a pending Gate 3 plan after its Video Factory revision is saved."""
    repo = WorkflowRepository(connection)
    approval_service = ApprovalService(connection)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, current_version = workflow
    if expected_version is not None and expected_version != current_version:
        raise ValueError(f"Stale workflow version: expected {expected_version}, current {current_version}")
    gate = ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value
    pending_revision = aggregate.get("state") == gate and aggregate.get("pending_approval_gate") == gate
    storyboard_recovery = (
        aggregate.get("state") in {
            ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value,
            ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL.value,
        }
        and aggregate.get("pending_approval_gate") == aggregate.get("state")
    )
    provider_recovery = aggregate.get("state") == ProductVideoWorkflowState.CREATING_STORYBOARD.value
    qc_recovery = (
        aggregate.get("state") == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
        and aggregate.get("operator_stage") == QCStage.KEYFRAME.value
        and aggregate.get("operator_action") in {"qc_authorization", "qc_retry_authorization"}
    )
    if not pending_revision and not storyboard_recovery and not provider_recovery and not qc_recovery:
        raise ValueError("Workflow is not waiting for a Scene Plan approval or keyframe recovery")
    if storyboard_recovery:
        render = aggregate.get("render_result") or {}
        failed_video = QCReportRepository(connection).get_latest_report(
            workflow_id, QCStage.VIDEO,
            expected_artifact_digest=str(render.get("sha256") or "").removeprefix("sha256:"),
        )
        if failed_video is None or failed_video.verdict != QCVerdict.FAIL:
            raise ValueError("Storyboard rework requires failed Video QC on the current render")
    if provider_recovery or qc_recovery:
        receipt = aggregate.get("latest_approval_receipt") or {}
        if receipt.get("gate") != gate:
            raise ValueError("Storyboard recovery has no approved Scene Plan receipt")
        if provider_recovery:
            failed = connection.execute(
                "SELECT message, last_error FROM workflow_outbox "
                "WHERE workflow_id = ? AND state = 'quarantined' ORDER BY outbox_id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if not failed:
                raise ValueError("Storyboard provider failure is not quarantined")
            failed_message = json.loads(failed["message"])
            if (
                failed_message.get("payload", {}).get("job_name") != "product_video.storyboard"
                or not any(
                    code in str(failed["last_error"])
                    for code in ("PROVIDER_UNAVAILABLE", "INVALID_RESPONSE")
                )
            ):
                raise ValueError("Storyboard did not fail on provider capability")
        previous_request_id = receipt.get("approval_request_id")
    elif storyboard_recovery:
        previous_request_id = aggregate.get("pending_approval_id")
    else:
        previous_request_id = aggregate.get("pending_approval_id")
    if not isinstance(previous_request_id, str) or not previous_request_id:
        raise ValueError("Workflow is missing its pending Scene Plan approval")
    previous_request = approval_service.get_request(previous_request_id)
    project_id = aggregate.get("project_id")
    expected_status = ApprovalStatus.PENDING if (pending_revision or storyboard_recovery) else ApprovalStatus.APPROVED
    if (
        previous_request.status != expected_status
        or previous_request.payload.get("workflow_id") != workflow_id
        or previous_request.payload.get("owner_user_id") != owner_user_id
        or previous_request.payload.get("project_id") != project_id
    ):
        raise ValueError("Pending Scene Plan approval identity or status changed")
    artifact_id = revised_plan.get("artifact_id") or revised_plan.get("scene_plan_id")
    artifact_version = revised_plan.get("version")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("Revised Scene Plan is missing scene_plan_id")
    if not isinstance(artifact_version, int) or artifact_version < 1:
        raise ValueError("Revised Scene Plan is missing a positive version")
    if revised_plan.get("project_id") != project_id:
        raise ValueError("Revised Scene Plan belongs to another project")
    previous_plan = aggregate.get("scene_plan") or aggregate.get("latest_artifact")
    if isinstance(previous_plan, dict) and artifact_version <= int(previous_plan.get("version") or 0):
        raise ValueError("Revised Scene Plan version must advance")

    now = datetime.now(timezone.utc).isoformat()
    next_aggregate = dict(aggregate)
    next_aggregate["state"] = gate
    next_aggregate["scene_plan"] = dict(revised_plan)
    next_aggregate["latest_artifact"] = dict(revised_plan)
    if storyboard_recovery:
        next_aggregate["creative_prior_render_job_id"] = aggregate.get("active_render_job_id")
        next_aggregate.pop("active_render_job_id", None)
        next_aggregate.pop("render_dispatch_status", None)
    if qc_recovery:
        for key in (
            "operator_action", "operator_stage", "operator_reason", "operator_qc_report_id",
            "keyframe_recovery_plan",
        ):
            next_aggregate.pop(key, None)
    revisions = list(next_aggregate.get("scene_plan_revisions") or [])
    if isinstance(previous_plan, dict):
        revisions.append({
            "artifact": previous_plan,
            "superseded_at": now,
            "superseded_approval_id": previous_request_id,
        })
    next_aggregate["scene_plan_revisions"] = revisions
    approval_payload = {
        "workflow_id": workflow_id,
        "project_id": project_id,
        "gate": gate,
        "workflow_version": current_version + 1,
        "artifact_ref": dict(revised_plan),
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "owner_user_id": owner_user_id,
        "supersedes_approval_id": previous_request_id,
    }
    with connection:
        cursor = connection.cursor()
        if pending_revision or storyboard_recovery:
            cursor.execute(
                "UPDATE approval_request SET status = ?, updated_at = ? WHERE request_id = ? AND status = ?",
                (ApprovalStatus.REJECTED.value, now, previous_request_id, ApprovalStatus.PENDING.value),
            )
            if cursor.rowcount != 1:
                raise ValueError("Pending Scene Plan approval changed while being revised")
        new_request_id = approval_service.submit_request(approval_payload, cursor=cursor)
        next_aggregate["pending_approval_id"] = new_request_id
        next_aggregate["pending_approval_gate"] = gate
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=[{
                "event_id": f"evt_scene_plan_revised_{workflow_id}_v{current_version + 1}",
                "command_id": f"cmd_scene_plan_revised_{workflow_id}_v{current_version + 1}",
                "event_type": "ScenePlanRevised",
                "timestamp": now,
                "payload": {
                    "previous_approval_id": previous_request_id,
                    "new_approval_id": new_request_id,
                    "artifact_id": artifact_id,
                    "artifact_version": artifact_version,
                },
            }],
            expected_version=current_version,
        )
    return {
        "workflow_id": workflow_id,
        "previous_request_id": previous_request_id,
        "request_id": new_request_id,
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "state": gate,
    }


def authorize_workflow_cost(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str,
    authorization: dict[str, Any],
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Record an operator-issued replacement cost authorization for one workflow."""
    repo = WorkflowRepository(connection)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = workflow
    if expected_version is not None and expected_version != version:
        raise ValueError(f"Stale workflow version: expected {expected_version}, current {version}")
    if aggregate.get("owner_user_id") != owner_user_id:
        raise ValueError("Workflow belongs to another owner")
    max_cost = authorization.get("workflow_max_cost_usd", authorization.get("max_cost_usd"))
    if not isinstance(max_cost, (int, float)) or isinstance(max_cost, bool) or max_cost <= 0:
        raise ValueError("Authorization requires a positive workflow_max_cost_usd")
    authorization_id = str(authorization.get("authorization_id") or authorization.get("run_id") or "").strip()
    if not authorization_id:
        raise ValueError("Authorization requires authorization_id or run_id")

    now = datetime.now(timezone.utc).isoformat()
    next_aggregate = dict(aggregate)
    previous = aggregate.get("qc_authorization")
    history = list(next_aggregate.get("cost_authorization_history") or [])
    if isinstance(previous, dict):
        history.append({"authorization": previous, "superseded_at": now})
    replacement = dict(authorization)
    replacement["workflow_id"] = workflow_id
    replacement["workflow_max_cost_usd"] = float(max_cost)
    next_aggregate["qc_authorization"] = replacement
    next_aggregate["cost_authorization_history"] = history
    with connection:
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=[{
                "event_id": f"evt_cost_authorized_{workflow_id}_v{version + 1}",
                "command_id": f"cmd_cost_authorized_{workflow_id}_v{version + 1}",
                "event_type": "WorkflowCostAuthorized",
                "timestamp": now,
                "payload": {"authorization_id": authorization_id, "max_cost_usd": float(max_cost)},
            }],
            expected_version=version,
        )
    return {"workflow_id": workflow_id, "authorization": replacement, "version": version + 1}


def block_workflow_for_qc(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str,
    stage: QCStage,
    reason: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Invalidate a pending gate when its required automatic QC is not PASS."""
    repo = WorkflowRepository(connection)
    approvals = ApprovalService(connection)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = workflow
    if expected_version is not None and expected_version != version:
        raise ValueError(f"Stale workflow version: expected {expected_version}, current {version}")
    if aggregate.get("owner_user_id") != owner_user_id:
        raise ValueError("Workflow belongs to another owner")
    request_id = aggregate.get("pending_approval_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Workflow has no pending approval to invalidate")
    request = approvals.get_request(request_id)
    if request.status != ApprovalStatus.PENDING:
        raise ValueError("Workflow approval is not pending")

    now = datetime.now(timezone.utc).isoformat()
    next_aggregate = dict(aggregate)
    next_aggregate["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    next_aggregate["operator_action"] = "qc_retry_authorization"
    next_aggregate["operator_stage"] = stage.value
    next_aggregate["operator_reason"] = reason
    next_aggregate.pop("pending_approval_id", None)
    next_aggregate.pop("pending_approval_gate", None)
    with connection:
        cur = connection.cursor()
        cur.execute(
            "UPDATE approval_request SET status = ?, updated_at = ? WHERE request_id = ? AND status = ?",
            (ApprovalStatus.REJECTED.value, now, request_id, ApprovalStatus.PENDING.value),
        )
        if cur.rowcount != 1:
            raise ValueError("Pending approval changed while blocking workflow")
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=[{
                "event_id": f"evt_qc_block_{workflow_id}_v{version + 1}",
                "command_id": f"cmd_qc_block_{workflow_id}_v{version + 1}",
                "event_type": "QCBlockedApproval",
                "timestamp": now,
                "payload": {"stage": stage.value, "approval_request_id": request_id, "reason": reason},
            }],
            expected_version=version,
        )
    return {"workflow_id": workflow_id, "request_id": request_id, "state": next_aggregate["state"]}


def resume_storyboard_after_qc_authorization(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str,
    expected_version: int | None = None,
    authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resume a blocked storyboard job after an operator authorizes QC retry."""
    repo = WorkflowRepository(connection)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = workflow
    if expected_version is not None and expected_version != version:
        raise ValueError(f"Stale workflow version: expected {expected_version}, current {version}")
    if aggregate.get("owner_user_id") != owner_user_id:
        raise ValueError("Workflow belongs to another owner")
    current_state = aggregate.get("state")
    if current_state == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
        if aggregate.get("operator_stage") != QCStage.KEYFRAME.value:
            raise ValueError("Workflow is not waiting for Keyframe QC retry")
    elif current_state != ProductVideoWorkflowState.CREATING_STORYBOARD.value:
        raise ValueError("Workflow is not ready for storyboard retry")

    next_aggregate = dict(aggregate)
    next_aggregate["state"] = ProductVideoWorkflowState.CREATING_STORYBOARD.value
    recovery_plan = aggregate.get("keyframe_recovery_plan")
    previous_storyboard = aggregate.get("storyboard")
    if previous_storyboard is None:
        latest = aggregate.get("latest_artifact")
        if isinstance(latest, dict) and (latest.get("schema") in ("storyboard", "storyboard_package", "storyboard_v2") or "generated_keyframes" in latest):
            previous_storyboard = latest
    if not isinstance(recovery_plan, dict) or not recovery_plan.get("frames"):
        raise ValueError("Workflow is missing the persisted keyframe recovery plan")
    if current_state == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value and not isinstance(previous_storyboard, dict):
        raise ValueError("Workflow is missing the previous storyboard package")
    attempt = int(aggregate.get("keyframe_recovery_attempt", 0) or 0) + 1
    next_aggregate["keyframe_recovery_attempt"] = attempt
    for key in ("operator_action", "operator_stage", "operator_reason", "operator_qc_report_id"):
        next_aggregate.pop(key, None)
    now = datetime.now(timezone.utc).isoformat()
    scene_ref = aggregate.get("scene_plan") or aggregate.get("latest_artifact")
    if not isinstance(scene_ref, dict):
        raise ValueError("Workflow is missing the persisted Scene Plan")
    outbox = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_product_video.storyboard_recovery_{workflow_id}_a{attempt}_v{version + 1}",
        payload={
            "job_name": "product_video.storyboard",
            "workflow_id": workflow_id,
            "project_id": aggregate.get("project_id"),
            "args": [scene_ref],
            "kwargs": {
                "project_id": aggregate.get("project_id"),
                "owner_user_id": owner_user_id,
                "recovery_plan": recovery_plan,
                "recovery_attempt": attempt,
                "previous_storyboard": previous_storyboard,
            },
            "owner_user_id": owner_user_id,
        },
    )
    with connection:
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=[{
                "event_id": f"evt_qc_retry_{workflow_id}_v{version + 1}",
                "command_id": f"cmd_qc_retry_{workflow_id}_v{version + 1}",
                "event_type": "QCRetryAuthorized",
                "timestamp": now,
                "payload": {"stage": QCStage.KEYFRAME.value},
            }],
            outbox_message=outbox,
            expected_version=version,
        )
    return {
        "workflow_id": workflow_id,
        "state": next_aggregate["state"],
        "version": version + 1,
        "recovery_attempt": attempt,
        "recovery_plan": recovery_plan,
    }


def enqueue_keyframe_recovery(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    plan: dict[str, Any],
    authorization: dict[str, Any] | None = None,
    actor: str = "operator",
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Enqueue a targeted keyframe recovery job with its durable recovery plan."""
    repo = WorkflowRepository(connection)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = workflow
    if expected_version is not None and expected_version != version:
        raise ValueError(f"Stale workflow version: expected {expected_version}, current {version}")

    next_aggregate = dict(aggregate)
    next_aggregate["state"] = ProductVideoWorkflowState.CREATING_STORYBOARD.value
    next_aggregate["keyframe_recovery_plan"] = plan
    attempt = int(aggregate.get("keyframe_recovery_attempt", 0) or 0) + 1
    next_aggregate["keyframe_recovery_attempt"] = attempt

    for key in ("operator_action", "operator_stage", "operator_reason", "operator_qc_report_id"):
        next_aggregate.pop(key, None)

    previous_storyboard = aggregate.get("storyboard") or aggregate.get("latest_artifact")
    scene_ref = aggregate.get("scene_plan") or aggregate.get("latest_artifact")
    owner_user_id = aggregate.get("owner_user_id", actor)

    now = datetime.now(timezone.utc).isoformat()
    outbox = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_product_video.storyboard_recovery_{workflow_id}_a{attempt}_v{version + 1}",
        payload={
            "job_name": "product_video.storyboard",
            "workflow_id": workflow_id,
            "project_id": aggregate.get("project_id"),
            "args": [scene_ref] if scene_ref else [],
        "kwargs": {
                "project_id": aggregate.get("project_id"),
                "owner_user_id": owner_user_id,
                "recovery_plan": plan,
                "recovery_attempt": attempt,
                "previous_storyboard": previous_storyboard,
            },
            "owner_user_id": owner_user_id,
        },
    )

    with connection:
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=[{
                "event_id": f"evt_keyframe_recovery_{workflow_id}_v{version + 1}",
                "command_id": f"cmd_keyframe_recovery_{workflow_id}_v{version + 1}",
                "event_type": "KeyframeRecoveryEnqueued",
                "timestamp": now,
                "payload": {"stage": QCStage.KEYFRAME.value, "actor": actor},
            }],
            outbox_message=outbox,
            expected_version=version,
        )

    return {
        "status": "enqueued",
        "workflow_id": workflow_id,
        "recovery_attempt": attempt,
        "version": version + 1,
    }


def authorize_skipped_keyframe_policy_recovery(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    frame_id: str,
    reason: str,
    actor: str = "operator",
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Authorize one audited, single-frame recovery after exhausted legacy QC.

    Existing QC history and quota rows are preserved. Two additional QC
    evaluations may be explicitly authorized for this policy migration; each
    invocation regenerates only one frame and requires operator review before
    another recovery is queued.
    """
    clean_frame = str(frame_id or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_frame or len(clean_frame) > 160:
        raise ValueError("A valid --frame-id is required")
    if not clean_reason or len(clean_reason) > 500:
        raise ValueError("A recovery reason of 1–500 characters is required")
    repo = WorkflowRepository(connection)
    loaded = repo.get_workflow(workflow_id)
    if loaded is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = loaded
    if expected_version is not None and expected_version != version:
        raise ValueError(f"Stale workflow version: expected {expected_version}, current {version}")

    authorized = int(aggregate.get("keyframe_policy_recovery_count", 0) or 0)
    audited_count = int(connection.execute(
        "SELECT COUNT(*) FROM workflow_event WHERE workflow_id=? AND event_type='OperatorKeyframePolicyRecoveryAuthorized'",
        (workflow_id,),
    ).fetchone()[0])
    if audited_count != authorized:
        raise ValueError("Keyframe policy-recovery audit count does not match workflow state")
    if authorized >= 2:
        raise ValueError("The two audited keyframe policy-recovery cycles are already used")
    if authorized == 0:
        if aggregate.get("state") != "skipped":
            raise ValueError("First policy recovery requires a skipped workflow")
        if aggregate.get("failure_code") != "KEYFRAME_QC_RETRIES_EXHAUSTED":
            raise ValueError("Workflow was not skipped because keyframe QC retries were exhausted")
        report_id = str((aggregate.get("failure_report") or {}).get("report_id") or "")
    else:
        if (
            aggregate.get("state") != ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
            or aggregate.get("operator_action") != "keyframe_policy_recovery"
        ):
            raise ValueError("Next frame recovery requires the previous policy-recovery QC to be reviewed")
        report_id = str(aggregate.get("operator_qc_report_id") or "")

    report = QCReportRepository(connection).get_report(report_id) if report_id else None
    if (
        report is None
        or report.workflow_id != workflow_id
        or report.stage != QCStage.KEYFRAME
        or report.verdict != QCVerdict.FAIL
    ):
        raise ValueError("The latest failed Keyframe QC report is missing or does not match this workflow")
    package = aggregate.get("storyboard") or aggregate.get("latest_artifact") or {}
    latest_report = QCReportRepository(connection).get_latest_report(workflow_id, QCStage.KEYFRAME)
    package_digest = str(package.get("package_digest") or "").removeprefix("sha256:") if isinstance(package, dict) else ""
    if (
        latest_report is None
        or latest_report.report_id != report.report_id
        or not package_digest
        or str(report.artifact_digest).removeprefix("sha256:") != package_digest
    ):
        raise ValueError("The failed report is stale or does not match the current saved storyboard digest")
    scene_plan = aggregate.get("scene_plan")
    frames = package.get("generated_keyframes", []) if isinstance(package, dict) else []
    if not isinstance(scene_plan, dict) or not frames:
        raise ValueError("Workflow is missing its saved scene plan or storyboard frames")
    frame = next((item for item in frames if isinstance(item, dict) and item.get("frame_id") == clean_frame), None)
    if frame is None:
        raise ValueError(f"Frame '{clean_frame}' is not in the saved storyboard")
    findings = report.findings if isinstance(report.findings, dict) else {}
    report_items = findings.get("items") if isinstance(findings.get("items"), list) else []
    failed_item = next((item for item in report_items if isinstance(item, dict) and item.get("frame_id") == clean_frame), None)
    if not failed_item or str(failed_item.get("verdict", "")).lower() != "fail":
        raise ValueError(f"Frame '{clean_frame}' is not marked FAIL in QC report {report_id}")

    active_outbox = connection.execute(
        "SELECT outbox_id FROM workflow_outbox WHERE workflow_id=? AND state IN ('pending','leased') LIMIT 1",
        (workflow_id,),
    ).fetchone()
    if active_outbox:
        raise ValueError(f"Workflow has active outbox job {active_outbox['outbox_id']}; inspect it before recovery")
    for table, query in (
        ("workflow_qc_retries", "SELECT 1 FROM workflow_qc_retries WHERE workflow_id=? AND status IN ('claimed','reserved','sent','technical_unknown') LIMIT 1"),
        ("product_video_operation_attempt", "SELECT 1 FROM product_video_operation_attempt WHERE workflow_id=? AND capability_id='product_video.qc.keyframe' AND status IN ('claimed','reserved','sent','technical_unknown') LIMIT 1"),
    ):
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists and connection.execute(query, (workflow_id,)).fetchone():
            raise ValueError(f"Workflow has an unresolved Keyframe QC attempt in {table}")

    now = datetime.now(timezone.utc).isoformat()
    next_aggregate = dict(aggregate)
    next_aggregate["state"] = ProductVideoWorkflowState.CREATING_STORYBOARD.value
    next_aggregate["keyframe_policy_recovery_count"] = authorized + 1
    next_aggregate["keyframe_policy_recovery_pending"] = True
    next_aggregate["keyframe_policy_recovery_frame_id"] = clean_frame
    recovery_plan = {
        "policy": "operator_stepwise_keyframe_policy_recovery",
        "operator_stepwise": True,
        "frames": [{
            "frame_id": clean_frame,
            "actions": ["regenerate_keyframe", "rerun_keyframe_qc"],
            "feedback": clean_reason,
        }],
    }
    next_aggregate["keyframe_recovery_plan"] = recovery_plan
    attempt = int(aggregate.get("keyframe_recovery_attempt", 0) or 0) + 1
    next_aggregate["keyframe_recovery_attempt"] = attempt
    next_aggregate.pop("operator_action", None)
    next_aggregate.pop("operator_stage", None)
    next_aggregate.pop("operator_reason", None)
    next_aggregate.pop("operator_qc_report_id", None)
    owner_user_id = str(aggregate.get("owner_user_id") or actor)
    outbox = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_keyframe_policy_recovery_{workflow_id}_n{authorized + 1}_v{version + 1}",
        payload={
            "job_name": "product_video.storyboard",
            "workflow_id": workflow_id,
            "project_id": aggregate.get("project_id"),
            "args": [scene_plan],
            "kwargs": {
                "project_id": aggregate.get("project_id"),
                "owner_user_id": owner_user_id,
                "recovery_plan": recovery_plan,
                "recovery_attempt": attempt,
                "previous_storyboard": package,
            },
            "owner_user_id": owner_user_id,
        },
    )
    event_payload = {
        "actor": actor,
        "reason": clean_reason,
        "frame_id": clean_frame,
        "report_id": report.report_id,
        "previous_policy_version": report.policy_version,
        "new_policy_version": KEYFRAME_POLICY_VERSION,
        "authorized_recovery_number": authorized + 1,
        "max_authorized_recoveries": 2,
        "previous_qc_history_preserved": True,
    }
    with connection:
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=[{
                "event_id": f"evt_keyframe_policy_recovery_{workflow_id}_n{authorized + 1}_v{version + 1}",
                "command_id": f"cmd_keyframe_policy_recovery_{workflow_id}_n{authorized + 1}_v{version + 1}",
                "event_type": "OperatorKeyframePolicyRecoveryAuthorized",
                "timestamp": now,
                "payload": event_payload,
            }],
            outbox_message=outbox,
            expected_version=version,
        )
    return {
        "workflow_id": workflow_id,
        "state": next_aggregate["state"],
        "version": version + 1,
        "recovery_number": authorized + 1,
        "frame_id": clean_frame,
        "report_id": report.report_id,
        "reason": clean_reason,
    }


def _resume_keyframe_after_pre_send_capacity_wait(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str,
    operation_id: str,
    aggregate: dict[str, Any],
    version: int,
) -> dict[str, Any]:
    """Requeue an approved storyboard after proving capacity denied before Send."""
    import hashlib
    from video_factory.creative.contracts import ResearchDossier, ScriptScenePlanV2
    from video_factory.creative.agents.contracts import BackendProfile, ScenePromptBundle
    from video_factory.creative.storyboard import compile_keyframe_requests
    from video_factory.integrations.capacity import ProviderCapacityStore
    from video_factory.workflows.envelope import build_envelope

    if (
        aggregate.get("state") != ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
        or aggregate.get("failure_code") != "PROVIDER_CAPACITY_WAIT"
        or aggregate.get("operator_action") != "qc_provider_recovery"
        or aggregate.get("operator_stage") != "keyframe"
    ):
        raise ValueError("No recoverable pre-Send keyframe capacity wait is recorded")
    if connection.execute(
        "SELECT 1 FROM workflow_outbox WHERE workflow_id=? LIMIT 1", (workflow_id,)
    ).fetchone():
        raise ValueError("Workflow already has an outbox job; refusing to enqueue another storyboard")
    if connection.execute(
        "SELECT 1 FROM video_factory_keyframe_operation WHERE workflow_id=? LIMIT 1", (workflow_id,)
    ).fetchone():
        raise ValueError("Workflow has keyframe operation history; use the operation-specific reconciliation path")
    if connection.execute(
        "SELECT 1 FROM video_factory_provider_capacity WHERE provider_kind='google_flow' LIMIT 1"
    ).fetchone():
        raise ValueError("Google Flow capacity still has a bound row; reconcile it before retrying")

    scene_ref = aggregate.get("scene_plan")
    receipt = aggregate.get("latest_approval_receipt") or {}
    if (
        not isinstance(scene_ref, dict)
        or scene_ref.get("creative_mode") != "four_agent"
        or receipt.get("gate") != ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value
        or receipt.get("project_id") != aggregate.get("project_id")
    ):
        raise ValueError("The pinned approved Scene Plan receipt is missing or no longer matches")
    script = ScriptScenePlanV2.from_dict(scene_ref)
    dossier = ResearchDossier.from_dict(scene_ref["research_dossier"])
    bundle = ScenePromptBundle.from_dict(scene_ref["scene_prompts"])
    profile = BackendProfile.from_dict(scene_ref["backend_profile"])
    if profile.image_provider_kind != "google_flow":
        raise ValueError("Pinned image provider is not Google Flow")
    requests = compile_keyframe_requests(
        workflow_id=workflow_id,
        project_id=str(aggregate["project_id"]),
        dossier=dossier,
        script=script,
        prompt_bundle=bundle,
        backend_profile=profile,
    )
    request = next((item for item in requests if item.operation_id == operation_id), None)
    if request is None:
        raise ValueError(f"Operation '{operation_id}' does not belong to the pinned Scene Plan")
    prompt_hash = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
    attempt = connection.execute(
        """SELECT id, attempt_number FROM workflow_qc_retries
           WHERE workflow_id=? AND operation_id=? AND retry_type='four_agent_image_send'
             AND item_id=? AND input_hash=? AND status='technical_unknown'""",
        (workflow_id, operation_id, request.frame_id, prompt_hash),
    ).fetchone()
    if attempt is None:
        raise ValueError("No matching technical-unknown generation attempt proves the capacity wait")
    if connection.execute(
        "SELECT 1 FROM video_factory_keyframe_operation WHERE operation_id=? LIMIT 1",
        (operation_id,),
    ).fetchone():
        raise ValueError("A provider operation exists; capacity wait is not proven pre-Send")
    now = datetime.now(timezone.utc).isoformat()
    next_aggregate = dict(aggregate)
    next_aggregate["state"] = ProductVideoWorkflowState.CREATING_STORYBOARD.value
    for key in ("operator_action", "operator_stage", "operator_reason", "operator_qc_report_id",
                "failure_category", "failure_code", "failure_reason"):
        next_aggregate.pop(key, None)
    history = list(next_aggregate.get("provider_recovery_history") or [])
    history.append({
        "operation_id": operation_id,
        "resolution": "reconciled_not_sent",
        "reason": "Provider capacity admission denied before a keyframe operation or Flow project was created",
        "attempt_number": int(attempt["attempt_number"]),
        "at": now,
    })
    next_aggregate["provider_recovery_history"] = history
    receipt_data = dict(receipt)
    outbox = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_product_video.storyboard_capacity_recovery_{workflow_id}_{operation_id}_v{version + 1}",
        payload={
            "job_name": "product_video.storyboard",
            "workflow_id": workflow_id,
            "project_id": aggregate.get("project_id"),
            "args": [scene_ref],
            "kwargs": {
                "approval_receipt": receipt_data,
                "approval_request_id": receipt_data["approval_request_id"],
                "project_id": aggregate.get("project_id"),
                "owner_user_id": owner_user_id,
            },
            "owner_user_id": owner_user_id,
            "approval_request_id": receipt_data["approval_request_id"],
        },
    )
    event = {
        "event_id": f"evt_keyframe_capacity_reconciled_{workflow_id}_v{version + 1}",
        "command_id": f"cmd_keyframe_capacity_reconciled_{workflow_id}_v{version + 1}",
        "event_type": "KeyframeCapacityWaitReconciled",
        "timestamp": now,
        "payload": {
            "operation_id": operation_id,
            "attempt_number": int(attempt["attempt_number"]),
            "prompt_sha256": prompt_hash,
            "resolution": "reconciled_not_sent",
        },
    }
    with connection:
        connection.execute(
            """UPDATE workflow_qc_retries SET status='reconciled_not_sent', verdict='not_sent',
               action_taken='capacity_wait_before_provider_submit', reason_text=?, updated_at=?
               WHERE id=? AND status='technical_unknown'""",
            ("Provider capacity denied before provider operation creation; same approved request requeued", now, attempt["id"]),
        )
        ProviderCapacityStore(connection)
        WorkflowRepository(connection).commit_workflow(
            workflow_id,
            json.dumps(next_aggregate, ensure_ascii=False),
            [event],
            outbox_message=outbox,
            expected_version=version,
            in_transaction=True,
        )
    return {
        "workflow_id": workflow_id,
        "operation_id": operation_id,
        "status": "reconciled_not_sent",
        "attempt_number": int(attempt["attempt_number"]),
        "resumed": True,
        "outbox_enqueued": True,
    }


def recover_unknown_keyframe_submission(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str,
    operation_id: str,
    authorization: dict[str, Any] | None = None,
    expected_version: int | None = None,
    replacement_note: str | None = None,
    provider_mode: str | None = None,
) -> dict[str, Any]:
    """Reconcile an unknown call or replace a proven pre-Send failure for one frame."""
    import hashlib
    from video_factory.integrations.keyframe_provider import (
        MAX_OUTPUT_RECONCILIATION_ATTEMPTS, KeyframeOperationStore, _stable_request_digest,
    )
    from video_factory.integrations.keyframe_provider_factory import (
        resolve_keyframe_provider,
    )
    from video_factory.integrations.capacity import ProviderCapacityStore, process_lease_owner_alive
    from video_factory.qc.retry_policy import ensure_qc_retry_schema
    from video_factory.creative.contracts import ResearchDossier, ScriptScenePlanV2
    from video_factory.creative.storyboard import (
        bind_recovery_reference_revisions,
        compile_keyframe_requests,
        plan_initial_storyboard_recovery,
        prepare_keyframe_generation_request,
        resolve_reusable_storyboard_frames,
        storyboard_package_digest,
    )
    from video_factory.storage.layout import get_workspace_layout
    from video_factory.workflows.outbox_repository import OutboxRepository

    if not operation_id or not str(operation_id).strip():
        raise ValueError("operation_id is required for keyframe recovery")
    ensure_qc_retry_schema(connection)
    target_op_id = str(operation_id).strip()

    repo = WorkflowRepository(connection)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = workflow
    if expected_version is not None and version != expected_version:
        raise ValueError("Workflow đã thay đổi trước khi phục hồi keyframe")
    if aggregate.get("owner_user_id") != owner_user_id:
        raise ValueError("Workflow belongs to another owner")
    if aggregate.get("state") not in (
        ProductVideoWorkflowState.CREATING_STORYBOARD.value,
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
    ):
        raise ValueError(
            f"Workflow '{workflow_id}' is in state '{aggregate.get('state')}', not creating a storyboard"
        )
    row = connection.execute(
        "SELECT outbox_id, message, state, last_error FROM workflow_outbox "
        "WHERE workflow_id = ? ORDER BY outbox_id DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    if row is None:
        return _resume_keyframe_after_pre_send_capacity_wait(
            connection,
            workflow_id=workflow_id,
            owner_user_id=owner_user_id,
            operation_id=target_op_id,
            aggregate=aggregate,
            version=version,
        )
    last_error = str(row["last_error"] or "") if row else ""
    if not row or row["state"] != "quarantined":
        raise ValueError("No quarantined storyboard job to recover")
    uncertain_submission = any(
        code in last_error for code in ("NETWORK_UNKNOWN_OUTCOME", "RECONCILIATION_REQUIRED", "GOOGLE_FLOW_KEYFRAME_TIMEOUT", "OUTPUT_RECONCILIATION_EXHAUSTED", "GOOGLE_FLOW_OUTPUT_NOT_RETRIEVED")
    )
    known_invalid_output = "GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED" in last_error
    presend_failure_codes = (
        "GOOGLE_FLOW_NEW_PROJECT_SELECTOR_NOT_UNIQUE",
        "GOOGLE_FLOW_AUTH_REQUIRED",
        "GOOGLE_FLOW_AUTH_UNVERIFIED",
        "GOOGLE_FLOW_SETTINGS_PANEL_UNVERIFIED",
        "GOOGLE_FLOW_SETTINGS_SELECTOR_NOT_UNIQUE",
        "GOOGLE_FLOW_IMAGE_MODE_SELECTOR_NOT_UNIQUE",
        "GOOGLE_FLOW_IMAGE_MODE_UNVERIFIED",
        "GOOGLE_FLOW_916_SELECTOR_NOT_UNIQUE",
        "GOOGLE_FLOW_916_UNVERIFIED",
        "GOOGLE_FLOW_SETTINGS_SAVE_SELECTOR_NOT_UNIQUE",
        "GOOGLE_FLOW_REFERENCE_ASSET_NOT_UNIQUE",
        "FLOW_UPLOAD_CONSENT_REQUIRED",
    )
    presend_failure_code = next(
        (code for code in presend_failure_codes if code in last_error), None
    )
    confirmed_presend_failure = (
        "GOOGLE_FLOW_KEYFRAME_FAILED" in last_error and presend_failure_code is not None
    )
    if not uncertain_submission and not confirmed_presend_failure and not known_invalid_output:
        raise ValueError("Quarantined storyboard job has neither an unknown submission, a verified invalid Flow image, nor a recognized pre-Send Flow selector failure")
    envelope = json.loads(row["message"])
    job_payload = envelope.get("payload", {})
    if job_payload.get("job_name") != "product_video.storyboard":
        raise ValueError("Quarantined job is not storyboard generation")
    job_kwargs = job_payload.get("kwargs") or {}

    scene_ref = aggregate.get("scene_plan")
    if not isinstance(scene_ref, dict) or not isinstance(scene_ref.get("research_dossier"), dict):
        raise ValueError("Approved Scene Plan and dossier are required")
    script = ScriptScenePlanV2.from_dict(scene_ref)
    dossier = ResearchDossier.from_dict(scene_ref["research_dossier"])
    prompt_bundle = backend_profile = None
    if scene_ref.get("creative_mode") == "four_agent":
        from video_factory.creative.agents.contracts import BackendProfile, ScenePromptBundle

        prompt_bundle = ScenePromptBundle.from_dict(scene_ref["scene_prompts"])
        backend_profile = BackendProfile.from_dict(scene_ref["backend_profile"])
    recovery_plan = job_kwargs.get("recovery_plan", aggregate.get("keyframe_recovery_plan"))
    if not isinstance(recovery_plan, dict):
        recovery_plan = None
    if recovery_plan and prompt_bundle is not None:
        recovery_plan = bind_recovery_reference_revisions(
            recovery_plan, script=script, prompt_bundle=prompt_bundle, dossier=dossier,
        )
    previous_package = job_kwargs.get("previous_storyboard")
    if not isinstance(previous_package, dict):
        previous_package = None
    request_inputs = dict(
        workflow_id=workflow_id,
        project_id=str(aggregate["project_id"]),
        dossier=dossier,
        script=script,
        prompt_bundle=prompt_bundle,
        backend_profile=backend_profile,
        recovery_plan=recovery_plan,
        recovery_attempt=int(job_kwargs.get("recovery_attempt", aggregate.get("keyframe_recovery_attempt")) or 0),
        previous_package=previous_package,
    )
    requests = compile_keyframe_requests(**request_inputs)
    by_operation = {request.operation_id: request for request in requests}
    omitted_legacy_end = target_op_id not in by_operation
    if omitted_legacy_end:
        # Older workers could have sent an END during START recovery even when omitted.
        requests = compile_keyframe_requests(**request_inputs, legacy_implicit_end_recovery=True)
        by_operation = {request.operation_id: request for request in requests}
    if target_op_id not in by_operation or (omitted_legacy_end and by_operation[target_op_id].frame_role != "end"):
        raise ValueError(f"Operation '{target_op_id}' does not belong to the approved Scene Plan")

    rows = connection.execute(
        "SELECT operation_id, provider_operation_id, provider_kind, status, error_code, error_message, "
        "provider_metadata_json, output_path, output_sha256, updated_at, request_digest "
        "FROM video_factory_keyframe_operation "
        "WHERE workflow_id = ? AND operation_id = ?",
        (workflow_id, target_op_id),
    ).fetchall()
    if not rows:
        raise ValueError(f"Operation '{target_op_id}' does not exist in this workflow")
    target_row = rows[0]
    abandonment = None
    if replacement_note is not None:
        note = replacement_note.strip()
        if not note or len(note) > 1000:
            raise ValueError("A replacement note of 1–1000 characters is required")
        metadata = json.loads(target_row["provider_metadata_json"] or "{}")
        attempts = metadata.get("output_reconciliation_attempts")
        finished_attempts = int(metadata.get("output_reconciliation_finished_attempt") or 0)
        finished = finished_attempts >= MAX_OUTPUT_RECONCILIATION_ATTEMPTS
        if not finished and metadata.get("output_reconciliation_last_attempt_at"):
            last_claim = datetime.fromisoformat(metadata["output_reconciliation_last_attempt_at"])
            saved_at = datetime.fromisoformat(target_row["updated_at"]).replace(tzinfo=timezone.utc)
            finished = (saved_at > last_claim) and (type(attempts) is int and attempts >= MAX_OUTPUT_RECONCILIATION_ATTEMPTS and finished_attempts >= MAX_OUTPUT_RECONCILIATION_ATTEMPTS)
        is_timeout_or_exhausted = target_row["error_code"] in (
            "GOOGLE_FLOW_KEYFRAME_TIMEOUT", "OUTPUT_RECONCILIATION_EXHAUSTED",
        )
        exhausted_poll_budget = finished
        valid_stop_condition = exhausted_poll_budget or (is_timeout_or_exhausted and finished)

        if (not uncertain_submission or target_row["provider_kind"] != "google_flow"
                or target_row["status"] != "unknown" or target_row["output_path"]
                or not target_row["provider_operation_id"] or metadata.get("send_attempted") is not True
                or target_row["error_code"] not in ("GOOGLE_FLOW_OUTPUT_NOT_RETRIEVED", "OUTPUT_RECONCILIATION_EXHAUSTED", "GOOGLE_FLOW_KEYFRAME_TIMEOUT")
                or not valid_stop_condition):
            raise ValueError("An unknown Flow operation with no output and an exhausted poll budget is required")
        abandonment = {
            "note": note, "provider_outcome": "unknown", "output_reconciliation_attempts": attempts,
            "provider_operation_id": target_row["provider_operation_id"],
            "abandoned_at": datetime.now(timezone.utc).isoformat(),
        }
    presend_proven = False
    invalid_output_proven = False
    if confirmed_presend_failure:
        try:
            operation_metadata = json.loads(target_row["provider_metadata_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            operation_metadata = None
        presend_proven = (
            target_row["status"] == "failed"
            and target_row["error_code"] == "GOOGLE_FLOW_KEYFRAME_FAILED"
            and presend_failure_code in str(target_row["error_message"] or "")
            and isinstance(operation_metadata, dict)
            and operation_metadata.get("send_attempted") is False
            and not target_row["provider_operation_id"]
            and not target_row["output_path"]
        )
        if not presend_proven:
            raise ValueError(
                f"Operation '{target_op_id}' lacks durable proof that Google Flow failed before Send"
            )
    elif known_invalid_output:
        try:
            operation_metadata = json.loads(target_row["provider_metadata_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            operation_metadata = None
        output_path = Path(str(target_row["output_path"] or ""))
        expected_sha = str(target_row["output_sha256"] or "")
        try:
            if output_path.is_file() and expected_sha:
                actual_sha = hashlib.sha256(output_path.read_bytes()).hexdigest()
                from PIL import Image
                with Image.open(output_path) as image:
                    image.verify()
                with Image.open(output_path) as image:
                    width, height = image.size
            else:
                actual_sha, width, height = "", 0, 0
        except Exception:
            actual_sha, width, height = "", 0, 0
        expected_dimensions = [width, height]
        wrong_native_ratio = (
            width > 0 and height > 0
            and abs((width / height) - (9 / 16)) > (9 / 16) * 0.01
        )
        invalid_output_proven = (
            target_row["status"] == "failed"
            and target_row["error_code"] == "GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED"
            and "GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED" in str(target_row["error_message"] or "")
            and isinstance(operation_metadata, dict)
            and operation_metadata.get("send_attempted") is True
            and operation_metadata.get("output_received") is True
            and operation_metadata.get("output_dimensions") == expected_dimensions
            and actual_sha == expected_sha
            and wrong_native_ratio
        )
        if not invalid_output_proven:
            raise ValueError(
                f"Operation '{target_op_id}' output bytes do not match the recorded SHA-256, decoded dimensions, or native 9:16 rejection metadata"
            )
    elif target_row["status"] not in ("unknown", "submitting", "project_created", "completed"):
        raise ValueError(f"Operation '{target_op_id}' is not in an uncertain submission state")

    store = KeyframeOperationStore(connection)
    try:
        wf_provider_mode = str(provider_mode or (workflow[0].get("creative_policy") or {}).get("provider_mode") or workflow[0].get("provider_mode") or "")
        provider, _ = resolve_keyframe_provider(get_workspace_layout().root / "keyframes", provider_mode=wf_provider_mode)
    except Exception:
        from video_factory.integrations.google_flow_keyframe_provider import (
            GoogleFlowKeyframeProvider,
        )
        provider = GoogleFlowKeyframeProvider(output_dir=get_workspace_layout().root / "keyframes")

    target_request = by_operation[target_op_id]
    provider_id = target_row["provider_operation_id"] or target_op_id
    provider_kind = str(target_row["provider_kind"] or "google_flow")
    if hasattr(provider, "bind_operation_store"):
        provider.bind_operation_store(store)
    now = datetime.now(timezone.utc).isoformat()
    op_completed = False
    op_row = connection.execute(
        "SELECT status, output_path, output_sha256 FROM video_factory_keyframe_operation WHERE operation_id = ?",
        (target_op_id,),
    ).fetchone()
    if op_row and op_row["status"] == "completed" and op_row["output_path"]:
        p = Path(op_row["output_path"])
        if p.is_file() and (not op_row["output_sha256"] or hashlib.sha256(p.read_bytes()).hexdigest() == op_row["output_sha256"]):
            op_completed = True
    if target_row["status"] == "completed" and not op_completed:
        raise ValueError(f"Completed operation '{target_op_id}' has no integrity-verified output")
    if not op_completed and not presend_proven and not invalid_output_proven and abandonment is None:
        recovery_frame_ids, feedback = plan_initial_storyboard_recovery(recovery_plan)
        start_path = ""
        if target_request.frame_role == "end":
            scene_id = target_request.frame_id.removesuffix("-end")
            start_request = next(request for request in requests if request.frame_id == f"{scene_id}-start")
            reusable_scenes, prior_frames = resolve_reusable_storyboard_frames(
                previous_package, workflow_id=workflow_id, project_id=str(aggregate["project_id"]), script=script,
            )
            previous_start = prior_frames.get(start_request.frame_id)
            if previous_start and scene_id in reusable_scenes and start_request.frame_id not in recovery_frame_ids:
                start_path = str(previous_start.get("image_path") or "")
                start_sha = previous_start.get("sha256")
            else:
                start = store.get(start_request.operation_id)
                if not start or start["status"] != "completed":
                    raise ValueError("END keyframe requires its completed START frame")
                start_path = str(start["output_path"] or "")
                start_sha = start["output_sha256"]
            path = Path(start_path)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != start_sha:
                raise ValueError("END keyframe START conditioning image changed")
        target_request = prepare_keyframe_generation_request(
            target_request, feedback=feedback.get(target_request.frame_id, ""), conditioning_start_path=start_path,
        )
        if _stable_request_digest(target_request) != target_row["request_digest"]:
            raise ValueError("Quarantined keyframe request does not match the submitted request digest")
        capacity_store = ProviderCapacityStore(connection)
        lease_owner = f"operator_reconcile_{owner_user_id}_{target_op_id}"
        acquired = capacity_store.try_acquire(
            provider_kind=provider_kind,
            operation_id=target_op_id,
            lease_owner=lease_owner,
            limit=1,
            profile_id=str(getattr(provider, "profile_id", "") or ""),
            reconciliation_only=True,
        )
        if not acquired:
            raise ValueError(f"Provider capacity for reconciliation on operation '{target_op_id}' could not be acquired")
        if provider_id:
            capacity_store.bind_provider_operation(provider_kind, target_op_id, provider_id)
        if hasattr(provider, "lease_owner"):
            provider.lease_owner = lease_owner

        reconcile_output = getattr(provider, "reconcile_existing_output", None)
        result = (
            reconcile_output(target_request, provider_id)
            if callable(reconcile_output)
            else provider.get_status(provider_id)
        )
        if str(getattr(result, "provider_operation_id", "") or "") != str(provider_id):
            raise ValueError(
                f"Provider status did not confirm exact operation identity '{provider_id}'"
            )
        if result.status == "completed":
            store.save_result(target_request, result)
            op_completed = True
        elif result.status in {"unknown", "submitted", "running", "project_created", "submitting"}:
            if result.error_code != "FLOW_PROJECT_CHECKPOINT_INVALID":
                store.save_result(target_request, result)
            raise ValueError(
                f"Provider operation '{provider_id}' is still {result.status} "
                f"({result.error_code or 'OUTCOME_UNRESOLVED'}): "
                f"{result.error_message or 'keep its capacity reserved and reconcile before replacement'}"
            )
        elif result.status not in {"failed", "cancelled", "canceled"} or result.error_code == "RECONCILIATION_REQUIRED":
            raise ValueError(
                f"Provider operation '{provider_id}' has no confirmed terminal failure/cancellation; keep its capacity reserved"
            )
        if op_completed:
            op_row = connection.execute(
                "SELECT status, output_path, output_sha256 FROM video_factory_keyframe_operation WHERE operation_id = ?",
                (target_op_id,),
            ).fetchone()

    other_pending_rows = connection.execute(
        "SELECT operation_id FROM video_factory_keyframe_operation "
        "WHERE workflow_id = ? AND operation_id != ? AND status IN ('unknown', 'submitting', 'project_created')",
        (workflow_id, target_op_id),
    ).fetchall()
    pending_operation_ids = [
        r["operation_id"]
        for r in other_pending_rows
        if r["operation_id"] in by_operation
    ]

    if op_completed:
        if len(pending_operation_ids) > 0:
            return {
                "workflow_id": workflow_id,
                "reconciled": 1,
                "replaced": 0,
                "resumed": False,
                "pending_operation_ids": pending_operation_ids,
            }
        outbox_repo = OutboxRepository(connection)
        capacity = ProviderCapacityStore(connection)
        with connection:
            capacity.release_terminal_in_transaction(provider_kind, target_op_id)
            if op_row and op_row["output_sha256"]:
                connection.execute(
                    """UPDATE workflow_qc_retries
                       SET status='completed', verdict='generated', output_hash=?,
                           action_taken='reconciled_exact_provider_output', reason_text=?, updated_at=?
                       WHERE workflow_id=? AND operation_id=? AND retry_type='four_agent_image_send'
                         AND status='technical_unknown' AND input_hash=?""",
                    (
                        op_row["output_sha256"],
                        f"Reconciled exact Flow project {provider_id}; output SHA-256 verified",
                        now,
                        workflow_id,
                        target_op_id,
                        hashlib.sha256(target_request.prompt.encode("utf-8")).hexdigest(),
                    ),
                )
            repo.commit_workflow_in_transaction(
                workflow_id=workflow_id,
                aggregate_json=json.dumps(aggregate),
                events=[{
                    "event_id": f"evt_keyframe_output_reconciled_{target_op_id}",
                    "command_id": f"cmd_keyframe_output_reconciled_{target_op_id}",
                    "event_type": "KeyframeOutputReconciled",
                    "timestamp": now,
                    "payload": {
                        "operation_id": target_op_id,
                        "provider_operation_id": provider_id,
                        "output_sha256": op_row["output_sha256"] if op_row else "",
                        "download_attempts": json.loads(
                            (store.get(target_op_id) or {}).get("provider_metadata_json") or "{}"
                        ).get("output_reconciliation_attempts", 0),
                    },
                }],
                expected_version=version,
            )
            requeued = outbox_repo.requeue_quarantined_in_transaction(int(row["outbox_id"]))
            if not requeued:
                raise RuntimeError(f"Failed to requeue quarantined storyboard job {row['outbox_id']}")
        return {
            "workflow_id": workflow_id,
            "reconciled": 1,
            "replaced": 0,
            "resumed": True,
            "pending_operation_ids": [],
        }

    # Provider uncertainty must be reconciled before this function is reached.
    # Once reconciled as not accepted, Flow keyframe replacement is not gated
    # by a Video Factory monetary reservation or cost authorization.

    # Build the selected frame's plan before either partial or final resolution.
    recovery_attempt = int(aggregate.get("keyframe_recovery_attempt", 0) or 0) + 1
    previous_storyboard = aggregate.get("storyboard")
    if previous_storyboard is None:
        latest = aggregate.get("latest_artifact")
        if isinstance(latest, dict) and (latest.get("schema") in ("storyboard", "storyboard_package", "storyboard_v2") or "generated_keyframes" in latest):
            previous_storyboard = latest

    # A storyboard job may generate several frames before one later frame
    # fails. Rebuild a digest-bound partial package from completed operations
    # in the operation ledger so recovery can reuse their exact bytes.
    completed_keyframes: list[dict[str, Any]] = []
    completed_frame_ids: set[str] = set()
    for request in requests:
        if request.frame_role != "start":
            continue
        completed = connection.execute(
            "SELECT status, output_path, output_sha256 FROM video_factory_keyframe_operation "
            "WHERE workflow_id = ? AND operation_id = ?",
            (workflow_id, request.operation_id),
        ).fetchone()
        if not completed or completed["status"] != "completed" or not completed["output_path"]:
            continue
        output_path = Path(completed["output_path"])
        if (
            not output_path.is_file()
            or not completed["output_sha256"]
            or hashlib.sha256(output_path.read_bytes()).hexdigest() != completed["output_sha256"]
        ):
            continue
        reference_sha256s = []
        for reference_path in request.reference_image_paths:
            reference = Path(reference_path)
            if not reference.is_file():
                reference_sha256s = []
                break
            reference_sha256s.append(hashlib.sha256(reference.read_bytes()).hexdigest())
        if not reference_sha256s:
            continue
        completed_keyframes.append({
            "frame_id": request.frame_id,
            "scene_id": request.frame_id.removesuffix("-start").removesuffix("-end"),
            "frame_role": request.frame_role,
            "image_path": str(output_path),
            "sha256": str(completed["output_sha256"]),
            "status": "completed",
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "reference_image_paths": list(request.reference_image_paths),
            "reference_sha256s": reference_sha256s,
            "conditioning_start_sha256": "",
        })
        completed_frame_ids.add(request.frame_id)
    if completed_keyframes and not isinstance(previous_storyboard, dict):
        script_dict = script.to_dict()
        previous_storyboard = {
            "schema_version": "storyboard-package-v2",
            "storyboard_id": f"sb_{aggregate.get('project_id')}",
            "version": 1,
            "owner_user_id": owner_user_id,
            "generated_keyframes": completed_keyframes,
            "storyboard_frames": [],
            "project_id": aggregate.get("project_id"),
            "workflow_id": workflow_id,
            "script": script_dict,
            "scenes": script_dict.get("scenes", []),
            "recovery_attempt": int(aggregate.get("keyframe_recovery_attempt", 0) or 0),
            "end_keyframe_omissions": [],
        }
        previous_storyboard["package_digest"] = storyboard_package_digest(previous_storyboard)

    existing_plan = aggregate.get("keyframe_recovery_plan") or {}
    existing_frames = [
        f for f in existing_plan.get("frames", [])
        if isinstance(f, dict)
        and f.get("frame_id") != target_request.frame_id
        and f.get("frame_id") not in completed_frame_ids
    ]
    if invalid_output_proven:
        dimensions = operation_metadata.get("output_dimensions")
        recovery_feedback = (
            f"Previous Google Flow output was verified as {dimensions[0]}x{dimensions[1]} "
            f"(SHA-256 {expected_sha}) and rejected by the native 9:16 gate. Create a new native full-frame 9:16 "
            "vertical image from the attached approved product references; fill the entire portrait canvas with "
            "one continuous scene, with no bars, borders, padding, or inset square image. Do not place or reformat "
            "the square output onto a vertical canvas. Preserve the exact approved scene-plan moment and product identity."
        )
    elif presend_proven:
        recovery_feedback = f"Operator-authorized replacement after confirmed pre-Send failure for {target_op_id}"
    else:
        recovery_feedback = f"Operator-authorized replacement after unknown outcome for {target_op_id}"
    new_frame = {
        "frame_id": target_request.frame_id,
        "actions": ["regenerate_keyframe"],
        "feedback": recovery_feedback,
    }
    plan = {"frames": existing_frames if omitted_legacy_end else existing_frames + [new_frame]}

    def retire_previous_operation(capacity):
        if abandonment is not None:
            updated = connection.execute(
                """UPDATE video_factory_keyframe_operation
                   SET status='abandoned_unknown', error_code='OPERATOR_REPLACED_UNRETRIEVABLE_OUTPUT',
                       error_message=?, provider_metadata_json=?, updated_at=CURRENT_TIMESTAMP
                   WHERE operation_id=? AND workflow_id=? AND provider_operation_id=?
                     AND status='unknown' AND COALESCE(output_path,'')='' AND error_code=?
                     AND provider_metadata_json=?""",
                (abandonment["note"], json.dumps({**metadata, "operator_abandonment": abandonment}),
                 target_op_id, workflow_id, provider_id, target_row["error_code"], target_row["provider_metadata_json"]),
            )
            if updated.rowcount != 1:
                raise ValueError("The Flow operation changed before replacement")
            slot = connection.execute(
                "SELECT provider_operation_id, lease_owner, lease_expires_at "
                "FROM video_factory_provider_capacity WHERE provider_kind=? AND operation_id=?",
                (provider_kind, target_op_id),
            ).fetchone()
            if slot is not None:
                if slot["provider_operation_id"] != provider_id:
                    raise ValueError("Provider capacity slot is missing or bound to a different operation")
                if (slot["lease_expires_at"] > datetime.now(timezone.utc).timestamp()
                        or process_lease_owner_alive(slot["lease_owner"])):
                    raise ValueError("The old Flow operation still has an active lease")
            connection.execute(
                """UPDATE workflow_qc_retries SET status='abandoned_unknown',
                   action_taken='operator_abandoned_unretrievable_output', reason_text=?, updated_at=?
                   WHERE workflow_id=? AND operation_id=? AND retry_type='four_agent_image_send'
                     AND status='technical_unknown'""",
                (abandonment["note"], now, workflow_id, target_op_id),
            )
        elif not invalid_output_proven:
            connection.execute(
                "UPDATE video_factory_keyframe_operation SET status='failed', "
                "error_code='REPLACED_AFTER_UNKNOWN_OUTCOME', updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                (target_op_id,),
            )
        capacity.release_terminal_in_transaction(provider_kind, target_op_id)

    if len(pending_operation_ids) > 0:
        # Partial resolution: keep other uncertain operations quarantined.
        updated = dict(aggregate)
        updated["keyframe_recovery_plan"] = plan
        event = {
            "event_id": f"evt_unknown_keyframe_recovery_{workflow_id}_v{version + 1}",
            "command_id": f"cmd_unknown_keyframe_recovery_{workflow_id}_v{version + 1}",
            "event_type": "UnknownKeyframeReplacementAuthorized",
            "timestamp": now,
            "payload": {
                "quarantined_outbox_id": int(row["outbox_id"]),
                "unknown_operation_ids": [target_op_id],
            },
        }
        if abandonment is not None:
            event["payload"]["operator_abandonment"] = abandonment
        if omitted_legacy_end:
            event["payload"]["omitted_frame_id"] = target_request.frame_id
        capacity = ProviderCapacityStore(connection)
        with connection:
            retire_previous_operation(capacity)
            repo.commit_workflow_in_transaction(
                workflow_id=workflow_id,
                aggregate_json=json.dumps(updated),
                events=[event],
                outbox_message=None,
                expected_version=version,
            )
        return {
            "workflow_id": workflow_id,
            "reconciled": 0,
            "replaced": int(not omitted_legacy_end),
            "resumed": False,
            "pending_operation_ids": pending_operation_ids,
        }

    # Final resolution commits the complete recovery aggregate and outbox together.
    outbox = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_product_video.storyboard_recovery_{workflow_id}_a{recovery_attempt}_v{version + 1}",
        payload={
            "job_name": "product_video.storyboard",
            "workflow_id": workflow_id,
            "project_id": aggregate.get("project_id"),
            "args": [scene_ref],
            "kwargs": {
                "project_id": aggregate.get("project_id"),
                "owner_user_id": owner_user_id,
                "recovery_plan": plan,
                "recovery_attempt": recovery_attempt,
                "previous_storyboard": previous_storyboard,
            },
            "owner_user_id": owner_user_id,
        },
    )

    next_aggregate = dict(aggregate)
    next_aggregate["state"] = ProductVideoWorkflowState.CREATING_STORYBOARD.value
    next_aggregate["keyframe_recovery_plan"] = plan
    next_aggregate["keyframe_recovery_attempt"] = recovery_attempt
    for key in ("operator_action", "operator_stage", "operator_reason", "operator_qc_report_id"):
        next_aggregate.pop(key, None)

    retry_event = {
        "event_id": f"evt_qc_retry_{workflow_id}_v{version + 1}",
        "command_id": f"cmd_qc_retry_{workflow_id}_v{version + 1}",
        "event_type": "QCRetryAuthorized",
        "timestamp": now,
        "payload": {"stage": QCStage.KEYFRAME.value},
    }
    recovery_event = {
        "event_id": f"evt_unknown_keyframe_recovery_{workflow_id}_v{version + 1}",
        "command_id": f"cmd_unknown_keyframe_recovery_{workflow_id}_v{version + 1}",
        "event_type": "UnknownKeyframeReplacementAuthorized",
        "timestamp": now,
        "payload": {
            "quarantined_outbox_id": int(row["outbox_id"]),
            "unknown_operation_ids": [target_op_id],
        },
    }
    if abandonment is not None:
        recovery_event["payload"]["operator_abandonment"] = abandonment
    if presend_proven:
        recovery_event["event_type"] = "PreSendKeyframeRetryAuthorized"
        recovery_event["payload"] = {
            "quarantined_outbox_id": int(row["outbox_id"]),
            "operation_id": target_op_id,
                "evidence": (
                    f"{presend_failure_code}; operation ledger send_attempted=false "
                    "and no provider operation/output"
                ),
        }
    elif invalid_output_proven:
        recovery_event["event_type"] = "KnownInvalidKeyframeOutputReplacementAuthorized"
        recovery_event["payload"] = {
            "quarantined_outbox_id": int(row["outbox_id"]),
            "operation_id": target_op_id,
            "output_sha256": expected_sha,
            "output_dimensions": operation_metadata.get("output_dimensions"),
            "evidence": "provider output received; recorded file SHA and decoded dimensions verified; native 9:16 ratio rejected",
        }
    if omitted_legacy_end:
        recovery_event["event_type"] = "UnplannedEndKeyframeOmitted"
        recovery_event["payload"]["omitted_frame_id"] = target_request.frame_id
    events = [
    ]
    events.append(recovery_event)
    if not presend_proven and not omitted_legacy_end:
        events.append(retry_event)

    capacity = ProviderCapacityStore(connection)
    with connection:
        retire_previous_operation(capacity)
        repo.commit_workflow_in_transaction(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=events,
            outbox_message=outbox,
            expected_version=version,
        )

    return {
        "workflow_id": workflow_id,
        "state": next_aggregate["state"],
        "version": version + 1,
        "recovery_attempt": recovery_attempt,
        "recovery_plan": plan,
        "reconciled": 0,
        "replaced": int(not omitted_legacy_end),
        "resumed": True,
        "pending_operation_ids": [],
    }


def confirm_keyframe_submission(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    operation_id: str,
    image_path: str | Path,
    owner_user_id: str = "local-owner",
    project_url: str | None = None,
    resume_workflow: bool = True,
) -> dict[str, Any]:
    """Operator action 1: Confirm genuine verified output image for an unknown/project_created keyframe.

    Validates workflow ownership, active storyboard state, quarantined outbox source,
    scene plan membership, operation existence in reconciliation state, image bytes integrity,
    and exact project URL match BEFORE any mutation. Updates the durable operation record to 'completed'
    atomically and resumes quarantined storyboard outbox only when all operations are resolved.
    """
    import hashlib
    from PIL import Image
    from video_factory.creative.contracts import ResearchDossier, ScriptScenePlanV2
    from video_factory.creative.storyboard import compile_keyframe_requests
    from video_factory.integrations.capacity import ProviderCapacityStore
    from video_factory.qc.retry_policy import ensure_qc_retry_schema
    from video_factory.workflows.outbox_repository import OutboxRepository

    ensure_qc_retry_schema(connection)

    repo = WorkflowRepository(connection)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = workflow
    if aggregate.get("owner_user_id") != owner_user_id:
        raise ValueError("Workflow belongs to another owner")

    # Pre-condition: Workflow must be in an active storyboard state
    if aggregate.get("state") not in (
        ProductVideoWorkflowState.CREATING_STORYBOARD.value,
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
    ):
        raise ValueError(
            f"Workflow '{workflow_id}' is in state '{aggregate.get('state')}', not in an active storyboard state"
        )

    # Pre-condition: Quarantined storyboard outbox check must succeed BEFORE any mutation
    outbox_row = connection.execute(
        "SELECT outbox_id, message, state, last_error FROM workflow_outbox "
        "WHERE workflow_id = ? ORDER BY outbox_id DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    if not outbox_row or outbox_row["state"] != "quarantined":
        raise ValueError("No quarantined storyboard job found for keyframe confirmation")

    try:
        envelope = json.loads(outbox_row["message"])
        job_name = envelope.get("payload", {}).get("job_name")
    except Exception:
        job_name = None
    if job_name != "product_video.storyboard":
        raise ValueError(f"Quarantined job is '{job_name}', expected 'product_video.storyboard'")

    last_err = str(outbox_row["last_error"] or "")
    if not any(code in last_err for code in ("NETWORK_UNKNOWN_OUTCOME", "RECONCILIATION_REQUIRED", "GOOGLE_FLOW_KEYFRAME_TIMEOUT", "OUTPUT_RECONCILIATION_EXHAUSTED")):
        raise ValueError("Quarantined storyboard job last_error indicates a deterministic error, not an uncertain outcome")

    outbox_id_to_requeue = int(outbox_row["outbox_id"])

    scene_ref = aggregate.get("scene_plan")
    if not isinstance(scene_ref, dict) or not isinstance(scene_ref.get("research_dossier"), dict):
        raise ValueError("Approved Scene Plan and dossier are required")
    script = ScriptScenePlanV2.from_dict(scene_ref)
    dossier = ResearchDossier.from_dict(scene_ref["research_dossier"])
    prompt_bundle = backend_profile = None
    if scene_ref.get("creative_mode") == "four_agent":
        from video_factory.creative.agents.contracts import BackendProfile, ScenePromptBundle

        prompt_bundle = ScenePromptBundle.from_dict(scene_ref["scene_prompts"])
        backend_profile = BackendProfile.from_dict(scene_ref["backend_profile"])
    requests = compile_keyframe_requests(
        workflow_id=workflow_id,
        project_id=str(aggregate["project_id"]),
        dossier=dossier,
        script=script,
        prompt_bundle=prompt_bundle,
        backend_profile=backend_profile,
    )
    by_operation = {req.operation_id: req for req in requests}
    if operation_id not in by_operation:
        raise ValueError(f"Operation '{operation_id}' does not belong to the approved Scene Plan")

    row = connection.execute(
        "SELECT operation_id, workflow_id, provider_operation_id, provider_kind, status, provider_metadata_json "
        "FROM video_factory_keyframe_operation WHERE operation_id = ? AND workflow_id = ?",
        (operation_id, workflow_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"Keyframe operation '{operation_id}' not found for workflow '{workflow_id}'")

    if row["status"] not in ("unknown", "project_created", "submitting"):
        raise ValueError(
            f"Operation '{operation_id}' is in status '{row['status']}', not in a reconciliation state ('unknown', 'project_created', 'submitting')"
        )

    recorded_url = str(row["provider_operation_id"] or "").strip()
    if not recorded_url:
        raise ValueError(f"Operation '{operation_id}' has no recorded project URL for reconciliation")
    if not project_url or not str(project_url).strip():
        raise ValueError("Operator project_url is required for keyframe confirmation")
    provided_url = str(project_url).strip()
    if provided_url != recorded_url:
        raise ValueError(
            f"Project URL mismatch: provided '{provided_url}' does not match recorded '{recorded_url}'"
        )

    img_p = Path(image_path).expanduser().resolve()
    if not img_p.is_file() or img_p.stat().st_size == 0:
        raise ValueError(f"Image file not found or empty: {img_p}")

    try:
        with Image.open(img_p) as im:
            im.verify()
    except Exception as exc:
        raise ValueError(f"Corrupted or invalid image bytes at {img_p}: {exc}") from exc

    file_bytes = img_p.read_bytes()
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()

    meta = {}
    if row["provider_metadata_json"]:
        try:
            meta = json.loads(row["provider_metadata_json"])
        except Exception:
            meta = {}
    meta["project_url"] = recorded_url
    meta["operator_confirmed"] = True
    meta["confirmed_at"] = datetime.now(timezone.utc).isoformat()

    pending_rows = connection.execute(
        "SELECT operation_id FROM video_factory_keyframe_operation "
        "WHERE workflow_id = ? AND status IN ('unknown', 'submitting', 'project_created')",
        (workflow_id,),
    ).fetchall()
    pending_operation_ids = [
        p["operation_id"]
        for p in pending_rows
        if p["operation_id"] in by_operation and p["operation_id"] != operation_id
    ]

    resumed_ok = False
    provider_kind = str(row["provider_kind"] or "google_flow")
    capacity = ProviderCapacityStore(connection)
    outbox_repo = OutboxRepository(connection)
    with connection:
        connection.execute(
            """UPDATE video_factory_keyframe_operation
               SET status = 'completed',
                   output_path = ?,
                   output_sha256 = ?,
                   provider_operation_id = ?,
                   provider_metadata_json = ?,
                   error_code = '',
                   error_message = '',
                   updated_at = CURRENT_TIMESTAMP
               WHERE operation_id = ?""",
            (str(img_p), file_sha256, recorded_url, json.dumps(meta, sort_keys=True), operation_id),
        )
        capacity.release_terminal_in_transaction(provider_kind, operation_id)
        prompt_hash = hashlib.sha256(by_operation[operation_id].prompt.encode("utf-8")).hexdigest()
        connection.execute(
            """UPDATE workflow_qc_retries
               SET status='completed', verdict='generated', output_hash=?,
                   action_taken='reconciled_exact_provider_output', reason_text=?, updated_at=?
               WHERE workflow_id=? AND operation_id=? AND retry_type='four_agent_image_send'
                 AND status='technical_unknown' AND input_hash=?""",
            (
                file_sha256,
                f"Operator reconciled exact Flow project {recorded_url}; output SHA-256 verified",
                datetime.now(timezone.utc).isoformat(),
                workflow_id,
                operation_id,
                prompt_hash,
            ),
        )

        if resume_workflow and len(pending_operation_ids) == 0:
            resumed_ok = outbox_repo.requeue_quarantined_in_transaction(outbox_id_to_requeue)
            if not resumed_ok:
                raise RuntimeError(f"Failed to requeue quarantined storyboard job {outbox_id_to_requeue}")

    recovery_result = {
        "workflow_id": workflow_id,
        "reconciled": 1,
        "replaced": 0,
        "resumed": resumed_ok,
        "pending_operation_ids": pending_operation_ids,
    }
    return {
        "status": "confirmed",
        "workflow_id": workflow_id,
        "operation_id": operation_id,
        "image_path": str(img_p),
        "sha256": file_sha256,
        "project_url": recorded_url,
        "resumed": resumed_ok,
        "pending_operation_ids": pending_operation_ids,
        "recovery_result": recovery_result,
    }


def record_terminal_keyframe_outcome(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    operation_id: str,
    owner_user_id: str,
    project_url: str,
    outcome: str,
    evidence_reference: str,
    note: str,
) -> dict[str, Any]:
    """Record an operator-verified terminal Flow failure/cancellation and release its exact slot."""
    import hashlib
    from urllib.parse import urlsplit
    from video_factory.creative.contracts import ResearchDossier, ScriptScenePlanV2
    from video_factory.creative.storyboard import compile_keyframe_requests
    from video_factory.integrations.capacity import ProviderCapacityStore
    from video_factory.qc.retry_policy import ensure_qc_retry_schema

    terminal = str(outcome or "").strip().lower()
    if terminal not in {"failed", "cancelled", "canceled"}:
        raise ValueError("A confirmed terminal outcome (failed or cancelled) is required")
    terminal = "cancelled" if terminal == "canceled" else terminal
    recorded_url = str(project_url or "").strip()
    evidence = str(evidence_reference or "").strip()
    clean_note = str(note or "").strip()
    if not recorded_url or not evidence or len(evidence) > 500:
        raise ValueError("Exact project URL and evidence reference (1-500 characters) are required")
    parsed_url = urlsplit(recorded_url)
    if parsed_url.scheme != "https" or parsed_url.netloc != "flow.google.com" or not parsed_url.path.startswith("/project/") or parsed_url.query or parsed_url.fragment:
        raise ValueError("A canonical Google Flow project URL is required")
    if not clean_note or len(clean_note) > 1000:
        raise ValueError("Operator reconciliation note (1-1000 characters) is required")
    ensure_qc_retry_schema(connection)

    repo = WorkflowRepository(connection)
    current = repo.get_workflow(workflow_id)
    if current is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = current
    if aggregate.get("owner_user_id") != owner_user_id:
        raise ValueError("Workflow belongs to another owner")
    if aggregate.get("state") not in (
        ProductVideoWorkflowState.CREATING_STORYBOARD.value,
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
    ):
        raise ValueError("Workflow is not awaiting keyframe reconciliation")
    outbox = connection.execute(
        "SELECT state, last_error, message FROM workflow_outbox "
        "WHERE workflow_id=? ORDER BY outbox_id DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    if not outbox or outbox["state"] != "quarantined" or not any(
        code in str(outbox["last_error"] or "")
        for code in ("NETWORK_UNKNOWN_OUTCOME", "RECONCILIATION_REQUIRED", "GOOGLE_FLOW_KEYFRAME_TIMEOUT", "OUTPUT_RECONCILIATION_EXHAUSTED")
    ):
        raise ValueError("No quarantined uncertain storyboard operation is available to reconcile")
    envelope = json.loads(outbox["message"])
    if envelope.get("payload", {}).get("job_name") != "product_video.storyboard":
        raise ValueError("Quarantined job is not storyboard generation")

    scene_ref = aggregate.get("scene_plan")
    if not isinstance(scene_ref, dict) or not isinstance(scene_ref.get("research_dossier"), dict):
        raise ValueError("Approved Scene Plan and dossier are required")
    script = ScriptScenePlanV2.from_dict(scene_ref)
    dossier = ResearchDossier.from_dict(scene_ref["research_dossier"])
    prompt_bundle = backend_profile = None
    if scene_ref.get("creative_mode") == "four_agent":
        from video_factory.creative.agents.contracts import BackendProfile, ScenePromptBundle
        prompt_bundle = ScenePromptBundle.from_dict(scene_ref["scene_prompts"])
        backend_profile = BackendProfile.from_dict(scene_ref["backend_profile"])
    requests = compile_keyframe_requests(
        workflow_id=workflow_id,
        project_id=str(aggregate["project_id"]),
        dossier=dossier,
        script=script,
        prompt_bundle=prompt_bundle,
        backend_profile=backend_profile,
        recovery_plan=aggregate.get("keyframe_recovery_plan") if isinstance(aggregate.get("keyframe_recovery_plan"), dict) else None,
        recovery_attempt=int(aggregate.get("keyframe_recovery_attempt", 0) or 0),
    )
    request = next((item for item in requests if item.operation_id == operation_id), None)
    if request is None:
        raise ValueError(f"Operation '{operation_id}' does not belong to the approved Scene Plan")
    op = connection.execute(
        "SELECT provider_operation_id, provider_kind, status, output_path, provider_metadata_json "
        "FROM video_factory_keyframe_operation WHERE workflow_id=? AND operation_id=?",
        (workflow_id, operation_id),
    ).fetchone()
    if op is None:
        raise KeyError(f"Keyframe operation '{operation_id}' not found")
    if op["status"] not in {"unknown", "submitting", "project_created"} or op["output_path"]:
        raise ValueError("Operation is not unresolved or already has output")
    if str(op["provider_operation_id"] or "").strip() != recorded_url:
        raise ValueError("Project URL does not match the operation ledger")
    capacity = ProviderCapacityStore(connection)
    slot = connection.execute(
        "SELECT provider_operation_id FROM video_factory_provider_capacity "
        "WHERE provider_kind=? AND operation_id=?",
        (op["provider_kind"] or "google_flow", operation_id),
    ).fetchone()
    if slot is None or str(slot["provider_operation_id"] or "").strip() != recorded_url:
        raise ValueError("Provider capacity slot is missing or bound to a different operation")

    metadata = json.loads(op["provider_metadata_json"] or "{}")
    metadata["operator_terminal_reconciliation"] = {
        "outcome": terminal,
        "project_url": recorded_url,
        "evidence_reference": evidence,
        "note": clean_note,
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
    }
    now = datetime.now(timezone.utc).isoformat()
    retry_hash = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
    next_aggregate = dict(aggregate)
    next_aggregate["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    next_aggregate.update({
        "operator_action": "qc_provider_recovery",
        "operator_stage": "keyframe",
        "operator_reason": f"Flow operation {terminal} was reconciled by operator; retry requires explicit workflow recovery",
        "failure_code": f"PROVIDER_OPERATION_TERMINAL_{terminal.upper()}",
    })
    event = {
        "event_id": f"evt_keyframe_terminal_reconciled_{workflow_id}_{operation_id}",
        "command_id": f"cmd_keyframe_terminal_reconciled_{workflow_id}_{operation_id}",
        "event_type": "KeyframeOperationTerminalReconciled",
        "timestamp": now,
        "payload": {
            "operation_id": operation_id,
            "provider_operation_id": recorded_url,
            "outcome": terminal,
            "evidence_reference": evidence,
            "note": clean_note,
        },
    }
    with connection:
        updated = connection.execute(
            """UPDATE video_factory_keyframe_operation
               SET status=?, error_code=?, error_message=?, provider_metadata_json=?, updated_at=CURRENT_TIMESTAMP
               WHERE workflow_id=? AND operation_id=? AND provider_operation_id=?
                 AND status IN ('unknown','submitting','project_created')
                 AND COALESCE(output_path,'')=''""",
            (
                terminal, f"OPERATOR_RECONCILED_{terminal.upper()}",
                clean_note, json.dumps(metadata, sort_keys=True), workflow_id, operation_id, recorded_url,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("Operation changed during reconciliation; no state was released")
        slot = connection.execute(
            "SELECT provider_operation_id FROM video_factory_provider_capacity "
            "WHERE provider_kind=? AND operation_id=?",
            (op["provider_kind"] or "google_flow", operation_id),
        ).fetchone()
        if slot is None or str(slot["provider_operation_id"] or "").strip() != recorded_url:
            raise ValueError("Provider capacity binding changed during reconciliation")
        retry_status = connection.execute(
            "SELECT 1 FROM workflow_qc_retries WHERE workflow_id=? AND operation_id=? "
            "AND retry_type='four_agent_image_send' AND input_hash=? AND status='technical_unknown' LIMIT 1",
            (workflow_id, operation_id, retry_hash),
        ).fetchone()
        if retry_status:
            connection.execute(
                "UPDATE workflow_qc_retries SET status=?, verdict=?, action_taken='operator_terminal_reconciliation', "
                "reason_text=?, updated_at=? WHERE workflow_id=? AND operation_id=? "
                "AND retry_type='four_agent_image_send' AND input_hash=? AND status='technical_unknown'",
                (terminal, terminal, clean_note, now, workflow_id, operation_id, retry_hash),
            )
        capacity.release_terminal_in_transaction(str(op["provider_kind"] or "google_flow"), operation_id)
        repo.commit_workflow(
            workflow_id, json.dumps(next_aggregate, ensure_ascii=False, sort_keys=True),
            [event], expected_version=version, in_transaction=True,
        )
    return {
        "workflow_id": workflow_id,
        "operation_id": operation_id,
        "provider_operation_id": recorded_url,
        "status": terminal,
        "capacity_released": True,
        "workflow_resumed": False,
    }


def create_operator_command_handler(
    connection: sqlite3.Connection,
    provider_mode: str = "configured",
) -> Callable[[OperatorCommand], dict[str, Any]]:
    """Create canonical operator command handler for EventsAPI."""
    repo = WorkflowRepository(connection)

    def handle_command(cmd: OperatorCommand) -> dict[str, Any]:
        if cmd.command_type == "start_workflow":
            payload = cmd.payload
            wf_id = cmd.target_id
            product_name = payload.get("product_name", "Default Product")
            keywords = payload.get("keywords", [])
            pdp_url = payload.get("pdp_url")

            project_id = payload.get("project_id") or f"proj_{wf_id}"

            start_product_video_workflow(
                workflow_repository=repo,
                workflow_id=wf_id,
                product_name=product_name,
                keywords=keywords,
                pdp_url=pdp_url,
                project_id=project_id,
                owner_user_id=cmd.owner_user_id,
                idempotency_key=cmd.idempotency_key,
                qc_authorization=payload.get("qc_authorization"),
                qc_max_cost_usd=payload.get("qc_max_cost_usd"),
                research_snapshot_id=payload.get("research_snapshot_id"),
            )
            return {"workflow_id": wf_id, "status": "started"}

        elif cmd.command_type == "approve":
            payload_dict = dict(cmd.payload) if cmd.payload else {}
            return approve_and_advance_workflow(
                connection=connection,
                target_id=cmd.target_id,
                owner_user_id=cmd.owner_user_id,
                payload=payload_dict,
                expected_version=cmd.expected_version,
            )

        elif cmd.command_type == "reject":
            approval_service = ApprovalService(connection)
            approval_service.reject(cmd.target_id)
            return {"target_id": cmd.target_id, "status": "rejected"}

        return {"target_id": cmd.target_id, "status": "accepted"}

    return handle_command


def _creative_attempt_id_from_aggregate(aggregate: dict[str, Any]) -> str:
    message = str(aggregate.get("operator_reason") or "")
    marker = "route_receipt="
    offset = message.find(marker)
    if offset < 0:
        return ""
    raw = message[offset + len(marker):].strip()
    try:
        receipt, _end = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return ""
    return str(receipt.get("attempt_id") or "").strip() if isinstance(receipt, dict) else ""


def can_resume_legacy_invalid_response_skip(connection: sqlite3.Connection, workflow_id: str) -> bool:
    """Recognize old script skips only when the failed response is durably recorded."""
    row = connection.execute("SELECT aggregate_json FROM workflow WHERE workflow_id=?", (workflow_id,)).fetchone()
    if not row:
        return False
    try:
        aggregate = json.loads(row[0])
    except (TypeError, ValueError):
        return False
    if (aggregate.get('state') != 'skipped' or aggregate.get('failure_code') != 'CREATIVE_RESPONSE_INVALID'
            or not (aggregate.get('latest_artifact') or {}).get('brief_id')):
        return False
    if connection.execute("SELECT 1 FROM workflow_event WHERE workflow_id=? AND event_type='OperatorWorkflowSkipped' LIMIT 1",
                          (workflow_id,)).fetchone():
        return False
    steps = connection.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='PipelineStep' ORDER BY timestamp DESC",
        (workflow_id,),
    ).fetchall()
    recorded_script_failure = False
    for item in steps:
        try:
            step = json.loads(item[0])
        except (TypeError, ValueError):
            continue
        if (step.get('step_name') == 'SCENE_PLAN' and step.get('status') == 'pending_operator'
                and step.get('error_code') == 'CREATIVE_RESPONSE_INVALID'):
            recorded_script_failure = True
            break
    if not recorded_script_failure:
        return False
    try:
        raw = str(aggregate.get('skip_reason') or '').split('route_receipt=', 1)[1]
        receipt, _end = json.JSONDecoder().raw_decode(raw)
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if not (receipt.get('attempt_id') and receipt.get('router_usage_id')
            and receipt.get('cost_status') == 'recorded'):
        return False
    columns = {column[1] for column in connection.execute('PRAGMA table_info(workflow_outbox)')}
    if {'workflow_id', 'state'} <= columns and connection.execute(
        "SELECT 1 FROM workflow_outbox WHERE workflow_id=? AND state IN ('pending','leased') LIMIT 1",
        (workflow_id,),
    ).fetchone():
        return False
    return True


def close_creative_attempt(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    attempt_id: str,
    resolution: str,
    evidence_reference: str,
    owner_user_id: str = "local-owner",
    note: str = "",
) -> dict[str, Any]:
    """Record explicit operator closure of one UNKNOWN creative request."""
    import uuid

    allowed_resolutions = {
        "response_received_unverified",
        "not_sent_proven",
        "operator_retry_authorized",
        "abandoned_unknown",
    }
    if resolution not in allowed_resolutions:
        raise ValueError(f"Unsupported creative attempt resolution {resolution!r}")
    if not evidence_reference.strip():
        raise ValueError("Creative attempt closure requires an evidence reference")
    if resolution in {"operator_retry_authorized", "abandoned_unknown"} and not note.strip():
        raise ValueError(
            "UNKNOWN attempt retry requires a note that the prior outcome remains UNKNOWN "
            "and a duplicate request or cost is possible"
        )

    repo = WorkflowRepository(connection)
    loaded = repo.get_workflow(workflow_id)
    if loaded is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = loaded
    if aggregate.get("state") != ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
        raise ValueError(f"Workflow '{workflow_id}' is not waiting for operator reconciliation")
    if aggregate.get("operator_action") != "provider_reconciliation":
        raise ValueError("Workflow is not waiting for creative provider reconciliation")
    if aggregate.get("failure_code") != "CREATIVE_OUTCOME_UNCERTAIN":
        raise ValueError("Workflow has no UNKNOWN creative provider attempt to close")
    aggregate_owner = str(aggregate.get("owner_user_id") or owner_user_id)
    if aggregate_owner != owner_user_id:
        raise ValueError("Workflow belongs to another owner")
    current_attempt_id = _creative_attempt_id_from_aggregate(aggregate)
    if not current_attempt_id:
        raise ValueError("UNKNOWN creative attempt id is missing from the saved route trace")
    if attempt_id != current_attempt_id:
        raise ValueError("Attempt ID does not match the UNKNOWN request saved on this workflow")

    for row in connection.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='CreativeAttemptClosed'",
        (workflow_id,),
    ):
        payload = json.loads(row[0])
        if payload.get("attempt_id") != attempt_id:
            continue
        if payload.get("resolution") != resolution or payload.get("evidence_reference") != evidence_reference:
            raise ValueError("Creative attempt already has a different closure record")
        return {"status": "already_closed", "workflow_id": workflow_id, "attempt_id": attempt_id}

    now = datetime.now(timezone.utc).isoformat()
    closure = {
        "attempt_id": attempt_id,
        "resolution": resolution,
        "evidence_reference": evidence_reference.strip(),
        "note": note.strip(),
        "operator_id": owner_user_id,
        "closed_at": now,
    }
    closures = aggregate.setdefault("creative_attempt_closures", {})
    if not isinstance(closures, dict):
        raise ValueError("Saved creative attempt closures have an invalid shape")
    closures[attempt_id] = closure
    repo.commit_workflow(
        workflow_id,
        json.dumps(aggregate, ensure_ascii=False),
        [{
            "event_id": f"evt_creative_attempt_closed_{uuid.uuid4().hex}",
            "command_id": f"creative_attempt_close:{workflow_id}:{attempt_id}",
            "event_type": "CreativeAttemptClosed",
            "timestamp": now,
            "payload": closure,
        }],
        expected_version=version,
    )
    return {
        "status": "closed",
        "workflow_id": workflow_id,
        "attempt_id": attempt_id,
        "resolution": resolution,
    }


def replace_blocked_source_images(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str,
    image_paths: list[str | Path] | None = None,
    reason: str,
    product_info: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Revalidate corrected source images or identity details for a blocked Exchange item."""
    import hashlib
    from PIL import Image

    clean_reason = str(reason or "").strip()
    if not clean_reason or len(clean_reason) > 500:
        raise ValueError("A correction reason of 1–500 characters is required")
    image_paths = list(image_paths or [])
    clean_info = {str(key): str(value).strip() for key, value in (product_info or {}).items()
                  if value is not None and str(value).strip()}
    allowed_info = {"name", "brand", "model", "variant", "category", "key_features", "source_url", "price_display"}
    if set(clean_info) - allowed_info:
        raise ValueError("Unsupported product information fields")
    if not image_paths and not clean_info:
        raise ValueError("Provide replacement images or corrected product information")

    images: list[dict[str, Any]] = []
    hashes: list[str] = []
    for index, raw_path in enumerate(image_paths, start=1):
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Replacement image {index} is missing or empty: {path}")
        try:
            with Image.open(path) as image:
                image.verify()
                image_format = str(image.format or "").upper()
            with Image.open(path) as image:
                width, height = image.size
        except Exception as exc:
            raise ValueError(f"Replacement image {index} cannot be decoded: {path}") from exc
        if width < 1 or height < 1:
            raise ValueError(f"Replacement image {index} has invalid dimensions: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in hashes:
            raise ValueError("Replacement images must contain distinct bytes")
        hashes.append(digest)
        extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(image_format, "img")
        asset_id = f"user-input-{index}-{digest[:12]}"
        images.append({
            "asset_id": asset_id,
            "local_path": str(path),
            "absolute_path": str(path),
            "path": str(path),
            "sha256": digest,
            "digest": digest,
            "uri": path.as_uri(),
            "mime_type": f"image/{'jpeg' if image_format == 'JPEG' else extension}",
            "file_name": path.name,
            "identity_conflict": False,
            "identity_review_reason": "REPLACEMENT_REQUIRES_CURRENT_IMAGE_QC",
        })

    from video_factory.creative.contracts import canonical_digest
    from video_factory.workflows.repository import WorkflowRepository

    repo = WorkflowRepository(connection)
    current = repo.get_workflow(workflow_id)
    if current is None:
        raise KeyError(f"Workflow {workflow_id!r} not found")
    aggregate, version = current
    if aggregate.get("owner_user_id") != owner_user_id:
        raise ValueError("Workflow belongs to another owner")
    source = aggregate.get("source_context")
    if not isinstance(source, dict) or source.get("source") != "hermes_product_video_exchange":
        raise ValueError("Source image replacement is supported only for an Exchange product workflow")

    if not image_paths:
        for index, prior_image in enumerate(source.get("images") or [], start=1):
            if not isinstance(prior_image, dict):
                raise ValueError(f"Existing source image {index} is not a valid record")
            raw_path = prior_image.get("absolute_path") or prior_image.get("local_path") or prior_image.get("path")
            path = Path(str(raw_path or "")).expanduser().resolve()
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Existing source image {index} is missing or empty: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = str(prior_image.get("sha256") or prior_image.get("digest") or "").removeprefix("sha256:")
            if expected and digest != expected:
                raise ValueError(f"Existing source image {index} changed; supply it again with --image")
            hashes.append(digest)
            images.append({**prior_image, "local_path": str(path), "absolute_path": str(path),
                           "path": str(path), "sha256": digest, "digest": digest,
                           "uri": path.as_uri(), "identity_conflict": False,
                           "identity_review_reason": "REQUIRES_CURRENT_IMAGE_QC"})
        if not images:
            raise ValueError("No readable existing source image is available; supply at least one --image")

    next_info = {**{key: source.get(key) for key in clean_info}, **clean_info}
    next_canonical_key = str(source.get("canonical_product_key") or "")
    if any(key in clean_info for key in ("brand", "model", "variant")):
        from video_factory.automation.contracts import compute_canonical_product_key
        brand = clean_info.get("brand", str(source.get("brand") or ""))
        model = clean_info.get("model", str(source.get("model") or ""))
        variant = clean_info.get("variant", str(source.get("variant") or ""))
        if not brand or not model:
            raise ValueError("Correcting identity requires both brand and model")
        next_canonical_key = compute_canonical_product_key(brand, model, variant or None)
        for row in connection.execute("SELECT workflow_id,aggregate_json FROM workflow WHERE workflow_id!=?", (workflow_id,)):
            other = json.loads(row["aggregate_json"])
            other_source = other.get("source_context") or {}
            if (str(other_source.get("canonical_product_key") or "").casefold(),
                    str(other_source.get("variant") or "").casefold()) == (next_canonical_key.casefold(), variant.casefold()):
                raise ValueError(f"Corrected product identity is already bound to workflow {row['workflow_id']}")

    input_revision = "sha256:" + canonical_digest({
        "product": next_canonical_key or source.get("product_key"),
        "variant": next_info.get("variant", source.get("variant")),
        "product_info": clean_info,
        "image_sha256s": hashes,
    }).removeprefix("sha256:")
    prior_history = list(aggregate.get("source_input_recovery_history") or [])
    replay = next((entry for entry in prior_history if isinstance(entry, dict)
                   and entry.get("input_revision") == input_revision), None)
    if replay and source.get("source_input_revision") == input_revision:
        return {"workflow_id": workflow_id, "state": aggregate.get("state"),
                "input_revision": input_revision, "image_sha256s": hashes, "reused": True}

    if aggregate.get("state") not in {"skipped", ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value}:
        raise ValueError("Workflow is not blocked at source image QC")
    if aggregate.get("state") == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value and aggregate.get("operator_stage") != "image":
        raise ValueError("Workflow is not blocked at source image QC")
    if aggregate.get("failure_code") not in {
        "EXCHANGE_IMAGE_QC_FAILED", "PRODUCT_IMAGE_QC_REJECTED", "IMAGE_QC_FAILED",
    }:
        raise ValueError("Only a known source image QC failure can be corrected")
    if any(aggregate.get(key) for key in (
        "locked_pack", "creative_brief", "scene_plan", "storyboard", "render_result", "export_result",
    )):
        raise ValueError("Source images cannot be replaced after Product Lock or downstream work has started")
    if not isinstance(aggregate.get("research_result"), dict):
        raise ValueError("Durable Exchange product snapshot is missing")

    active_outbox = connection.execute(
        "SELECT outbox_id,state FROM workflow_outbox WHERE workflow_id=? AND state IN ('pending','leased') LIMIT 1",
        (workflow_id,),
    ).fetchone()
    if active_outbox:
        raise ValueError(f"Workflow has active outbox job {active_outbox['outbox_id']}; let it finish before replacing input")
    for table in ("workflow_qc_retries", "product_video_operation_attempt"):
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            if "workflow_id" in columns and "status" in columns and connection.execute(
                f"SELECT 1 FROM {table} WHERE workflow_id=? AND status IN ('reserved','claimed','sent','technical_unknown') LIMIT 1",
                (workflow_id,),
            ).fetchone():
                raise ValueError(f"Workflow has unresolved provider/QC work in {table}; reconcile it before replacing input")

    previous_state = str(aggregate.get("state") or "")
    previous_code = str(aggregate.get("failure_code") or "")
    previous_reason = str(aggregate.get("failure_reason") or aggregate.get("skip_reason") or aggregate.get("operator_reason") or "")
    previous_hashes = [str(item.get("sha256") or item.get("digest") or "") for item in source.get("images", [])
                       if isinstance(item, dict)]
    next_source = dict(source)
    next_source.update(next_info)
    next_source.update(canonical_product_key=next_canonical_key, images=images,
                       source_input_revision=input_revision, auto_admitted=True,
                       input_review={"replacement_reason": clean_reason})
    updated = dict(aggregate)
    updated["source_context"] = next_source
    research_result = dict(aggregate["research_result"])
    research_result["source_context"] = next_source
    if clean_info.get("name"):
        research_result["product_name"] = clean_info["name"]
    updated["research_result"] = research_result
    updated["state"] = ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value
    updated["source_input_recovery_history"] = [*prior_history, {
        "previous_state": previous_state, "previous_failure_code": previous_code,
        "previous_failure_reason": previous_reason, "previous_image_sha256s": previous_hashes,
        "previous_product_info": {key: source.get(key) for key in clean_info},
        "product_info": clean_info, "image_sha256s": hashes, "input_revision": input_revision, "reason": clean_reason,
        "replaced_at": datetime.now(timezone.utc).isoformat(),
    }]
    for key in ("resource_pack", "latest_artifact", "operator_action", "operator_stage", "operator_reason",
                "operator_qc_report_id", "failure_category", "failure_code", "failure_reason", "skip_reason", "skipped_at"):
        updated.pop(key, None)

    project_id = str(aggregate.get("project_id") or "")
    envelope = build_envelope(
        workflow_id=workflow_id, topic="job.dispatch",
        idempotency_key=f"replace-source-images-{workflow_id}-{input_revision.removeprefix('sha256:')[:20]}",
        payload={"job_name": "product_video.resource_pack", "workflow_id": workflow_id,
                 "project_id": project_id, "args": [aggregate["research_result"]],
                 "kwargs": {"project_id": project_id, "source_context": next_source,
                            "images": images, "max_qc_retries": aggregate.get("max_qc_retries", 2)},
                 "owner_user_id": owner_user_id},
    )
    now = datetime.now(timezone.utc).isoformat()
    event = {"event_id": f"evt_source_images_replaced_{workflow_id}_{input_revision[-16:]}",
             "command_id": f"cmd_source_images_replaced_{workflow_id}_{input_revision[-16:]}",
             "event_type": "SourceImagesReplaced", "timestamp": now,
             "payload": {"previous_state": previous_state, "previous_failure_code": previous_code,
                         "previous_failure_reason": previous_reason, "reason": clean_reason,
                         "image_sha256s": hashes, "input_revision": input_revision,
                         "project_id": project_id,
                         "canonical_product_key": source.get("canonical_product_key"),
                         "corrected_canonical_product_key": next_canonical_key,
                         "source_batch_id": source.get("source_batch_id")}}
    with connection:
        repo.commit_workflow(workflow_id, json.dumps(updated, ensure_ascii=False), [event],
                             outbox_message=envelope, expected_version=version, in_transaction=True)
        if next_canonical_key != str(source.get("canonical_product_key") or ""):
            connection.execute(
                "UPDATE daily_product_video_import SET canonical_product_key=?,updated_at=? WHERE workflow_id=?",
                (next_canonical_key, now, workflow_id),
            )
    try:
        from video_factory.automation.repository import DailyImportRepository
        DailyImportRepository(connection).mark_started_by_workflow_id(workflow_id)
    except (sqlite3.OperationalError, AttributeError):
        pass
    return {"workflow_id": workflow_id, "state": updated["state"],
            "input_revision": input_revision, "image_sha256s": hashes, "reused": False}


def recover_creative_stage(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    owner_user_id: str = "local-owner",
    note: str = "",
    allow_one_extra_retry: bool = False,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Reconcile a quarantined creative stage (direction, script, storyboard) and resume execution."""
    import uuid
    repo = WorkflowRepository(connection)
    loaded = repo.get_workflow(workflow_id)
    if loaded is None:
        raise KeyError(f"Workflow '{workflow_id}' not found")
    aggregate, version = loaded
    if expected_version is not None and version != expected_version:
        raise ValueError("Workflow đã thay đổi trước khi phục hồi creative")
    legacy_skip = None
    if aggregate.get('state') == 'skipped' and can_resume_legacy_invalid_response_skip(connection, workflow_id):
        legacy_skip = {'failure_code':aggregate.get('failure_code'), 'failure_category':aggregate.get('failure_category'),
                       'reason':aggregate.get('skip_reason'), 'resumed_at':datetime.now(timezone.utc).isoformat()}
        aggregate.update({'state':ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
                          'operator_action':'operator_review', 'operator_stage':'script',
                          'operator_reason':legacy_skip['reason'], 'failure_reason':legacy_skip['reason']})
    if aggregate.get("state") != ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
        raise ValueError(f"Workflow '{workflow_id}' is not in WaitingForOperator state")
    action = aggregate.get("operator_action")
    stage = aggregate.get("operator_stage")
    if action not in ("provider_reconciliation", "creative_review", "operator_review"):
        raise ValueError(f"Workflow operator_action '{action}' cannot be resumed by creative recovery")
    closed_attempt_id = ""
    if action == "provider_reconciliation" or aggregate.get("failure_code") == "CREATIVE_OUTCOME_UNCERTAIN":
        closed_attempt_id = _creative_attempt_id_from_aggregate(aggregate)
        closures = aggregate.get("creative_attempt_closures")
        closure = closures.get(closed_attempt_id) if isinstance(closures, dict) and closed_attempt_id else None
        if not isinstance(closure, dict) or closure.get("resolution") not in {
            "response_received_unverified", "not_sent_proven", "operator_retry_authorized", "abandoned_unknown"
        }:
            raise ValueError(
                "UNKNOWN creative attempt must be reconciled or explicitly closed before recover-creative; "
                "use close-creative-attempt with the saved attempt ID and evidence first"
            )

    stage_to_job = {
        "direction": ("product_video.creative_brief", ProductVideoWorkflowState.CREATING_BRIEF),
        "script": ("product_video.scene_plan", ProductVideoWorkflowState.CREATING_SCENE_PLAN),
        "storyboard": ("product_video.storyboard", ProductVideoWorkflowState.CREATING_STORYBOARD),
    }
    if stage not in stage_to_job:
        raise ValueError(f"Operator stage '{stage}' is not a recoverable creative stage")

    job_name, target_state = stage_to_job[stage]
    if stage == "script":
        script_retry_count = 0
        for row in connection.execute(
            "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='CreativeStageRecovered'",
            (workflow_id,),
        ):
            try:
                event_payload = json.loads(row[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(event_payload, dict) and event_payload.get("stage") == "script":
                script_retry_count += 1
        prior_overrides = 0
        for row in connection.execute(
            "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='CreativeStageRecovered'",
            (workflow_id,),
        ):
            try:
                event_payload = json.loads(row[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(event_payload, dict) and event_payload.get("retry_cap_override") is True:
                prior_overrides += 1
        if allow_one_extra_retry:
            if not note.strip():
                raise ValueError("Retry cap override requires an operator note")
            if script_retry_count != 2 or prior_overrides:
                raise ValueError("One extra Scene Plan retry is allowed only once after the two standard retries")
        elif script_retry_count >= 2:
            raise ValueError("Maximum two Scene Plan retries have already been used; stop and report the blocker")
    elif allow_one_extra_retry:
        raise ValueError("The one-extra-retry override applies only to Scene Plan")
    project_id = str(aggregate.get("project_id") or "").strip()
    if not project_id:
        project_id = f"proj_{workflow_id}"
    locked_pack = aggregate.get("locked_pack") or aggregate.get("resource_pack") or {}
    raw_pack = locked_pack.get("resource_pack", locked_pack)
    product_id = str(
        raw_pack.get("product_id") or raw_pack.get("resource_pack_id")
        or raw_pack.get("id") or f"prod_{workflow_id}"
    )

    now = datetime.now(timezone.utc).isoformat()
    aggregate["state"] = target_state.value
    if legacy_skip:
        aggregate['legacy_terminal_skip'] = legacy_skip
        for key in ('skip_reason', 'skipped_at', 'failure_category'):
            aggregate.pop(key, None)
    for key in ("operator_action", "operator_stage", "operator_reason", "failure_code"):
        aggregate.pop(key, None)

    if stage == "direction":
        args = [product_id, {"resource_pack": locked_pack, "project_id": project_id}]
    elif stage == "script":
        brief_ref = aggregate.get("creative_brief") or aggregate.get("latest_artifact")
        args = [brief_ref]
    elif stage == "storyboard":
        scene_ref = aggregate.get("scene_plan") or aggregate.get("latest_artifact")
        args = [scene_ref]
    else:
        args = [product_id, {"resource_pack": locked_pack, "project_id": project_id}]

    recovery_kwargs = {"project_id": project_id}
    prior_receipt = aggregate.get("latest_approval_receipt") or {}
    expected_gate = {"script":"AwaitingBriefApproval", "storyboard":"AwaitingSceneApproval"}.get(stage)
    if expected_gate and prior_receipt.get("gate") == expected_gate and prior_receipt.get("project_id") == project_id:
        recovery_kwargs["approval_receipt"] = dict(prior_receipt)
        recovery_kwargs["approval_request_id"] = prior_receipt.get("approval_request_id")
    if stage == "script" and aggregate.get("creative_policy", {}).get("creative_mode") == "four_agent":
        recovery_kwargs["revision_feedback"] = json.dumps({
            "validation_errors": str(aggregate.get("failure_reason") or "")[:2500],
            "operator_note": note[:1500],
        }, ensure_ascii=False)
    attempt_nonce = uuid.uuid4().hex[:8]
    outbox_message = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_{stage}_{workflow_id}_recovery_{attempt_nonce}",
        payload={
            "job_name": job_name,
            "workflow_id": workflow_id,
            "args": args,
            "kwargs": recovery_kwargs,
            "owner_user_id": owner_user_id,
            "project_id": project_id,
        },
    )

    repo.commit_workflow(
        workflow_id,
        json.dumps(aggregate, ensure_ascii=False),
        [{
            "event_id": f"evt_creative_recovery_{uuid.uuid4().hex}",
            "command_id": f"recover_creative:{workflow_id}:{stage}:{attempt_nonce}",
            "event_type": "CreativeStageRecovered",
            "timestamp": now,
            "payload": {
                "stage": stage,
                "note": note,
                "operator_id": owner_user_id,
                "closed_attempt_id": closed_attempt_id or None,
                "retry_cap_override": bool(allow_one_extra_retry),
                "legacy_terminal_skip_reopened": bool(legacy_skip),
            },
        }],
        outbox_message=outbox_message,
        expected_version=version,
    )
    return {
        "status": "recovered",
        "workflow_id": workflow_id,
        "stage": stage,
        "job_name": job_name,
        "workflow_version": version + 1,
        "retry_cap_override": bool(allow_one_extra_retry),
    }
