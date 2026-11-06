"""End-to-end Baseus headphone AI video creation workflow test.

Reuses the offline deterministic harness from test_happy_path_e2e to prove
the Product-to-Video pipeline handles a real-world headphone product (Baseus).
"""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import pytest

from video_factory.creative.contracts import (
    AudienceSpec, CreativeBriefV2, EligibleAsset, EvidenceClaim,
    ProductIdentityV2, ReasonToBelieve, ResearchDossier,
    ScriptScenePlanV2, ScriptSceneV2,
)
from hermes_personal.application.operator_handler import approve_and_advance_workflow
from hermes_personal.integrations.video_factory import server
from video_factory.integrations.ports import VideoGenerationResult, KeyframeGenerationResult
from video_factory.handlers import (
    JobContext, JobSucceeded, ProductVideoWorkflowState, create_product_video_topic_handlers,
    start_product_video_workflow,
)
from video_factory.qc.models import QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.approvals import ApprovalService
from video_factory.workflows.budgets import BudgetPolicy, init_default_engine
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.outbox_dispatcher import OutboxDispatcher
from video_factory.workflows.outbox_repository import OutboxRepository
from video_factory.workflows.repository import WorkflowRepository


class _BaseusCreativeGenerator:
    """Deterministic creative generator for Baseus headphones offline test."""

    def __init__(self, workflow_id="", reference_paths=None):
        self.workflow_id = workflow_id
        if workflow_id and reference_paths:
            self.dossier = ResearchDossier.create(
                workflow_id=workflow_id,
                product_identity=ProductIdentityV2(
                    brand="Baseus",
                    model="Headphones",
                    display_name="Baseus Tai Nghe Bluetooth",
                ),
                verified_claims=(
                    EvidenceClaim(
                        claim_id="claim-audio",
                        text="Hỗ trợ âm thanh không gian 24bit/96kHz",
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
                "Nguoi dung am nhac dien thoai",
                "Can chat luong am thanh khong gian",
                "Trai nghiem am nhac 24bit/96kHz",
            ),
            consumer_insight="Tai nghe Baseus mang lai chat luong am thanh xuat sac.",
            problem="Tai nghe dien thoay thuong thieu chi tiet am thanh va pin yeu.",
            creative_angle="Theo chân chuyen nghe nhac khong day voi Baseus headphones.",
            hook="Chat luong am thanh song dong trong mot lan cham?",
            promise="Baseus Headphone cho trai nghiem am nhac song dong.",
            reason_to_believe=(ReasonToBelieve(evidence.text, (evidence.claim_id,)),),
            tone="Nang dong, hien dai, ro rang",
            story_archetype="problem_solution_demo",
            cta="Kham pha ngay Baseus headphones.",
            restrictions=("Khong hien thi gia san",),
            target_duration_seconds=18,
            dossier_digest=dossier.digest(),
        )

    def create_script(self, dossier, brief):
        self.dossier = dossier
        first, second = dossier.eligible_assets[:2]
        claim = dossier.verified_claims[0]
        scenes = (
            ScriptSceneV2(
                scene_id="scene-1", order=1, story_role="hook", duration_seconds=6,
                narration="Mot nguoi ban tre dang lang nghe nhac trong khong gian do thi.",
                overlay_text="Am nhac cam xung?",
                visual_objective="Thiet lap van de ve chat luong am thanh",
                environment="Pho thi buoi toi",
                subject_action="Nguoi dung dang dung tai nghe dien thoai",
                product_interaction="Tai nghe ket noi Bluetooth",
                start_state="Dien thoai dang phat nhac",
                end_state="Tai nghe duoc dat len tai",
                visual_change="Tu man hinh sang am thanh thuc",
                camera_framing="Trung canh",
                camera_movement="Tien lai gan san pham",
                transition_out="Cat theo dong tac",
                claim_refs=(claim.claim_id,),
                reference_asset_ids=(first.asset_id,),
            ),
            ScriptSceneV2(
                scene_id="scene-2", order=2, story_role="demo", duration_seconds=6,
                narration="Chat luong am thanh cao cap lan toan trong tung nut nhac.",
                overlay_text="Chi tiet am thanh song dong",
                visual_objective="The hien Baseus headphones tren thuc te",
                environment="Cung khong gian do thi",
                subject_action="Nguoi dung dang nghe nhac voi tai nghe",
                product_interaction="Tai nghe phat am thanh khong day",
                start_state="Tai nghe dang ket noi",
                end_state="Am thanh lan toan khap khong gian",
                visual_change="Tu ket noi sang trai nghiem tron ven",
                camera_framing="Can canh",
                camera_movement="May固定",
                transition_out="Cat thang",
                claim_refs=(claim.claim_id,),
                reference_asset_ids=(second.asset_id,),
            ),
            ScriptSceneV2(
                scene_id="scene-3", order=3, story_role="cta", duration_seconds=6,
                narration="Trai nghiem am nhac khong gioi han voi Baseus headphones.",
                overlay_text="Kham pha Baseus ngay",
                visual_objective="Ket thuc bang trang thai su dung hoan chinh",
                environment="Khong gian am nhac da chieu sang",
                subject_action="Nguoi dung dang thuong thuc am nhac",
                product_interaction="Tai nghe Baseus dang hoat dong",
                start_state="Tai nghe sang, khong gian yen tinh",
                end_state="Nguoi dung dang nghe nhac",
                visual_change="Tu trưng bày sang su dung thuc",
                camera_framing="Toan canh doc",
                camera_movement="Lui de lo toan bo san pham",
                transition_out="Fade out",
                claim_refs=(),
                reference_asset_ids=(first.asset_id,),
            ),
        )
        self.script = ScriptScenePlanV2.create(
            target_duration_seconds=18,
            brief_digest=brief.digest(),
            scenes=scenes,
        )
        return self.script


class _OfflineKeyframeRuntime:
    def __init__(self, output_dir):
        self.output_dir = output_dir

    def run(self, request):
        output = self.output_dir / f"{request.frame_id}.png"
        output.write_bytes(f"generated-keyframe:{request.frame_id}".encode())
        return KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="completed",
            provider_operation_id=request.operation_id,
            image_path=str(output),
        )


