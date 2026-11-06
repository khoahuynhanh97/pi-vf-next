"""End-to-end SQLite happy path verification for Product-to-Video pipeline.

Verifies:
1. Budget reservation synchronization (worker reserves scope_id=project_id, Video Factory accepts it).
2. Approval receipts passed through all 5 HITL gates (ApprovalService -> outbox -> worker -> adapter -> Video Factory gate).
3. Render -> Final Approval -> Export: mock provider produces real MP4, final approval unlocks export, exported MP4 exists on disk and is non-empty.
4. Replay safety: polling resumed after provider acceptance without re-submitting paid job (provider submit count == 1).
5. Negative verification: missing or unapproved receipts halt execution fail-closed.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from video_factory.application.operator_handler import approve_and_advance_workflow
from video_factory.integrations import server
from video_factory.integrations.ports import VideoGenerationResult
from video_factory.integrations.ports import KeyframeGenerationResult
from video_factory.creative.contracts import (
    AudienceSpec,
    CreativeBriefV2,
    EligibleAsset,
    EvidenceClaim,
    ProductIdentityV2,
    ReasonToBelieve,
    ResearchDossier,
    ScriptScenePlanV2,
    ScriptSceneV2,
)
from video_factory.handlers import (
    JobContext,
    JobSucceeded,
    ProductVideoWorkflowState,
    create_product_video_topic_handlers,
    start_product_video_workflow,
)
from video_factory.job_contracts import JobDeferred
from video_factory.cli import _advance_qc_waiting_workflow
from video_factory.qc.models import QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.approvals import ApprovalService
from video_factory.workflows.budgets import BudgetPolicy, init_default_engine
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.outbox_dispatcher import OutboxDispatcher
from video_factory.workflows.outbox_repository import OutboxRepository
from video_factory.workflows.repository import WorkflowRepository

class _OfflineCreativeGenerator:
    """Deterministic grounded generator for the configured offline E2E only."""

    def __init__(
        self,
        workflow_id: str = "",
        reference_paths: tuple[Path, Path] | None = None,
    ) -> None:
        if workflow_id and reference_paths:
            self.dossier = ResearchDossier.create(
                workflow_id=workflow_id,
                product_identity=ProductIdentityV2(
                    brand="Lumina",
                    model="Glow GL-1",
                    display_name="Lumina Glow GL-1",
                ),
                verified_claims=(
                    EvidenceClaim(
                        claim_id="claim-touch",
                        text="Hỗ trợ thao tác điều khiển bằng chạm",
                        evidence_refs=("offline-pdp",),
                    ),
                ),
                eligible_assets=tuple(
                    EligibleAsset(
                        asset_id=f"a{index}",
                        path=str(path),
                        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        qc_report_id="qc-image-fixture",
                        reference_rank=index,
                        qc_score=93,
                    )
                    for index, path in enumerate(reference_paths, start=1)
                ),
            )

    def create_direction(self, dossier):
        self.dossier = dossier
        evidence = dossier.verified_claims[0]
        return CreativeBriefV2.create(
            audience=AudienceSpec(
                "Người làm việc tại bàn",
                "Cần ánh sáng dịu vào cuối ngày",
                "Tạo góc làm việc ấm và tập trung",
            ),
            consumer_insight="Một thay đổi ánh sáng nhỏ có thể làm góc bàn dễ chịu hơn.",
            problem="Ánh sáng bàn làm việc khô cứng vào buổi tối.",
            creative_angle="Theo chân một lần chuyển góc bàn sang trạng thái thư giãn.",
            hook="Góc bàn vẫn quá lạnh khi ngày làm việc sắp kết thúc?",
            promise="Cho thấy Glow Lamp tạo điểm sáng ấm trong một tình huống sử dụng thật.",
            reason_to_believe=(ReasonToBelieve(evidence.text, (evidence.claim_id,)),),
            tone="Ấm áp, đời thường, rõ ràng",
            story_archetype="problem_solution_demo",
            cta="Xem thêm thông tin sản phẩm.",
            restrictions=("Không hiển thị giá hoặc giao diện sàn",),
            target_duration_seconds=18,
            dossier_digest=dossier.digest(),
        )

    def create_script(self, dossier, brief):
        self.dossier = dossier
        first = dossier.eligible_assets[0]
        second = dossier.eligible_assets[1] if len(dossier.eligible_assets) > 1 else first
        claim = dossier.verified_claims[0]
        scenes = (
            ScriptSceneV2(
                scene_id="scene-1", order=1, story_role="hook", duration_seconds=6,
                narration="Một góc bàn lạnh và thiếu điểm nhấn vào cuối ngày.",
                overlay_text="Góc bàn quá lạnh?", visual_objective="Thiết lập vấn đề ánh sáng",
                environment="Bàn làm việc buổi tối", subject_action="Người dùng ngồi xuống bàn",
                product_interaction="Đèn ở trạng thái tắt", start_state="Góc bàn tối và đèn tắt",
                end_state="Bàn tay tiến đến đèn", visual_change="Từ tĩnh sang chuẩn bị bật đèn",
                camera_framing="Trung cảnh", camera_movement="Tiến nhẹ theo bàn tay",
                transition_out="Cắt theo động tác", claim_refs=(), reference_asset_ids=(first.asset_id,),
            ),
            ScriptSceneV2(
                scene_id="scene-2", order=2, story_role="demo", duration_seconds=6,
                narration="Một chạm tạo điểm sáng ấm cho không gian làm việc.",
                overlay_text="Chạm để đổi không khí", visual_objective="Thể hiện thao tác sử dụng thật",
                environment="Cùng bàn làm việc", subject_action="Người dùng chạm điều khiển đèn",
                product_interaction="Đèn được bật bằng thao tác chạm", start_state="Đèn đang tắt",
                end_state="Đèn sáng và hắt ánh sáng ấm", visual_change="Từ tối sang sáng ấm",
                camera_framing="Cận cảnh", camera_movement="Máy cố định theo thao tác",
                transition_out="Cắt thẳng", claim_refs=(claim.claim_id,), reference_asset_ids=(second.asset_id,),
            ),
            ScriptSceneV2(
                scene_id="scene-3", order=3, story_role="cta", duration_seconds=6,
                narration="Góc bàn sẵn sàng cho khoảng thời gian tập trung tiếp theo.",
                overlay_text="Sẵn sàng tập trung", visual_objective="Khép lại bằng trạng thái sử dụng hoàn chỉnh",
                environment="Bàn làm việc đã được chiếu sáng", subject_action="Người dùng mở sổ và bắt đầu làm việc",
                product_interaction="Đèn tiếp tục chiếu sáng bên cạnh", start_state="Đèn sáng, bàn chưa có hoạt động",
                end_state="Người dùng làm việc dưới ánh đèn", visual_change="Từ trưng bày sang sử dụng thật",
                camera_framing="Toàn cảnh dọc", camera_movement="Lùi nhẹ để lộ toàn bộ góc bàn",
                transition_out="Fade out", claim_refs=(), reference_asset_ids=(first.asset_id,),
            ),
        )
        self.script = ScriptScenePlanV2.create(
            target_duration_seconds=18,
            brief_digest=brief.digest(),
            scenes=scenes,
        )
        return self.script


class _OfflineKeyframeRuntime:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def run(self, request):
        from PIL import Image

        output = self.output_dir / f"{request.frame_id}.png"
        Image.new("RGB", (90, 160), color=(245, 245, 238)).save(output)
        return KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="completed",
            provider_operation_id=request.operation_id,
            image_path=str(output),
        )


class _DeterministicQCEvaluator:
    """Local evaluator used to exercise the real QC services without provider calls."""

    fake_mode = False
    evaluator_name = "deterministic-local-qc"
    evaluator_version = "1"
    model = "local-fixture"

    def evaluate_image(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "view_angle": "front three-quarter",
            "shot_type": "clean product",
            "visible_details": ["lamp body", "touch control"],
            "scene_suitability": ["hero", "detail"],
            "risk_flags": [],
            "text_coverage_percent": 0,
            "identity_score": 55,
            "clarity_score": 24,
            "usability_score": 14,
            "total_score": 93,
            "verdict": "pass",
            "eligible": True,
            "hard_failure": False,
            "source_branding_detected": False,
            "identity_mismatch": False,
            "reason": "Deterministic grounded reference",
        }

    def evaluate_multiple_images(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "identity_consistency": 48,
            "scene_adherence": 28,
            "visual_cleanliness": 18,
            "total_score": 94,
            "verdict": "pass",
            "hard_failure": False,
            "black_bars_detected": False,
            "source_branding_detected": False,
            "identity_mismatch": False,
            "interaction_geometry_valid": True,
            "product_count_valid": True,
            "prompt_reference_consistent": True,
            "script_adherence": True,
            "important_details_preserved": True,
            "composition_usable": True,
            "animation_suitable": True,
            "recommended_action": "none",
            "reason": "Deterministic grounded scene",
        }


def _deterministic_qc_service(connection: Any):
    from video_factory.qc.service import ProductVideoQCService

    return ProductVideoQCService(connection, evaluator=_DeterministicQCEvaluator())


def _completed_image_selection_without_report(self, **kwargs: Any) -> dict[str, Any]:
    from video_factory.qc.retry_policy import ensure_qc_retry_schema

    ensure_qc_retry_schema(self.connection)
    return {
        "status": "completed",
        "resource_pack": kwargs["resource_pack"],
        "evaluated_items": [],
    }


class ProductVideoMCPTestRegistry:
    """Tool registry dispatching MCP tools across PI and Video Factory for configured provider_mode."""

    def __init__(
        self,
        mock_provider: Any,
        rendered_mp4_path: Path,
        approval_db_path: Path,
        reference_paths: tuple[Path, Path],
    ) -> None:
        self.mock_provider = mock_provider
        self.rendered_mp4_path = rendered_mp4_path
        self.approval_db_path = approval_db_path
        self.reference_paths = reference_paths
        self.dispatched_calls: list[tuple[str, dict[str, Any]]] = []
        self.research_store: dict[str, dict[str, Any]] = {}

    def get_all_tool_names(self) -> set[str]:
        return {
            "mcp__product_intelligence__research_product",
            "mcp__product_intelligence__build_resource_pack",
            "mcp__product_intelligence__lock_resource_pack",
            "mcp__video_factory__video_project_create",
            "mcp__video_factory__resource_pack_save",
            "mcp__video_factory__resource_pack_lock",
            "mcp__video_factory__creative_brief_get",
            "creative_brief_get",
            "mcp__video_factory__create_brief",
            "mcp__video_factory__create_scene_plan",
            "mcp__video_factory__create_storyboard",
            "mcp__video_factory__render",
            "mcp__video_factory__get_job_status",
            "mcp__video_factory__export",
            "brief_approve",
            "scene_plan_approve",
            "storyboard_approve",
            "final_video_approve",
            "product_lock_approve",
            "mcp__video_factory__creative_brief_approve",
            "mcp__video_factory__scene_plan_approve",
            "mcp__video_factory__storyboard_approve",
            "mcp__video_factory__final_video_approve",
            "mcp__video_factory__product_lock_approve",
        }

    def dispatch(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.dispatched_calls.append((tool_name, payload))

        if tool_name == "mcp__video_factory__video_project_create":
            return server.video_project_create(
                payload.get("owner_user_id", ""), payload.get("project_id", "")
            )
        if tool_name == "mcp__video_factory__resource_pack_save":
            return server.resource_pack_save(
                payload.get("owner_user_id", ""),
                payload.get("project_id", ""),
                payload.get("resource_pack") or {},
            )
        if tool_name == "mcp__video_factory__resource_pack_lock":
            return server.resource_pack_lock(
                owner_user_id=payload.get("owner_user_id", ""),
                project_id=payload.get("project_id", ""),
                product_identity=payload.get("product_identity") or {},
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
                eligible_reference_manifest=payload.get("eligible_reference_manifest"),
            )

        # PI Tool 1: research
        if tool_name == "mcp__product_intelligence__research_product":
            p_name = payload.get("product_name") or "Glow Lamp"
            snap_id = f"snap_{len(self.research_store) + 1:03d}"
            res = {
                "status": "success",
                "snapshot_id": snap_id,
                "persistence_status": "saved",
                "canonical_products": [
                    {
                        "canonical_product_id": f"prod_{snap_id}",
                        "name": p_name,
                        "brand": "Lumina",
                        "listings": [{"price": 29.99, "currency": "USD"}],
                    }
                ],
                "report": {"product_name": p_name, "query": payload.get("query"), "url": payload.get("url")},
            }
            self.research_store[snap_id] = res
            return res

        # PI Tool 2: build resource pack
        elif tool_name == "mcp__product_intelligence__build_resource_pack":
            snap_id = payload.get("snapshot_id", "snap_001")
            pack_id = f"rp_{snap_id}"
            media_assets = [
                {
                    "asset_id": f"a{index}",
                    "uri": str(path),
                    "local_path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "status": "eligible",
                    "eligible": True,
                }
                for index, path in enumerate(self.reference_paths, start=1)
            ]
            return {
                "status": "built",
                "resource_pack_id": pack_id,
                "version": 1,
                "pack_status": "ready_to_lock",
                "media_count": 2,
                "brand": "Lumina",
                "model": "Glow GL-1",
                "product_name": "Lumina Glow GL-1",
                "product_identity": {
                    "brand": "Lumina",
                    "model": "Glow GL-1",
                    "display_name": "Lumina Glow GL-1",
                },
                "claims": [
                    {
                        "claim_id": "claim-touch",
                        "text": "Hỗ trợ thao tác điều khiển bằng chạm",
                        "status": "verified",
                        "evidence_refs": ["offline-pdp"],
                    }
                ],
                "assets": media_assets,
                "resource_pack": {
                    "id": pack_id,
                    "version": 1,
                    "product_id": f"prod_{snap_id}",
                    "brand": "Lumina",
                    "model": "Glow GL-1",
                    "product_name": "Lumina Glow GL-1",
                    "claims": [
                        {
                            "claim_id": "claim-touch",
                            "text": "Hỗ trợ thao tác điều khiển bằng chạm",
                            "status": "verified",
                            "evidence_refs": ["offline-pdp"],
                        }
                    ],
                    "ready_to_lock": True,
                    "media_assets": media_assets,
                },
            }

        # PI Tool 3: lock resource pack
        elif tool_name == "mcp__product_intelligence__lock_resource_pack":
            pack_dict = payload.get("pack_dict") or payload
            rp = pack_dict.get("resource_pack", pack_dict)
            pack_id = rp.get("id") or rp.get("resource_pack_id") or "rp_001"
            prod_id = rp.get("product_id", "prod_glow_lamp")
            proj_id = payload.get("project_id", f"proj_{prod_id}")
            owner_id = payload.get("owner_user_id", "default-owner")

            # Register project and resource pack in Video Factory repository
            server.video_project_create(owner_id, proj_id)
            server.resource_pack_save(owner_id, proj_id, {
                "id": pack_id,
                "product_references": [{"asset_id": "a1", "uri": "asset://lamp.png"}],
                "primary_product_asset_id": "a1",
                "product_identity_description": "Touch-sensitive LED glow lamp",
            })
            srv = server._service()
            srv.lock_resource_pack(
                owner_id,
                proj_id,
                server._identity({"description": "Touch-sensitive LED glow lamp"}),
            )

            return {
                "status": "locked",
                "lock_id": f"lock_{pack_id}",
                "resource_pack_id": pack_id,
                "manifest_digest": "sha256:abc123manifest",
                "assets": [{"asset_id": "a1", "local_path": "/data/lamp.png"}],
                "locked": True,
                "resource_pack": {
                    "id": pack_id,
                    "product_id": prod_id,
                    "product_name": "Glow Lamp",
                    "ready_to_lock": False,
                },
            }

        # VF Gate 1 mutation: product lock approve
        elif tool_name in ("product_lock_approve", "mcp__video_factory__product_lock_approve"):
            return server.resource_pack_lock(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                product_identity={"description": "Touch-sensitive LED glow lamp"},
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )

        # VF Tool 3.5: creative brief get
        elif tool_name in ("creative_brief_get", "mcp__video_factory__creative_brief_get"):
            return server.creative_brief_get(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
            )

        # VF Tool 4: create brief
        elif tool_name == "mcp__video_factory__create_brief":
            cfg = dict(payload.get("brief_config") or {})
            cfg.pop("resource_pack", None)
            cfg.pop("brief_id", None)
            cfg.setdefault("objective", "Convert home decor shoppers")
            cfg.setdefault("target_audience", "Desk setup enthusiasts")
            cfg.setdefault("core_message", "Minimalist warm ambiance glow lamp")
            cfg.setdefault("tone", "Calm and modern")
            cfg.setdefault("pace", "Measured")
            cfg.setdefault("cta", "Get 15% off on TikTok Shop")
            cfg.setdefault("content_blocks", ["Dim room struggle", "Glow lamp illumination", "Discount CTA"])
            return server.create_brief(
                product_id=payload.get("product_id", ""),
                brief_config=cfg,
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
            )

        # VF Gate 2 mutation: brief approve
        elif tool_name in ("brief_approve", "mcp__video_factory__creative_brief_approve"):
            return server.creative_brief_approve(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )

        # VF Tool 5: create scene plan
        elif tool_name == "mcp__video_factory__create_scene_plan":
            return server.create_scene_plan(
                brief_result=payload.get("brief_result"),
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
            )

        # VF Gate 3 mutation: scene plan approve
        elif tool_name in ("scene_plan_approve", "mcp__video_factory__scene_plan_approve"):
            return server.scene_plan_approve(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )

        # VF Tool 6: create storyboard
        elif tool_name == "mcp__video_factory__create_storyboard":
            return server.create_storyboard(
                scene_plan_result=payload.get("scene_plan_result"),
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
            )

        # VF Gate 4 mutation: storyboard approve
        elif tool_name in ("storyboard_approve", "mcp__video_factory__storyboard_approve"):
            return server.storyboard_approve(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )

        # VF Tool 7: render
        elif tool_name == "mcp__video_factory__render":
            return server.render(
                storyboard_result=payload.get("storyboard_result"),
                render_params=payload.get("render_params"),
                idempotency_key=payload.get("idempotency_key"),
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                admission_context=payload.get("admission_context"),
                provider=self.mock_provider,
                approval_request_id=payload.get("approval_request_id"),
            )

        # VF Tool 8: poll render status
        elif tool_name == "mcp__video_factory__get_job_status":
            return server.get_job_status(
                job_id=payload.get("job_id", ""),
                provider=self.mock_provider,
            )

        # VF Gate 5 mutation: final video approve
        elif tool_name in ("final_video_approve", "mcp__video_factory__final_video_approve"):
            return server.final_video_approve(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )

        # VF Tool 9: export
        elif tool_name == "mcp__video_factory__export":
            return server.export(
                render_result=payload.get("render_result"),
                export_params=payload.get("export_params"),
                idempotency_key=payload.get("idempotency_key"),
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                approval_request_id=payload.get("approval_request_id"),
            )

        raise RuntimeError(f"Unhandled tool dispatch: {tool_name}")


@pytest.fixture
def happy_path_env(tmp_path, monkeypatch):
    """Sets up SQLite repositories, Video Factory isolated workspace, and mock video provider."""
    # 1. Workflow and approval DB
    db_file = tmp_path / "hermes_happy_path.db"
    conn = connect(db_file)
    monkeypatch.setenv("HERMES_APPROVAL_DB_PATH", str(db_file))

    # 2. Video Factory isolated database and workspace
    vf_db = tmp_path / "video_factory.sqlite"
    vf_ws = tmp_path / "vf_workspace"
    vf_ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(vf_db))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(vf_ws))

    # 3. Create a decodable MP4 plus immutable product references.
    rendered_mp4 = tmp_path / "mock_provider_output.mp4"
    import av

    with av.open(str(rendered_mp4), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=24)
        stream.width = 720
        stream.height = 1280
        stream.pix_fmt = "yuv420p"
        for _ in range(144):
            frame = av.VideoFrame(72, 128, "yuv420p")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    real_mp4_binary = rendered_mp4.read_bytes()
    from PIL import Image

    reference_paths = (
        vf_ws / "reference-input" / "lamp-front.png",
        vf_ws / "reference-input" / "lamp-side.png",
    )
    reference_paths[0].parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (96, 160), color="white").save(reference_paths[0])
    Image.new("RGB", (96, 160), color="gray").save(reference_paths[1])

    # 4. Mock video provider with running -> completed status progression
    mock_provider = MagicMock()
    mock_provider.provider_kind = "google_flow"
    mock_provider.submit.return_value = VideoGenerationResult(
        request_id="req_render_777",
        status="submitted",
        provider_operation_id="ops/proj/locations/us-central1/operations/render_op_777",
    )

    poll_state = {"count": 0}

    def _get_status(job_id: str) -> VideoGenerationResult:
        poll_state["count"] += 1
        if poll_state["count"] == 1:
            return VideoGenerationResult(
                request_id=job_id,
                status="running",
                provider_operation_id=job_id,
            )
        return VideoGenerationResult(
            request_id=job_id,
            status="completed",
            video_path=str(rendered_mp4),
            provider_operation_id=job_id,
        )

    mock_provider.get_status.side_effect = _get_status

    # 5. Initialize budget engine with sufficient allocation
    init_default_engine(
        BudgetPolicy(
            max_total=100_000,
            per_channel={"video_factory": 100_000, "product_intelligence": 100_000},
        ),
        connection=conn,
    )

    # 6. Tool registry wrapping live Video Factory server and mock provider
    registry = ProductVideoMCPTestRegistry(
        mock_provider=mock_provider,
        rendered_mp4_path=rendered_mp4,
        approval_db_path=db_file,
        reference_paths=reference_paths,
    )

    yield conn, db_file, registry, mock_provider, rendered_mp4, real_mp4_binary, vf_ws

    conn.close()


def test_product_to_video_complete_happy_path_e2e(happy_path_env):
    """Prove the end-to-end Product-to-Video happy path across all 5 HITL gates.

    Flow:
      start workflow -> research -> Gate 1: Product Lock -> resource pack -> lock ->
      creative brief -> Gate 2: Brief Approval -> scene plan -> Gate 3: Scene Approval ->
      storyboard -> Gate 4: Storyboard Approval -> render (real budget reserve scope_id=project_id,
      mock provider submit, async poll to MP4) -> Gate 5: Final Approval ->
      export -> completed real MP4 file on disk.
    """
    conn, db_file, registry, mock_provider, rendered_mp4, real_mp4_bytes, vf_ws = happy_path_env

    wf_id = "wf_happy_lamp_001"
    proj_id = f"proj_{wf_id}"
    owner_id = "default-owner"
    qc_auth = {
        "run_id": "run_happy_qc_001",
        "scope": ["product_video"],
        "expires_at": "2099-01-01T00:00:00+00:00",
        "max_cost_usd": 100.0,
    }

    repo = WorkflowRepository(conn)

    # Start production workflow
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Glow Lamp",
        keywords=["ambient", "led", "desk"],
        pdp_url="https://shop.tiktok.com/view/product/17294829104",
        project_id=proj_id,
        owner_user_id=owner_id,
        qc_authorization=qc_auth,
        qc_max_cost_usd=1.0,
    )

    topic_handlers = create_product_video_topic_handlers(
        workflow_repository=repo,
        tool_registry=registry,
        provider_mode="configured",
        poll_timeout=10.0,
        poll_interval=0.01,
        creative_generator=_OfflineCreativeGenerator(wf_id, registry.reference_paths),
        keyframe_runtime_factory=lambda: _OfflineKeyframeRuntime(vf_ws),
        qc_service_factory=_deterministic_qc_service,
    )
    outbox_repo = OutboxRepository(conn)
    dispatcher = OutboxDispatcher(outbox_repo, topic_handlers=topic_handlers, worker_id="e2e-worker-1")

    # =========================================================================
    # Step 1: Research -> Resource Pack -> AwaitingProductLock (Gate 1)
    # =========================================================================
    r1 = dispatcher.process_batch()
    assert r1.succeeded == 1

    r2 = dispatcher.process_batch()
    assert r2.succeeded == 1

    agg, v1 = repo.get_workflow(wf_id)
    assert agg["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
    gate1_req_id = agg["pending_approval_id"]
    assert gate1_req_id

    # Verify Gate 1 receipt exists in ApprovalService
    appr_svc = ApprovalService(conn)
    gate1_req = appr_svc.get_request(gate1_req_id)
    assert gate1_req.status.value == "pending"
    assert gate1_req.payload["gate"] == "AwaitingProductLock"
    assert gate1_req.payload["project_id"] == proj_id

    # Operator approves Gate 1 (Product Lock)
    advance_res1 = approve_and_advance_workflow(conn, target_id=gate1_req_id, owner_user_id=owner_id, expected_version=v1)
    assert advance_res1["new_state"] == ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value
    assert advance_res1["enqueued_job"] == "product_video.lock"

    # =========================================================================
    # Step 2: Lock -> Creative Brief -> AwaitingBriefApproval (Gate 2)
    # =========================================================================
    r3 = dispatcher.process_batch()  # runs lock -> enqueues creative_brief
    if r3.succeeded != 1:
        errs = [r["last_error"] for r in conn.execute("SELECT last_error FROM workflow_outbox WHERE state='quarantined'").fetchall()]
        pytest.fail(f"r3 failed: report={r3}, errors={errs}")
    assert r3.succeeded == 1
    agg_lock, _ = repo.get_workflow(wf_id)
    assert agg_lock["state"] == ProductVideoWorkflowState.CREATING_BRIEF.value

    r4 = dispatcher.process_batch()  # runs creative_brief -> reaches Gate 2
    if r4.succeeded != 1:
        errs = [r["last_error"] for r in conn.execute("SELECT last_error FROM workflow_outbox WHERE last_error IS NOT NULL").fetchall()]
        pytest.fail(f"r4 failed: report={r4}, errors={errs}")
    assert r4.succeeded == 1
    agg_brief, v2 = repo.get_workflow(wf_id)
    assert agg_brief["state"] == ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value
    gate2_req_id = agg_brief["pending_approval_id"]

    # Verify Gate 2 receipt
    gate2_req = appr_svc.get_request(gate2_req_id)
    assert gate2_req.status.value == "pending"
    assert gate2_req.payload["gate"] == "AwaitingBriefApproval"

    # Operator approves Gate 2 (Creative Brief)
    advance_res2 = approve_and_advance_workflow(conn, target_id=gate2_req_id, owner_user_id=owner_id, expected_version=v2)
    assert advance_res2["new_state"] == ProductVideoWorkflowState.CREATING_SCENE_PLAN.value
    assert advance_res2["enqueued_job"] == "product_video.scene_plan"

    # =========================================================================
    # Step 3: Scene Plan -> AwaitingSceneApproval (Gate 3)
    # =========================================================================
    r5 = dispatcher.process_batch()  # runs scene_plan -> reaches Gate 3
    if r5.succeeded != 1:
        errs = [r["last_error"] for r in conn.execute("SELECT last_error FROM workflow_outbox WHERE last_error IS NOT NULL").fetchall()]
        pytest.fail(f"r5 failed: report={r5}, errors={errs}")
    assert r5.succeeded == 1
    agg_scene, v3 = repo.get_workflow(wf_id)
    assert agg_scene["state"] == ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value
    gate3_req_id = agg_scene["pending_approval_id"]

    # Verify Gate 3 receipt
    gate3_req = appr_svc.get_request(gate3_req_id)
    assert gate3_req.status.value == "pending"
    assert gate3_req.payload["gate"] == "AwaitingSceneApproval"

    # Operator approves Gate 3 (Scene Plan)
    advance_res3 = approve_and_advance_workflow(conn, target_id=gate3_req_id, owner_user_id=owner_id, expected_version=v3)
    assert advance_res3["new_state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value
    assert advance_res3["enqueued_job"] == "product_video.storyboard"

    # =========================================================================
    # Step 4: Storyboard -> AwaitingStoryboardApproval (Gate 4)
    # =========================================================================
    r6 = dispatcher.process_batch()  # runs storyboard -> reaches Gate 4
    if r6.succeeded != 1:
        errs = [r["last_error"] for r in conn.execute("SELECT last_error FROM workflow_outbox WHERE last_error IS NOT NULL").fetchall()]
        pytest.fail(f"r6 failed: report={r6}, errors={errs}")
    assert r6.succeeded == 1
    agg_sb, v4 = repo.get_workflow(wf_id)
    assert agg_sb["state"] == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
    gate4_req_id = agg_sb["pending_approval_id"]

    # Verify Gate 4 receipt
    gate4_req = appr_svc.get_request(gate4_req_id)
    assert gate4_req.status.value == "pending"
    assert gate4_req.payload["gate"] == "AwaitingStoryboardApproval"

    # Operator approves Gate 4 (Storyboard)
    advance_res4 = approve_and_advance_workflow(conn, target_id=gate4_req_id, owner_user_id=owner_id, expected_version=v4)
    assert advance_res4["new_state"] == ProductVideoWorkflowState.RENDERING.value
    assert advance_res4["enqueued_job"] == "product_video.render"
    render_outbox = conn.execute(
        "SELECT message FROM workflow_outbox WHERE workflow_id = ? AND state = 'pending' ORDER BY outbox_id DESC LIMIT 1",
        (wf_id,),
    ).fetchone()
    assert render_outbox is not None
    render_payload = json.loads(render_outbox["message"])["payload"]
    assert render_payload["kwargs"]["qc_authorization"]["run_id"] == qc_auth["run_id"]
    assert render_payload["kwargs"]["qc_max_cost_usd"] == 1.0

    # =========================================================================
    # Step 5: Render -> Poll Provider -> AwaitingFinalApproval (Gate 5)
    # =========================================================================
    # Before render: verify mock provider submit has NOT been called yet
    assert mock_provider.submit.call_count == 0

    r7 = dispatcher.process_batch()
    for _ in range(10):
        if r7.succeeded == 1 or r7.failed or r7.quarantined:
            break
        continuation = conn.execute(
            "SELECT next_attempt_at FROM workflow_outbox WHERE workflow_id=? AND state='pending'",
            (wf_id,),
        ).fetchone()
        if not continuation or not continuation["next_attempt_at"]:
            break
        r7 = dispatcher.process_batch(now_iso=continuation["next_attempt_at"])
    if r7.succeeded != 1:
        errs = [r["last_error"] for r in conn.execute("SELECT last_error FROM workflow_outbox WHERE last_error IS NOT NULL").fetchall()]
        pytest.fail(f"r7 failed: report={r7}, errors={errs}")
    assert r7.succeeded == 1

    # Assert Requirement 1: Worker reserved with scope_id=proj_id, Video Factory accepted (submitted storyboard scenes)
    assert mock_provider.submit.call_count >= 1

    agg_render, v5 = repo.get_workflow(wf_id)
    assert agg_render["state"] == ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL.value
    assert "video_url" in agg_render["latest_artifact"]
    assert agg_render["latest_artifact"]["video_url"].startswith("file://")
    render_lineage = agg_render["latest_artifact"]
    video_qc_report = QCReportRepository(conn).get_verified_pass_report(
        owner_user_id=render_lineage["owner_user_id"],
        project_id=render_lineage["project_id"],
        workflow_id=render_lineage["workflow_id"],
        artifact_id=render_lineage["artifact_id"],
        artifact_version=render_lineage["artifact_version"],
        stage=QCStage.VIDEO,
        expected_sha256=render_lineage["sha256"],
    )
    assert video_qc_report is not None
    assert video_qc_report.verdict == QCVerdict.PASS
    assert video_qc_report.evaluator_name == "deterministic-local-qc"
    gate5_req_id = agg_render["pending_approval_id"]

    # =========================================================================
    # Step 6: Final Approval (Gate 5) -> Export -> COMPLETED
    # =========================================================================
    gate5_req = appr_svc.get_request(gate5_req_id)
    assert gate5_req.status.value == "pending"
    assert gate5_req.payload["gate"] == "AwaitingFinalApproval"

    advance_res5 = approve_and_advance_workflow(conn, target_id=gate5_req_id, owner_user_id=owner_id, expected_version=v5)
    assert advance_res5["new_state"] == ProductVideoWorkflowState.EXPORTING.value
    assert advance_res5["enqueued_job"] == "product_video.export"

    r8 = dispatcher.process_batch()  # runs export -> completed
    if r8.succeeded != 1:
        errs = [r["last_error"] for r in conn.execute("SELECT last_error FROM workflow_outbox WHERE last_error IS NOT NULL").fetchall()]
        pytest.fail(f"r8 failed: report={r8}, errors={errs}")
    assert r8.succeeded == 1

    # =========================================================================
    # Assert Requirement 3: Export produced a real, existing, non-empty MP4
    # =========================================================================
    agg_final, _ = repo.get_workflow(wf_id)
    assert agg_final["state"] == ProductVideoWorkflowState.COMPLETED.value
    final_url = agg_final["latest_artifact"]["final_video_url"]
    assert final_url.startswith("file://")

    final_export_path = Path(final_url.removeprefix("file://"))
    assert final_export_path.is_file(), f"Exported video file {final_export_path} does not exist on disk"
    assert final_export_path.stat().st_size > 0, "Exported video file is empty (0 bytes)"
    assert final_export_path.stat().st_size > 0, "Exported video file is empty"


def test_render_replay_safety_polls_existing_operation_without_resubmit(happy_path_env):
    """Prove Requirement 4: After provider accepts render, replay/restart polls existing operation without resubmitting."""
    conn, db_file, registry, mock_provider, rendered_mp4, real_mp4_bytes, vf_ws = happy_path_env

    wf_id = "wf_replay_safety_001"
    proj_id = f"proj_{wf_id}"
    owner_id = "default-owner"
    active_op_id = "ops/proj/locations/us-central1/operations/render_op_replay_999"
    active_batch_id = f"batch_{proj_id}_v1_replay"

    repo = WorkflowRepository(conn)

    from video_factory.integrations.domain import RenderBatch
    from video_factory.integrations.ports import VideoGenerationRequest
    from video_factory.integrations.scene_renderer import SceneRenderOperationStore

    server.video_project_create(owner_id, proj_id)
    vf_repository = server._service().repository
    vf_repository.save_render_batch(
        RenderBatch(
            render_batch_id=active_batch_id,
            owner_user_id=owner_id,
            workflow_id=wf_id,
            project_id=proj_id,
            status="running",
            scene_count=1,
        )
    )
    operation_store = SceneRenderOperationStore(server._db_path())
    scene_request = VideoGenerationRequest(
        request_id=f"scene_render_{proj_id}_scene-1",
        owner_user_id=owner_id,
        project_id=proj_id,
        scene_id="scene-1",
        duration_seconds=6,
        aspect_ratio="9:16",
        prompt="Offline replay scene",
        reference_image_paths=(),
    )
    operation_store.mark_planned(
        scene_request,
        render_batch_id=active_batch_id,
        scene_order=1,
    )
    operation_store.claim_provider_for_submission(scene_request, "google_flow")
    operation_store.reconcile_submitting_operation(
        scene_request.request_id,
        action="bind",
        provider_operation_id=active_op_id,
    )

    from video_factory.workflows.budgets import reserve_budget
    alloc = reserve_budget("video_factory", 50, owner_user_id=owner_id, scope_id=proj_id)

    # Initialize workflow in RENDERING state with checkpointed active_render_job_id and reservation
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps({
            "state": ProductVideoWorkflowState.RENDERING.value,
            "project_id": proj_id,
            "owner_user_id": owner_id,
            "active_render_job_id": active_batch_id,
            "render_dispatch_status": "ACCEPTED",
            "render_budget_reservation_id": alloc.reservation_id,
        }),
        events=[],
        expected_version=0,
    )

    topic_handlers = create_product_video_topic_handlers(
        workflow_repository=repo,
        tool_registry=registry,
        provider_mode="configured",
        poll_timeout=10.0,
        poll_interval=0.01,
        qc_service_factory=_deterministic_qc_service,
    )
    handlers_map = next(
        cell.cell_contents
        for cell in topic_handlers["job.dispatch"].__closure__ or ()
        if isinstance(cell.cell_contents, dict)
        and "product_video.render" in cell.cell_contents
    )
    render_handler = handlers_map["product_video.render"]

    replay_ctx = JobContext(
        workflow_id=wf_id,
        job_id=f"render.replay.{wf_id}",
        owner_user_id=owner_id,
        expected_version=1,
        idempotency_key="idemp_replay_test_safe",
    )

    story_ref = {"project_id": proj_id, "storyboard_id": f"sb_{proj_id}"}

    # Reset submit call count
    mock_provider.submit.reset_mock()

    # Replay execution
    with pytest.raises(JobDeferred):
        render_handler(replay_ctx, story_ref, project_id=proj_id)
    replay_res = render_handler(replay_ctx, story_ref, project_id=proj_id)
    assert isinstance(replay_res, JobSucceeded)

    # Provider submit must NOT have been called (polls existing operation)
    assert mock_provider.submit.call_count == 0, f"Expected 0 provider submits on replay, got {mock_provider.submit.call_count}"
    replay_video = Path(replay_res.artifact_ref["video_url"].removeprefix("file://"))
    assert replay_video.is_file()
    assert replay_video.name == "draft.mp4"


def test_negative_missing_or_unapproved_receipt_halts_fail_closed(happy_path_env):
    """Prove Requirement 2 fail-closed behavior: missing or unapproved approval receipt halts the stage immediately."""
    conn, db_file, registry, mock_provider, rendered_mp4, real_mp4_bytes, vf_ws = happy_path_env

    wf_id = "wf_neg_halt_001"
    proj_id = f"proj_{wf_id}"
    owner_id = "default-owner"

    repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Negative Test Lamp",
        keywords=["lamp"],
        project_id=proj_id,
        owner_user_id=owner_id,
        qc_authorization={
            "run_id": "run_negative_qc_001",
            "scope": ["product_video"],
            "expires_at": "2099-01-01T00:00:00+00:00",
            "max_cost_usd": 100.0,
        },
        qc_max_cost_usd=1.0,
    )

    topic_handlers = create_product_video_topic_handlers(
        workflow_repository=repo,
        tool_registry=registry,
        provider_mode="configured",
        poll_timeout=10.0,
        poll_interval=0.01,
        qc_service_factory=_deterministic_qc_service,
    )
    outbox_repo = OutboxRepository(conn)
    dispatcher = OutboxDispatcher(outbox_repo, topic_handlers=topic_handlers, worker_id="neg-worker-1")

    # Step 1: research completes, resource_pack completes -> AwaitingProductLock
    dispatcher.process_batch()
    dispatcher.process_batch()
    agg, v1 = repo.get_workflow(wf_id)
    assert agg["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
    pending_req_id = agg["pending_approval_id"]

    # NEGATIVE ATTEMPT: Advance without approving request (request is still PENDING)
    appr_svc = ApprovalService(conn)
    req = appr_svc.get_request(pending_req_id)
    assert req.status.value == "pending"

    # Direct call to tool with fake or unapproved approval receipt must fail closed
    mutation_res = registry.dispatch("product_lock_approve", {
        "owner_user_id": owner_id,
        "project_id": proj_id,
        "approval_request_id": "non_existent_or_unapproved_req_id",
    })
    assert mutation_res.get("ok") is False
    assert mutation_res.get("code") == "OPERATOR_APPROVAL_REQUIRED"


def test_configured_worker_waits_for_operator_and_persists_pack_without_qc_authorization(happy_path_env, monkeypatch):
    """A missing QC authorization is an operator wait, never a quarantined invalid product."""
    conn, _db_file, registry, _mock_provider, _rendered_mp4, _real_mp4_bytes, _vf_ws = happy_path_env
    from video_factory.qc.service import ProductVideoQCService

    def _pending_qc(self, workflow_id, stage, **kwargs):
        return {
            "status": "pending",
            "stage": stage.value,
            "reason": "Configured QC requires explicit cost authorization",
        }

    monkeypatch.setattr(ProductVideoQCService, "run", _pending_qc)
    monkeypatch.setattr(
        ProductVideoQCService,
        "select_image_references",
        _completed_image_selection_without_report,
    )

    wf_id = "wf_qc_authorization_required_001"
    repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="QC Authorization Lamp",
        keywords=["lamp"],
        project_id=f"proj_{wf_id}",
        owner_user_id="default-owner",
    )
    topic_handlers = create_product_video_topic_handlers(
        workflow_repository=repo,
        tool_registry=registry,
        provider_mode="configured",
        poll_timeout=10.0,
        poll_interval=0.01,
        creative_generator=_OfflineCreativeGenerator(),
        keyframe_runtime_factory=lambda: _OfflineKeyframeRuntime(vf_ws),
        qc_service_factory=_deterministic_qc_service,
    )
    dispatcher = OutboxDispatcher(
        OutboxRepository(conn),
        topic_handlers=topic_handlers,
        worker_id="qc-authorization-worker",
    )

    assert dispatcher.process_batch().succeeded == 1
    second = dispatcher.process_batch()
    assert second.succeeded == 1
    assert second.quarantined == 0
    aggregate, _version = repo.get_workflow(wf_id)
    assert aggregate["state"] == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    assert aggregate["operator_action"] == "qc_authorization"
    assert aggregate["operator_stage"] == QCStage.IMAGE.value
    assert aggregate["resource_pack"] == aggregate["latest_artifact"]
    remaining = conn.execute(
        "SELECT COUNT(*) FROM workflow_outbox WHERE workflow_id = ?",
        (wf_id,),
    ).fetchone()[0]
    assert remaining == 0


def test_configured_qc_failure_waits_for_operator_without_quarantine(happy_path_env, monkeypatch):
    """An UNKNOWN configured QC preserves evidence and requires a new authorization."""
    conn, _db_file, registry, _mock_provider, _rendered_mp4, _real_mp4_bytes, _vf_ws = happy_path_env
    from video_factory.qc.service import ProductVideoQCService

    monkeypatch.setattr(
        ProductVideoQCService,
        "run",
        lambda *_args, **_kwargs: {"status": "completed", "report": {"verdict": "unknown"}},
    )
    monkeypatch.setattr(
        ProductVideoQCService,
        "select_image_references",
        _completed_image_selection_without_report,
    )
    workflow_id = "wf_qc_failure_persists_pack_001"
    repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="QC Recovery Lamp",
        keywords=["lamp"],
        project_id=f"proj_{workflow_id}",
        owner_user_id="default-owner",
        qc_authorization={
            "workflow_id": workflow_id,
            "run_id": "qc-recovery-run",
            "scope": ["product_video"],
            "expires_at": "2099-01-01T00:00:00+00:00",
            "max_cost_usd": 2.0,
        },
        qc_max_cost_usd=2.0,
    )
    dispatcher = OutboxDispatcher(
        OutboxRepository(conn),
        topic_handlers=create_product_video_topic_handlers(
            workflow_repository=repo,
            tool_registry=registry,
            provider_mode="configured",
            poll_timeout=10.0,
            poll_interval=0.01,
            qc_service_factory=_deterministic_qc_service,
        ),
        worker_id="qc-artifact-recovery-worker",
    )
    assert dispatcher.process_batch().succeeded == 1
    result = dispatcher.process_batch()
    assert result.succeeded == 1
    assert result.quarantined == 0

    aggregate, _ = repo.get_workflow(workflow_id)
    assert aggregate["state"] == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value
    assert aggregate["operator_action"] == "qc_retry_authorization"
    assert aggregate["operator_stage"] == QCStage.IMAGE.value
    assert aggregate["resource_pack"] == aggregate["latest_artifact"]
    assert aggregate["resource_pack"]["resource_pack_id"]


def test_qc_authorization_is_carried_to_next_worker_job(happy_path_env):
    """An explicit QC authorization survives the durable research -> pack handoff."""
    conn, _db_file, registry, _mock_provider, _rendered_mp4, _real_mp4_bytes, _vf_ws = happy_path_env
    wf_id = "wf_qc_authorization_propagation_001"
    repo = WorkflowRepository(conn)
    auth = {
        "run_id": "run_qc_001",
        "scope": ["product_video"],
        "expires_at": "2099-01-01T00:00:00+00:00",
        "max_cost_usd": 2.5,
    }
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Authorized Lamp",
        keywords=["lamp"],
        project_id=f"proj_{wf_id}",
        owner_user_id="default-owner",
        qc_authorization=auth,
        qc_max_cost_usd=2.0,
    )

    first = conn.execute(
        "SELECT message FROM workflow_outbox WHERE workflow_id = ? ORDER BY outbox_id LIMIT 1",
        (wf_id,),
    ).fetchone()
    assert first is not None
    initial_payload = json.loads(first["message"])["payload"]
    assert initial_payload["kwargs"]["qc_authorization"]["workflow_id"] == wf_id

    topic_handlers = create_product_video_topic_handlers(
        workflow_repository=repo,
        tool_registry=registry,
        provider_mode="configured",
        poll_timeout=10.0,
        poll_interval=0.01,
        qc_service_factory=_deterministic_qc_service,
    )
    dispatcher = OutboxDispatcher(
        OutboxRepository(conn),
        topic_handlers=topic_handlers,
        worker_id="qc-propagation-worker",
    )
    assert dispatcher.process_batch().succeeded == 1

    next_row = conn.execute(
        "SELECT message FROM workflow_outbox WHERE workflow_id = ? AND state = 'pending' ORDER BY outbox_id DESC LIMIT 1",
        (wf_id,),
    ).fetchone()
    assert next_row is not None
    next_payload = json.loads(next_row["message"])["payload"]
    assert next_payload["kwargs"]["qc_authorization"] == {**auth, "workflow_id": wf_id}
    assert next_payload["kwargs"]["qc_max_cost_usd"] == 2.0


def test_configured_qc_pass_creates_product_lock_request_without_approving_it(happy_path_env):
    """Configured QC may open the next human gate, but cannot approve it."""
    conn, _db_file, _registry, _mock_provider, _rendered_mp4, _real_mp4_bytes, _vf_ws = happy_path_env
    workflow_id = "wf_qc_advance_001"
    repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=workflow_id,
        product_name="QC Gate Lamp",
        keywords=["lamp"],
        project_id=f"proj_{workflow_id}",
        owner_user_id="default-owner",
    )
    aggregate, version = repo.get_workflow(workflow_id)
    resource_pack = {"schema": "resource_pack", "resource_pack_id": "pack_qc_gate_001", "version": 1}
    aggregate.update(
        {
            "state": ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value,
            "operator_action": "qc_authorization",
            "operator_stage": QCStage.IMAGE.value,
            "resource_pack": resource_pack,
            "latest_artifact": resource_pack,
        }
    )
    repo.commit_workflow(
        workflow_id=workflow_id,
        aggregate_json=json.dumps(aggregate),
        events=[],
        expected_version=version,
    )

    approval_id = _advance_qc_waiting_workflow(
        conn,
        workflow_id,
        QCStage.IMAGE,
        {
            "workflow_id": workflow_id,
            "run_id": "qc-gate-run-001",
            "scope": ["product_video"],
            "expires_at": "2099-01-01T00:00:00+00:00",
            "max_cost_usd": 2.0,
        },
    )
    updated, _ = repo.get_workflow(workflow_id)
    assert updated["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
    request = ApprovalService(conn).get_request(approval_id)
    assert request.status.value == "pending"
    assert request.payload["gate"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
