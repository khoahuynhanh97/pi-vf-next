"""CLI for Hermes Product Video selection, automation, and approval."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from video_factory.constants import get_hermes_home
from video_factory.application.operator_handler import (
    approve_and_advance_workflow,
    recover_unknown_keyframe_submission,
    record_terminal_keyframe_outcome,
)
from video_factory.candidate_service import (
    CandidateItem,
    DailyCandidateService,
)
from video_factory.workflow import (
    HITL_WAITING_STATES,
    ProductVideoWorkflowState,
    transition,
)
from video_factory.automation.repository import DailyImportRepository
from video_factory.qc.models import QCStage, QCVerdict
from video_factory.qc.image_qc import compute_artifact_digest
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.service import ProductVideoQCService
from video_factory.qc.video_qc import compute_file_sha256
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.approvals import ApprovalService
from video_factory.workflows.envelope import build_envelope
from video_factory.workflows.repository import WorkflowRepository
from video_factory.workflows.worker import run_worker


def cmd_open_folder(args: argparse.Namespace) -> int:
    """Open VideoAI hub folder in Windows File Explorer."""
    import os
    import subprocess
    from video_factory.storage.video_hub import (
        find_latest_video,
        get_video_ai_hub_root,
    )
    
    hub_root = Path(args.hub_root) if args.hub_root else get_video_ai_hub_root()
    
    if args.latest:
        folder = find_latest_video(hub_root, brand=args.brand)
        if not folder:
            print("No video folders found in hub")
            return 1
        target = folder
    else:
        if not hub_root.exists():
            print(f"Hub root does not exist: {hub_root}")
            return 1
        target = hub_root
    
    print(f"Opening: {target}")
    
    try:
        if os.name == 'nt':
            os.startfile(str(target))
        else:
            subprocess.run(["xdg-open", str(target)], check=True)
        return 0
    except Exception as e:
        print(f"Failed to open folder: {e}")
        return 1


def cmd_sync_video_hub(args: argparse.Namespace) -> int:
    """Sync all historical exports to VideoAI hub."""
    from video_factory.storage.video_hub import (
        sync_all_exports_to_hub,
        get_video_ai_hub_root,
    )
    
    # Resolve exports root
    if args.exports_root:
        exports_root = Path(args.exports_root)
    else:
        try:
            from video_factory.storage.layout import ProductVideoWorkspaceLayout
            layout = ProductVideoWorkspaceLayout()
            exports_root = layout.exports_dir
        except Exception:
            workspace = Path(os.getenv("HERMES_VIDEO_FACTORY_WORKSPACE", 
                                       get_hermes_home() / "video_factory_workspace"))
            exports_root = workspace / "exports"

    
    hub_root = Path(args.hub_root) if args.hub_root else get_video_ai_hub_root()
    
    print(f"Syncing exports from: {exports_root}")
    print(f"To VideoAI hub: {hub_root}")
    print()
    
    result = sync_all_exports_to_hub(exports_root, hub_root)
    
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"✓ Synced: {result['total_synced']}")
        print(f"⊘ Skipped: {result['skipped']}")
        print(f"✗ Errors: {result['errors']}")
        
        if result['synced_folders']:
            print("\nSynced folders:")
            for item in result['synced_folders']:
                print(f"  • [{item['product']}]")
                print(f"    → {item['hub_path']}")
    
    return 0 if result['errors'] == 0 else 1


def _resolve_db_path(db_path_arg: str | None) -> Path:
    if db_path_arg:
        return Path(db_path_arg)
    return get_hermes_home() / "hermes.db"


def _normalize_file_path(path_or_url: str | Path) -> Path:
    s = str(path_or_url).strip()
    if s.startswith("file://"):
        raw = s[7:]
        while raw.startswith(("/", "\\")) and not (len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha()):
            raw = raw[1:]
        return Path(raw).resolve()
    return Path(s).resolve()


def _format_status_badge(status: str) -> str:
    badges = {
        "unprocessed": "[CHƯA TẠO VIDEO]",
        "awaiting_product_lock": "[CHỜ DUYỆT PRODUCT LOCK]",
        "in_production": "[ĐANG SẢN XUẤT]",
        "completed": "[ĐÃ HOÀN TẤT VIDEO]",
        "waiting_for_operator": "[CHỜ NGƯỜI XỬ LÝ (OPERATOR)]",
        "invalid": "[KHÔNG ĐẠT TIÊU CHUẨN]",
        "skipped": "[ĐÃ BỎ QUA]",
    }
    return badges.get(status, f"[{status.upper()}]")


def _quarantined_job_error(conn: Any, workflow_id: str, state: str) -> str | None:
    """Expose a terminal worker error while the aggregate still shows its old step."""
    if state not in {
        ProductVideoWorkflowState.RESEARCHING.value,
        ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value,
    }:
        return None
    row = conn.execute(
        "SELECT state, last_error FROM workflow_outbox WHERE workflow_id = ? "
        "ORDER BY outbox_id DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    if row is None or row["state"] != "quarantined":
        return None
    return str(row["last_error"] or "Worker job was quarantined")


def _record_auto_approval_failure(conn: Any, workflow_id: str, state: str, error: Exception) -> None:
    step_name = {
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value: "PRODUCT_LOCK",
        ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value: "CREATIVE_BRIEF",
        ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value: "SCENE_PLAN",
        ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value: "KEYFRAME_GENERATION",
    }.get(str(state))
    if not step_name:
        return
    from video_factory.workflows.steps import record_pipeline_step

    aggregate = WorkflowRepository(conn).get_workflow(workflow_id)
    product_key = str((aggregate[0].get("product_key") if aggregate else None) or workflow_id)
    record_pipeline_step(
        conn,
        workflow_id=workflow_id,
        product_key=product_key,
        step_name=step_name,
        status="pending_operator",
        cost_status="not_applicable",
        error_code="AUTO_APPROVAL_FAILED",
        message=f"Approval did not complete ({type(error).__name__}): {error}",
        next_action="Inspect the workflow approval state and resolve the error before resuming the batch.",
    )


def _qc_authorization_from_args(args: argparse.Namespace, workflow_id: str | None = None) -> dict[str, Any] | None:
    """Build an optional explicit QC authorization without inventing defaults."""
    run_id = getattr(args, "qc_authorization_run_id", None) or getattr(args, "authorization_run_id", None)
    scopes = getattr(args, "qc_authorization_scope", None) or getattr(args, "authorization_scope", None) or []
    expires_at = getattr(args, "qc_authorization_expires_at", None) or getattr(args, "authorization_expires_at", None)
    auth_max = getattr(args, "qc_authorization_max_cost_usd", None) or getattr(args, "authorization_max_cost_usd", None)
    budget_mode = getattr(args, "qc_budget_mode", None)
    if not (run_id or scopes or expires_at or auth_max is not None):
        return None
    auth: dict[str, Any] = {
        "run_id": run_id,
        "scope": list(scopes),
        "expires_at": expires_at,
        "max_cost_usd": auth_max,
    }
    if budget_mode:
        auth["qc_budget_mode"] = budget_mode
    campaign_id = getattr(args, "authorization_campaign_id", None) or getattr(args, "qc_authorization_campaign_id", None)
    if campaign_id:
        auth["campaign_id"] = campaign_id
    if auth_max is not None:
        auth["campaign_max_cost_usd"] = 100.0
    if workflow_id:
        auth["workflow_id"] = workflow_id
    return auth


def _render_authorization_for_args(args: argparse.Namespace, workflow_id: str) -> dict[str, Any] | None:
    """Build the explicit Vertex/render authorization required by --authorize-cost."""
    if not getattr(args, "authorize_cost", False):
        return _qc_authorization_from_args(args, workflow_id=workflow_id)
    import uuid
    from datetime import timedelta

    auth = _qc_authorization_from_args(args, workflow_id=workflow_id) or {}
    now = datetime.now(timezone.utc)
    auth["run_id"] = str(auth.get("run_id") or f"auth-render-{uuid.uuid4().hex[:12]}")
    scope = auth.get("scope")
    scopes = scope if isinstance(scope, list) else [scope] if scope else []
    if not scopes:
        scopes = ["video"]
    if "video" not in scopes and "product_video" not in scopes:
        raise ValueError("Render authorization scope must include 'video' or 'product_video'")
    auth["scope"] = scopes
    auth["workflow_id"] = workflow_id
    auth["expires_at"] = str(auth.get("expires_at") or (now + timedelta(days=2)).isoformat())
    try:
        expiry = datetime.fromisoformat(auth["expires_at"].replace("Z", "+00:00"))
        max_cost = float(auth.get("workflow_max_cost_usd", auth.get("max_cost_usd", 100.0)))
        campaign_max = float(auth.get("campaign_max_cost_usd", 100.0))
    except (TypeError, ValueError):
        raise ValueError("Render authorization needs a valid ISO-8601 expiry and USD limit")
    if expiry.tzinfo is None or expiry <= now:
        raise ValueError("Render authorization expiry must be a future timezone-aware ISO-8601 time")
    if max_cost < 0.50 or max_cost > 100.0 or campaign_max < max_cost or campaign_max > 100.0:
        raise ValueError("Render authorization must be $0.50–$100 and remain within the $100 pilot cap")
    auth["max_cost_usd"] = max_cost
    auth["workflow_max_cost_usd"] = max_cost
    auth["campaign_max_cost_usd"] = campaign_max
    auth["campaign_id"] = str(auth.get("campaign_id") or f"vertex-pilot-{workflow_id}")
    return auth


def _auto_render_authorization_error(aggregate: dict[str, Any], workflow_id: str) -> str | None:
    """Keep auto approval at the storyboard gate until render and video QC are authorized."""
    if aggregate.get("creative_policy", {}).get("creative_mode") == "four_agent":
        return None
    auth = aggregate.get("qc_authorization")
    if not isinstance(auth, dict) or auth.get("workflow_id") != workflow_id:
        return "render requires persisted qc_authorization bound to this workflow"
    scope = auth.get("scope")
    scopes = scope if isinstance(scope, list) else [scope]
    if "video" not in scopes and "product_video" not in scopes:
        return "render requires video or product_video authorization scope"
    try:
        max_cost = float(auth.get("workflow_max_cost_usd", auth.get("max_cost_usd")))
        expires_at = datetime.fromisoformat(str(auth["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return "render authorization needs a valid budget and expiry"
    if max_cost < 0.50 or max_cost > 100.0 or expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
        return "render authorization is expired, below 0.50 USD, or exceeds the 100 USD pilot cap"
    return None


def _advance_qc_waiting_workflow(
    conn: Any,
    workflow_id: str,
    stage: QCStage,
    authorization: dict[str, Any] | None = None,
    qc_passed_retry: bool = False,
) -> str:
    """Create the next human gate only after configured QC passed this exact wait."""
    repo = WorkflowRepository(conn)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow '{workflow_id}' was not found")
    aggregate, version = workflow
    if aggregate.get("state") != ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
        raise ValueError("Workflow is not waiting for QC authorization")
    if aggregate.get("operator_action") not in {
        "qc_authorization",
        "qc_retry_authorization",
        "qc_retry",
        "qc_reconciliation",
        "qc_provider_retry",
        "qc_provider_recovery",
        "qc_retry_exhausted",
    }:
        raise ValueError("Workflow is not waiting for a QC authorization")
    if aggregate.get("operator_stage") != stage.value:
        raise ValueError(f"Workflow is waiting for {aggregate.get('operator_stage')!r} QC, not {stage.value!r}")

    if (
        stage is QCStage.KEYFRAME
        and aggregate.get("operator_action") == "qc_retry_authorization"
        and not qc_passed_retry
    ):
        return _schedule_keyframe_recovery(
            conn,
            workflow_id,
            aggregate,
            version,
        )

    if stage is QCStage.IMAGE:
        target = ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK
        gate = ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
        artifact = aggregate.get("resource_pack") or aggregate.get("latest_artifact")
    elif stage is QCStage.KEYFRAME:
        target = ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL
        gate = ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
        artifact = aggregate.get("storyboard") or aggregate.get("latest_artifact")
    else:
        target = ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL
        gate = ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL.value
        artifact = aggregate.get("render_result") or aggregate.get("latest_artifact")
    if not isinstance(artifact, dict):
        raise ValueError("Waiting workflow is missing its persisted stage artifact")

    if stage is QCStage.KEYFRAME:
        current_pass = QCReportRepository(conn).get_latest_report(
            workflow_id,
            stage,
            expected_artifact_digest=str(artifact.get("package_digest") or ""),
            provider_mode="configured",
            owner_user_id=str(aggregate.get("owner_user_id") or ""),
            project_id=str(aggregate.get("project_id") or ""),
            artifact_id=str(artifact.get("storyboard_id") or "") or None,
            artifact_version=int(artifact.get("version") or 0) or None,
            verdict=QCVerdict.PASS,
        )
        if current_pass is None:
            raise ValueError("Current configured keyframe QC PASS report is required to advance")
        artifact = dict(artifact)
        artifact["keyframe_qc_receipt"] = {
            "report_id": current_pass.report_id,
            "stage": stage.value,
            "verdict": current_pass.verdict.value,
            "score": current_pass.score,
            "artifact_digest": current_pass.artifact_digest,
            "findings": current_pass.findings,
        }

    artifact_id = str(
        artifact.get("artifact_id")
        or artifact.get("resource_pack_id")
        or artifact.get("storyboard_id")
        or artifact.get("render_id")
        or artifact.get("id")
        or ""
    )
    if not artifact_id:
        raise ValueError("Persisted stage artifact is missing an identifier")

    next_state = transition(ProductVideoWorkflowState.WAITING_FOR_OPERATOR, target)
    next_aggregate = dict(aggregate)
    next_aggregate["state"] = next_state.value
    if stage is QCStage.KEYFRAME:
        next_aggregate["storyboard"] = artifact
        next_aggregate["latest_artifact"] = artifact
    recovered_infra_pause = (
        aggregate.get("failure_category") == "QC_INFRA_PAUSED"
        and aggregate.get("operator_action") in {"qc_provider_recovery", "qc_reconciliation"}
    )
    if recovered_infra_pause:
        next_aggregate.pop("failure_category", None)
        next_aggregate.pop("failure_code", None)
        next_aggregate.pop("failure_reason", None)
    if authorization is not None:
        next_aggregate["qc_authorization"] = authorization
    next_aggregate.pop("operator_action", None)
    next_aggregate.pop("operator_stage", None)
    next_aggregate.pop("operator_reason", None)
    next_aggregate.pop("operator_qc_report_id", None)

    approval_svc = ApprovalService(conn)
    with conn:
        cursor = conn.cursor()
        approval_id = approval_svc.submit_request(
            {
                "workflow_id": workflow_id,
                "project_id": aggregate.get("project_id"),
                "gate": gate,
                "workflow_version": version + 1,
                "artifact_ref": artifact,
                "artifact_id": artifact_id,
                "artifact_version": artifact.get("version") or artifact.get("resource_pack_version"),
                "owner_user_id": aggregate.get("owner_user_id"),
                "qc_stage": stage.value,
            },
            cursor=cursor,
        )
        next_aggregate["pending_approval_gate"] = gate
        next_aggregate["pending_approval_id"] = approval_id
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(next_aggregate),
            events=[
                {
                    "event_id": f"evt_qc_advanced_{workflow_id}_v{version + 1}",
                    "command_id": f"cmd_qc_advance_{workflow_id}_{stage.value}_v{version + 1}",
                    "event_type": "QCCompletedAwaitingHumanApproval",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": {"stage": stage.value, "artifact_id": artifact_id},
                }
            ],
            expected_version=version,
        )
        if recovered_infra_pause:
            from video_factory.automation.repository import DailyImportRepository

            DailyImportRepository(conn).mark_started_by_workflow_id(workflow_id)
    return approval_id


def _record_failed_keyframe_qc(
    conn: Any,
    workflow_id: str,
    report: Any,
) -> None:
    """Persist a real failed QC result so the existing recovery path can resume."""
    repo = WorkflowRepository(conn)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow '{workflow_id}' was not found")
    aggregate, version = workflow
    if (aggregate.get("state"), aggregate.get("operator_action"), aggregate.get("operator_stage")) != (
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
        "qc_authorization",
        QCStage.KEYFRAME.value,
    ):
        raise ValueError("Workflow is not waiting for keyframe QC authorization")
    storyboard = aggregate.get("storyboard") or aggregate.get("latest_artifact")
    if not isinstance(storyboard, dict) or report.artifact_digest != storyboard.get("package_digest"):
        raise ValueError("QC report does not match the current storyboard")
    recovery_plan = report.findings.get("recovery_plan") if isinstance(report.findings, dict) else None
    if not isinstance(recovery_plan, dict) or not recovery_plan.get("frames"):
        raise ValueError("Failed keyframe QC has no actionable recovery plan")
    updated = dict(aggregate)
    updated["operator_action"] = "qc_retry_authorization"
    updated["operator_qc_report_id"] = report.report_id
    updated["keyframe_recovery_plan"] = recovery_plan
    updated["operator_reason"] = "Keyframe QC failed; regenerate only the failed frames"
    with conn:
        repo.commit_workflow(
            workflow_id=workflow_id,
            aggregate_json=json.dumps(updated),
            events=[{
                "event_id": f"evt_keyframe_qc_failed_{workflow_id}_v{version + 1}",
                "command_id": f"cmd_keyframe_qc_failed_{workflow_id}_v{version + 1}",
                "event_type": "KeyframeQCRecoveryRequired",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"report_id": report.report_id, "failed_frames": len(recovery_plan["frames"])},
            }],
            expected_version=version,
        )


def _pause_pipeline_for_qc_result(
    conn: Any,
    workflow_id: str,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Persist provider/contract QC blockers so CLI use cannot bypass worker pause."""
    if result.get("status") not in {"pending", "unknown", "failed"}:
        return None
    from video_factory.automation.failure_classification import (
        FailureCategory,
        classify_failure,
    )
    from video_factory.workflows.runtime_control import pause_pipeline

    code = str(result.get("code") or "QC_OUTCOME_UNCERTAIN")
    category = classify_failure(code)
    if category not in {
        FailureCategory.QC_INFRA_PAUSED,
        FailureCategory.CONTRACT_BROKEN,
    }:
        return None
    message = str(result.get("reason") or "QC result requires operator investigation")
    pause_pipeline(
        conn,
        category=category,
        code=code,
        message=message,
        workflow_id=workflow_id,
    )
    workflow_repository = WorkflowRepository(conn)
    workflow = workflow_repository.get_workflow(workflow_id)
    if workflow is not None:
        aggregate, version = workflow
        updated = dict(aggregate)
        updated["failure_category"] = category.value
        updated["failure_code"] = code
        updated["failure_reason"] = message
        updated["operator_reason"] = message
        if category == FailureCategory.QC_INFRA_PAUSED:
            updated["state"] = ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
            updated["operator_action"] = (
                "qc_reconciliation"
                if "UNKNOWN" in code.upper() or "UNCERTAIN" in code.upper()
                else "qc_provider_recovery"
            )
            updated["operator_stage"] = str(result.get("stage") or "unknown")
        else:
            updated["state"] = "contract_broken"
            updated["operator_action"] = "repair_contract"
        workflow_repository.commit_workflow(
            workflow_id,
            json.dumps(updated),
            events=[{
                "event_id": f"evt_cli_qc_paused_{workflow_id}_v{version + 1}",
                "command_id": f"cmd_cli_qc_paused_{workflow_id}_v{version + 1}",
                "event_type": "PipelinePaused",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"category": category.value, "code": code, "reason": message},
            }],
            expected_version=version,
        )
    return {"category": category.value, "code": code, "message": message}


