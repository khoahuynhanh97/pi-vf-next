"""Kanban & Operator REST API for Product Video workflows.

Provides structured endpoints for visual Kanban Board interface and HITL gate operations.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from video_factory.workflow import ProductVideoWorkflowState
from video_factory.application.operator_handler import (
    approve_and_advance_workflow,
    inspect_quarantined_creative_brief,
    resume_quarantined_creative_brief,
    resume_video_scene_after_qc_failure,
)
from video_factory.integrations.scene_renderer import SceneRenderOperationStore
from video_factory.workflows.steps import read_pipeline_steps
from video_factory.application.studio_commands import execution_context, quarantine_context


class ApprovalRequest(BaseModel):
    """Request body for approval actions."""
    workflow_id: str
    gate: str = Field(..., description="Gate name: brief|scene_plan|storyboard|final")
    action: str = Field(..., description="Action: approve|reject|rework")
    feedback: str = ""
    qc_authorization: dict[str, Any] | None = None
    qc_max_cost_usd: float | None = None
    render_provider_kind: str | None = None


class SceneReRenderRequest(BaseModel):
    """Request body to trigger re-rendering of a single scene."""
    workflow_id: str
    scene_order: int = Field(..., ge=1, le=10, description="1-indexed scene order (e.g. 1, 2, 3, 4)")
    provider_kind: str | None = Field(None, description="Optional provider kind (e.g. google_flow, vertex_veo)")
    custom_prompt: str | None = Field(None, description="Optional prompt override for this scene retry")


class StudioCommandRequest(BaseModel):
    model_config = {"extra": "forbid"}
    action_id: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=1)
    request_id: UUID
    note: str = Field(min_length=1, max_length=500)


class DeleteWorkflowItem(BaseModel):
    model_config = {"extra": "forbid"}
    workflow_id: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=1)


class DeleteWorkflowsRequest(BaseModel):
    model_config = {"extra": "forbid"}
    workflows: list[DeleteWorkflowItem] = Field(min_length=1, max_length=100)
    confirmation: str


def _get_kanban_cards(connection: sqlite3.Connection, owner_user_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Query workflows grouped by Kanban column (state)."""
    query = """
    SELECT 
        workflow_id,
        aggregate_json,
        version
    FROM workflow
    WHERE 1=1
    """
    params = []
    
    if owner_user_id:
        query += " AND json_extract(aggregate_json, '$.owner_user_id') = ?"
        params.append(owner_user_id)
    
    query += " ORDER BY json_extract(aggregate_json, '$.created_at') DESC LIMIT 100"
    
    rows = connection.execute(query, params).fetchall()
    
    columns = {
        "awaiting_product_lock": [],
        "awaiting_brief_approval": [],
        "awaiting_scene_plan_approval": [],
        "awaiting_storyboard_approval": [],
        "in_production": [],
        "awaiting_final_approval": [],
        "completed": [],
        "error": [],
    }
    
    for row in rows:
        agg = json.loads(row["aggregate_json"])
        state = agg.get("state", "")
        workflow_id = row["workflow_id"]
        
        card = {
            "workflow_id": workflow_id,
            "product_name": agg.get("product_name", agg.get("source_context", {}).get("model", "Unknown")),
            "created_at": agg.get("created_at", ""),
            "state": state,
            "version": row["version"],
            "product_key": (agg.get("source_context") or {}).get("product_key") or agg.get("product_key"),
            "failure_code": agg.get("failure_code"),
            "failure_reason": agg.get("failure_reason") or agg.get("skip_reason"),
            "operator_reason": agg.get("operator_reason"),
            "operator_stage": agg.get("operator_stage"),
        }
        card["pipeline_steps"] = read_pipeline_steps(connection, workflow_id)
        card["updated_at"] = agg.get("updated_at") or next((
            s.get("finished_at") or s.get("started_at") for s in reversed(card["pipeline_steps"])
            if s.get("finished_at") or s.get("started_at")
        ), card["created_at"])

        try:
            outbox = connection.execute(
                "SELECT state, last_error FROM workflow_outbox WHERE workflow_id=? "
                "ORDER BY outbox_id DESC LIMIT 1", (workflow_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            outbox = None
        quarantined = bool(outbox and outbox["state"] == "quarantined")
        if quarantined:
            card["quarantined_job"] = quarantine_context(connection, workflow_id)
        
        # Add thumbnail if available
        latest_artifact = agg.get("latest_artifact", {})
        if isinstance(latest_artifact, dict):
            thumbnail = (
                latest_artifact.get("thumbnail_url")
                or (latest_artifact.get("generated_keyframes", [{}])[0].get("image_url") if latest_artifact.get("generated_keyframes") else None)
            )
            if thumbnail:
                card["thumbnail_url"] = thumbnail
        source_images = (agg.get("source_context") or {}).get("images") or []
        first_image = source_images[0] if source_images and isinstance(source_images[0], dict) else {}
        if first_image.get("asset_id") and (first_image.get("sha256") or first_image.get("digest")):
            card["thumbnail_url"] = f"/api/operator/workflow/{quote(workflow_id, safe='')}/media/source/{quote(first_image['asset_id'], safe='')}"
        
        # Add estimated cost if available
        qc_auth = agg.get("qc_authorization", {})
        if isinstance(qc_auth, dict):
            card["estimated_cost"] = qc_auth.get("workflow_max_cost_usd", qc_auth.get("max_cost_usd"))
        
        # Add target duration if available
        for artifact_key in ("creative_brief", "scene_plan", "latest_artifact"):
            artifact = agg.get(artifact_key)
            if isinstance(artifact, dict) and artifact.get("target_duration"):
                card["target_duration"] = artifact["target_duration"]
                break

        # Robust state to column mapping
        s = str(state or "").strip().lower()
        if quarantined:
            columns["error"].append(card)
        elif s in ("awaitingproductlock", "awaiting_product_lock"):
            columns["awaiting_product_lock"].append(card)
        elif s in ("awaitingbriefapproval", "awaiting_brief_approval"):
            columns["awaiting_brief_approval"].append(card)
        elif s in ("awaitingsceneapproval", "awaiting_scene_approval", "awaitingsceneplanapproval", "awaiting_scene_plan_approval"):
            columns["awaiting_scene_plan_approval"].append(card)
        elif s in ("awaitingstoryboardapproval", "awaiting_storyboard_approval"):
            columns["awaiting_storyboard_approval"].append(card)
        elif s in ("awaitingfinalapproval", "awaiting_final_approval"):
            columns["awaiting_final_approval"].append(card)
        elif s in ("completed", "done", "success"):
            columns["completed"].append(card)
        elif s in ("waitingforoperator", "waiting_for_operator", "error", "failed", "skipped", "contract_broken"):
            columns["error"].append(card)
        else:
            # All in-progress active pipeline states (researching, building_resource_pack, rendering, exporting, etc.)
            columns["in_production"].append(card)
    
    return columns


def _get_workflow_detail(connection: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
    """Return complete workflow state object."""
    row = connection.execute(
        "SELECT aggregate_json, version FROM workflow WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    
    if not row:
        raise ValueError(f"Workflow {workflow_id} not found")
    
    # The stored aggregate contains versioned V2 creative artifacts that are not
    # compatible with the legacy v1 detail schema. This read model only selects
    # fields; preserve the stored artifact instead of rejecting the whole detail.
    agg_dict = json.loads(row["aggregate_json"])
    agg_dict.setdefault("workflow_id", workflow_id)
    
    # Fetch approval history (table may not exist in all environments)
    approvals = []
    try:
        approval_rows = connection.execute(
            """
            SELECT request_id, status, payload_json, created_at, updated_at
            FROM approval_request
            WHERE json_extract(payload_json, '$.workflow_id') = ?
            ORDER BY created_at DESC
            """,
            (workflow_id,),
        ).fetchall()
        
        approvals = [
            {
                "request_id": r["request_id"],
                "status": r["status"],
                "gate": json.loads(r["payload_json"]).get("gate"),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in approval_rows
        ]
    except sqlite3.OperationalError:
        pass
    
    # Fetch QC reports for the scorecard
    qc_reports = []
    try:
        qc_rows = connection.execute(
            """
            SELECT report_id, stage, artifact_digest, score, verdict,
                   findings_json, evaluator_name, created_at
            FROM product_video_qc_reports
            WHERE workflow_id = ?
            ORDER BY created_at ASC
            """,
            (workflow_id,),
        ).fetchall()
        qc_reports = [
            {
                "report_id": r["report_id"],
                "stage": r["stage"],
                "artifact_digest": r["artifact_digest"],
                "score": r["score"],
                "verdict": r["verdict"],
                "findings": json.loads(r["findings_json"] or "{}"),
                "evaluator_name": r["evaluator_name"],
                "created_at": r["created_at"],
            }
            for r in qc_rows
        ]
    except sqlite3.OperationalError:
        pass

    # Fetch cost ledger
    budget_ledger = []
    try:
        budget_rows = connection.execute(
            """
            SELECT reservation_id, capability_id, amount, currency, state, created_at
            FROM budget_reservation
            WHERE scope_id = ?
            ORDER BY created_at DESC
            """,
            (workflow_id,),
        ).fetchall()
        
        budget_ledger = [
            {
                "reservation_id": r["reservation_id"],
                "capability_id": r["capability_id"],
                "amount": r["amount"],
                "currency": r["currency"],
                "state": r["state"],
                "created_at": r["created_at"],
            }
            for r in budget_rows
        ]
    except sqlite3.OperationalError:
        pass
    
    # Fetch scene operations (rendered clips status per scene)
    scene_operations = []
    try:
        render_result = agg_dict.get("render_result") or {}
        batch_id = render_result.get("render_id") or ""
        if batch_id:
            scene_columns = {r[1] for r in connection.execute("PRAGMA table_info(video_factory_scene_operation)")}
            request_column = "request_json" if "request_json" in scene_columns else "'{}' AS request_json"
            scene_rows = connection.execute(
                f"""
                SELECT operation_id, scene_id, scene_order, attempt, status,
                       output_path, provider_kind, updated_at, {request_column}
                FROM video_factory_scene_operation
                WHERE render_batch_id = ?
                ORDER BY scene_order ASC, attempt DESC
                """,
                (batch_id,),
            ).fetchall()
            seen_scenes: set[int] = set()
            for r in scene_rows:
                order = r["scene_order"]
                if order not in seen_scenes:
                    seen_scenes.add(order)
                    try:
                        render_request = json.loads(r["request_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        render_request = {}
                    if not isinstance(render_request, dict):
                        render_request = {}
                    scene_operations.append({
                        "operation_id": r["operation_id"],
                        "scene_id": r["scene_id"],
                        "scene_order": order,
                        "attempt": r["attempt"],
                        "status": r["status"],
                        "output_path": r["output_path"],
                        "provider_kind": r["provider_kind"],
                        "updated_at": r["updated_at"],
                        "render_prompt": render_request.get("prompt"),
                        "reference_images": render_request.get("reference_images", []),
                        "output_url": f"/api/operator/workflow/{quote(workflow_id, safe='')}/media/scene/{quote(r['operation_id'], safe='')}",
                    })
    except sqlite3.OperationalError:
        pass
    
    # Return the validated model as a dictionary
    detail = {key: agg_dict.get(key) for key in (
        "workflow_id", "state", "owner_user_id", "created_at", "research_dossier",
        "creative_brief", "scene_plan", "storyboard", "render_result", "export_result",
        "qc_authorization", "pending_approval_id", "pending_approval_gate", "product_name",
        "failure_code", "failure_reason", "skip_reason", "operator_reason", "operator_stage",
        "skipped_at", "updated_at", "source_input_recovery_history", "locked_pack",
        "operator_action", "operator_qc_report_id",
    )} | {
        "version": row["version"],
        "source_context": agg_dict.get("source_context") or {},
        "research_snapshot": agg_dict.get("research_snapshot") or {},
        "qc_reports": qc_reports,
        "approval_history": approvals,
        "budget_ledger": budget_ledger,
        "scene_operations": scene_operations,
        "pipeline_steps": read_pipeline_steps(connection, workflow_id),
        "execution": execution_context(connection, workflow_id),
    }
    # Serve only exact recorded media, with workflow ownership and SHA checks.
    source = dict(detail["source_context"])
    source["images"] = [dict(image) if isinstance(image, dict) else image for image in source.get("images", [])]
    for image in source["images"]:
        if isinstance(image, dict) and image.get("asset_id") and (image.get("sha256") or image.get("digest")) and not image.get("asset_url"):
            image["asset_url"] = f"/api/operator/workflow/{quote(workflow_id, safe='')}/media/source/{quote(image['asset_id'], safe='')}"
    detail["source_context"] = source
    if agg_dict.get("storyboard"):
        storyboard = dict(agg_dict["storyboard"])
        if isinstance(storyboard.get("payload"), str):
            try:
                storyboard = json.loads(storyboard["payload"])
            except json.JSONDecodeError:
                storyboard = {}
        frames = [dict(f) if isinstance(f, dict) else f for f in storyboard.get("generated_keyframes", [])]
        for frame in frames:
            if isinstance(frame, dict) and frame.get("frame_id") and frame.get("image_path"):
                frame["preview_url"] = f"/api/operator/workflow/{quote(workflow_id, safe='')}/media/keyframe/{quote(frame['frame_id'], safe='')}"
        storyboard["generated_keyframes"] = frames
        detail["storyboard"] = storyboard
    if agg_dict.get("state") == "completed" and agg_dict.get("export_result"):
        detail["verified_video_url"] = f"/api/personal/product-video/videos/{quote(workflow_id, safe='')}/media"
    quarantine = quarantine_context(connection, workflow_id)
    if quarantine and quarantine.get("job_name") == "product_video.creative_brief":
        inspected = inspect_quarantined_creative_brief(connection, workflow_id=workflow_id)
        if inspected.get("recovery_available"):
            quarantine.update(inspected)
    elif not quarantine and agg_dict.get("state") == ProductVideoWorkflowState.CREATING_BRIEF.value:
        # Keep the existing read contract for a brief recovery that is already
        # leased/queued, without applying brief-only inspection to other jobs.
        inspected = inspect_quarantined_creative_brief(connection, workflow_id=workflow_id)
        if inspected.get("quarantined"):
            quarantine = inspected
    if quarantine:
        detail["quarantined_job"] = quarantine
    return detail



async def _event_stream(connection: sqlite3.Connection, owner_user_id: str | None = None) -> AsyncGenerator[str, None]:
    """SSE stream for live workflow state changes."""
    import asyncio
    
    last_cursor = 0
    
    while True:
        # Poll for new events
        query = "SELECT cursor, event_id FROM owner_event_cursor WHERE cursor > ?"
        params = [last_cursor]
        
        if owner_user_id:
            query += " AND owner_user_id = ?"
            params.append(owner_user_id)
        
        query += " ORDER BY cursor ASC LIMIT 10"
        
        rows = connection.execute(query, params).fetchall()
        
        for row in rows:
            last_cursor = row["cursor"]
            event_id = row["event_id"]
            
            # Fetch event payload
            event_row = connection.execute(
                "SELECT payload_json FROM turn_event WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            
            if event_row:
                yield f"data: {event_row['payload_json']}\n\n"
        
        await asyncio.sleep(1.0)


def create_kanban_router(connection: sqlite3.Connection | None = None) -> APIRouter:
    """Create Kanban & Operator API router."""
    router = APIRouter(prefix="/api/operator", tags=["operator"])
    
    def get_connection() -> sqlite3.Connection:
        if connection is not None:
            return connection
        try:
            from video_factory.constants import get_hermes_db_path
            from video_factory.storage_infra.sqlite import connect
            db_path = get_hermes_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return connect(db_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Database connection error: {exc}")

    
    @router.get("/kanban")
    async def get_kanban(
        owner_user_id: str | None = Query(None),
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """Get Kanban board with workflows grouped by state column."""
        try:
            columns = _get_kanban_cards(conn, owner_user_id)
            return {"columns": columns, "timestamp": datetime.now(timezone.utc).isoformat()}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/workflow/{workflow_id}")
    async def get_workflow_detail(
        workflow_id: str,
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """Get complete workflow state object."""
        try:
            return _get_workflow_detail(conn, workflow_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    media_connection = Depends(get_connection)

    @router.post("/workflows/delete")
    def delete_selected_workflows(body: DeleteWorkflowsRequest, request: Request, conn: sqlite3.Connection = media_connection) -> dict[str, Any]:
        from video_factory.application import candidate_api
        from video_factory.application.studio_deletion import delete_workflows
        from video_factory.integrations.server import _db_path, _workspace_path

        if request.headers.get('origin') != str(request.base_url).rstrip('/'):
            raise HTTPException(status_code=403, detail='Chỉ xóa từ Studio cùng origin')
        if body.confirmation != 'XÓA VĨNH VIỄN':
            raise HTTPException(status_code=400, detail='Cần xác nhận XÓA VĨNH VIỄN')
        if len({i.workflow_id for i in body.workflows}) != len(body.workflows):
            raise HTTPException(status_code=400, detail='Danh sách có workflow trùng')
        if not candidate_api._studio_batch_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail='Batch Studio đang chạy; chưa thể xóa')
        try:
            results = delete_workflows(conn, [i.model_dump() for i in body.workflows], workspace=_workspace_path(), integration_db=_db_path())
            return {'ok':True, 'results':results}
        finally:
            candidate_api._studio_batch_lock.release()

    @router.get("/workflow/{workflow_id}/actions")
    def get_studio_actions(workflow_id: str, conn: sqlite3.Connection = media_connection) -> dict[str, Any]:
        from video_factory.application import candidate_api
        from video_factory.application.studio_commands import workflow_actions

        try:
            result = workflow_actions(conn, workflow_id)
            if candidate_api._studio_batch_lock.locked():
                for option in result["options"]:
                    option.update(enabled=False, reason="Batch Studio đang chạy; chờ batch hiện tại kết thúc trước khi chạy thêm.")
            return result
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.post("/workflow/{workflow_id}/actions")
    def run_studio_action(
        workflow_id: str, command: StudioCommandRequest, request: Request,
        background_tasks: BackgroundTasks, conn: sqlite3.Connection = media_connection,
    ) -> Any:
        from fastapi.responses import JSONResponse

        from video_factory.application import candidate_api
        from video_factory.application.studio_commands import (
            command_receipt,
            execute_command,
            prepare_runner,
        )

        if request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Chỉ chạy command từ Studio cùng origin")
        body = command.model_dump(mode="json")
        body["note"] = body["note"].strip()
        if not body["note"]:
            raise HTTPException(status_code=422, detail="Cần ghi chú lý do chạy lại")
        locked = False
        scheduled = False
        try:
            receipt = command_receipt(conn, workflow_id, body)
            if receipt:
                return receipt
            if not candidate_api._studio_batch_lock.acquire(blocking=False):
                raise HTTPException(status_code=409, detail="Batch Studio đang chạy; không khởi động worker song song.")
            locked = True
            runner = prepare_runner(conn, workflow_id)
            result = execute_command(conn, workflow_id, body)
            background_tasks.add_task(candidate_api._run_studio_batch_background, **runner)
            scheduled = True
            return JSONResponse(status_code=202, content={**result, "execution": "studio_selected_batch"})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            if locked and not scheduled:
                candidate_api._studio_batch_lock.release()

    @router.get("/workflow/{workflow_id}/actions/{request_id}")
    def get_command_receipt(workflow_id: str, request_id: UUID, conn: sqlite3.Connection = media_connection) -> dict[str, Any]:
        from video_factory.application.studio_commands import command_status

        try:
            return command_status(conn, workflow_id, str(request_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.get("/workflow/{workflow_id}/media/{kind}/{asset_id}")
    def get_workflow_media(
        workflow_id: str, kind: str, asset_id: str,
        conn: sqlite3.Connection = media_connection,
    ) -> FileResponse:
        from video_factory.application.studio_media import bound_media_path

        try:
            path = bound_media_path(conn, workflow_id, kind, asset_id)
            return FileResponse(path, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=404, detail="Không đọc được media trên máy Studio") from exc

    @router.post("/workflow/{workflow_id}/retry-quarantined")
    async def retry_quarantined_workflow(
        workflow_id: str,
        background_tasks: BackgroundTasks,
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """Resume the validated CREATIVE_BRIEF reference-path failure in place."""
        import os
        from pathlib import Path
        from video_factory.application import candidate_api
        from video_factory.automation.importer import get_default_exchange_inbox

        inbox = Path(os.environ.get("HERMES_EXCHANGE_INBOX") or get_default_exchange_inbox()).resolve()
        try:
            inspection = inspect_quarantined_creative_brief(conn, workflow_id=workflow_id, inbox_root=inbox)
            if inspection.get("status") == "already_queued":
                return {"ok": True, "status": "already_queued", "reason": inspection["reason"]}
            if not inspection.get("recovery_available"):
                raise HTTPException(status_code=409, detail=inspection.get("reason") or "Job cannot be resumed safely.")
            if not str(inspection.get("qc_level") or "middle") == "middle":
                raise HTTPException(status_code=409, detail="Chỉ có thể tiếp tục workflow PR-1 ở mức QC middle.")

            profile = os.environ.get("VF_CREATIVE_RUN_CONFIG")
            if not profile:
                profile = str(Path(__file__).resolve().parents[3] / "config" / "creative_run_profile.json")
            profile_path = Path(profile).resolve()
            if not profile_path.is_file():
                raise HTTPException(status_code=409, detail=f"Creative profile not found: {profile_path}")
            main_db = next(
                (Path(row["file"]).resolve() for row in conn.execute("PRAGMA database_list")
                 if row["name"] == "main" and row["file"]), None,
            )
            if main_db is None:
                raise HTTPException(status_code=409, detail="Studio batch requires a persistent SQLite database")
            if not candidate_api._studio_batch_lock.acquire(blocking=False):
                raise HTTPException(status_code=409, detail="Một batch Studio đang chạy; không tạo worker thứ hai.")
            release_lock = True
            try:
                recovered = resume_quarantined_creative_brief(conn, workflow_id=workflow_id, inbox_root=inbox)
                if recovered["status"] == "already_queued":
                    return {"ok": True, "status": "already_queued"}
                background_tasks.add_task(
                    candidate_api._run_studio_batch_background,
                    db_path=main_db, inbox_root=inbox, profile_path=profile_path,
                    product_keys=[recovered["product_key"]], target_date=recovered["target_date"],
                    owner_user_id="local-owner", limit=None, qc_level="middle", disable_keyframe_qc=False,
                )
                release_lock = False
                return {"ok": True, **recovered}
            finally:
                if release_lock:
                    candidate_api._studio_batch_lock.release()
        except HTTPException:
            raise
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    
    @router.post("/approve")
    async def approve_workflow(
        request: ApprovalRequest,
        background_tasks: BackgroundTasks,
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """Approve, reject, or request rework on a workflow gate."""
        if request.action != "approve":
            raise HTTPException(status_code=400, detail="Only 'approve' action is currently supported")
        
        try:
            payload = {}
            if request.qc_authorization:
                payload["qc_authorization"] = request.qc_authorization
            if request.qc_max_cost_usd is not None:
                payload["qc_max_cost_usd"] = request.qc_max_cost_usd
            if request.render_provider_kind:
                payload["render_provider_kind"] = request.render_provider_kind
            
            result = approve_and_advance_workflow(
                connection=conn,
                target_id=request.workflow_id,
                owner_user_id="local-owner",  # TODO: Extract from auth context
                payload=payload if payload else None,
            )
            
            try:
                from video_factory.application.studio_commands import prepare_runner
                from video_factory.application import candidate_api
                runner = prepare_runner(conn, request.workflow_id)
                if candidate_api._studio_batch_lock.acquire(blocking=False):
                    background_tasks.add_task(candidate_api._run_studio_batch_background, **runner)
            except Exception:
                pass
            
            return result
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/stream")
    async def stream_events(
        owner_user_id: str | None = Query(None),
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> StreamingResponse:
        """SSE stream for real-time workflow state changes."""
        return StreamingResponse(
            _event_stream(conn, owner_user_id),
            media_type="text/event-stream",
        )
    
    @router.post("/rework")
    async def rework_workflow(
        request: ApprovalRequest,
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """Request rework on a workflow gate."""
        if request.action != "rework":
            raise HTTPException(status_code=400, detail="Only 'rework' action is supported here")
        if not request.feedback:
            raise HTTPException(status_code=400, detail="Feedback is required for rework")
        
        # TODO: Implement actual rework logic in operator_handler
        return {"status": "rework_requested", "workflow_id": request.workflow_id}

    @router.post("/workflow/{workflow_id}/scene/re-render")
    async def re_render_scene(
        workflow_id: str,
        request: SceneReRenderRequest,
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """Granular scene re-render endpoint. Rerenders a single scene and resumes batch."""
        if workflow_id != request.workflow_id:
            raise HTTPException(status_code=400, detail="Mismatched workflow_id in path and body")
        
        try:
            scene_store = SceneRenderOperationStore(conn)
            result = resume_video_scene_after_qc_failure(
                connection=conn,
                scene_store=scene_store,
                workflow_id=workflow_id,
                owner_user_id="local-owner",
                scene_order=request.scene_order,
                provider_kind=request.provider_kind,
            )
            return {"ok": True, "result": result}
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/product/{product_key}/workflows")
    async def get_product_workflows(
        product_key: str,
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """List all video workflows associated with a product (up to 10 videos)."""
        try:
            rows = conn.execute(
                """
                SELECT workflow_id, aggregate_json, version
                FROM workflow
                WHERE json_extract(aggregate_json, '$.source_context.product_key') = ?
                   OR workflow_id LIKE ?
                ORDER BY json_extract(aggregate_json, '$.created_at') ASC
                LIMIT 10
                """,
                (product_key, f"%{product_key}%"),
            ).fetchall()

            workflows = []
            for r in rows:
                agg = json.loads(r["aggregate_json"])
                workflows.append({
                    "workflow_id": r["workflow_id"],
                    "state": agg.get("state"),
                    "version": r["version"],
                    "created_at": agg.get("created_at"),
                    "video_url": (agg.get("latest_artifact") or {}).get("video_url") or (agg.get("export_result") or {}).get("video_path"),
                    "thumbnail_url": (agg.get("latest_artifact") or {}).get("thumbnail_url"),
                })
            return {"ok": True, "product_key": product_key, "workflows": workflows}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return router
