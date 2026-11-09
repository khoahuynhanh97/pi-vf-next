from __future__ import annotations

import sqlite3
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
import pytest

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
    JobFailed,
    ProductVideoJobHandlers,
    ProductVideoWorkflowState,
    create_product_video_topic_handlers,
    MCPVideoFactoryAdapter,
)
from video_factory.creative.storyboard import storyboard_package_digest
from video_factory.integrations.ports import KeyframeGenerationResult
from video_factory.workflows.budgets import BudgetPolicy, init_default_engine
from video_factory.workflows.engine import WorkflowEngine
from video_factory.workflows.repository import WorkflowRepository


def _dossier(root: Path | None = None) -> ResearchDossier:
    if root is None:
        first_path, second_path = "C:/one.jpg", "C:/two.jpg"
        first_sha, second_sha = "1" * 64, "2" * 64
    else:
        first = root / "one.jpg"
        second = root / "two.jpg"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        first_path, second_path = str(first), str(second)
        first_sha = hashlib.sha256(b"one").hexdigest()
        second_sha = hashlib.sha256(b"two").hexdigest()
    return ResearchDossier.create(
        workflow_id="pv-grounded",
        product_identity=ProductIdentityV2(
            brand="Baseus",
            model="Bowie WM02",
            display_name="Baseus Bowie WM02",
        ),
        verified_claims=(EvidenceClaim("claim-1", "Có hộp sạc", ("src-1",)),),
        eligible_assets=(
            EligibleAsset("asset-1", first_path, first_sha, "qc-1"),
            EligibleAsset("asset-2", second_path, second_sha, "qc-1"),
        ),
        use_cases=("Cất tai nghe trước khi rời bàn",),
    )


def _brief(dossier: ResearchDossier) -> CreativeBriefV2:
    return CreativeBriefV2.create(
        audience=AudienceSpec("Người đi làm", "Rời bàn", "Cất tai nghe gọn"),
        consumer_insight="Thao tác cất tai nghe phải nhanh khi chuẩn bị di chuyển",
        problem="Tai nghe dễ bị bỏ quên trên bàn",
        creative_angle="Theo chân một lần cất tai nghe trước khi rời bàn",
        hook="Sắp đi nhưng tai nghe vẫn còn trên bàn?",
        promise="Cho thấy thao tác đặt tai nghe vào hộp trong tình huống thật",
        reason_to_believe=(ReasonToBelieve("Có hộp sạc", ("claim-1",)),),
        tone="Đời thường, rõ ràng",
        story_archetype="problem_solution_demo",
        cta="Xem thêm thông tin sản phẩm.",
        restrictions=("Không hiển thị giá",),
        target_duration_seconds=18,
        dossier_digest=dossier.digest(),
    )


def _script(brief: CreativeBriefV2) -> ScriptScenePlanV2:
    scenes = (
        ScriptSceneV2(
            scene_id="scene-1", order=1, story_role="hook", duration_seconds=6,
            narration="Sắp đi nhưng tai nghe còn trên bàn.", overlay_text="Đừng để quên",
            visual_objective="Người dùng nhận ra tai nghe trước khi rời bàn",
            environment="Bàn làm việc", subject_action="Vươn tay lấy tai nghe",
            product_interaction="Cầm tai nghe lên", start_state="Tai nghe trên bàn",
            end_state="Tai nghe trong tay", visual_change="Từ bỏ quên sang được nhặt lên",
            camera_framing="Trung cảnh", camera_movement="Theo chuyển động bàn tay",
            transition_out="Cắt theo động tác", claim_refs=(), reference_asset_ids=("asset-1",),
        ),
        ScriptSceneV2(
            scene_id="scene-2", order=2, story_role="demo", duration_seconds=6,
            narration="Đặt vào hộp sạc trước khi đi.", overlay_text="Cất gọn",
            visual_objective="Thể hiện thao tác cất tai nghe vào hộp",
            environment="Bàn làm việc", subject_action="Đặt tai nghe vào hộp",
            product_interaction="Tai nghe được đặt vào hộp sạc", start_state="Hộp mở",
            end_state="Tai nghe nằm trong hộp", visual_change="Hộp từ trống sang chứa tai nghe",
            camera_framing="Cận cảnh", camera_movement="Máy cố định",
            transition_out="Cắt thẳng", claim_refs=("claim-1",), reference_asset_ids=("asset-2",),
        ),
        ScriptSceneV2(
            scene_id="scene-3", order=3, story_role="cta", duration_seconds=6,
            narration="Gọn gàng để sẵn sàng di chuyển.", overlay_text="Sẵn sàng đi",
            visual_objective="Khép lại bằng hành động rời bàn",
            environment="Lối ra văn phòng", subject_action="Cầm hộp và rời bàn",
            product_interaction="Hộp sạc nằm trong tay", start_state="Người dùng đứng cạnh bàn",
            end_state="Người dùng bước ra cửa", visual_change="Từ đứng yên sang di chuyển",
            camera_framing="Toàn cảnh", camera_movement="Bám theo nhân vật",
            transition_out="Fade out", claim_refs=(), reference_asset_ids=("asset-1",),
        ),
    )
    return ScriptScenePlanV2.create(
        target_duration_seconds=18,
        brief_digest=brief.digest(),
        scenes=scenes,
    )