class _DeterministicQCEvaluator:
    fake_mode = False
    evaluator_name = "deterministic-local-qc"
    evaluator_version = "1"
    model = "local-fixture"

    def evaluate_image(self, **_kwargs):
        return {
            "view_angle": "front three-quarter",
            "shot_type": "clean product",
            "visible_details": ["headphone body", "ear cup", "headband"],
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
            "reason": "Deterministic grounded reference - Baseus headphones",
        }

    def evaluate_multiple_images(self, *_args, **_kwargs):
        return {
            "identity_consistency": 48,
            "scene_adherence": 28,
            "visual_cleanliness": 18,
            "total_score": 94,
            "verdict": "pass",
            "hard_failure": False,
            "source_branding_detected": False,
            "identity_mismatch": False,
            "interaction_geometry_valid": True,
            "product_count_valid": True,
            "prompt_reference_consistent": True,
            "important_details_preserved": True,
            "composition_usable": True,
            "animation_suitable": True,
            "recommended_action": "none",
            "reason": "Deterministic grounded scene - Baseus headphones",
        }


def _deterministic_qc_service(connection):
    from video_factory.qc.service import ProductVideoQCService
    return ProductVideoQCService(connection, evaluator=_DeterministicQCEvaluator())


class BaseusMCPTestRegistry:
    """Tool registry dispatching MCP tools for Baseus headphone workflow."""

    def __init__(self, mock_provider, rendered_mp4_path, db_path, reference_paths):
        self.mock_provider = mock_provider
        self.rendered_mp4_path = rendered_mp4_path
        self.db_path = db_path
        self.reference_paths = reference_paths
        self.research_store = {}

    def get_all_tool_names(self):
        return {
            "mcp__product_intelligence__research_product",
            "mcp__product_intelligence__build_resource_pack",
            "mcp__product_intelligence__lock_resource_pack",
            "mcp__video_factory__video_project_create",
            "mcp__video_factory__resource_pack_save",
            "mcp__video_factory__resource_pack_lock",
            "mcp__video_factory__creative_brief_get",
            "creative_brief_get", "mcp__video_factory__create_brief",
            "mcp__video_factory__create_scene_plan",
            "mcp__video_factory__create_storyboard",
            "mcp__video_factory__render",
            "mcp__video_factory__get_job_status",
            "mcp__video_factory__export",
            "brief_approve", "scene_plan_approve", "storyboard_approve",
            "final_video_approve", "product_lock_approve",
            "mcp__video_factory__creative_brief_approve",
            "mcp__video_factory__scene_plan_approve",
            "mcp__video_factory__storyboard_approve",
            "mcp__video_factory__final_video_approve",
            "mcp__video_factory__product_lock_approve",
        }

    def dispatch(self, tool_name, payload):
        if tool_name == "mcp__video_factory__video_project_create":
            return server.video_project_create(payload.get("owner_user_id", ""), payload.get("project_id", ""))
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
            )
        if tool_name == "mcp__product_intelligence__research_product":
            snap_id = f"snap_{len(self.research_store) + 1:03d}"
            res = {
                "status": "success",
                "snapshot_id": snap_id,
                "persistence_status": "saved",
                "canonical_products": [{
                    "canonical_product_id": f"prod_{snap_id}",
                    "name": "Baseus Tai Nghe Bluetooth",
                    "brand": "Baseus",
                    "listings": [{"price": 499000, "currency": "VND"}],
                }],
                "report": {"product_name": "Baseus Tai Nghe Bluetooth", "query": payload.get("query"), "url": payload.get("url")},
            }
            self.research_store[snap_id] = res
            return res
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
                "brand": "Baseus",
                "model": "Headphones",
                "product_name": "Baseus Tai Nghe Bluetooth",
                "product_identity": {"brand": "Baseus", "model": "Headphones", "display_name": "Baseus Tai Nghe Bluetooth"},
                "claims": [{"claim_id": "claim-audio", "text": "Hỗ trợ âm thanh không gian 24bit/96kHz", "status": "verified", "evidence_refs": ["offline-pdp"]}],
                "assets": media_assets,
                "resource_pack": {
                    "id": pack_id, "version": 1, "product_id": f"prod_{snap_id}",
                    "brand": "Baseus", "model": "Headphones", "product_name": "Baseus Tai Nghe Bluetooth",
                    "claims": [{"claim_id": "claim-audio", "text": "Hỗ trợ âm thanh không gian 24bit/96kHz", "status": "verified", "evidence_refs": ["offline-pdp"]}],
                    "ready_to_lock": True,
                    "media_assets": media_assets,
                },
            }
        elif tool_name == "mcp__product_intelligence__lock_resource_pack":
            pack_dict = payload.get("pack_dict") or payload
            rp = pack_dict.get("resource_pack", pack_dict)
            pack_id = rp.get("id") or rp.get("resource_pack_id") or "rp_001"
            prod_id = rp.get("product_id", "prod_baseus")
            proj_id = payload.get("project_id", f"proj_{prod_id}")
            owner_id = payload.get("owner_user_id", "default-owner")
            server.video_project_create(owner_id, proj_id)
            server.resource_pack_save(owner_id, proj_id, {
                "id": pack_id,
                "product_references": [{"asset_id": "a1", "uri": "asset://headphone.png"}],
                "primary_product_asset_id": "a1",
                "product_identity_description": "Baseus không dây headphones",
            })
            srv = server._service()
            srv.lock_resource_pack(owner_id, proj_id, server._identity({"description": "Baseus không dây headphones"}))
            return {
                "status": "locked", "lock_id": f"lock_{pack_id}",
                "resource_pack_id": pack_id,
                "manifest_digest": "sha256:abc123manifest",
                "assets": [{"asset_id": "a1", "local_path": "/data/headphone.png"}],
                "locked": True,
                "resource_pack": {"id": pack_id, "product_id": prod_id, "product_name": "Baseus Tai Nghe Bluetooth", "ready_to_lock": False},
            }
        elif tool_name in ("product_lock_approve", "mcp__video_factory__product_lock_approve"):
            return server.resource_pack_lock(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                product_identity={"description": "Baseus không dây headphones"},
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )
        elif tool_name in ("creative_brief_get", "mcp__video_factory__creative_brief_get"):
            return server.creative_brief_get(payload.get("owner_user_id", "default-owner"), payload.get("project_id", ""))
        elif tool_name == "mcp__video_factory__create_brief":
            cfg = dict(payload.get("brief_config") or {})
            cfg.pop("resource_pack", None); cfg.pop("brief_id", None)
            cfg.setdefault("objective", "Convert audio shoppers")
            cfg.setdefault("target_audience", "Audio enthusiasts")
            cfg.setdefault("core_message", "Baseus headphone audio")
            cfg.setdefault("tone", "Modern and clear")
            cfg.setdefault("pace", "Measured")
            cfg.setdefault("cta", "Get discount on TikTok Shop")
            cfg.setdefault("content_blocks", ["Audio quality", "Wireless use", "Discount CTA"])
            return server.create_brief(payload.get("product_id", ""), cfg, payload.get("owner_user_id", "default-owner"), payload.get("project_id", ""))
        elif tool_name in ("brief_approve", "mcp__video_factory__creative_brief_approve"):
            return server.creative_brief_approve(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )
        elif tool_name == "mcp__video_factory__create_scene_plan":
            return server.create_scene_plan(
                brief_result=payload.get("brief_result"),
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
            )
        elif tool_name in ("scene_plan_approve", "mcp__video_factory__scene_plan_approve"):
            return server.scene_plan_approve(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )
        elif tool_name == "mcp__video_factory__create_storyboard":
            return server.create_storyboard(
                scene_plan_result=payload.get("scene_plan_result"),
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
            )
        elif tool_name in ("storyboard_approve", "mcp__video_factory__storyboard_approve"):
            return server.storyboard_approve(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )
        elif tool_name == "mcp__video_factory__render":
            return server.render(
                storyboard_result=payload.get("storyboard_result"),
                render_params=payload.get("render_params"),
                idempotency_key=payload.get("idempotency_key"),
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                provider=self.mock_provider,
                admission_context=payload.get("admission_context"),
                approval_request_id=payload.get("approval_request_id"),
            )
        elif tool_name == "mcp__video_factory__get_job_status":
            return server.get_job_status(job_id=payload.get("job_id", ""), provider=self.mock_provider)
        elif tool_name in ("final_video_approve", "mcp__video_factory__final_video_approve"):
            return server.final_video_approve(
                owner_user_id=payload.get("owner_user_id", "default-owner"),
                project_id=payload.get("project_id", ""),
                approval_request_id=payload.get("approval_request_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                workflow_version=payload.get("workflow_version"),
            )
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
def baseus_env(tmp_path, monkeypatch):
    db_file = tmp_path / "hermes_baseus.db"
    conn = connect(db_file)
    monkeypatch.setenv("HERMES_APPROVAL_DB_PATH", str(db_file))
    vf_db = tmp_path / "video_factory_baseus.sqlite"
    vf_ws = tmp_path / "vf_workspace_baseus"
    vf_ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(vf_db))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(vf_ws))
    rendered_mp4 = tmp_path / "mock_provider_output_baseus.mp4"
    import av
    with av.open(str(rendered_mp4), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=24)
        stream.width = 720; stream.height = 1280; stream.pix_fmt = "yuv420p"
        for _ in range(144):
            frame = av.VideoFrame(72, 128, "yuv420p")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    real_mp4_binary = rendered_mp4.read_bytes()
    from PIL import Image
    reference_paths = (
        vf_ws / "reference-input" / "headphone-front.png",
        vf_ws / "reference-input" / "headphone-side.png",
    )
    reference_paths[0].parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (96, 160), color="white").save(reference_paths[0])
    Image.new("RGB", (96, 160), color="gray").save(reference_paths[1])
    mock_provider = MagicMock()
    mock_provider.provider_kind = "google_flow"
    mock_provider.submit.return_value = VideoGenerationResult(
        request_id="req_render_baseus_777",
        status="submitted",
        provider_operation_id="ops/proj/locations/us-central1/operations/render_op_baseus_777",
    )
    poll_state = {"count": 0}
    def _get_status(job_id):
        poll_state["count"] += 1
        if poll_state["count"] == 1:
            return VideoGenerationResult(request_id=job_id, status="running", provider_operation_id=job_id)
        return VideoGenerationResult(request_id=job_id, status="completed", video_path=str(rendered_mp4), provider_operation_id=job_id)
    mock_provider.get_status.side_effect = _get_status
    init_default_engine(BudgetPolicy(max_total=100_000, per_channel={"video_factory": 100_000, "product_intelligence": 100_000}), connection=conn)
    registry = BaseusMCPTestRegistry(mock_provider=mock_provider, rendered_mp4_path=rendered_mp4, db_path=db_file, reference_paths=reference_paths)
    yield conn, db_file, registry, mock_provider, rendered_mp4, real_mp4_binary, vf_ws
    conn.close()