def _mark_cli_image_qc_rejected(
    conn: Any,
    workflow_id: str,
    stage: QCStage,
    result: dict[str, Any],
) -> str | None:
    """Persist an exhausted semantic Image QC failure as a terminal product skip."""
    if stage is not QCStage.IMAGE or result.get("status") != "failed":
        return None
    from video_factory.automation.failure_classification import (
        FailureCategory,
        classify_failure,
        has_consumed_semantic_image_qc_evidence,
    )

    code = str(result.get("code") or "")
    if code == "IMAGE_QC_FAILED" and has_consumed_semantic_image_qc_evidence(result):
        code = "PRODUCT_IMAGE_QC_REJECTED"
    if classify_failure(code) != FailureCategory.PRODUCT_REJECTED:
        return None
    repo = WorkflowRepository(conn)
    workflow = repo.get_workflow(workflow_id)
    if workflow is None:
        return None
    aggregate, version = workflow
    if (
        aggregate.get("state") != ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
        or aggregate.get("operator_stage") != QCStage.IMAGE.value
    ):
        return None
    active_outbox = conn.execute(
        "SELECT 1 FROM workflow_outbox WHERE workflow_id=? AND state IN ('pending','leased') LIMIT 1",
        (workflow_id,),
    ).fetchone()
    if active_outbox:
        return None
    product_key = str(
        aggregate.get("product_key")
        or (aggregate.get("source_context") or {}).get("canonical_product_key")
        or workflow_id
    )
    reason = str(result.get("reason") or "Image QC exhausted its semantic recovery budget")
    failure_report: dict[str, Any] = {
        "stage": QCStage.IMAGE.value,
        "report_id": result.get("report_id"),
        "reason": reason,
        "evaluated_items": result.get("evaluated_items", []),
        "distinct_passed_count": result.get("distinct_passed_count"),
    }
    updated = dict(aggregate)
    updated.update({
        "state": "skipped",
        "failure_category": FailureCategory.PRODUCT_REJECTED.value,
        "failure_code": code,
        "failure_reason": reason,
        "failure_report": failure_report,
        "product_key": product_key,
        "skip_reason": reason,
    })
    for key in ("operator_action", "operator_stage", "operator_reason", "operator_qc_report_id"):
        updated.pop(key, None)
    repo.commit_workflow(
        workflow_id,
        json.dumps(updated),
        events=[{
            "event_id": f"evt_cli_image_rejected_{workflow_id}_v{version + 1}",
            "command_id": f"cmd_cli_image_rejected_{workflow_id}_v{version + 1}",
            "event_type": "ProductSkipped",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"product_key": product_key, "code": code, "reason": reason},
        }],
        expected_version=version,
    )
    DailyImportRepository(conn).mark_error_by_workflow_id(
        workflow_id,
        f"PRODUCT_REJECTED:{code}:{reason}",
    )
    return product_key


def _schedule_keyframe_recovery(
    conn: Any,
    workflow_id: str,
    aggregate: dict[str, Any],
    version: int,
) -> str:
    """Regenerate failed keyframes before spending on another QC evaluation."""

    scene_plan = aggregate.get("scene_plan")
    storyboard = aggregate.get("storyboard") or aggregate.get("latest_artifact")
    recovery_plan = aggregate.get("keyframe_recovery_plan")
    if not isinstance(scene_plan, dict):
        raise ValueError("Keyframe recovery requires the persisted approved scene plan")
    if not isinstance(storyboard, dict):
        raise ValueError("Keyframe recovery requires the previous storyboard package")
    if not isinstance(recovery_plan, dict) or not recovery_plan.get("frames"):
        raise ValueError("Keyframe recovery requires a persisted recovery plan")

    # An END generated from START must be regenerated when that START changes.
    frames = [dict(item) for item in recovery_plan["frames"] if isinstance(item, dict)]
    selected = {str(item.get("frame_id") or "") for item in frames}
    prior_ids = {
        str(item.get("frame_id") or "") for item in storyboard.get("generated_keyframes", [])
        if isinstance(item, dict)
    }
    for item in list(frames):
        frame_id = str(item.get("frame_id") or "")
        if not frame_id.endswith("-start"):
            continue
        end_id = frame_id.removesuffix("-start") + "-end"
        if end_id in prior_ids and end_id not in selected:
            frames.append({
                "frame_id": end_id,
                "scene_id": frame_id.removesuffix("-start"),
                "actions": ["regenerate_keyframe"],
                "feedback": "Regenerate END against the corrected START frame; preserve exact setting and intended end state.",
            })
            selected.add(end_id)
    recovery_plan = {**recovery_plan, "frames": frames}

    attempt = int(aggregate.get("keyframe_recovery_attempt", 0) or 0) + 1
    next_state = transition(
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
        ProductVideoWorkflowState.CREATING_STORYBOARD,
    )
    next_aggregate = dict(aggregate)
    next_aggregate["state"] = next_state.value
    next_aggregate["keyframe_recovery_attempt"] = attempt
    next_aggregate["keyframe_recovery_plan"] = recovery_plan
    for key in (
        "operator_action",
        "operator_stage",
        "operator_reason",
        "operator_qc_report_id",
        "pending_approval_gate",
        "pending_approval_id",
    ):
        next_aggregate.pop(key, None)

    envelope = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_storyboard_recovery_{workflow_id}_a{attempt}_v{version + 1}",
        payload={
            "job_name": "product_video.storyboard",
            "workflow_id": workflow_id,
            "args": [scene_plan],
            "kwargs": {
                "project_id": aggregate.get("project_id"),
                "recovery_plan": recovery_plan,
                "recovery_attempt": attempt,
                "previous_storyboard": storyboard,
            },
            "owner_user_id": aggregate.get("owner_user_id", "local-owner"),
            "project_id": aggregate.get("project_id"),
        },
    )
    WorkflowRepository(conn).commit_workflow(
        workflow_id=workflow_id,
        aggregate_json=json.dumps(next_aggregate),
        events=[
            {
                "event_id": f"evt_keyframe_recovery_{workflow_id}_a{attempt}",
                "command_id": f"cmd_keyframe_recovery_{workflow_id}_a{attempt}",
                "event_type": "KeyframeRecoveryScheduled",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "attempt": attempt,
                    "failed_frames": len(recovery_plan.get("frames", [])),
                },
            }
        ],
        expected_version=version,
        outbox_message=envelope,
    )
    return "recovery:" + str(json.loads(envelope)["message_id"])


def cmd_list(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db_path)
    conn = connect(db_path)
    try:
        inbox_root = getattr(args, "inbox_root", None)
        source_root = getattr(args, "legacy_source_root", None) or getattr(args, "source_root", None)
        service = DailyCandidateService(conn, source_root=source_root, inbox_root=inbox_root, db_path=db_path)
        target_date = date.fromisoformat(args.date) if args.date else None
        try:
            candidates = service.get_candidates(target_date)
        except FileNotFoundError as exc:
            print(f"[LỖI EXCHANGE INBOX] {exc}", file=sys.stderr)
            return 1

        if args.status and args.status != "all":
            candidates = [c for c in candidates if c.status == args.status]

        if args.json:
            print(json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2))
            return 0

        source_date = candidates[0].source_date if candidates else (args.date or "N/A")
        print(f"\n=== DANH SÁCH SẢN PHẨM PRODUCT-INTELLIGENCE ({source_date}) ===")
        print(f"Tổng số: {len(candidates)} sản phẩm | Database: {db_path}\n")

        for idx, c in enumerate(candidates, start=1):
            badge = _format_status_badge(c.status)
            print(f"[{idx:02d}] {badge} {c.name}")
            print(f"     Brand: {c.brand} | Model: {c.model} | Giá: {c.price_display}")
            print(f"     Số ảnh hợp lệ: {len(c.images)} | Key: {c.product_key[:12]}...")
            if c.status == "awaiting_product_lock":
                if c.workflow_id:
                    print(f"     -> Chờ duyệt tại Gate: {c.gate} | Approval ID: {c.approval_id} | WF: {c.workflow_id}")
                    if c.rejection_reason:
                        print(f"     -> Lý do cần xem lại: {c.rejection_reason}")
                else:
                    print(f"     -> Chờ xác nhận đầu vào: {c.rejection_reason}")
                for image in c.images:
                    print(f"        - {image.get('asset_id')}: {image.get('original_url') or '(không có URL)'} | {image.get('identity_review_reason') or 'cần xem ảnh'}")
            elif c.status == "in_production":
                print(f"     -> Đang sản xuất tại WF: {c.workflow_id}")
            elif c.status == "waiting_for_operator":
                print(f"     -> Chờ người xử lý (Operator) tại WF: {c.workflow_id}")
                if c.rejection_reason:
                    print(f"     -> Lý do: {c.rejection_reason}")
            elif c.status in {"invalid", "skipped"} and c.rejection_reason:
                print(f"     -> Lý do: {c.rejection_reason}")
            elif c.status == "invalid":
                print(f"     -> Lý do loại: {c.rejection_reason}")
                if c.rejection_reason and "APPROVED_IMAGE_MISMATCH" in c.rejection_reason:
                    target_id = c.workflow_id or c.product_key
                    print(f"        [GỢI Ý] Đặt lại: python -m video_factory.cli reset {target_id}")
                    print(f'                Chạy lại: python -m video_factory.cli start --product-keys {c.product_key} --confirmed-image-asset-id <id> --confirmed-claim "claim|https://nguồn-thật"')
            elif c.hot_reason:
                print(f"     Hot: {c.hot_reason[:90]}...")
            print()

        return 0
    finally:
        conn.close()