class _Generator:
    def __init__(self, dossier: ResearchDossier) -> None:
        self.dossier = dossier
        self.brief = _brief(dossier)
        self.script = _script(self.brief)
        self.calls: list[tuple[str, Any]] = []

    def create_direction(self, dossier: ResearchDossier) -> CreativeBriefV2:
        self.calls.append(("direction", dossier))
        return self.brief

    def create_script(
        self, dossier: ResearchDossier, brief: CreativeBriefV2
    ) -> ScriptScenePlanV2:
        self.calls.append(("script", (dossier, brief)))
        return self.script


def _persisted_storyboard_result(
    script: ScriptScenePlanV2,
    *,
    project_id: str = "project-1",
    owner_user_id: str = "user",
) -> dict[str, Any]:
    frames = [{"scene_id": scene.scene_id} for scene in script.scenes]
    return {
        "ok": True,
        "project_id": project_id,
        "storyboard_id": f"sb_{project_id}",
        "frames": frames,
        "project": {
            "id": project_id,
            "owner_user_id": owner_user_id,
            "scene_version": 1,
            "scene_plan_approval": "approved",
            "scene_plan": {"scenes": frames},
            "storyboard": {"storyboard_id": f"sb_{project_id}", "frames": frames},
        },
    }


def test_handlers_use_grounded_generator_and_persist_v2_artifacts() -> None:
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_factory": 500}))
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE workflow (
            workflow_id TEXT PRIMARY KEY,
            aggregate_json TEXT NOT NULL,
            version INTEGER NOT NULL
        );
        CREATE TABLE processed_command (
            workflow_id TEXT,
            command_id TEXT PRIMARY KEY
        );
        CREATE TABLE workflow_event (
            event_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            event_type TEXT,
            timestamp TEXT,
            payload_json TEXT
        );
        CREATE TABLE workflow_outbox (
            workflow_id TEXT,
            message TEXT,
            created_at TEXT
        );
        """
    )
    repo = WorkflowRepository(connection)
    engine = WorkflowEngine(repo)
    dossier = _dossier()
    generator = _Generator(dossier)
    video_client = MagicMock()
    video_client.create_creative_brief.return_value = {
        "project_id": "project-1", "brief_id": "legacy-brief", "version": 7
    }
    video_client.create_scene_plan.return_value = {
        "project_id": "project-1", "scene_plan_id": "legacy-scenes"
    }
    handlers = ProductVideoJobHandlers(
        video_client=video_client,
        workflow_engine=engine,
        creative_generator=generator,
    )

    engine.set_workflow_state("pv-grounded", ProductVideoWorkflowState.CREATING_BRIEF.value)
    brief_result = handlers.creative_brief_job(
        JobContext(workflow_id="pv-grounded", job_id="brief-job"),
        "baseus-wm02",
        brief_config={"research_dossier": dossier.to_dict()},
    )

    assert isinstance(brief_result, JobSucceeded)
    assert brief_result.artifact_ref["schema_version"] == "creative-brief-v2"
    assert brief_result.artifact_ref["version"] == 7
    assert brief_result.artifact_ref["contract_version"] == 2
    assert brief_result.artifact_ref["brief_digest"] == generator.brief.digest()
    sent_config = video_client.create_creative_brief.call_args.kwargs["brief_config"]
    assert sent_config["objective"] == generator.brief.creative_angle
    assert sent_config["target_audience"] == "Người đi làm — Rời bàn"
    assert sent_config["core_message"] == generator.brief.promise
    assert sent_config["tone"] == generator.brief.tone
    assert sent_config["pace"] == "Nhanh gọn, đời thường"
    assert sent_config["cta"] == generator.brief.cta
    assert sent_config["content_blocks"] == [
        generator.brief.hook,
        generator.brief.problem,
        generator.brief.promise,
    ]
    assert sent_config["verified_selling_points"] == [
        {"claim": "Có hộp sạc", "status": "verified", "evidence_refs": ["claim-1"]}
    ]
    assert sent_config["restrictions"] == ["Không hiển thị giá"]
    assert "Sản phẩm công nghệ tiện ích" not in str(sent_config)

    engine.set_workflow_state(
        "pv-grounded", ProductVideoWorkflowState.CREATING_SCENE_PLAN.value
    )
    scene_result = handlers.scene_plan_job(
        JobContext(workflow_id="pv-grounded", job_id="scene-job"),
        brief_result.artifact_ref,
    )

    assert isinstance(scene_result, JobSucceeded)
    assert scene_result.artifact_ref["schema_version"] == "script-scene-plan-v2"
    persisted_script = dict(scene_result.artifact_ref)
    persisted_script["script_digest"] = ""
    assert scene_result.artifact_ref["script_digest"] == ScriptScenePlanV2.from_dict(persisted_script).digest()
    assert scene_result.artifact_ref["scenes"] == generator.script.to_dict()["scenes"]
    sent_brief = video_client.create_scene_plan.call_args.args[0]
    assert sent_brief["scenes"][1]["main_action"] == "Đặt tai nghe vào hộp"


def test_storyboard_job_generates_real_keyframes_before_gate_four(tmp_path: Path) -> None:
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_factory": 500}))
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE workflow (workflow_id TEXT PRIMARY KEY, aggregate_json TEXT, version INTEGER)"
    )
    repo = WorkflowRepository(connection)
    engine = WorkflowEngine(repo)
    dossier = _dossier(tmp_path)
    generator = _Generator(dossier)
    script = generator.script
    scene_ref = {
        **script.to_dict(),
        "project_id": "project-1",
        "research_dossier": dossier.to_dict(),
    }

    class Runtime:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, request: Any) -> KeyframeGenerationResult:
            self.calls.append(request.frame_id)
            output = tmp_path / f"{request.frame_id}.png"
            output.write_bytes(f"generated:{request.frame_id}".encode())
            return KeyframeGenerationResult(
                operation_id=request.operation_id,
                status="completed",
                provider_operation_id=request.operation_id,
                image_path=str(output),
            )

    runtime = Runtime()
    video_client = MagicMock()
    video_client.create_storyboard.return_value = _persisted_storyboard_result(script)
    handlers = ProductVideoJobHandlers(
        video_client=video_client,
        workflow_engine=engine,
        creative_generator=generator,
        keyframe_runtime_factory=lambda: runtime,
    )
    engine.set_workflow_state(
        "pv-grounded", ProductVideoWorkflowState.CREATING_STORYBOARD.value
    )

    result = handlers.storyboard_job(
        JobContext(workflow_id="pv-grounded", job_id="storyboard-job"),
        scene_ref,
        project_id="project-1",
    )

    assert isinstance(result, JobSucceeded)
    assert result.artifact_ref["schema_version"] == "storyboard-package-v2"
    assert len(result.artifact_ref["generated_keyframes"]) == 3
    assert result.artifact_ref["generated_keyframes"][0]["reference_sha256s"] == [
        dossier.eligible_assets[0].sha256
    ]
    assert result.artifact_ref["generated_keyframes"][0]["prompt"]
    assert result.artifact_ref["generated_keyframes"][0]["negative_prompt"]
    assert runtime.calls == ["scene-1-start", "scene-2-start", "scene-3-start"]
    assert result.artifact_ref["package_digest"] == storyboard_package_digest(
        result.artifact_ref
    )


@pytest.mark.parametrize(
    "failure",
    ("exception", "provider_value_error", "error_result", "wrong_project", "wrong_scene"),
)
def test_storyboard_write_failure_prevents_paid_keyframes(
    tmp_path: Path, failure: str
) -> None:
    init_default_engine(BudgetPolicy(max_total=1000, per_channel={"video_factory": 500}))
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE workflow (workflow_id TEXT PRIMARY KEY, aggregate_json TEXT, version INTEGER)"
    )
    engine = WorkflowEngine(WorkflowRepository(connection))
    dossier = _dossier(tmp_path)
    script = _Generator(dossier).script
    runtime = MagicMock()
    video_client = MagicMock()
    response = _persisted_storyboard_result(script)
    if failure == "exception":
        video_client.create_storyboard.side_effect = RuntimeError("VF write unavailable")
    elif failure == "provider_value_error":
        video_client.create_storyboard.side_effect = ValueError("provider response parse failed transiently")
    elif failure == "error_result":
        response = {"ok": False, "code": "PERSISTENCE_ERROR", "error": "VF write failed"}
    elif failure == "wrong_project":
        response["project_id"] = "foreign-project"
    else:
        response["frames"] = [{"scene_id": "foreign-scene"}]
    if failure != "exception":
        video_client.create_storyboard.return_value = response
    handlers = ProductVideoJobHandlers(
        video_client=video_client,
        workflow_engine=engine,
        creative_generator=_Generator(dossier),
        keyframe_runtime_factory=lambda: runtime,
    )
    engine.set_workflow_state(
        "pv-grounded", ProductVideoWorkflowState.CREATING_STORYBOARD.value
    )

    result = handlers.storyboard_job(
        JobContext(workflow_id="pv-grounded", job_id="storyboard-job"),
        {**script.to_dict(), "project_id": "project-1", "research_dossier": dossier.to_dict()},
        project_id="project-1",
    )

    assert isinstance(result, JobFailed)
    if failure in {"exception", "provider_value_error"}:
        assert result.code == "VF_STORYBOARD_FAILED"
        assert result.retryable is True
    else:
        assert result.code == "STORYBOARD_INPUT_INVALID"
        assert result.retryable is False
    runtime.run.assert_not_called()


def test_topic_handler_factory_injects_grounded_creative_dependencies() -> None:
    generator = object()
    runtime_factory = lambda: object()

    topic_handlers = create_product_video_topic_handlers(
        provider_mode="configured",
        creative_generator=generator,
        keyframe_runtime_factory=runtime_factory,
    )

    handlers_map = next(
        cell.cell_contents
        for cell in topic_handlers["job.dispatch"].__closure__ or ()
        if isinstance(cell.cell_contents, dict)
        and "product_video.creative_brief" in cell.cell_contents
    )
    service = handlers_map["product_video.creative_brief"].__self__
    assert service.creative_generator is generator
    assert service.keyframe_runtime_factory is runtime_factory


def test_video_factory_adapter_syncs_only_qc_eligible_assets_before_brief() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Registry:
        def get_all_tool_names(self):
            return {"project", "save", "lock"}

        def dispatch(self, name: str, payload: dict[str, Any]):
            calls.append((name, payload))
            return {"ok": True}

    dossier = _dossier().to_dict()
    dossier["product_identity"].update(
        {
            "silhouette": "low rounded capsule case",
            "main_proportions": "case is wider than tall",
            "product_count": "one charging case and two earbuds",
            "control_placement": "touch controls on outer earbud faces",
            "port_count": "one USB-C port",
            "port_placement": "centered on bottom edge of charging case",
            "display_shape": "no display",
        }
    )
    adapter = MCPVideoFactoryAdapter(
        Registry(),
        {"project_create": "project", "resource_pack_save": "save", "resource_pack_lock": "lock"},
    )
    adapter.ensure_project_locked(
        owner_user_id="owner",
        project_id="project-1",
        resource_pack={"resource_pack_id": "pack-1"},
        dossier=dossier,
        approval_receipt={"approval_request_id": "approval-1"},
        workflow_id="pv-grounded",
        workflow_version=9,
    )

    assert [name for name, _ in calls] == ["project", "save", "lock"]
    assert [ref["asset_id"] for ref in calls[1][1]["resource_pack"]["product_references"]] == ["asset-1", "asset-2"]
    assert calls[2][1]["approval_request_id"] == "approval-1"
    locked_identity = calls[2][1]["product_identity"]
    assert locked_identity["silhouette"] == "low rounded capsule case"
    assert locked_identity["product_count"] == "one charging case and two earbuds"
    assert locked_identity["port_placement"] == "centered on bottom edge of charging case"



@pytest.mark.parametrize("case", ["PASS", "FAIL", "error", None, "v2_observe", "v2_error"])
def test_scene_plan_observes_preproduction_without_changing_gate(tmp_path, monkeypatch, case):
    from copy import deepcopy
    from test_agent_contracts import profile
    from video_factory.automation.creative_policy import evaluate_auto_gate
    from video_factory.creative.agents.critic import CreativeCritic
    from video_factory.creative.agents.gates import current_preproduction_review
    from video_factory.creative.agents.contracts import CreativeReview, ScenePromptBundle, ScenePrompt

    from dataclasses import replace
    from PIL import Image

    verdict = case
    is_v2 = case in ("v2_observe", "v2_error")
    if case == "v2_observe":
        verdict = "PASS"
    elif case == "v2_error":
        verdict = "error"

    monkeypatch.chdir(tmp_path)
    dossier = _dossier(tmp_path)
    assets = []
    for asset in dossier.eligible_assets:
        path = Path(asset.path)
        Image.new("RGB", (32, 32), "red").save(path)
        assets.append(replace(asset, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    dossier = replace(dossier, eligible_assets=tuple(assets), dossier_digest="")
    dossier = replace(dossier, dossier_digest=dossier.digest())
    generator = _Generator(dossier)
    generator.creative_mode = "four_agent"
    generator.profile = profile()
    generator.model = object()
    generator.recipe = {"digest": "playbook"}
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(tmp_path / "workspace"))
    generator.create_scene_prompts = lambda dossier, script: ScenePromptBundle(
        script.digest(), generator.profile.digest, "playbook",
        tuple(ScenePrompt(s.scene_id, "start", "", "motion", "", s.reference_asset_ids)
              for s in script.scenes))

    received_context = {}

    def review(self, *, stage, context, media):
        from video_factory.creative.agents.critic import review_input_digest
        received_context.update(context)
        if verdict == "error":
            raise OSError("critic unavailable")
        evaluator_receipt = {"model": "fixture"}
        if context.get("rubric_version") == "v2":
            evaluator_receipt["rubric_version"] = "v2"
            evaluator_receipt["director_config_version"] = context.get("director_config_version")
        return CreativeReview(stage, verdict, review_input_digest(stage, context, media), (),
                              {"method": "fixture"}, evaluator_receipt)
    monkeypatch.setattr(CreativeCritic, "review", review)

    policy = {"creative_mode": "four_agent", "auto_export": True}
    if verdict is not None:
        policy["preproduction_gate"] = "observe"
    if is_v2:
        policy["rubric_version"] = "v2"
        policy["director_config_version"] = "scene-bible-v1"
    repo = MagicMock()
    repo.get_workflow.return_value = ({"state": "creating_scene_plan", "creative_policy": policy}, 1)
    handlers = ProductVideoJobHandlers(pi_client=MagicMock(), video_client=MagicMock(),
                                      workflow_repository=repo, creative_generator=generator, provider_mode="fake")
    monkeypatch.setattr(handlers, "_bind_creative_run", lambda ctx: None)
    result = handlers.scene_plan_job(JobContext(workflow_id=dossier.workflow_id, job_id="scene"),
                                     generator.brief.to_dict())
    assert isinstance(result, JobSucceeded)
    artifact = result.artifact_ref
    parsed_plan = ScriptScenePlanV2.from_dict(artifact)
    assert artifact["script_digest"] == parsed_plan.script_digest
    assert artifact["script_digest"] == artifact["scene_prompts"]["script_digest"]
    current = current_preproduction_review(artifact)
    if verdict is None:
        assert current is None and "preproduction_review" not in artifact
    else:
        assert current is not None
        assert current.verdict == ("UNKNOWN" if verdict == "error" else verdict)
        if is_v2:
            assert received_context.get("rubric_version") == "v2"
            assert received_context.get("director_config_version") == "scene-bible-v1"
            assert current.evaluator_receipt.get("rubric_version") == "v2"
            assert current.evaluator_receipt.get("director_config_version") == "scene-bible-v1"
        for field in ("scenes", "scene_prompts"):
            changed = deepcopy(artifact)
            if field == "scenes":
                changed[field][0]["subject_action"] = "changed"
            else:
                changed[field]["prompts"][0]["video_prompt"] = "changed"
            assert current_preproduction_review(changed) is None
    assert evaluate_auto_gate(state="AwaitingSceneApproval", artifact=artifact,
                              reviews=(current,) if current else (), policy=policy)["action"] == "advance"