def test_baseus_headphone_video_complete_e2e(baseus_env):
    """Baseus headphone AI product video full workflow across 5 HITL gates."""
    conn, db_file, registry, mock_provider, rendered_mp4, real_mp4_bytes, vf_ws = baseus_env
    wf_id = "wf_baseus_headphone_001"
    proj_id = f"proj_{wf_id}"
    owner_id = "default-owner"
    qc_auth = {
        "run_id": "run_baseus_qc_001",
        "scope": ["product_video"],
        "expires_at": "2099-01-01T00:00:00+00:00",
        "max_cost_usd": 100.0,
    }
    repo = WorkflowRepository(conn)
    start_product_video_workflow(
        workflow_repository=repo,
        workflow_id=wf_id,
        product_name="Baseus Tai Nghe Bluetooth",
        keywords=["tai nghe", "baseus", "không dây", "bluetooth", "audio"],
        pdp_url="https://shopee.vn/Baseus-Tai-nghe-bluetooth",
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
        creative_generator=_BaseusCreativeGenerator(wf_id, registry.reference_paths),
        keyframe_runtime_factory=lambda: _OfflineKeyframeRuntime(vf_ws),
        qc_service_factory=_deterministic_qc_service,
    )
    outbox_repo = OutboxRepository(conn)
    dispatcher = OutboxDispatcher(outbox_repo, topic_handlers=topic_handlers, worker_id="baseus-worker-1")

    # Gate 1: Product Lock
    assert dispatcher.process_batch().succeeded == 1
    assert dispatcher.process_batch().succeeded == 1
    agg, v1 = repo.get_workflow(wf_id)
    assert agg["state"] == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value
    gate1_req_id = agg["pending_approval_id"]
    appr_svc = ApprovalService(conn)
    gate1_req = appr_svc.get_request(gate1_req_id)
    assert gate1_req.status.value == "pending"
    assert gate1_req.payload["gate"] == "AwaitingProductLock"
    advance_res1 = approve_and_advance_workflow(conn, target_id=gate1_req_id, owner_user_id=owner_id, expected_version=v1)
    assert advance_res1["new_state"] == ProductVideoWorkflowState.LOCKING_RESOURCE_PACK.value

    # Gate 2: Brief Approval
    r3 = dispatcher.process_batch()
    assert r3.succeeded == 1
    r4 = dispatcher.process_batch()
    assert r4.succeeded == 1
    agg_brief, v2 = repo.get_workflow(wf_id)
    assert agg_brief["state"] == ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL.value
    gate2_req_id = agg_brief["pending_approval_id"]
    gate2_req = appr_svc.get_request(gate2_req_id)
    assert gate2_req.status.value == "pending"
    assert gate2_req.payload["gate"] == "AwaitingBriefApproval"
    advance_res2 = approve_and_advance_workflow(conn, target_id=gate2_req_id, owner_user_id=owner_id, expected_version=v2)
    assert advance_res2["new_state"] == ProductVideoWorkflowState.CREATING_SCENE_PLAN.value

    # Gate 3: Scene Approval
    r5 = dispatcher.process_batch()
    assert r5.succeeded == 1
    agg_scene, v3 = repo.get_workflow(wf_id)
    assert agg_scene["state"] == ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL.value
    gate3_req_id = agg_scene["pending_approval_id"]
    gate3_req = appr_svc.get_request(gate3_req_id)
    assert gate3_req.status.value == "pending"
    assert gate3_req.payload["gate"] == "AwaitingSceneApproval"
    advance_res3 = approve_and_advance_workflow(conn, target_id=gate3_req_id, owner_user_id=owner_id, expected_version=v3)
    assert advance_res3["new_state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value

    # Gate 4: Storyboard Approval
    r6 = dispatcher.process_batch()
    assert r6.succeeded == 1
    agg_sb, v4 = repo.get_workflow(wf_id)
    assert agg_sb["state"] == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
    gate4_req_id = agg_sb["pending_approval_id"]
    gate4_req = appr_svc.get_request(gate4_req_id)
    assert gate4_req.status.value == "pending"
    assert gate4_req.payload["gate"] == "AwaitingStoryboardApproval"
    advance_res4 = approve_and_advance_workflow(conn, target_id=gate4_req_id, owner_user_id=owner_id, expected_version=v4)
    assert advance_res4["new_state"] == ProductVideoWorkflowState.RENDERING.value

    # Render -> Gate 5
    assert mock_provider.submit.call_count == 0
    r7 = dispatcher.process_batch()
    assert r7.succeeded == 1
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
    gate5_req_id = agg_render["pending_approval_id"]
    gate5_req = appr_svc.get_request(gate5_req_id)
    assert gate5_req.status.value == "pending"
    assert gate5_req.payload["gate"] == "AwaitingFinalApproval"
    advance_res5 = approve_and_advance_workflow(conn, target_id=gate5_req_id, owner_user_id=owner_id, expected_version=v5)
    assert advance_res5["new_state"] == ProductVideoWorkflowState.EXPORTING.value
    r8 = dispatcher.process_batch()
    assert r8.succeeded == 1
    agg_final, _ = repo.get_workflow(wf_id)
    assert agg_final["state"] == ProductVideoWorkflowState.COMPLETED.value
    final_url = agg_final["latest_artifact"]["final_video_url"]
    assert final_url.startswith("file://")
    final_export_path = Path(final_url.removeprefix("file://"))
    assert final_export_path.is_file(), f"Exported video file {final_export_path} does not exist on disk"
    assert final_export_path.stat().st_size > 0, "Exported video file is empty"