def cmd_start(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db_path)
    conn = connect(db_path)
    try:
        inbox_root = getattr(args, "inbox_root", None)
        source_root = getattr(args, "legacy_source_root", None) or getattr(args, "source_root", None)
        catalog_path = getattr(args, "catalog_v2_candidates", None)
        source_is_exchange = bool(
            source_root
            and (
                list(Path(source_root).glob("????-??-??/run_*/READY"))
                or (Path(source_root) / "READY").is_file()
            )
        )
        if catalog_path is None and (not source_root or source_is_exchange):
            catalog_env = os.environ.get("HERMES_CATALOG_V2_CANDIDATES")
            from video_factory.automation.catalog_candidates import DEFAULT_CATALOG_V2_CANDIDATES

            catalog_path = Path(catalog_env) if catalog_env else DEFAULT_CATALOG_V2_CANDIDATES
        service = DailyCandidateService(
            conn,
            source_root=source_root,
            inbox_root=inbox_root,
            db_path=db_path,
            catalog_candidates_path=catalog_path,
        )
        minimum_daily = int(getattr(args, "minimum_daily", 0) or 0)
        target_date = (
            date.today() if args.date == "today" or (minimum_daily and not args.date)
            else date.fromisoformat(args.date) if args.date else None
        )
        if getattr(args, "all_products", False):
            if not args.auto:
                print("[ERROR] --all chỉ dùng cùng --auto.", file=sys.stderr)
                return 1
            args.limit = min(100, max(1, len(service.get_candidates(target_date))))
        if minimum_daily < 0 or minimum_daily > args.limit:
            print("[ERROR] --minimum-daily phải nằm từ 0 đến --limit.", file=sys.stderr)
            return 1
        qc_authorization = _qc_authorization_from_args(args)
        if getattr(args, "authorize_cost", False) and qc_authorization is None:
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
        qc_max_cost_usd = getattr(args, "qc_max_cost_usd", None)
        research_snapshot_id = getattr(args, "research_snapshot_id", None)
        confirmed_assets = list(getattr(args, "confirmed_image_asset_id", []) or [])
        confirmed_claims = []
        for raw_claim in getattr(args, "confirmed_claim", []) or []:
            claim_text, separator, source_url = str(raw_claim).partition("|")
            if not separator or not claim_text.strip() or not source_url.strip():
                print("[ERROR] --confirmed-claim phải có dạng 'claim|https://nguồn-thật'", file=sys.stderr)
                return 1
            confirmed_claims.append({"text": claim_text.strip(), "source_url": source_url.strip()})
        input_reviews = None
        if confirmed_assets or confirmed_claims:
            review = {"confirmed_image_asset_ids": confirmed_assets, "confirmed_claims": confirmed_claims}
            input_reviews = {key: review for key in (args.product_keys or [])}

        try:
            max_qc_retries = getattr(args, "max_qc_retries", None)
            if args.auto:
                print(f"[*] Bắt đầu tìm kiếm tự động sản phẩm chưa tạo video (Giới hạn: {args.limit})...")
                batch_res = service.start_auto(
                    limit=args.limit,
                    owner_user_id=args.owner_user_id,
                    target_date=target_date,
                    qc_authorization=qc_authorization,
                    qc_max_cost_usd=qc_max_cost_usd,
                    research_snapshot_id=research_snapshot_id,
                    max_qc_retries=max_qc_retries,
                    execution_mode="auto" if getattr(args, "auto_approve", True) else "manual",
                )
            elif args.product_keys:
                print(f"[*] Bắt đầu tạo video cho {len(args.product_keys)} sản phẩm theo khóa chỉ định...")
                batch_res = service.start_selection(
                    args.product_keys,
                    owner_user_id=args.owner_user_id,
                    target_date=target_date,
                    qc_authorization=qc_authorization,
                    qc_max_cost_usd=qc_max_cost_usd,
                    research_snapshot_id=research_snapshot_id,
                    input_reviews=input_reviews,
                    max_qc_retries=max_qc_retries,
                )
            else:
                print("[ERROR] Cần cung cấp --auto hoặc danh sách --product-keys. Hoặc dùng lệnh 'select' để chọn trực quan.")
                return 1
        except FileNotFoundError as exc:
            print(f"[LỖI EXCHANGE INBOX] {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1

        if batch_res.has_errors:
            for s in batch_res.skipped:
                if s.reason.startswith(("CLAIM_ERROR", "START_FAILED", "ERROR", "BATCH_STOPPED")):
                    print(f"[LỖI KHỞI ĐỘNG] Sản phẩm {s.product_key}: {s.reason}", file=sys.stderr)
            return 1

        def report_daily_shortfall() -> bool:
            if not minimum_daily or target_date is None:
                return False
            rows = DailyImportRepository(conn).get_all_for_date(target_date.isoformat())
            daily_total = sum(row["status"] == "started" for row in rows.values())
            if daily_total < minimum_daily:
                print(
                    f"[THIẾU SẢN PHẨM] {target_date}: đã khởi động {daily_total}/{minimum_daily} "
                    "workflow từ Product-Intelligence; kiểm tra batch READY và sản phẩm hợp lệ.",
                    file=sys.stderr,
                )
                for candidate in service.get_candidates(target_date):
                    if candidate.status == "invalid":
                        print(
                            f"  - {candidate.name}: {candidate.rejection_reason or 'không hợp lệ'}",
                            file=sys.stderr,
                        )
                return True
            print(f"[MỤC TIÊU NGÀY] {target_date}: đã khởi động {daily_total} workflow.")
            return False

        started_wfs = [s.workflow_id for s in batch_res.started]
        research_wfs = list(started_wfs)
        if args.with_research and target_date is not None:
            workflow_repo = WorkflowRepository(conn)
            import_repo = DailyImportRepository(conn)
            for row in import_repo.get_all_for_date(target_date.isoformat()).values():
                if row["status"] != "started":
                    continue
                wf_id = row["workflow_id"]
                workflow = workflow_repo.get_workflow(wf_id)
                if not workflow:
                    continue
                wf_state = str(workflow[0].get("state") or "")
                if wf_state in {
                    ProductVideoWorkflowState.RESEARCHING.value,
                    ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value,
                } and wf_id not in research_wfs:
                    q_err = _quarantined_job_error(conn, wf_id, wf_state)
                    if q_err:
                        continue
                    research_wfs.append(wf_id)

        if not started_wfs and not research_wfs:
            if report_daily_shortfall():
                return 1
            benign = all(s.reason in ("ALREADY_STARTED", "ALREADY_CLAIMED") for s in batch_res.skipped)
            ledger_rows = list(
                DailyImportRepository(conn).get_all_for_date(target_date.isoformat()).values()
                if target_date is not None else ()
            )
            persisted_skips = {
                row["product_key"]: row
                for row in ledger_rows if row["status"] == "error"
            }
            if any(row["status"] == "started" for row in ledger_rows) or (
                benign and batch_res.skipped
            ) or (
                getattr(args, "auto", False) and not batch_res.skipped and not persisted_skips
            ):
                print("[THÔNG BÁO] Không có sản phẩm mới nào cần khởi động (Tất cả sản phẩm đã được xử lý hoặc có video).")
                return 0
            print("[THÔNG BÁO] Không có sản phẩm mới nào được khởi động.", file=sys.stderr)
            reported = set()
            for skipped in batch_res.skipped:
                row = persisted_skips.get(skipped.product_key)
                product_label = row["canonical_product_key"] if row else skipped.product_key
                print(f"  - {product_label}: {skipped.reason}", file=sys.stderr)
                reported.add(skipped.product_key)
            for product_key, row in persisted_skips.items():
                if product_key not in reported:
                    print(f"  - {row['canonical_product_key']}: {row.get('last_error') or 'SKIPPED'}", file=sys.stderr)
            return 1

        if started_wfs:
            print(f"[THÀNH CÔNG] Đã tạo {len(started_wfs)} workflow mới:")
            for wf_id in started_wfs:
                print(f"  + {wf_id}")
        elif research_wfs:
            print(f"[*] Tiếp tục {len(research_wfs)} workflow đã bắt đầu nhưng chưa tới Product Lock.")

        if args.with_research and research_wfs:
            print("\n[*] Đang tạo Resource Pack trực tiếp từ ảnh sản phẩm sẵn có để chuẩn bị Product Lock...")
            if service._coordinator is not None:
                adv = service._coordinator.advance_to_product_lock(
                    workflow_ids=research_wfs,
                    provider_mode=args.provider_mode,
                )
                locked_total: list[str] = list(adv.get("locked_workflows") or [])
                if adv.get("succeeded"):
                    locked_total = list(adv.get("locked_workflows") or [])
                    # A semantic product rejection frees its slot. Refill one
                    # slot at a time from the same READY batch, then advance
                    # that replacement before selecting another. Infra and
                    # contract failures return succeeded=False above and never
                    # reach this rollover loop.
                    processed_research = set(research_wfs)
                    skipped_workflows = list(adv.get("skipped_workflows") or [])
                    while args.auto and len(locked_total) < args.limit and skipped_workflows:
                        replacement = service.start_auto(
                            limit=args.limit,
                            owner_user_id=args.owner_user_id,
                            target_date=target_date,
                            qc_authorization=qc_authorization,
                            qc_max_cost_usd=qc_max_cost_usd,
                            research_snapshot_id=research_snapshot_id,
                            max_qc_retries=max_qc_retries,
                            execution_mode="auto" if getattr(args, "auto_approve", True) else "manual",
                        )
                        replacement_ids = [
                            item.workflow_id for item in replacement.started
                            if item.workflow_id and item.workflow_id not in processed_research
                        ]
                        if replacement.has_errors:
                            for item in replacement.skipped:
                                if item.reason.startswith(("CLAIM_ERROR", "START_FAILED", "ERROR", "BATCH_STOPPED")):
                                    print(f"[LỖI KHỞI ĐỘNG] Sản phẩm {item.product_key}: {item.reason}", file=sys.stderr)
                            return 1
                        if not replacement_ids:
                            break
                        processed_research.update(replacement_ids)
                        research_wfs.extend(replacement_ids)
                        next_adv = service._coordinator.advance_to_product_lock(
                            workflow_ids=replacement_ids,
                            provider_mode=args.provider_mode,
                        )
                        if not next_adv.get("succeeded"):
                            print(f"[LỖI ADVANCE TO PRODUCT LOCK] {next_adv.get('error')}", file=sys.stderr)
                            for wf_id, details in next_adv.get("workflows", {}).items():
                                print(f"  - {wf_id}: {details}", file=sys.stderr)
                            print("[DỪNG BATCH] Lỗi provider hoặc contract cần được xử lý trước khi tiếp tục.", file=sys.stderr)
                            return 1
                        locked_total.extend(next_adv.get("locked_workflows") or [])
                        skipped_workflows = list(next_adv.get("skipped_workflows") or [])
                else:
                    print(f"[LỖI ADVANCE TO PRODUCT LOCK] {adv.get('error')}", file=sys.stderr)
                    for wf_id, details in adv.get("workflows", {}).items():
                        print(f"  - {wf_id}: {details}", file=sys.stderr)

                    print("[DỪNG BATCH] Cần xử lý workflow hiện tại trước khi xét sản phẩm kế tiếp.", file=sys.stderr)
                    return 1
            else:
                report = run_worker(
                    db_path=db_path,
                    provider_mode=args.provider_mode,
                    once=True,
                    batch_size=len(research_wfs) * 2,
                    workflow_ids=research_wfs,
                )
                if not report or report.failed > 0:
                    print(f"[LỖI RESEARCH] {report.failed if report else 'Unknown'} lỗi xảy ra khi chạy research.", file=sys.stderr)
                    return 1
                locked_total = list(research_wfs)

            is_auto = getattr(args, "auto", False)
            auto_approve = getattr(args, "auto_approve", True)
            if is_auto and auto_approve is not False and locked_total:
                print(f"\n[*] [TỰ ĐỘNG PHÊ DUYỆT] Duyệt tự động Gate 1 (Product Lock) cho {len(locked_total)} workflow...")
                workflow_repo = WorkflowRepository(conn)
                for wf_id in locked_total:
                    wf_data = workflow_repo.get_workflow(wf_id)
                    if not wf_data:
                        continue
                    agg, _ = wf_data
                    state = agg.get("state")
                    if state == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value:
                        try:
                            approval_payload = {
                                "owner_user_id": args.owner_user_id,
                                "provider_mode": args.provider_mode,
                            }
                            if getattr(args, "qc_max_cost_usd", None) is not None:
                                approval_payload["qc_max_cost_usd"] = args.qc_max_cost_usd
                            approve_res = approve_and_advance_workflow(
                                connection=conn,
                                target_id=wf_id,
                                owner_user_id=args.owner_user_id,
                                payload=approval_payload,
                                provider_mode=args.provider_mode,
                            )
                            print(f"  + [{wf_id}] Đã tự động duyệt Product Lock -> {approve_res.get('enqueued_job')}")
                        except Exception as exc:
                            _record_auto_approval_failure(conn, wf_id, str(state), exc)
                            print(f"  - [{wf_id}] Lỗi tự động duyệt: {exc}", file=sys.stderr)
                            print("[DỪNG BATCH] Không tiếp tục sau lỗi Product Lock approval.", file=sys.stderr)
                            return 1

                print("\n[*] [TỰ ĐỘNG THỰC THI] Đang chạy chu trình sinh kịch bản & phân cảnh video...")
                max_auto_cycles = 25
                for wf_id in locked_total:
                    for _auto_cycle in range(max_auto_cycles):
                        report = run_worker(
                            db_path=db_path,
                            provider_mode=args.provider_mode,
                            once=True,
                            batch_size=1,
                            workflow_ids=[wf_id],
                        )
                        wf_data = workflow_repo.get_workflow(wf_id)
                        if not wf_data:
                            print(f"[DỪNG BATCH] Không tìm thấy workflow {wf_id} sau worker.", file=sys.stderr)
                            return 1
                        agg, _ = wf_data
                        st = agg.get("state", "")
                        if st == "skipped" and agg.get("failure_category") == "PRODUCT_REJECTED":
                            print(
                                f"[SKIPPED] workflow={wf_id} product="
                                f"{agg.get('product_key') or agg.get('source_context', {}).get('canonical_product_key') or wf_id} "
                                f"reason={agg.get('skip_reason') or agg.get('failure_code')}"
                            )
                            break
                        if agg.get("failure_category") in {"QC_INFRA_PAUSED", "CONTRACT_BROKEN"}:
                            print(
                                f"[DỪNG PIPELINE] workflow={wf_id} "
                                f"category={agg.get('failure_category')} code={agg.get('failure_code')}: "
                                f"{agg.get('failure_reason') or agg.get('operator_reason')}",
                                file=sys.stderr,
                            )
                            return 1
                        outbox = conn.execute(
                            "SELECT state, last_error FROM workflow_outbox WHERE workflow_id=? "
                            "ORDER BY outbox_id DESC LIMIT 1",
                            (wf_id,),
                        ).fetchone()
                        if outbox and str(outbox["state"] or "") == "quarantined":
                            print(
                                f"[DỪNG BATCH] workflow={wf_id} step={st} error="
                                f"{outbox['last_error'] or 'OUTBOX_QUARANTINED'}",
                                file=sys.stderr,
                            )
                            return 1
                        if st == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
                            print(
                                f"[DỪNG BATCH] workflow={wf_id} error="
                                f"{agg.get('failure_code') or agg.get('operator_action') or 'OPERATOR_ACTION_REQUIRED'}: "
                                f"{agg.get('operator_reason') or 'operator action required'}",
                                file=sys.stderr,
                            )
                            return 1
                        if st == ProductVideoWorkflowState.COMPLETED.value:
                            break
                        if st in (
                            ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value,
                            ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value,
                            ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value,
                        ) or (st == ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL.value and
                              agg.get('render_result', {}).get('creative_mode') == 'four_agent'):
                            if st == ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL.value:
                                from video_factory.automation.creative_policy import evaluate_auto_gate
                                from video_factory.creative.agents.contracts import CreativeReview
                                final_artifact = agg['render_result']
                                decision = evaluate_auto_gate(state=st,artifact=final_artifact,
                                    reviews=(CreativeReview.from_dict(final_artifact['final_review']),),
                                    policy={'creative_mode':'four_agent','auto_export':agg.get('execution_mode') == 'auto'})
                                if decision['action'] != 'advance':
                                    print('Final auto gate blocked: '+decision['reason'], file=sys.stderr)
                                    return 1
                            if (st == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
                                    and agg.get("creative_policy", {}).get("creative_mode") != "four_agent"
                                    and not getattr(args, "authorize_cost", False)):
                                print(f"[DỪNG BATCH] [{wf_id}] Render cần --authorize-cost; giữ tại Storyboard Approval.", file=sys.stderr)
                                return 1
                            if st == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value:
                                render_error = _auto_render_authorization_error(agg, wf_id)
                                if render_error:
                                    print(f"[DỪNG BATCH] [{wf_id}] {render_error}; giữ tại Storyboard Approval.", file=sys.stderr)
                                    return 1
                            try:
                                advance_payload = {
                                    "owner_user_id": args.owner_user_id,
                                    "provider_mode": args.provider_mode,
                                    'automation_actor':'creative_auto_policy',
                                }
                                if getattr(args, "authorize_cost", False):
                                    advance_payload["authorize_cost"] = True
                                if qc_authorization:
                                    advance_payload["qc_authorization"] = qc_authorization
                                next_appr = approve_and_advance_workflow(
                                    connection=conn,
                                    target_id=wf_id,
                                    owner_user_id=args.owner_user_id,
                                    payload=advance_payload,
                                    provider_mode=args.provider_mode,
                                )
                                print(f"  + [{wf_id}] Đã tự động duyệt {st} -> Chuyển sang {next_appr.get('new_state')}")
                            except Exception as exc:
                                _record_auto_approval_failure(conn, wf_id, str(st), exc)
                                print(f"  - [{wf_id}] Lỗi tự động duyệt {st}: {exc}", file=sys.stderr)
                                print("[DỪNG BATCH] Cần xử lý lỗi approval trước khi tiếp tục.", file=sys.stderr)
                                return 1
                            continue
                        if st in {state.value for state in HITL_WAITING_STATES}:
                            print(f"[DỪNG BATCH] workflow={wf_id} đang chờ operator tại {st}.", file=sys.stderr)
                            return 1
                        if report and report.claimed == 0 and report.failed == 0:
                            print(
                                f"[DỪNG BATCH] workflow={wf_id} không có job khả dụng nhưng vẫn ở state {st}; "
                                "kiểm tra outbox trước khi tiếp tục.",
                                file=sys.stderr,
                            )
                            return 1
                    else:
                        print(f"[DỪNG BATCH] workflow={wf_id} vượt quá {max_auto_cycles} chu kỳ worker.", file=sys.stderr)
                        return 1

                print("\n[HOÀN TẤT] Tiến độ các workflow sau chu trình tự động:")
                for wf_id in locked_total:
                    wf_data = workflow_repo.get_workflow(wf_id)
                    if wf_data:
                        agg, _ = wf_data
                        st = agg.get("state", "")
                        appr_id = agg.get("pending_approval_id")
                        print(f"  * {wf_id}: {st}" + (f" (Cần duyệt: {appr_id})" if appr_id else ""))
            else:
                print("[HOÀN TẤT] Các sản phẩm đã sẵn sàng tại cổng 'AwaitingProductLock' để bạn duyệt.")

        return 1 if report_daily_shortfall() else 0
    finally:
        conn.close()


def cmd_select(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db_path)
    conn = connect(db_path)
    try:
        inbox_root = getattr(args, "inbox_root", None)
        source_root = getattr(args, "legacy_source_root", None) or getattr(args, "source_root", None)
        service = DailyCandidateService(conn, source_root=source_root, inbox_root=inbox_root, db_path=db_path)
        target_date = date.fromisoformat(args.date) if args.date else None
        try:
            candidates = service.get_candidates(target_date)
        except FileNotFoundError as exc:
            print(f"[LỖI EXCHANGE INBOX] {exc}", file=sys.stderr)
            return 1

        source_date = candidates[0].source_date if candidates else (args.date or "N/A")
        print(f"\n=== CHỌN SẢN PHẨM TẠO VIDEO ({source_date}) ===")
        print("Danh sách sản phẩm từ Product-Intelligence:\n")

        valid_indices: dict[int, CandidateItem] = {}
        for idx, c in enumerate(candidates, start=1):
            badge = _format_status_badge(c.status)
            prefix = f"[{idx:02d}]"
            print(f"{prefix} {badge} {c.name}")
            print(f"     Brand: {c.brand} | Model: {c.model} | Giá: {c.price_display}")
            if c.status == "unprocessed":
                valid_indices[idx] = c
                print("     >>> [HỢP LỆ ĐỂ TẠO VIDEO]")
            elif c.status == "awaiting_product_lock":
                print(f"     -> Đã research, đang chờ duyệt Product Lock (ID: {c.approval_id})")
            elif c.status == "invalid":
                print(f"     -> Không chọn được: {c.rejection_reason}")
            print()

        if not valid_indices:
            print("[THÔNG BÁO] Không còn sản phẩm mới nào ở trạng thái hợp lệ để bắt đầu.")
            return 0

        prompt = f"Nhập số thứ tự các sản phẩm muốn tạo video (1-{len(candidates)}, phân cách bằng dấu cách): "
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = sys.stdin.readline().strip()
        if not line:
            print("[HỦY BỎ] Không chọn sản phẩm nào.")
            return 0

        selected_keys: list[str] = []
        for part in line.replace(",", " ").split():
            try:
                val = int(part)
                if val in valid_indices:
                    selected_keys.append(valid_indices[val].product_key)
                else:
                    print(f"[BỎ QUA] Mục [{val}] không hợp lệ hoặc đã có video.")
            except ValueError:
                pass

        if not selected_keys:
            print("[HỦY BỎ] Không có lựa chọn hợp lệ nào.")
            return 0

        print(f"\n[*] Đang khởi động {len(selected_keys)} sản phẩm được chọn...")
        started = service.start_selection(
            selected_keys,
            owner_user_id=args.owner_user_id,
            target_date=target_date,
            qc_authorization=_qc_authorization_from_args(args),
            qc_max_cost_usd=getattr(args, "qc_max_cost_usd", None),
            research_snapshot_id=getattr(args, "research_snapshot_id", None),
        )
        print(f"[THÀNH CÔNG] Đã tạo workflow: {', '.join(started)}")

        if args.with_research:
            print("\n[*] Đang chạy Research MCP tự động để chuẩn bị Product Lock...")
            run_worker(
                db_path=db_path,
                provider_mode=args.provider_mode,
                once=True,
                batch_size=len(started) + 2,
                workflow_id_prefix="pv-daily-",
            )
            print("[HOÀN TẤT] Các sản phẩm đã sẵn sàng tại cổng 'AwaitingProductLock'. Dùng lệnh 'approve' để duyệt.")

        return 0
    finally:
        conn.close()


def _read_export_caption(artifact: dict[str, Any]) -> str | None:
    """Read the canonical export caption; status must never synthesize another one."""
    caption_path = artifact.get("caption_path")
    if not caption_path and "payload" in artifact:
        payload = artifact["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if isinstance(payload, dict):
            caption_path = payload.get("caption_path")
    if not caption_path:
        return None
    path = _normalize_file_path(caption_path)
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8").strip()
    return content or None


def cmd_approve(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db_path)
    conn = connect(db_path)
    try:
        repo = WorkflowRepository(conn)
        approval_svc = ApprovalService(conn)

        # 1. Resolve target: could be approval request ID or workflow ID
        target_id = args.target_id
        is_approval_req = False
        try:
            req = approval_svc.get_request(target_id)
            is_approval_req = True
        except KeyError:
            is_approval_req = False

        if is_approval_req:
            wf_id = req.payload.get("workflow_id") or target_id
            gate_name = req.payload.get("gate") or ""
        else:
            wf_id = target_id
            wf_data = repo.get_workflow(wf_id)
            if not wf_data:
                print(f"[LỖI] Không tìm thấy workflow hoặc approval request '{target_id}'", file=sys.stderr)
                return 1
            agg, _ = wf_data
            gate_name = agg.get("pending_approval_gate") or ""

        # 2. Gate 4 Paid Render Cost Guard
        if gate_name == "AwaitingStoryboardApproval" or gate_name == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value:
            if not getattr(args, "authorize_cost", False):
                # Interactive prompt if running in interactive terminal
                confirmed = False
                if sys.stdin.isatty():
                    try:
                        print(
                            "\n⚠️  [XÁC NHẬN CHI PHÍ RENDER] Cổng Gate 4 (Storyboard) sẽ kích hoạt quá trình render video có trả phí (GPU/Vertex AI).\n"
                            "    - Hạn mức tiêu chuẩn: ~$1.50 - $2.00 / video.\n"
                        )
                        choice = input("👉 Bạn có đồng ý ủy quyền thanh toán và tiếp tục render không? [y/N]: ").strip().lower()
                        if choice in ("y", "yes", "co", "có", " đồng ý", "ok"):
                            confirmed = True
                            setattr(args, "authorize_cost", True)
                    except (KeyboardInterrupt, EOFError):
                        confirmed = False

                if not confirmed:
                    print(
                        "\n[LỖI XÁC NHẬN CHI PHÍ] Cổng phê duyệt Storyboard (Gate 4) sẽ kích hoạt quá trình render video có trả phí (GPU/Vertex AI).\n"
                        "Bắt buộc phải có sự đồng ý của operator.\n"
                        "Vui lòng chạy lại lệnh với cờ '--authorize-cost' để xác nhận ủy quyền chi phí.",
                        file=sys.stderr,
                    )
                    return 1

        print(f"[*] Đang phê duyệt và kích hoạt bước tiếp theo cho: {target_id} (Cổng: {gate_name or 'N/A'})...")
        approval_payload: dict[str, Any] = {}
        try:
            qc_authorization = (
                _render_authorization_for_args(args, wf_id)
                if gate_name in {"AwaitingStoryboardApproval", ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value}
                else _qc_authorization_from_args(args, workflow_id=wf_id)
            )
        except ValueError as exc:
            print(f"[LỖI RENDER AUTHORIZATION] {exc}", file=sys.stderr)
            return 1
        if qc_authorization:
            approval_payload["qc_authorization"] = qc_authorization
        if getattr(args, "qc_max_cost_usd", None) is not None:
            approval_payload["qc_max_cost_usd"] = args.qc_max_cost_usd
        if getattr(args, "render_provider", None):
            approval_payload["render_provider_kind"] = args.render_provider
        if getattr(args, "provider_mode", None):
            approval_payload["provider_mode"] = args.provider_mode
        result = approve_and_advance_workflow(
            connection=conn,
            target_id=target_id,
            owner_user_id=args.owner_user_id,
            payload=approval_payload,
            provider_mode=getattr(args, "provider_mode", None),
        )
        print("\n[PHÊ DUYỆT THÀNH CÔNG]")
        print(f"  - Request ID:   {result.get('request_id')}")
        print(f"  - Workflow ID:  {result.get('workflow_id')}")
        print(f"  - Trạng thái:   {result.get('new_state')}")
        print(f"  - Tác vụ kế:    {result.get('enqueued_job')} (Đã enqueue vào Outbox)")

        # 3. Process workflow through worker cycles until next HITL gate or COMPLETED
        print(f"\n[*] Đang tiến hành xử lý workflow {wf_id}...")
        max_cycles = 25
        cycle = 0

        while cycle < max_cycles:
            cycle += 1
            wf_data = repo.get_workflow(wf_id)
            if not wf_data:
                break
            agg, ver = wf_data
            curr_state = agg.get("state", "")

            # If in HITL waiting state or completed, stop
            if curr_state in [s.value for s in HITL_WAITING_STATES] or curr_state == ProductVideoWorkflowState.COMPLETED.value:
                break

            # Run worker turn targeting this workflow
            report = run_worker(
                db_path=db_path,
                provider_mode=getattr(args, "provider_mode", "configured"),
                once=True,
                batch_size=2,
                workflow_ids=[wf_id],
            )

            if report and report.failed > 0:
                print(f"[LỖI XỬ LÝ] Worker ghi nhận {report.failed} tác vụ thất bại khi xử lý workflow {wf_id}.", file=sys.stderr)
                cur = conn.cursor()
                cur.execute(
                    "SELECT message, last_error FROM workflow_outbox WHERE workflow_id = ? AND last_error IS NOT NULL ORDER BY outbox_id DESC LIMIT 5",
                    (wf_id,),
                )
                for msg_json, last_err in cur.fetchall():
                    try:
                        parsed = json.loads(msg_json)
                        job_name = parsed.get("payload", {}).get("job_name", "unknown")
                    except Exception:
                        job_name = "unknown"
                    print(f"  -> Chi tiết lỗi ({job_name}): {last_err}", file=sys.stderr)
                return 1

        # Re-fetch latest aggregate
        wf_data = repo.get_workflow(wf_id)
        if not wf_data:
            print(f"[LỖI] Không tìm thấy dữ liệu workflow {wf_id} sau khi xử lý.", file=sys.stderr)
            return 1

        agg, ver = wf_data
        final_state = agg.get("state", "")

        if final_state == ProductVideoWorkflowState.COMPLETED.value:
            latest_art = dict(agg.get("latest_artifact") or {})
            if "payload" in latest_art:
                payload = latest_art["payload"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                if isinstance(payload, dict):
                    for k in ("caption_path", "metadata_path", "file_path", "final_video_url", "video_url"):
                        if k in payload and not latest_art.get(k):
                            latest_art[k] = payload[k]
            video_url = latest_art.get("final_video_url") or latest_art.get("video_url") or ""
            video_file = latest_art.get("file_path") or video_url
            video_path = _normalize_file_path(video_file) if video_file else None

            print("\n" + "=" * 64)
            print("🎉 [XUẤT BẢN THÀNH CÔNG] VIDEO ĐÃ HOÀN TẤT VÀ SẴN SÀNG ĐĂNG TẢI")
            print("=" * 64)
            print(f"  - Workflow ID:  {wf_id}")
            print(f"  - Trạng thái:   {final_state}")
            if video_path and video_path.is_file():
                print(f"  - File MP4:       {video_path.resolve()}")
                print(f"  - Kích thước:     {video_path.stat().st_size:,} bytes")
            else:
                print(f"  - URL Video:      {video_url}")

            caption_file = latest_art.get("caption_path")
            meta_file = latest_art.get("metadata_path")
            if caption_file:
                print(f"  - File Caption:   {caption_file}")
            if meta_file:
                print(f"  - File Metadata:  {meta_file}")

            print("\n--- TIKTOK CAPTION & HASHTAGS GỢI Ý (ĐÃ XÁC THỰC) ---")
            caption = _read_export_caption(latest_art)
            print(caption or "Chưa có caption export chính thức.")
            print("-----------------------------------------------------")
            print("=" * 64 + "\n")
            return 0

        elif final_state in [s.value for s in HITL_WAITING_STATES]:
            next_gate = agg.get("pending_approval_gate")
            next_appr_id = agg.get("pending_approval_id")
            art = agg.get("latest_artifact")
            art_id = "N/A"
            if isinstance(art, dict):
                art_id = (
                    art.get("artifact_id")
                    or art.get("brief_id")
                    or art.get("scene_plan_id")
                    or art.get("storyboard_id")
                    or art.get("render_id")
                    or art.get("resource_pack_id")
                    or "N/A"
                )
            print("\n[DỪNG TẠI CỔNG DUYỆT TIẾP THEO]")
            print(f"  - Workflow ID:  {wf_id}")
            print(f"  - Trạng thái:   {final_state}")
            print(f"  - Cổng chờ:     {next_gate}")
            print(f"  - Approval ID:  {next_appr_id}")
            print(f"  - Artifact ID:  {art_id}")
            print(f"\nOperator kiểm tra: hermes personal video status {wf_id}")
            cost_hint = " --authorize-cost" if next_gate == "AwaitingStoryboardApproval" else ""
            print(f"Để duyệt tiếp:     hermes personal video approve {next_appr_id}{cost_hint}\n")
            return 0
        else:
            print(f"\n[THÔNG BÁO] Workflow {wf_id} đang ở trạng thái: {final_state}")
            return 0

    except Exception as exc:  # noqa: BLE001
        print(f"\n[LỖI PHÊ DUYỆT] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_status(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args.db_path)
    conn = connect(db_path)
    try:
        repo = WorkflowRepository(conn)
        target = args.workflow_id
        wf_data = repo.get_workflow(target)
        wf_id = target
        entry = None
        if wf_data is None:
            entry = DailyImportRepository(conn).find_by_target(target)
            if entry and entry.get("workflow_id"):
                wf_id = entry["workflow_id"]
                wf_data = repo.get_workflow(wf_id)
        if wf_data is None:
            print(f"[LỖI] Không tìm thấy workflow hoặc sản phẩm khớp với '{target}'", file=sys.stderr)
            return 1
        agg, version = wf_data
        if entry is None:
            entry = DailyImportRepository(conn).find_by_target(wf_id)
        print(f"\n=== CHI TIẾT TRẠNG THÁI WORKFLOW: {wf_id} ===")
        print(f"Version:            {version}")
        print(f"Trạng thái:         {agg.get('state')}")
        from video_factory.workflows.steps import read_pipeline_steps

        step_rows = read_pipeline_steps(conn, wf_id)
        if not step_rows:
            print("Pipeline step:      unknown/not recorded")
        else:
            active_step = next(
                (row for row in reversed(step_rows) if row.get("status") in {"running", "uncertain", "failed", "pending_operator"}),
                step_rows[-1],
            )
            print(f"Pipeline step:      {active_step.get('step_name')} ({active_step.get('status')})")
            print("Pipeline progress:")
            for row in step_rows:
                detail = f"attempt={row.get('attempt_number') or 'unknown'}"
                if row.get("operation_id"):
                    detail += f", operation={row['operation_id']}"
                if row.get("error_code"):
                    detail += f", error={row['error_code']}"
                if row.get("message"):
                    detail += f", {row['message']}"
                print(f"  - {row.get('step_name')}: {row.get('status')} ({detail})")
                if row.get("next_action"):
                    print(f"    Next: {row['next_action']}")
        attempt_columns = {row["name"] for row in conn.execute("PRAGMA table_info(product_video_operation_attempt)")}
        if attempt_columns:
            qc_rows = conn.execute(
                "SELECT attempt_id,actual_cost,actual_cost_usd,cost_status,status FROM product_video_operation_attempt "
                "WHERE workflow_id=? AND capability_id LIKE 'product_video.qc.%'",
                (wf_id,),
            ).fetchall()
            qc_recorded_usd = sum(
                float(row["actual_cost_usd"])
                if row["actual_cost_usd"] is not None
                else int(row["actual_cost"] or 0) / 100
                for row in qc_rows
                if row["actual_cost_usd"] is not None or row["actual_cost"] is not None
            )
            qc_operation_ids = {str(row["attempt_id"]) for row in qc_rows if row["attempt_id"]}
            qc_unknown_ids = {
                str(row["attempt_id"] or f"operation-row:{index}")
                for index, row in enumerate(qc_rows)
                if row["cost_status"] == "unknown" and row["status"] not in {"pre_send_fail", "reconciled_not_sent"}
            }
            retry_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_qc_retries'"
            ).fetchone()
            if retry_table:
                retry_columns = {row["name"] for row in conn.execute("PRAGMA table_info(workflow_qc_retries)")}
                if {"status", "operation_id", "cost_usd"}.issubset(retry_columns):
                    retry_rows = conn.execute(
                        "SELECT status,operation_id,cost_usd FROM workflow_qc_retries WHERE workflow_id=?",
                        (wf_id,),
                    ).fetchall()
                    for retry in retry_rows:
                        state = str(retry["status"] or "")
                        if state in {"technical_unknown", "reconciled_consumed"}:
                            if retry["cost_usd"] is None:
                                qc_unknown_ids.add(str(retry["operation_id"] or f"retry-row:{id(retry)}"))
                            elif str(retry["operation_id"] or "") not in qc_operation_ids:
                                qc_recorded_usd += float(retry["cost_usd"])
            if step_rows:
                for step in step_rows:
                    if step.get("step_name") not in {"IMAGE_QC", "KEYFRAME_QC", "VIDEO_QC"}:
                        continue
                    operation = str(step.get("operation_id") or f"{step.get('step_name')}:{step.get('attempt_number') or 'unknown'}")
                    if step.get("cost_status") == "recorded" and step.get("cost_usd") is not None:
                        # Step events summarize QC; a report/attempt ledger may already contain the same cost.
                        if operation not in qc_operation_ids:
                            qc_recorded_usd += float(step["cost_usd"])
                    elif step.get("cost_status") == "unknown" and step.get("status") in {"uncertain", "pending_operator"}:
                        qc_unknown_ids.add(operation)
            qc_recorded = qc_recorded_usd
            qc_unknown = len(qc_unknown_ids)
            video_rows = conn.execute(
                "SELECT actual_cost,cost_status,status FROM product_video_operation_attempt "
                "WHERE workflow_id=? AND capability_id NOT LIKE 'product_video.qc.%' "
                "AND (capability_id LIKE '%video%' OR capability_id LIKE '%render%' OR capability_id LIKE '%keyframe%')",
                (wf_id,),
            ).fetchall()
            video_recorded = sum(int(row["actual_cost"] or 0) for row in video_rows if row["actual_cost"] is not None) / 100
            video_unknown = sum(1 for row in video_rows if row["cost_status"] == "unknown" and row["status"] not in {"pre_send_fail", "reconciled_not_sent"})
            print(f"QC cost:            recorded ${qc_recorded:.4f}; unknown attempts {qc_unknown}")
            print(f"Video cost:         recorded ${video_recorded:.4f}; unknown attempts {video_unknown}")
        blocked_error = _quarantined_job_error(conn, wf_id, str(agg.get("state") or ""))
        if blocked_error:
            print(f"Worker bị chặn:     {blocked_error}")
            if "APPROVED_IMAGE_MISMATCH" in blocked_error:
                pkey = (agg.get("source_context") or {}).get("product_key") or (entry.get("product_key") if entry else None) or wf_id
                print("\n[HƯỚNG DẪN XỬ LÝ - APPROVED_IMAGE_MISMATCH]")
                print("Lý do: Resource pack từ PI không khớp SHA-256 với ít nhất 2 ảnh đã được người vận hành xác nhận.")
                print("Các bước khắc phục:")
                print(f"  1. Đặt lại workflow: python -m video_factory.cli reset {wf_id}")
                print("  2. Xác nhận lại 2 asset ID ảnh thực và 1 claim có nguồn kiểm chứng.")
                print(f'  3. Chạy lại: python -m video_factory.cli start --product-keys {pkey} --confirmed-image-asset-id <id> --confirmed-claim "claim|https://nguồn-thật"')
        print(f"Gate chờ:           {agg.get('pending_approval_gate') or 'Không'}")
        print(f"Approval ID:        {agg.get('pending_approval_id') or 'Không'}")
        if agg.get("state") == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
            print(f"Operator action:    {agg.get('operator_action') or 'Không'}")
            print(f"QC stage:           {agg.get('operator_stage') or 'N/A'}")
            if agg.get("failure_code"):
                print(f"Error code:         {agg.get('failure_code')}")
            print(f"Reason:             {agg.get('operator_reason') or 'N/A'}")
            recovery_plan = agg.get("keyframe_recovery_plan")
            if isinstance(recovery_plan, dict):
                frames = recovery_plan.get("frames") or []
                print(f"Recovery frames:    {len(frames)}")
                for frame in frames:
                    if isinstance(frame, dict):
                        actions = ", ".join(str(item) for item in frame.get("actions", []))
                        print(
                            f"  {frame.get('frame_id') or frame.get('scene_id')}: "
                            f"{actions} - {frame.get('feedback') or 'N/A'}"
                        )

        src = agg.get("source_context", {})
        if src:
            print(f"Sản phẩm:           {src.get('brand')} {src.get('model')} - {src.get('name')}")
            print(f"Giá niêm yết:       {src.get('price_display')}")
            imgs = src.get("images") or []
            print(f"Số lượng ảnh gốc:   {len(imgs)}")

        rp = agg.get("resource_pack") or {}
        if not rp and isinstance(agg.get("latest_artifact"), dict) and agg["latest_artifact"].get("schema") == "resource_pack":
            rp = agg["latest_artifact"]
        if rp:
            rp_id = rp.get("resource_pack_id") or rp.get("id") or "N/A"
            rp_st = rp.get("status") or "N/A"
            rp_digest = rp.get("digest") or rp.get("source_digest") or "N/A"
            print(f"Resource Pack ID:   {rp_id}")
            print(f"Pack Status:        {rp_st}")
            print(f"Pack Digest:        {rp_digest[:16]}..." if len(rp_digest) > 16 else f"Pack Digest:        {rp_digest}")

        art = agg.get("latest_artifact")
        if isinstance(art, dict):
            art_id = (
                art.get("artifact_id")
                or art.get("brief_id")
                or art.get("scene_plan_id")
                or art.get("storyboard_id")
                or art.get("render_id")
                or art.get("export_id")
                or art.get("resource_pack_id")
                or "N/A"
            )
            print(f"Artifact mới nhất:  {art.get('schema')} (ID: {art_id}, v{art.get('version', 1)})")

        qc_repo = QCReportRepository(conn)
        image_artifact = agg.get("resource_pack") or {}
        image_report = qc_repo.get_latest_report(
            args.workflow_id,
            QCStage.IMAGE,
            expected_artifact_digest=compute_artifact_digest(image_artifact) if image_artifact else None,
        )
        print(f"Image QC:           {image_report.verdict.value if image_report else 'pending/stale'}"
              f" ({image_report.report_id if image_report else 'N/A'})")
        if image_report and image_report.verdict.value == "fail":
            print("Cần xử lý:          Chọn hoặc nghiên cứu lại ảnh sản phẩm trước khi duyệt Product Lock")
        video_value = (art or {}).get("file_path") or (art or {}).get("final_video_url") or (art or {}).get("video_url")
        video_path = _normalize_file_path(video_value) if video_value else None
        video_report = qc_repo.get_latest_report(
            args.workflow_id,
            QCStage.VIDEO,
            expected_artifact_digest=compute_file_sha256(video_path) if video_path else None,
        )
        print(f"Video QC:           {video_report.verdict.value if video_report else 'pending/stale'}"
              f" ({video_report.report_id if video_report else 'N/A'})")

        if agg.get("state") == ProductVideoWorkflowState.COMPLETED.value:
            latest_art = agg.get("latest_artifact") or {}
            video_url = latest_art.get("final_video_url") or latest_art.get("video_url") or ""
            video_file = latest_art.get("file_path") or video_url
            video_path = _normalize_file_path(video_file) if video_file else None
            print("\n--- THÔNG TIN VIDEO HOÀN THÀNH ---")
            if video_path and video_path.is_file():
                print(f"Đường dẫn file:     {video_path.resolve()}")
                print(f"Kích thước file:    {video_path.stat().st_size:,} bytes")
            else:
                print(f"Đường dẫn file/URL: {video_url}")
            print("\n--- TIKTOK CAPTION GỢI Ý ---")
            caption = _read_export_caption(latest_art)
            print(caption or "Chưa có caption export chính thức.")

        print()
        return 0
    finally:
        conn.close()


def cmd_pipeline_status(args: argparse.Namespace) -> int:
    conn = connect(_resolve_db_path(args.db_path))
    try:
        from video_factory.workflows.runtime_control import ensure_pipeline_pause

        pause = ensure_pipeline_pause(conn)
        if not pause:
            print("Pipeline: ACTIVE (no durable pause)")
            return 0
        print("Pipeline: PAUSED")
        print(f"Category:  {pause.get('category')}")
        print(f"Code:      {pause.get('code')}")
        print(f"Workflow:  {pause.get('workflow_id') or 'unknown'}")
        print(f"Paused at: {pause.get('paused_at')}")
        print(f"Reason:    {pause.get('message')}")
        workflow_id = str(pause.get("workflow_id") or "")
        detail = ""
        pending_job_name = ""
        if workflow_id:
            try:
                row = conn.execute(
                    "SELECT last_error FROM workflow_outbox WHERE workflow_id=? "
                    "AND state='quarantined' AND COALESCE(last_error,'') != '' "
                    "ORDER BY outbox_id DESC LIMIT 1",
                    (workflow_id,),
                ).fetchone()
            except Exception:
                row = None
            if row:
                detail = str(row["last_error"] or "")
                detail = re.sub(
                    r"(?i)(authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|secret)(\s*[:=]\s*)[^\s,;]+",
                    r"\1\2<redacted>",
                    detail,
                )
                detail = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer <redacted>", detail)
                detail = re.sub(
                    r"(?i)([?&](?:token|key|api_key|access_token)=)[^&\s]+",
                    r"\1<redacted>",
                    detail,
                )
                print(f"Quarantined job: {detail[:600]}")
            pending = conn.execute(
                "SELECT outbox_id,message FROM workflow_outbox WHERE workflow_id=? AND state='pending' "
                "ORDER BY outbox_id LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if pending:
                try:
                    pending_job_name = str(
                        (json.loads(pending["message"]).get("payload") or {}).get("job_name") or "unknown"
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    pending_job_name = "invalid-envelope"
                print(
                    f"Pending job: outbox={pending['outbox_id']} job={pending_job_name} "
                    "(worker will not claim it while the pause is active)."
                )
        if (
            "[IMAGE_QC_ERROR]" in detail
            and "9Router local management API could not verify the route" in detail
        ):
            print(
                "After repairing the 9Router route, run "
                f"pipeline-requeue-preflight {workflow_id}; it performs read-only route/attempt checks. "
                "Then run pipeline-resume with a repair note."
            )
        elif pending_job_name:
            print("After resolving the pause, run pipeline-resume with a note; the pending job will then be claimable.")
        else:
            print("After fixing the route/contract and reconciling any uncertain attempt, run pipeline-resume with a note.")
        return 2
    finally:
        conn.close()


def _requeue_verified_presend_resource_pack(
    conn: Any,
    workflow_id: str,
    *,
    route_preflight: Any | None = None,
) -> int:
    """Requeue only a resource-pack job quarantined by the known pre-send 9Router check."""
    from video_factory.automation.failure_classification import FailureCategory
    from video_factory.workflows.runtime_control import get_pipeline_pause

    pause = get_pipeline_pause(conn)
    if not pause:
        raise ValueError("Checkout has no active QC infrastructure pause")
    if (
        pause.get("category") != FailureCategory.QC_INFRA_PAUSED.value
        or pause.get("code") != "IMAGE_QC_ERROR"
    ):
        raise ValueError("Only a QC_INFRA_PAUSED workflow can use this preflight recovery")

    if route_preflight is None:
        from video_factory.config import load_config
        from video_factory.integrations.nine_router import (
            NineRouterOpenAIClient,
            _route_from_config,
        )

        route_root = load_config().get("product_video", {}).get("nine_router", {})
        if not isinstance(route_root, dict) or route_root.get("enabled") is not True:
            raise ValueError("9Router Vision route is not enabled; refusing to switch providers during recovery")
        route = _route_from_config("vision_qc")
        if route is None:
            raise ValueError("Configured 9Router Vision route is unavailable")
        preflight_error = NineRouterOpenAIClient(route).configured_preflight()
    else:
        preflight_error = route_preflight()
    if preflight_error:
        raise ValueError(f"9Router Vision preflight is still failing: {preflight_error}")

    workflow = WorkflowRepository(conn).get_workflow(workflow_id)
    if workflow is None or workflow[0].get("state") != ProductVideoWorkflowState.BUILDING_RESOURCE_PACK.value:
        raise ValueError("Only a workflow still building its resource pack can be safely requeued")

    rows = conn.execute(
        "SELECT outbox_id, message, state, last_error FROM workflow_outbox WHERE workflow_id=? ORDER BY outbox_id",
        (workflow_id,),
    ).fetchall()
    if len(rows) != 1 or rows[0]["state"] not in {"quarantined", "pending"}:
        raise ValueError("Expected exactly one quarantined or already requeued outbox job")
    row = rows[0]
    error = str(row["last_error"] or "")
    if (
        "[IMAGE_QC_ERROR]" not in error
        or "9Router local management API could not verify the route" not in error
        or "may have been sent" in error.lower()
        or "reconciliation is required" in error.lower()
    ):
        raise ValueError("Quarantined error is not the verified pre-send 9Router management failure")
    try:
        envelope = json.loads(row["message"])
        payload = envelope.get("payload") or {}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Quarantined outbox envelope is invalid") from exc
    if payload.get("job_name") != "product_video.resource_pack":
        raise ValueError("Only the original resource-pack job can be requeued by this recovery")

    active_qc_statuses = ("reserved", "claimed", "sent", "technical_unknown")
    retry_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_qc_retries'"
    ).fetchone()
    if retry_table:
        placeholders = ",".join("?" for _ in active_qc_statuses)
        unresolved = conn.execute(
            f"SELECT 1 FROM workflow_qc_retries WHERE workflow_id=? AND status IN ({placeholders}) LIMIT 1",
            (workflow_id, *active_qc_statuses),
        ).fetchone()
        if unresolved:
            raise ValueError("QC retry ledger contains an unresolved attempt; reconcile before requeue")
    operation_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_video_operation_attempt'"
    ).fetchone()
    if operation_table:
        placeholders = ",".join("?" for _ in active_qc_statuses)
        unresolved = conn.execute(
            "SELECT 1 FROM product_video_operation_attempt WHERE workflow_id=? "
            f"AND capability_id LIKE 'product_video.qc.%' AND status IN ({placeholders}) LIMIT 1",
            (workflow_id, *active_qc_statuses),
        ).fetchone()
        if unresolved:
            raise ValueError("QC operation ledger contains an unresolved attempt; reconcile before requeue")

    now = datetime.now(timezone.utc).isoformat()
    pause_message = (
        "A known 9Router management preflight failure was revalidated; verified pre-send resource-pack "
        "jobs are pending, and the checkout remains paused until an operator resumes it."
    )
    with conn:
        if row["state"] == "quarantined":
            updated = conn.execute(
                "UPDATE workflow_outbox SET state='pending', lease_token=NULL, lease_owner=NULL, "
                "lease_expires_at=NULL, next_attempt_at=?, updated_at=? "
                "WHERE outbox_id=? AND state='quarantined'",
                (now, now, row["outbox_id"]),
            )
            if updated.rowcount != 1:
                raise ValueError("Quarantined job changed during recovery; nothing was requeued")
        pause_update = conn.execute(
            "UPDATE product_video_runtime_control SET message=? "
            "WHERE state_id=1 AND paused=1 AND category=? AND code=? AND workflow_id=?",
            (pause_message, FailureCategory.QC_INFRA_PAUSED.value, "IMAGE_QC_ERROR", str(pause.get("workflow_id") or "")),
        )
        if pause_update.rowcount != 1:
            raise ValueError("Pipeline pause changed during recovery; the job was not authorized for requeue")
        ledger_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_product_video_import'"
        ).fetchone()
        if ledger_exists:
            conn.execute(
                "UPDATE daily_product_video_import SET status='started', last_error=NULL, updated_at=? "
                "WHERE workflow_id=? AND status!='started'",
                (now, workflow_id),
            )
    return int(row["outbox_id"])


def cmd_pipeline_resume(args: argparse.Namespace) -> int:
    conn = connect(_resolve_db_path(args.db_path))
    try:
        for table, query, label in (
            (
                "workflow_qc_retries",
                "SELECT workflow_id,operation_id,status FROM workflow_qc_retries "
                "WHERE status IN ('reserved','claimed','sent','technical_unknown') "
                "AND NOT EXISTS (SELECT 1 FROM workflow_event e WHERE e.workflow_id=workflow_qc_retries.workflow_id AND e.event_type='OperatorWorkflowSkipped') LIMIT 1",
                "QC retry ledger",
            ),
            (
                "product_video_operation_attempt",
                "SELECT workflow_id,attempt_id,status FROM product_video_operation_attempt "
                "WHERE capability_id LIKE 'product_video.qc.%' AND status IN ('reserved','claimed','sent','technical_unknown') "
                "AND NOT EXISTS (SELECT 1 FROM workflow_event e WHERE e.workflow_id=product_video_operation_attempt.workflow_id AND e.event_type='OperatorWorkflowSkipped') LIMIT 1",
                "provider operation ledger",
            ),
        ):
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if exists:
                row = conn.execute(query).fetchone()
                if row:
                    print(
                        f"[BLOCKED] {label} still has unresolved attempt: "
                        f"workflow={row['workflow_id']} operation={row[1]} status={row['status']}",
                        file=sys.stderr,
                    )
                    return 1
        from video_factory.workflows.runtime_control import ensure_pipeline_pause, resume_pipeline

        pause = ensure_pipeline_pause(conn)
        if not resume_pipeline(conn, note=args.note):
            print("Pipeline is not paused.")
            return 0
        print("Pipeline pause cleared after operator repair/reconciliation note was recorded.")
        if pause and pause.get("workflow_id"):
            wf = WorkflowRepository(conn).get_workflow(str(pause["workflow_id"]))
            if wf and wf[0].get("state") == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
                stage = str(wf[0].get("operator_stage") or "")
                if stage in {"image", "keyframe", "video"}:
                    print(
                        "To resume this workflow after checking its stored artifact, run: "
                        f"python -m video_factory.cli qc {pause['workflow_id']} --stage {stage} --advance --provider-mode configured"
                    )
        return 0
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_skip_workflow(args: argparse.Namespace) -> int:
    """Preserve unresolved attempt history while terminally skipping one workflow."""
    conn = connect(_resolve_db_path(args.db_path))
    try:
        from video_factory.workflows.runtime_control import skip_unresolved_workflow

        skip_unresolved_workflow(conn, args.workflow_id, args.reason)
        print(
            f"Workflow {args.workflow_id} marked SKIPPED. QC attempts remain unchanged; "
            "pending jobs were quarantined and this workflow will not be resumed automatically."
        )
        return 0
    except ValueError as exc:
        print(f"[BLOCKED] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_pipeline_requeue_preflight(args: argparse.Namespace) -> int:
    """Requeue a narrowly verified resource-pack preflight failure while still paused."""
    conn = connect(_resolve_db_path(args.db_path))
    try:
        outbox_id = _requeue_verified_presend_resource_pack(conn, args.workflow_id)
        print(
            f"Prepared outbox job {outbox_id} for {args.workflow_id}; pipeline remains paused. "
            "After confirming route repair and attempt status, run pipeline-resume with a note."
        )
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"[BLOCKED] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_daily_status(args: argparse.Namespace) -> int:
    """Report actual completed exports for one Product-Intelligence day."""
    target_date = date.today() if args.date == "today" else date.fromisoformat(args.date)
    minimum = int(args.minimum)
    if minimum < 0 or minimum > 5:
        print("[ERROR] --minimum phải nằm từ 0 đến 5.", file=sys.stderr)
        return 1

    conn = connect(_resolve_db_path(args.db_path))
    try:
        rows = DailyImportRepository(conn).get_all_for_date(target_date.isoformat())
        repo = WorkflowRepository(conn)
        workflows: list[dict[str, str]] = []
        completed = 0
        for row in rows.values():
            if row["status"] != "started":
                continue
            wf_id = str(row["workflow_id"])
            wf_data = repo.get_workflow(wf_id)
            state = str(wf_data[0].get("state") or "") if wf_data else "missing"
            completed += state == ProductVideoWorkflowState.COMPLETED.value
            item = {"workflow_id": wf_id, "state": state}
            blocked_error = _quarantined_job_error(conn, wf_id, state)
            if blocked_error:
                item["blocking_error"] = blocked_error
            workflows.append(item)
        result = {
            "date": target_date.isoformat(),
            "started": len(workflows),
            "completed": completed,
            "minimum": minimum,
            "workflows": workflows,
            "ok": completed >= minimum,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"[VIDEO NGÀY] {target_date}: hoàn thành {completed}/{minimum}, đã bắt đầu {len(workflows)} workflow.")
            for item in workflows:
                print(f"  - {item['workflow_id']}: {item['state']}")
                if item.get("blocking_error"):
                    print(f"    Worker bị chặn: {item['blocking_error']}")
            if not result["ok"]:
                print("[THIẾU VIDEO] Chưa đạt mục tiêu best effort; xem trạng thái workflow và log provider/QC.", file=sys.stderr)
        return 0 if result["ok"] else 1
    finally:
        conn.close()


def cmd_qc(args: argparse.Namespace) -> int:
    """Run the governed QC producer without changing any HITL approval state."""
    db_path = _resolve_db_path(args.db_path)
    conn = connect(db_path)
    try:
        from video_factory.workflows.runtime_control import ensure_pipeline_pause

        pause = ensure_pipeline_pause(conn)
        if pause:
            print(
                f"[BLOCKED] Pipeline is paused ({pause.get('category')}/{pause.get('code')}): "
                f"{pause.get('message')}. Resolve/reconcile, then run pipeline-resume first.",
                file=sys.stderr,
            )
            return 1
        requested_stage = QCStage(args.stage)
        technical_keyframe_retry = False
        reused_saved_pass = None
        if (
            args.advance
            and args.provider_mode == "configured"
            and requested_stage is QCStage.IMAGE
        ):
            waiting = WorkflowRepository(conn).get_workflow(args.workflow_id)
            if waiting is not None:
                waiting_aggregate, _ = waiting
                existing_report = QCReportRepository(conn).get_latest_report(
                    args.workflow_id,
                    QCStage.IMAGE,
                )
                if (
                    waiting_aggregate.get("state") == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
                    and waiting_aggregate.get("operator_stage") == QCStage.IMAGE.value
                    and existing_report is not None
                    and getattr(existing_report.verdict, "value", existing_report.verdict) == "pass"
                ):
                    from video_factory.qc.validation import require_current_pass_report

                    reused_saved_pass = require_current_pass_report(
                        QCReportRepository(conn),
                        workflow_id=args.workflow_id,
                        stage=QCStage.IMAGE,
                        artifact=waiting_aggregate.get("resource_pack") or {},
                        owner_user_id=waiting_aggregate.get("owner_user_id"),
                        project_id=waiting_aggregate.get("project_id"),
                        provider_mode="configured",
                    )
                    result = {
                        "status": "completed",
                        "stage": QCStage.IMAGE.value,
                        "report_id": reused_saved_pass.report_id,
                        "report": reused_saved_pass,
                        "eligible_count": len(
                            (reused_saved_pass.findings or {}).get("eligible_reference_manifest") or []
                        ),
                    }
                    print(
                        f"QC reused:          saved configured Image QC PASS {reused_saved_pass.report_id}; "
                        "no provider request sent"
                    )
                else:
                    result = None
            else:
                result = None
        else:
            result = None
        if args.advance and args.provider_mode == "configured":
            waiting = WorkflowRepository(conn).get_workflow(args.workflow_id)
            if waiting is not None:
                waiting_aggregate, _ = waiting
                if (
                    requested_stage is QCStage.KEYFRAME
                    and waiting_aggregate.get("operator_action")
                    in {"qc_retry_authorization", "qc_retry"}
                ):
                    previous_id = waiting_aggregate.get("operator_qc_report_id")
                    previous = QCReportRepository(conn).get_report(str(previous_id)) if previous_id else None
                    items = previous.findings.get("items", []) if previous and isinstance(previous.findings, dict) else []
                    technical_keyframe_retry = bool(items) and all(
                        isinstance(item, dict)
                        and item.get("verdict") == "unknown"
                        and item.get("findings", {}).get("finish_reason") == "length"
                        for item in items
                    )
                if (
                    requested_stage is QCStage.KEYFRAME
                    and waiting_aggregate.get("operator_action") in {"qc_retry_authorization", "qc_retry"}
                    and not technical_keyframe_retry
                ):
                    recovery_id = _advance_qc_waiting_workflow(
                        conn,
                        args.workflow_id,
                        requested_stage,
                    )
                    print(f"Keyframe recovery:  {recovery_id}")
                    return 0
        if result is None:
            service = ProductVideoQCService(conn)
            result = service.run(
                args.workflow_id,
                requested_stage,
                provider_mode=args.provider_mode,
                authorize_cost=args.authorize_cost,
                max_cost_usd=args.max_cost_usd,
                qc_budget_mode=args.qc_budget_mode,
                authorization=(
                    {
                        "workflow_id": args.workflow_id,
                        "run_id": args.authorization_run_id,
                        "scope": args.authorization_scope,
                        "expires_at": args.authorization_expires_at,
                        "max_cost_usd": args.authorization_max_cost_usd or args.max_cost_usd,
                        "campaign_id": args.authorization_campaign_id,
                        "qc_budget_mode": args.qc_budget_mode,
                    }
                    if args.authorization_run_id or args.authorization_expires_at or args.authorization_scope or args.qc_budget_mode
                    else None
                ),
            )
        qc_pause = _pause_pipeline_for_qc_result(conn, args.workflow_id, result)
        rejected_product_key = (
            _mark_cli_image_qc_rejected(
                conn,
                args.workflow_id,
                requested_stage,
                result,
            )
            if args.advance and args.provider_mode == "configured"
            else None
        )
        print(f"QC stage:           {result['stage']}")
        print(f"QC status:           {result['status']}")
        print(f"QC report ID:        {result.get('report_id') or 'N/A'}")
        print(f"Reason:              {result.get('reason') or 'completed'}")
        if qc_pause:
            print(
                f"Pipeline paused:     {qc_pause['category']}/{qc_pause['code']} - "
                f"{qc_pause['message']}"
            )
        if result.get("report") is not None:
            report = result["report"]
            print(f"Verdict:             {report.verdict.value}")
            print(f"Rubric score:        {report.score}")
        if result.get("eligible_count") is not None:
            print(f"Eligible references: {result['eligible_count']}")
        if rejected_product_key:
            print(
                f"[SKIPPED] product_key={rejected_product_key}: semantic Image QC failed "
                "after its bounded evaluation/recovery attempts. The batch may select the next SKU."
            )
            return 0
        if args.advance:
            if args.provider_mode != "configured":
                print("[LỖI QC] --advance chỉ chấp nhận configured QC, không chấp nhận fake mode.", file=sys.stderr)
                return 1
            if result["status"] != "completed" or result.get("report") is None:
                print("[LỖI QC] Không thể advance khi QC chưa completed.", file=sys.stderr)
                return 1
            report = result["report"]
            verdict = getattr(getattr(report, "verdict", None), "value", None)
            from video_factory.qc.fingerprint import minimum_pass_score
            required_score = minimum_pass_score(requested_stage)
            if verdict != "pass" or int(report.score) < required_score:
                if requested_stage is QCStage.KEYFRAME and verdict == "fail":
                    _record_failed_keyframe_qc(conn, args.workflow_id, report)
                print(f"[LỖI QC] QC không PASS với điểm >= {required_score}; không tạo human approval gate.", file=sys.stderr)
                return 1
            authorization = _qc_authorization_from_args(args, workflow_id=args.workflow_id)
            approval_id = _advance_qc_waiting_workflow(
                conn,
                args.workflow_id,
                QCStage(args.stage),
                authorization,
                qc_passed_retry=technical_keyframe_retry,
            )
            print(f"Human approval ID:  {approval_id}")
        return 0 if result["status"] == "completed" else 1
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI QC] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_qc_reconcile(args: argparse.Namespace) -> int:
    """Reconcile one uncertain QC attempt without advancing workflow state."""
    conn = connect(_resolve_db_path(args.db_path))
    try:
        from video_factory.qc.service import ProductVideoQCService

        result = ProductVideoQCService(conn).reconcile_uncertain_attempt(
            args.workflow_id,
            args.attempt_id,
            outcome=args.outcome,
            evidence_reference=args.evidence_reference,
            note=args.note,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI QC RECONCILE] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_qc_reconcile_presend(args: argparse.Namespace) -> int:
    """Release an Image QC quota reservation only with durable pre-send proof."""
    conn = connect(_resolve_db_path(args.db_path))
    try:
        result = ProductVideoQCService(conn).reconcile_presend_reservation(
            args.workflow_id, args.reservation_id,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI QC PRESEND RECONCILE] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_attach_source_evidence(args: argparse.Namespace) -> int:
    """Attach an independently validated source claim after Product Lock."""
    from video_factory.creative.trusted_evidence import attach_validated_source_claim

    conn = connect(_resolve_db_path(args.db_path))
    try:
        result = attach_validated_source_claim(
            conn,
            args.workflow_id,
            args.record_id,
            record_dir=args.record_dir,
            operator_id=args.operator_id,
            review_note=args.review_note,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI SOURCE EVIDENCE] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_replace_source_images(args: argparse.Namespace) -> int:
    """Replace source images after a known per-product source Image QC failure."""
    from video_factory.application.operator_handler import replace_blocked_source_images

    conn = connect(_resolve_db_path(args.db_path))
    try:
        candidate = DailyImportRepository(conn).find_by_target(args.target)
        workflow_id = str(candidate.get("workflow_id") or args.target) if candidate else args.target
        result = replace_blocked_source_images(
            conn, workflow_id=workflow_id, owner_user_id=args.owner_user_id,
            image_paths=args.image, reason=args.reason,
            product_info={key: value for key, value in {
                "name": args.name, "brand": args.brand, "model": args.model,
                "variant": args.variant, "category": args.category,
                "key_features": args.key_features, "source_url": args.source_url,
                "price_display": args.price_display,
            }.items() if value is not None},
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI THAY ẢNH NGUỒN] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_recover_keyframe(args: argparse.Namespace) -> int:
    """Recover one quarantined keyframe after reconciliation or proven pre-Send failure."""
    conn = connect(_resolve_db_path(args.db_path))
    try:
        result = recover_unknown_keyframe_submission(
            conn,
            workflow_id=args.workflow_id,
            owner_user_id=args.owner_user_id,
            authorization=None,
            operation_id=args.operation_id,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI KEYFRAME RECOVERY] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_recover_skipped_keyframe_qc(args: argparse.Namespace) -> int:
    """Authorize a bounded, audited keyframe policy recovery for one frame."""
    from video_factory.application.operator_handler import authorize_skipped_keyframe_policy_recovery

    conn = connect(_resolve_db_path(args.db_path))
    try:
        result = authorize_skipped_keyframe_policy_recovery(
            conn,
            workflow_id=args.workflow_id,
            frame_id=args.frame_id,
            reason=args.reason,
            actor=args.actor,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI KEYFRAME POLICY RECOVERY] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_confirm_keyframe(args: argparse.Namespace) -> int:
    """Confirm a verified output image for a quarantined keyframe operation."""
    conn = connect(_resolve_db_path(args.db_path))
    try:
        from video_factory.application.operator_handler import confirm_keyframe_submission
        result = confirm_keyframe_submission(
            conn,
            workflow_id=args.workflow_id,
            operation_id=args.operation_id,
            image_path=args.image_path,
            owner_user_id=args.owner_user_id,
            project_url=args.project_url,
            resume_workflow=not getattr(args, "no_resume", False),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI CONFIRM KEYFRAME] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_reconcile_keyframe_terminal(args: argparse.Namespace) -> int:
    """Record an operator-verified terminal outcome for one exact Flow keyframe project."""
    conn = connect(_resolve_db_path(args.db_path))
    try:
        result = record_terminal_keyframe_outcome(
            conn,
            workflow_id=args.workflow_id,
            operation_id=args.operation_id,
            owner_user_id=args.owner_user_id,
            project_url=args.project_url,
            outcome=args.outcome,
            evidence_reference=args.evidence_reference,
            note=args.note,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI KEYFRAME TERMINAL RECONCILIATION] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_recover_creative(args: argparse.Namespace) -> int:
    """Resume a quarantined creative stage (direction, script, storyboard) after local reconciliation."""
    from video_factory.application.operator_handler import recover_creative_stage

    conn = connect(_resolve_db_path(args.db_path))
    try:
        result = recover_creative_stage(
            conn,
            workflow_id=args.workflow_id,
            owner_user_id=args.owner_user_id,
            note=args.note or "",
            allow_one_extra_retry=bool(getattr(args, "allow_one_extra_retry", False)),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI CREATIVE RECOVERY] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def cmd_close_creative_attempt(args: argparse.Namespace) -> int:
    """Explicitly close one UNKNOWN creative request before a later recovery."""
    from video_factory.application.operator_handler import close_creative_attempt

    conn = connect(_resolve_db_path(args.db_path))
    try:
        result = close_creative_attempt(
            conn,
            workflow_id=args.workflow_id,
            attempt_id=args.attempt_id,
            resolution=args.resolution,
            evidence_reference=args.evidence_reference,
            owner_user_id=args.owner_user_id,
            note=args.note or "",
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI ĐÓNG CREATIVE ATTEMPT] {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()



def cmd_clone_session(args: argparse.Namespace) -> int:
    """Clone Chrome profile to independent Video Factory profile."""
    from video_factory.integrations.chrome_profile import (
        clone_chrome_profile,
        get_default_system_chrome_user_data_dir,
        get_isolated_flow_profile_dir,
    )
    source = Path(args.source) if args.source else get_default_system_chrome_user_data_dir()
    target = Path(args.target) if args.target else get_isolated_flow_profile_dir()
    profile = args.profile or "Default"
    print("🔄 Đang clone session Chrome:")
    print(f"   • Nguồn: {source} (Profile: {profile})")
    print(f"   • Đích:  {target}")
    try:
        res = clone_chrome_profile(source, target, profile_name=profile)
        print(f"✓ Hoàn tất! Đã copy {res['copied_files']} mục (Cookies, Auth, Storage).")
        print("✓ Video Factory hiện đã có Profile Chrome độc lập, không còn xung đột với Chrome của bạn.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI CLONE SESSION] {exc}", file=sys.stderr)
        return 1


def cmd_login_flow(args: argparse.Namespace) -> int:
    """Launch isolated Chrome window to log in or inspect Google Flow."""
    from video_factory.integrations.chrome_profile import (
        launch_isolated_browser,
        get_isolated_flow_profile_dir,
    )
    target = Path(args.target) if args.target else get_isolated_flow_profile_dir()
    profile = args.profile or "Default"
    url = args.url or "https://flow.google.com/"
    print(f"🚀 Đang mở Chrome độc lập của Video Factory tại: {target} (Profile: {profile})")
    print(f"   URL: {url}")
    try:
        launch_isolated_browser(target, profile_name=profile, url=url)
        print("✓ Đã khởi chạy Chrome thành công. Bạn có thể kiểm tra trạng thái đăng nhập hoặc đăng nhập tài khoản Google.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[LỖI MỞ CHROME] {exc}", file=sys.stderr)
        return 1


def cmd_reset(args: argparse.Namespace) -> int:
    """Clear stalled/errored workflows so they can be retried cleanly or skipped."""
    from video_factory.automation.coordinator import ProductVideoCoordinator

    db_path = _resolve_db_path(args.db_path)
    conn = connect(db_path)
    try:
        inbox_root = getattr(args, "inbox_root", None)
        coordinator = ProductVideoCoordinator(connection=conn, inbox_root=inbox_root, db_path=db_path)

        if args.all_blocked:
            results = coordinator.reset_all_blocked(skip=args.skip, reason=args.reason)
            if not results:
                print("[THÔNG BÁO] Không phát hiện workflow hay sản phẩm nào đang bị nghẽn/lỗi.")
                return 0
            action_label = "đánh dấu bỏ qua (SKIP)" if args.skip else "đặt lại về [CHƯA TẠO VIDEO]"
            print(f"[THÀNH CÔNG] Đã giải phóng và {action_label} cho {len(results)} workflow bị nghẽn:")
            for r in results:
                print(f"  - Workflow: {r['workflow_id']} | Key: {(r['product_key'] or 'N/A')[:16]}... -> {r['action']}")
            return 0

        if not args.target_id:
            print("[ERROR] Vui lòng truyền <workflow_id_hoặc_product_key> hoặc dùng cờ '--all-blocked'.", file=sys.stderr)
            return 1

        try:
            res = coordinator.reset_product_or_workflow(
                args.target_id,
                skip=args.skip,
                reason=args.reason,
            )
            action_label = "Đánh dấu bỏ qua (chọn sản phẩm khác khi chạy --auto)" if args.skip else "Đặt lại về [CHƯA TẠO VIDEO] (có thể chạy lại)"
            print(f"[THÀNH CÔNG] Đã xóa nghẽn cho '{args.target_id}':")
            print(f"  - Workflow ID: {res['workflow_id']}")
            print(f"  - Product Key: {res['product_key'] or 'N/A'}")
            print(f"  - Hành động:   {action_label}")
            return 0
        except (KeyError, ValueError) as exc:
            print(f"[LỖI RESET] {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()


def cmd_watch(args: argparse.Namespace) -> int:
    """Theo dõi liên tục Exchange Inbox và tự động sản xuất video khi có sản phẩm mới."""
    import time

    db_path = _resolve_db_path(args.db_path)
    inbox_root = getattr(args, "inbox_root", None)
    source_root = getattr(args, "legacy_source_root", None) or getattr(args, "source_root", None)
    if getattr(args, "catalog_v2_candidates", None) is None:
        catalog_env = os.environ.get("HERMES_CATALOG_V2_CANDIDATES")
        from video_factory.automation.catalog_candidates import DEFAULT_CATALOG_V2_CANDIDATES

        args.catalog_v2_candidates = Path(catalog_env) if catalog_env else DEFAULT_CATALOG_V2_CANDIDATES
    interval = getattr(args, "interval", 30)
    limit = getattr(args, "limit", 3)
    run_once = getattr(args, "once", False)

    print("=" * 65)
    print("   HERMES VIDEO FACTORY - AUTONOMOUS INBOX WATCHER (DAEMON)")
    print("=" * 65)
    print(f"[*] Thư mục DB:        {db_path}")
    print(f"[*] Chu kỳ kiểm tra:   {interval}s" if not run_once else "[*] Chế độ:           Chạy 1 lượt (once)")
    print(f"[*] Giới hạn mỗi đợt:  {limit} sản phẩm")
    print(f"[*] Provider mode:     {args.provider_mode}")
    print(f"[*] Auto approve:      {getattr(args, 'auto_approve', True)}")
    print("[*] Nhấn Ctrl+C để dừng bộ theo dõi.\n")

    iteration = 0
    try:
        while True:
            iteration += 1
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = connect(db_path)
            try:
                from video_factory.workflows.runtime_control import ensure_pipeline_pause

                pause = ensure_pipeline_pause(conn)
                if pause:
                    print(
                        f"\n[{now_str}] [PIPELINE PAUSED] {pause.get('category')}/"
                        f"{pause.get('code')} workflow={pause.get('workflow_id') or 'unknown'}: "
                        f"{pause.get('message')}"
                    )
                    if run_once:
                        return 2
                    time.sleep(interval)
                    continue
                service = DailyCandidateService(
                    conn,
                    source_root=source_root,
                    inbox_root=inbox_root,
                    db_path=db_path,
                    catalog_candidates_path=args.catalog_v2_candidates,
                )
                try:
                    candidates = service.get_candidates()
                except FileNotFoundError:
                    candidates = []

                unprocessed = [c for c in candidates if c.status == "unprocessed"]
                if unprocessed:
                    print(f"\n[{now_str}] Phát hiện {len(unprocessed)} sản phẩm mới! Bắt đầu tạo video tự động (Giới hạn: {limit})...")
                    args.auto = True
                    args.auto_approve = getattr(args, "auto_approve", True)
                    args.with_research = True
                    args.product_keys = None
                    args.date = None
                    args.minimum_daily = 0
                    cmd_start(args)

                active_candidates = service.get_candidates() if unprocessed else candidates
                active = [c for c in active_candidates if c.status in ("in_production", "awaiting_product_lock")]
                status_summary = f"{len(active_candidates)} sản phẩm ({len(active)} đang xử lý, {len([c for c in active_candidates if c.status == 'unprocessed'])} chưa xử lý)"
                print(f"[{now_str}] Đang lắng nghe Exchange Inbox... ({status_summary})", end="\r", flush=True)
                workflow_repo = WorkflowRepository(conn)
                active_ids = []
                for candidate in active:
                    if not candidate.workflow_id:
                        continue
                    wf_data = workflow_repo.get_workflow(candidate.workflow_id)
                    if wf_data and wf_data[0].get("execution_mode", "manual") == "auto":
                        active_ids.append(candidate.workflow_id)
                active_ids = list(dict.fromkeys(active_ids))
                if active_ids:
                        if getattr(args, "auto_approve", True):
                            auto_gates = {
                                ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value,
                                ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value,
                                ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value,
                                ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value,
                            }
                            for wf_id in active_ids:
                                wf_data = workflow_repo.get_workflow(wf_id)
                                if wf_data and wf_data[0].get("state") in auto_gates:
                                    if (wf_data[0].get("state") == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
                                            and wf_data[0].get("creative_policy", {}).get("creative_mode") != "four_agent"
                                            and not getattr(args, "authorize_cost", False)):
                                        print(f"\n[{wf_id}] Render cần --authorize-cost; giữ tại Storyboard Approval.", file=sys.stderr)
                                        continue
                                    if wf_data[0].get("state") == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value:
                                        render_error = _auto_render_authorization_error(wf_data[0], wf_id)
                                        if render_error:
                                            print(f"\n[{wf_id}] {render_error}; giữ tại Storyboard Approval.", file=sys.stderr)
                                            continue
                                    try:
                                        approve_and_advance_workflow(
                                            connection=conn,
                                            target_id=wf_id,
                                            owner_user_id=args.owner_user_id,
                                            provider_mode=args.provider_mode,
                                        )
                                    except (KeyError, ValueError) as exc:
                                        print(f"\n[{wf_id}] Chưa thể tự động qua gate: {exc}", file=sys.stderr)
                        run_worker(
                            db_path=db_path,
                            provider_mode=args.provider_mode,
                            once=True,
                            batch_size=max(1, len(active_ids) * 2),
                            workflow_ids=active_ids,
                        )

            except Exception as exc:
                print(f"\n[{now_str}] [CẢNH BÁO] Lỗi chu kỳ watcher: {exc}", file=sys.stderr)
            finally:
                conn.close()

            if run_once:
                print("\n[HOÀN TẤT] Đã hoàn thành 1 chu kỳ kiểm tra.")
                return 0

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n\n[DỪNG] Đã dừng Video Factory Inbox Watcher.")
        return 0


def build_parser() -> argparse.ArgumentParser:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        prog="hermes personal video",
        description="Hermes Personal Video Factory - Quản lý, tuyển chọn và duyệt video sản phẩm hàng ngày",
    )
    parser.add_argument("--db-path", default=None, help="Đường dẫn file SQLite database")
    parser.add_argument("--inbox-root", type=Path, default=None, help="Thư mục exchange inbox chứa READY batches")
    parser.add_argument(
        "--legacy-source-root",
        dest="legacy_source_root",
        type=Path,
        default=None,
        help="[MIGRATION/DEBUG ONLY] Thư mục raw daily_runs của Product-Intelligence chứa products.json",
    )
    parser.add_argument("--source-root", dest="legacy_source_root", type=Path, default=None, help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="subcommand", metavar="<lệnh>")
    from video_factory.automation.creative_cli import cmd_creative_run, cmd_creative_prepare, cmd_creative_produce, cmd_creative_batch
    p_creative = subparsers.add_parser('creative-run',help='One fresh PI acquisition through four-agent worker/export; no batch')
    p_creative.add_argument('--run-id',required=True)
    p_creative.add_argument('--query',required=True)
    p_creative.add_argument('--profile',type=Path,required=True)
    p_creative.add_argument('--owner-user-id',default='local-owner')
    p_creative.add_argument('--max-ticks',type=int,default=100)
    p_creative.set_defaults(func=cmd_creative_run)

    p_prepare = subparsers.add_parser('creative-prepare', help='Prepare one saved catalog source; stop before media generation, no PI acquisition')
    source = p_prepare.add_mutually_exclusive_group(required=True)
    source.add_argument('--product-key', help='Selected product key in an existing Exchange manifest')
    source.add_argument('--source-workflow-id', help='Existing source/preparation workflow to resume')
    p_prepare.add_argument('--preparation-id', required=True)
    p_prepare.add_argument('--profile', type=Path, required=True)
    p_prepare.add_argument('--date', help='Existing Exchange batch date YYYY-MM-DD')
    p_prepare.add_argument('--owner-user-id', default='local-owner')
    p_prepare.add_argument('--max-ticks', type=int, default=25,
        help='Deprecated compatibility option; workflow progression stops on completion, skip or blocker')
    p_prepare.add_argument('--db-path', default=argparse.SUPPRESS)
    p_prepare.add_argument('--inbox-root', type=Path, default=argparse.SUPPRESS)
    p_prepare.set_defaults(func=cmd_creative_prepare)

    p_produce = subparsers.add_parser('creative-produce', help='Produce only a durable selection from prepared catalog; no acquisition or implicit preparation')
    p_produce.add_argument('--run-id', required=True)
    p_produce.add_argument('--prepared-ref', action='append', default=[], help='Prepared workflow ID; repeat to pin order, omit for stable catalog order')
    selection_limit=p_produce.add_mutually_exclusive_group(required=True)
    selection_limit.add_argument('--limit', type=int, help='Maximum number of eligible products')
    selection_limit.add_argument('--all', dest='select_all', action='store_true', help='Select all eligible prepared items from --date')
    p_produce.add_argument('--date', help='Restrict selection to this READY batch date (YYYY-MM-DD)')
    p_produce.add_argument('--profile', type=Path, required=True)
    p_produce.add_argument('--owner-user-id', default='local-owner')
    p_produce.add_argument('--max-ticks', type=int, default=100,
        help='Deprecated compatibility option; workflow progression stops on completion, skip or blocker')
    p_produce.add_argument('--db-path', default=argparse.SUPPRESS)
    p_produce.set_defaults(func=cmd_creative_produce)

    p_batch = subparsers.add_parser('creative-batch', help='Run the latest PI READY product batch through preparation, QC and MP4 export')
    p_batch.add_argument('--run-id', help='Advanced resume binding; generated automatically for normal use')
    p_batch.add_argument('--date', help='Optional READY batch date YYYY-MM-DD; defaults to the latest READY batch')
    batch_selection=p_batch.add_mutually_exclusive_group()
    batch_selection.add_argument('--all', action='store_true', help='All eligible products (default)')
    batch_selection.add_argument('--limit', type=int, help='Maximum eligible products to prepare and produce')
    batch_selection.add_argument('--product-key', action='append', help='Specific manifest product key; repeat to set selection')
    p_batch.add_argument('--profile', type=Path, help='Creative profile; defaults to VF_CREATIVE_RUN_CONFIG')
    p_batch.add_argument('--qc-level', choices=('low', 'middle', 'high'),
        help='QC policy level (new batches: high; resume: saved policy)')
    p_batch.add_argument('--disable-keyframe-qc', action='store_true',
        help='Low-level only: record QC as not performed and use the first generated keyframes')
    p_batch.add_argument('--owner-user-id', default='local-owner')
    p_batch.add_argument('--max-ticks', type=int, default=100,
        help='Deprecated compatibility option; workflow progression stops on completion, skip or blocker')
    p_batch.add_argument('--db-path', default=argparse.SUPPRESS)
    p_batch.add_argument('--inbox-root', type=Path, default=argparse.SUPPRESS)
    p_batch.add_argument('--exchange-batch', type=Path, help='Use this exact READY Exchange batch')
    p_batch.add_argument('--provider-mode', choices=('configured', 'fake'), default='configured',
        help='Provider execution mode (default: configured, or fake for offline verification)')
    p_batch.set_defaults(func=cmd_creative_batch)

    from video_factory.creative.agents.preview import cmd_creative_preview
    p_preview = subparsers.add_parser('creative-preview', help='Preview four-agent creative using local dossier/profile; no generation')
    p_preview.add_argument('--dossier', type=Path, required=True)
    p_preview.add_argument('--profile', type=Path, required=True)
    p_preview.add_argument('--output', type=Path, required=True)
    p_preview.set_defaults(func=cmd_creative_preview)

    # list
    p_list = subparsers.add_parser("list", help="Liệt kê danh sách sản phẩm scouted và trạng thái video")
    p_list.add_argument("--date", default=None, help="Ngày quét (YYYY-MM-DD), mặc định ngày mới nhất")
    p_list.add_argument("--status", choices=["all", "unprocessed", "awaiting_product_lock", "in_production", "completed", "invalid"], default="all")
    p_list.add_argument("--json", action="store_true", help="Xuất định dạng JSON")
    p_list.set_defaults(func=cmd_list)

    # start
    p_start = subparsers.add_parser("start", help="Khởi động workflow tạo video cho sản phẩm")
    p_start.add_argument("--auto", action="store_true", help="Tự động lấy các sản phẩm chưa tạo video")
    p_start.add_argument("--all", dest="all_products", action="store_true", help="Xét toàn bộ sản phẩm trong READY batch hiện tại (tối đa 100)")
    p_start.add_argument("--limit", type=int, default=5, help="Số lượng sản phẩm tối đa khi dùng --auto (mặc định: 5)")
    p_start.add_argument("--product-keys", nargs="+", help="Danh sách product_key cụ thể cần tạo video")
    p_start.add_argument(
        "--catalog-v2-candidates",
        type=Path,
        default=None,
        help="CSV Product Catalog V2; chọn theo canonical_product_key, chỉ identity VALID và READY_FOR_IMAGE_QC",
    )
    p_start.add_argument("--confirmed-image-asset-id", action="append", default=[], help="Asset ID đã xem và xác nhận, lặp lại được")
    p_start.add_argument("--confirmed-claim", action="append", default=[], help="Claim|https://nguồn-thật đã được xác nhận, lặp lại được")
    p_start.add_argument("--date", default=None, help="Ngày quét YYYY-MM-DD hoặc today")
    p_start.add_argument("--minimum-daily", type=int, default=0, help="Báo lỗi nếu tổng workflow bắt đầu trong ngày thấp hơn mức này")
    p_start.add_argument("--owner-user-id", default="local-owner")
    p_start.add_argument("--provider-mode", default="configured", choices=["configured", "fake"])
    p_start.add_argument("--qc-max-cost-usd", type=float, default=None, help="Legacy QC flag; ignored by QC admission")
    p_start.add_argument("--qc-authorization-run-id", default=None, help="Legacy QC field; may be retained for paid render authorization")
    p_start.add_argument("--qc-authorization-scope", action="append", default=[], help="Legacy QC field; may be retained for paid render authorization")
    p_start.add_argument("--qc-authorization-expires-at", default=None, help="Legacy QC field; may be retained for paid render authorization")
    p_start.add_argument("--qc-authorization-max-cost-usd", type=float, default=None, help="Legacy QC cost field; ignored by QC admission")
    p_start.add_argument("--qc-budget-mode", choices=["capped", "unlimited"], default=None, help="Legacy QC flag; accepted but ignored by QC admission")
    p_start.add_argument("--research-snapshot-id", default=None, help="Dùng snapshot PI đã lưu thay vì research lại")
    p_start.add_argument("--no-research", dest="with_research", action="store_false", help="Không tự động chạy worker research")
    p_start.add_argument("--no-auto-approve", dest="auto_approve", action="store_false", default=True, help="Dừng lại tại cổng AwaitingProductLock thay vì tự động phê duyệt")
    p_start.add_argument("--authorize-cost", action="store_true", help="Ủy quyền chi phí render video khi chạy tự động")
    p_start.add_argument("--max-qc-retries", type=int, default=3, help="Số lần retry QC tối đa sau lần đầu (0–3; tối đa 4 evaluations)")
    p_start.set_defaults(with_research=True, func=cmd_start)

    # watch (daemon)
    p_watch = subparsers.add_parser(
        "watch",
        help="Chạy daemon nền tự động theo dõi Exchange Inbox và sinh video ngay khi có sản phẩm mới",
    )
    p_watch.add_argument("--interval", type=int, default=30, help="Chu kỳ quét thư mục (giây, mặc định: 30)")
    p_watch.add_argument("--limit", type=int, default=3, help="Số lượng sản phẩm xử lý mỗi đợt (mặc định: 3)")
    p_watch.add_argument("--catalog-v2-candidates", type=Path, default=None, help="CSV Product Catalog V2 theo contract identity/pipeline")
    p_watch.add_argument("--once", action="store_true", help="Chạy 1 lượt kiểm tra rồi thoát (dùng cho Task Scheduler)")
    p_watch.add_argument("--owner-user-id", default="local-owner")
    p_watch.add_argument("--provider-mode", default="configured", choices=["configured", "fake"])
    p_watch.add_argument("--qc-max-cost-usd", type=float, default=None, help="Legacy QC flag; ignored by QC admission")
    p_watch.add_argument("--qc-authorization-run-id", default=None, help="Legacy QC field; may be retained for paid render authorization")
    p_watch.add_argument("--qc-authorization-scope", action="append", default=[], help="Legacy QC field; may be retained for paid render authorization")
    p_watch.add_argument("--qc-authorization-expires-at", default=None, help="Legacy QC field; may be retained for paid render authorization")
    p_watch.add_argument("--qc-authorization-max-cost-usd", type=float, default=None, help="Legacy QC cost field; ignored by QC admission")
    p_watch.add_argument("--qc-budget-mode", choices=["capped", "unlimited"], default=None, help="Legacy QC flag; accepted but ignored by QC admission")
    p_watch.add_argument("--no-auto-approve", dest="auto_approve", action="store_false", default=True, help="Không tự động duyệt gate")
    p_watch.add_argument("--authorize-cost", action="store_true", help="Ủy quyền chi phí render video")
    p_watch.add_argument("--max-qc-retries", type=int, default=3, help="Số lần retry QC tối đa sau lần đầu (0–3; tối đa 4 evaluations)")
    p_watch.set_defaults(func=cmd_watch)

    # select
    p_select = subparsers.add_parser("select", help="Giao diện CLI trực quan để chọn sản phẩm tạo video")
    p_select.add_argument("--date", default=None, help="Ngày quét")
    p_select.add_argument("--owner-user-id", default="local-owner")
    p_select.add_argument("--provider-mode", default="configured", choices=["configured", "fake"])
    p_select.add_argument("--qc-max-cost-usd", type=float, default=None, help="Legacy QC flag; ignored by QC admission")
    p_select.add_argument("--qc-authorization-run-id", default=None, help="Legacy QC field; may be retained for paid render authorization")
    p_select.add_argument("--qc-authorization-scope", action="append", default=[], help="Legacy QC field; may be retained for paid render authorization")
    p_select.add_argument("--qc-authorization-expires-at", default=None, help="Legacy QC field; may be retained for paid render authorization")
    p_select.add_argument("--qc-authorization-max-cost-usd", type=float, default=None, help="Legacy QC cost field; ignored by QC admission")
    p_select.add_argument("--qc-budget-mode", choices=["capped", "unlimited"], default=None, help="Legacy QC flag; accepted but ignored by QC admission")
    p_select.add_argument("--research-snapshot-id", default=None, help="Dùng snapshot PI đã lưu thay vì research lại")
    p_select.add_argument("--no-research", dest="with_research", action="store_false")
    p_select.add_argument("--max-qc-retries", type=int, default=3, help="Số lần retry QC tối đa sau lần đầu (0–3; tối đa 4 evaluations)")
    p_select.set_defaults(with_research=True, func=cmd_select)

    # approve
    p_approve = subparsers.add_parser("approve", help="Phê duyệt Product Lock (hoặc gate khác) để chuyển sang bước tiếp theo")
    p_approve.add_argument("target_id", help="Approval Request ID (UUID) hoặc Workflow ID")
    p_approve.add_argument("--owner-user-id", default="local-owner")
    p_approve.add_argument(
        "--authorize-cost",
        action="store_true",
        help="Ủy quyền chi phí render (bắt buộc tại Gate 4: AwaitingStoryboardApproval)",
    )
    p_approve.add_argument("--provider-mode", default="configured", choices=["configured", "fake"])
    p_approve.add_argument("--render-provider", choices=["google_flow", "vertex_ai"], help="Chọn nhà cung cấp render cho Gate Storyboard này")
    p_approve.add_argument("--qc-max-cost-usd", type=float, default=None, help="Legacy QC flag; ignored by QC admission")
    p_approve.add_argument("--qc-authorization-run-id", default=None, help="Run ID trong cost authorization cho QC")
    p_approve.add_argument("--qc-authorization-scope", action="append", default=[], help="Scope QC được cấp, lặp lại được")
    p_approve.add_argument("--qc-authorization-expires-at", default=None, help="Thời điểm hết hạn ISO-8601 của QC authorization")
    p_approve.add_argument("--qc-authorization-max-cost-usd", type=float, default=None, help="Legacy QC cost field; ignored by QC admission")
    p_approve.add_argument("--qc-budget-mode", choices=["capped", "unlimited"], default=None, help="Legacy QC flag; accepted but ignored by QC admission")
    p_approve.add_argument("--qc-authorization-campaign-id", default=None, help="Campaign ID của ủy quyền QC")
    p_approve.set_defaults(func=cmd_approve)

    # status
    p_status = subparsers.add_parser("status", help="Xem thông tin chi tiết trạng thái của một workflow hoặc sản phẩm")
    p_status.add_argument("workflow_id", help="Workflow ID hoặc Product Key (ví dụ: pv-daily-1e96ac740ec2f41f)")
    p_status.set_defaults(func=cmd_status)

    p_pipeline_status = subparsers.add_parser(
        "pipeline-status", help="Xem trạng thái pause bền vững của worker pipeline"
    )
    p_pipeline_status.add_argument("--db-path", default=None, help="Đường dẫn SQLite hiện tại")
    p_pipeline_status.set_defaults(func=cmd_pipeline_status)

    p_pipeline_resume = subparsers.add_parser(
        "pipeline-resume", help="Tiếp tục sau khi đã sửa blocker và đối soát attempt"
    )
    p_pipeline_resume.add_argument("--db-path", default=None, help="Đường dẫn SQLite hiện tại")
    p_pipeline_resume.add_argument("--note", required=True, help="Ghi chú sửa route/contract và đối soát")
    p_pipeline_resume.set_defaults(func=cmd_pipeline_resume)

    p_skip = subparsers.add_parser(
        "skip-workflow",
        help="Terminally skip one workflow with unresolved QC while preserving its attempt history",
    )
    p_skip.add_argument("workflow_id", help="Workflow ID to skip")
    p_skip.add_argument("--reason", required=True, help="Why the unresolved workflow cannot be reconciled")
    p_skip.add_argument("--db-path", default=None, help="SQLite path (defaults to the configured Hermes DB)")
    p_skip.set_defaults(func=cmd_skip_workflow)

    p_pipeline_requeue = subparsers.add_parser(
        "pipeline-requeue-preflight",
        help="Requeue resource-pack job chỉ khi quarantine khớp lỗi 9Router pre-send đã biết",
    )
    p_pipeline_requeue.add_argument(
        "workflow_id",
        help="Workflow ID có resource-pack job bị quarantine do lỗi 9Router pre-send đã biết",
    )
    p_pipeline_requeue.add_argument("--db-path", default=None, help="Đường dẫn SQLite hiện tại")
    p_pipeline_requeue.set_defaults(func=cmd_pipeline_requeue_preflight)

    # reset
    p_reset = subparsers.add_parser(
        "reset",
        help="Xóa nghẽn workflow/sản phẩm bị kẹt để chạy lại hoặc bỏ qua chuyển sang sản phẩm khác",
    )
    p_reset.add_argument("target_id", nargs="?", default=None, help="Workflow ID, Product Key hoặc Canonical Key")
    p_reset.add_argument("--all-blocked", action="store_true", help="Tự động giải phóng tất cả các workflow đang bị kẹt/lỗi")
    p_reset.add_argument("--skip", action="store_true", help="Đánh dấu bỏ qua sản phẩm này để '--auto' chọn sản phẩm khác")
    p_reset.add_argument("--reason", default=None, help="Ghi chú lý do bỏ qua khi dùng --skip")
    p_reset.set_defaults(func=cmd_reset)

    p_daily_status = subparsers.add_parser("daily-status", help="Báo số video hoàn thành theo ngày Product-Intelligence")
    p_daily_status.add_argument("--date", default="today", help="Ngày YYYY-MM-DD hoặc today")
    p_daily_status.add_argument("--minimum", type=int, default=3, help="Số video hoàn thành tối thiểu để trả exit code 0")
    p_daily_status.add_argument("--json", action="store_true")
    p_daily_status.set_defaults(func=cmd_daily_status)

    # qc
    p_qc = subparsers.add_parser("qc", help="Chạy QC governed cho một workflow; không tự phê duyệt gate")
    p_qc.add_argument("workflow_id", help="Workflow ID")
    p_qc.add_argument("--stage", choices=[stage.value for stage in QCStage], required=True)
    p_qc.add_argument("--provider-mode", default="configured", choices=["configured", "fake"])
    p_qc.add_argument("--authorize-cost", action="store_true", help="Legacy flag; QC no longer uses money authorization (paid recovery generation still does)")
    p_qc.add_argument("--max-cost-usd", type=float, default=None, help="Legacy QC budget flag; accepted but ignored by QC admission")
    p_qc.add_argument("--authorization-run-id", default=None, help="Legacy QC budget flag; accepted but ignored by QC admission")
    p_qc.add_argument("--authorization-scope", action="append", default=[], help="Legacy QC budget flag; accepted but ignored by QC admission")
    p_qc.add_argument("--authorization-expires-at", default=None, help="Legacy QC budget flag; accepted but ignored by QC admission")
    p_qc.add_argument("--authorization-max-cost-usd", type=float, default=None, help="Legacy QC budget flag; accepted but ignored by QC admission")
    p_qc.add_argument("--qc-budget-mode", choices=["capped", "unlimited"], default=None, help="Legacy QC budget flag; accepted but ignored by QC admission")
    p_qc.add_argument("--authorization-campaign-id", default=None, help="Legacy QC budget flag; accepted but ignored by QC admission")
    p_qc.add_argument("--advance", action="store_true", help="Sau configured QC PASS, tạo human approval gate phù hợp")
    p_qc.set_defaults(func=cmd_qc)

    p_qc_reconcile = subparsers.add_parser(
        "qc-reconcile",
        help="Ghi nhận đối soát operator cho một QC UNKNOWN trước khi cho phép thử lại",
    )
    p_qc_reconcile.add_argument("workflow_id", help="Workflow ID")
    p_qc_reconcile.add_argument("--attempt-id", required=True, help="QC attempt đang ở technical_unknown")
    p_qc_reconcile.add_argument("--outcome", required=True, choices=["consumed", "not_sent"])
    p_qc_reconcile.add_argument(
        "--evidence-reference", required=True,
        help="Tham chiếu tới provider usage record hoặc kết quả kiểm tra request chưa gửi",
    )
    p_qc_reconcile.add_argument("--note", required=True, help="Ghi chú đối soát của operator")
    p_qc_reconcile.set_defaults(func=cmd_qc_reconcile)

    p_qc_presend = subparsers.add_parser(
        "qc-reconcile-presend",
        help="Đối soát reservation Image QC bị quota từ chối trước khi gửi request",
    )
    p_qc_presend.add_argument("workflow_id", help="Workflow ID")
    p_qc_presend.add_argument("--reservation-id", required=True, help="Reservation ID cần chứng minh là pre-send")
    p_qc_presend.set_defaults(func=cmd_qc_reconcile_presend)

    p_attach_evidence = subparsers.add_parser(
        "attach-source-evidence",
        help="Gắn record nguồn đã qua Catalog V2 validator vào workflow sau Product Lock",
    )
    p_attach_evidence.add_argument("workflow_id", help="Workflow ID đã duyệt Product Lock")
    p_attach_evidence.add_argument("--record-id", required=True, help="Record ID do publisher trả về")
    p_attach_evidence.add_argument("--record-dir", required=True, type=Path, help="Thư mục record đã được validator xác nhận")
    p_attach_evidence.add_argument("--operator-id", required=True, help="Người duyệt nguồn và identity")
    p_attach_evidence.add_argument("--review-note", required=True, help="Ghi chú đối chiếu nguồn với đúng model/variant")
    p_attach_evidence.set_defaults(func=cmd_attach_source_evidence)

    p_replace_source_images = subparsers.add_parser(
        "replace-source-images",
        help="Sửa ảnh/thông tin nguồn cho sản phẩm block tại Image QC rồi chạy lại resource-pack/QC hiện có",
    )
    p_replace_source_images.add_argument("target", help="Product key or canonical product key")
    p_replace_source_images.add_argument("--image", action="append", default=[], type=Path,
        help="Đường dẫn ảnh mới; lặp lại cho nhiều ảnh")
    p_replace_source_images.add_argument("--name")
    p_replace_source_images.add_argument("--brand")
    p_replace_source_images.add_argument("--model")
    p_replace_source_images.add_argument("--variant")
    p_replace_source_images.add_argument("--category")
    p_replace_source_images.add_argument("--key-features")
    p_replace_source_images.add_argument("--source-url")
    p_replace_source_images.add_argument("--price-display")
    p_replace_source_images.add_argument("--reason", required=True, help="Lý do thay ảnh, lưu trong audit event")
    p_replace_source_images.add_argument("--owner-user-id", default="local-owner")
    p_replace_source_images.add_argument("--db-path", default=argparse.SUPPRESS)
    p_replace_source_images.set_defaults(func=cmd_replace_source_images)

    p_recover = subparsers.add_parser(
        "recover-keyframe", help="Reconcile an unknown keyframe or resume after a proven pre-Send Flow failure"
    )
    p_recover.add_argument("workflow_id", help="Workflow ID (positional)")
    p_recover.add_argument("--operation-id", required=True, help="Targeted keyframe operation ID to replace")
    p_recover.add_argument("--owner-user-id", default="local-owner")
    p_recover.add_argument("--authorization-run-id", default=None)
    p_recover.add_argument("--authorization-scope", action="append", default=[])
    p_recover.add_argument("--authorization-expires-at", default=None)
    p_recover.add_argument("--authorization-max-cost-usd", type=float, default=None)
    p_recover.add_argument("--qc-budget-mode", choices=["capped", "unlimited"], default=None)
    p_recover.add_argument("--authorization-campaign-id", default=None)
    p_recover.add_argument("--db-path", default=None, help="Đường dẫn file SQLite database")
    p_recover.set_defaults(func=cmd_recover_keyframe)

    p_recover_skipped_keyframe = subparsers.add_parser(
        "recover-skipped-keyframe-qc",
        help="Authorize one audited single-frame keyframe recovery after exhausted QC retries",
    )
    p_recover_skipped_keyframe.add_argument("workflow_id", help="Skipped workflow ID")
    p_recover_skipped_keyframe.add_argument("--frame-id", required=True, help="One failed frame from the current QC report")
    p_recover_skipped_keyframe.add_argument("--reason", required=True, help="Operator reason for this single-frame recovery")
    p_recover_skipped_keyframe.add_argument("--actor", default="local-owner")
    p_recover_skipped_keyframe.add_argument("--db-path", default=None, help="Path to the existing workflow SQLite database")
    p_recover_skipped_keyframe.set_defaults(func=cmd_recover_skipped_keyframe_qc)

    p_confirm = subparsers.add_parser(
        "confirm-keyframe", help="Confirm verified output image for a quarantined keyframe and resume workflow"
    )
    p_confirm.add_argument("workflow_id", help="Workflow ID (positional)")
    p_confirm.add_argument("--operation-id", required=True, help="Keyframe operation ID")
    p_confirm.add_argument("--image-path", required=True, help="Path to verified downloaded keyframe image")
    p_confirm.add_argument("--owner-user-id", default="local-owner", help="Owner user ID")
    p_confirm.add_argument("--project-url", required=True, help="Google Flow project URL")
    p_confirm.add_argument("--no-resume", action="store_true", help="Do not automatically resume workflow outbox")
    p_confirm.add_argument("--db-path", default=None, help="Đường dẫn file SQLite database")
    p_confirm.set_defaults(func=cmd_confirm_keyframe)

    p_reconcile_keyframe_terminal = subparsers.add_parser(
        "reconcile-keyframe-terminal",
        help="Record an operator-verified terminal failure/cancellation for one exact Flow project",
    )
    p_reconcile_keyframe_terminal.add_argument("workflow_id", help="Workflow ID")
    p_reconcile_keyframe_terminal.add_argument("--operation-id", required=True, help="Exact keyframe operation ID")
    p_reconcile_keyframe_terminal.add_argument("--owner-user-id", default="local-owner")
    p_reconcile_keyframe_terminal.add_argument("--project-url", required=True, help="Exact Flow URL stored in the operation ledger")
    p_reconcile_keyframe_terminal.add_argument("--outcome", required=True, choices=["failed", "cancelled", "canceled"])
    p_reconcile_keyframe_terminal.add_argument("--evidence-reference", required=True, help="Operator reference to the observed terminal evidence")
    p_reconcile_keyframe_terminal.add_argument("--note", required=True, help="Operator reconciliation note")
    p_reconcile_keyframe_terminal.add_argument("--db-path", default=None, help="Path to the existing workflow SQLite database")
    p_reconcile_keyframe_terminal.set_defaults(func=cmd_reconcile_keyframe_terminal)

    p_recover_creative = subparsers.add_parser(
        "recover-creative", help="Reconcile a quarantined creative stage and resume workflow"
    )
    p_recover_creative.add_argument("workflow_id", help="Workflow ID (positional)")
    p_recover_creative.add_argument("--owner-user-id", default="local-owner")
    p_recover_creative.add_argument("--note", default="", help="Ghi chú đối soát của operator")
    p_recover_creative.add_argument(
        "--allow-one-extra-retry",
        action="store_true",
        help="Operator authorization for one Scene Plan retry beyond the normal two-retry limit",
    )
    p_recover_creative.add_argument("--db-path", default=None, help="Đường dẫn file SQLite database")
    p_recover_creative.set_defaults(func=cmd_recover_creative)

    p_close_creative = subparsers.add_parser(
        "close-creative-attempt",
        help="Explicitly close an UNKNOWN creative provider attempt before recovery",
    )
    p_close_creative.add_argument("workflow_id", help="Workflow ID (positional)")
    p_close_creative.add_argument("--attempt-id", required=True, help="Saved 9Router attempt ID")
    p_close_creative.add_argument(
        "--resolution",
        required=True,
        choices=[
            "response_received_unverified",
            "not_sent_proven",
            "operator_retry_authorized",
            "abandoned_unknown",
        ],
        help=(
            "Reconciliation outcome; operator_retry_authorized and abandoned_unknown preserve the "
            "old UNKNOWN and record possible duplicate request/cost"
        ),
    )
    p_close_creative.add_argument("--evidence-reference", required=True, help="Trace, receipt, or evidence reference")
    p_close_creative.add_argument("--owner-user-id", default="local-owner")
    p_close_creative.add_argument("--note", default="", help="Operator note")
    p_close_creative.add_argument("--db-path", default=None, help="Đường dẫn file SQLite database")
    p_close_creative.set_defaults(func=cmd_close_creative_attempt)


    # open-folder
    p_open = subparsers.add_parser("open-folder", help="Open VideoAI hub folder in File Explorer")
    p_open.add_argument("--latest", action="store_true", help="Open the most recently modified video folder")
    p_open.add_argument("--brand", default=None, help="Filter by brand name")
    p_open.add_argument("--hub-root", default=None, help="Custom VideoAI hub root directory")
    p_open.set_defaults(func=cmd_open_folder)

    # sync-video-hub
    p_sync = subparsers.add_parser("sync-video-hub", help="Sync all historical exports to VideoAI hub")
    p_sync.add_argument("--exports-root", default=None, help="Custom exports root directory")
    p_sync.add_argument("--hub-root", default=None, help="Custom VideoAI hub root directory")
    p_sync.add_argument("--json", action="store_true", help="Output results as JSON")
    p_sync.set_defaults(func=cmd_sync_video_hub)

    # clone-session
    p_clone = subparsers.add_parser("clone-session", help="Clone Chrome profile cá nhân sang profile độc lập cho Video Factory")
    p_clone.add_argument("--source", default=None, help="Đường dẫn Chrome User Data nguồn (mặc định: tự động tìm Chrome trên máy)")
    p_clone.add_argument("--target", default=None, help="Đường dẫn Profile đích độc lập của Video Factory")
    p_clone.add_argument("--profile", default="Default", help="Tên profile Chrome cần clone (mặc định: Default)")
    p_clone.set_defaults(func=cmd_clone_session)

    # login-flow
    p_login = subparsers.add_parser("login-flow", help="Mở cửa sổ Chrome độc lập của Video Factory để kiểm tra / đăng nhập Google Flow")
    p_login.add_argument("--target", default=None, help="Đường dẫn Profile độc lập của Video Factory")
    p_login.add_argument("--profile", default="Default", help="Tên profile Chrome (mặc định: Default)")
    p_login.add_argument("--url", default="https://flow.google.com/", help="URL cần mở (mặc định: https://flow.google.com/)")
    p_login.set_defaults(func=cmd_login_flow)

    from video_factory.maintenance.cli import add_parser as add_flow_repair_parser
    add_flow_repair_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
