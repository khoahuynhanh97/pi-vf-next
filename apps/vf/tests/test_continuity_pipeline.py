import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from test_agent_contracts import profile
from test_creative_generator import _dossier
from test_production_integrity import _write_valid_video
from video_factory.creative.agents.contracts import CreativeReview, ScenePrompt, ScenePromptBundle
from video_factory.creative.agents.critic import review_input_digest
from video_factory.integrations import server
from video_factory.integrations.domain import RenderBatch
from video_factory.integrations.ports import VideoGenerationRequest, VideoGenerationResult
from video_factory.integrations.scene_renderer import SceneRenderOperationStore


def setup_pipeline(tmp_path, monkeypatch, *, first_verdict="PASS", endpoint_verdict="PASS", final_verdict="PASS"):
    workspace = tmp_path / "workspace"
    db_path = tmp_path / "factory.db"
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_DB_PATH", str(db_path))
    monkeypatch.setenv("HERMES_VIDEO_FACTORY_WORKSPACE", str(workspace))
    server.set_service(None)
    server.video_project_create("owner", "project")
    service = server._service()
    batch_id = "batch_continuity"
    service.repository.save_render_batch(RenderBatch(batch_id, "owner", "workflow", "project",
                                                     status="running", scene_count=2))
    source = tmp_path / "source.png"
    Image.new("RGB", (90, 160), "blue").save(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    dossier = _dossier()
    dossier = replace(dossier, eligible_assets=tuple(
        replace(asset, path=str(source), sha256=source_sha) for asset in dossier.eligible_assets), dossier_digest="")
    dossier = replace(dossier, dossier_digest=dossier.digest())
    scenes = [{"scene_id": "s1", "duration_seconds": 8, "subject_action": "Hold the product"},
              {"scene_id": "s2", "duration_seconds": 8, "subject_action": "Continue the view",
               "continuity_from_previous": "continue_previous_clip, same composition"}]
    prompts = ScenePromptBundle("script", profile().digest, "playbook", tuple(
        ScenePrompt(scene["scene_id"], "Start", "", scene["subject_action"], "", ("source-0",)) for scene in scenes))
    package = {"creative_mode": "four_agent", "qc_level": "high", "workflow_id": "workflow",
        "project_id": "project", "package_digest": "approved-package",
        "research_dossier": dossier.to_dict(), "backend_profile": profile().to_dict(),
        "scene_prompts": prompts.to_dict(), "scenes": scenes,
        "generated_keyframes": [{"frame_id": f"{s['scene_id']}-start", "scene_id": s["scene_id"],
                                "image_path": str(source), "sha256": source_sha} for s in scenes],
        "_batch_operation_ids": ["op1", "op2"]}
    context = workspace / "creative_batches" / f"{batch_id}.json"
    context.parent.mkdir(parents=True)
    context.write_text(json.dumps(package), encoding="utf-8")
    store = SceneRenderOperationStore(db_path)
    requests = [VideoGenerationRequest(f"op{i}", "owner", "project", scene["scene_id"],
        scene["subject_action"], 8, (str(source),),
        provider_options={"creative_mode": "four_agent", "creative_package": package})
        for i, scene in enumerate(scenes, 1)]
    for index, request in enumerate(requests, 1):
        store.mark_planned(request, render_batch_id=batch_id, scene_order=index)
    clip = _write_valid_video(tmp_path / "clip.mp4", width=90, height=160, duration_frames=192)
    store.bind_provider_kind("op1", "google_flow")
    store.finalize_provider_result("op1", VideoGenerationResult("op1", "completed", "provider-1", str(clip)))
    events = []

    class Critic:
        def review(self, *, stage, context, media):
            for ref in media:
                ref.content()
            events.append(("review", stage, context["scene_ids"]))
            verdict = (endpoint_verdict if "next_scene" in context else
                       final_verdict if stage == "final" else first_verdict)
            return CreativeReview(stage, verdict, review_input_digest(stage, context, media), (), {
                "method": "actual_pixels", "scene_ids": context["scene_ids"],
                "media_sha256": [ref.sha256 for ref in media],
                "timestamps": context.get("required_timestamps", []), "limitations": "offline fixture"},
                {"provider": "fake", "model": "offline"})

    monkeypatch.setattr("video_factory.creative.agents.video_review.configured_content_critic",
                        lambda *args, **kwargs: Critic())

    class Provider:
        provider_kind = "google_flow"
        def is_available(self):
            return True
        def submit(self, request):
            events.append(("submit", request))
            return VideoGenerationResult(request.request_id, "completed", "provider-2", str(clip))
        def get_status(self, operation_id):
            raise AssertionError("completed offline provider must never be polled")

    return batch_id, store, Provider(), events


@pytest.mark.parametrize("verdict", ["FAIL", "UNKNOWN"])
def test_normal_poll_stops_before_next_paid_submission_when_prior_clip_qc_fails(tmp_path, monkeypatch, verdict):
    batch, store, provider, events = setup_pipeline(tmp_path, monkeypatch, first_verdict=verdict)
    result = server.get_job_status(batch, provider=provider)
    assert result["code"] == f"CLIP_CONTENT_{verdict}"
    assert not any(event[0] == "submit" for event in events)
    assert store.get("op2")["status"] == "planned"


def test_normal_poll_binds_qc_tail_composes_and_reuses_with_content_receipt(tmp_path, monkeypatch):
    batch, store, provider, events = setup_pipeline(tmp_path, monkeypatch)
    result = server.get_job_status(batch, provider=provider)
    assert result["status"] == "completed", result
    assert result["final_review"]["verdict"] == "PASS"
    assert result["qc_level"] == "high"
    submissions = [event[1] for event in events if event[0] == "submit"]
    assert len(submissions) == 1
    bound = submissions[0]
    binding = bound.provider_options["continuity_binding"]
    assert binding["previous_operation_id"] == "op1"
    assert hashlib.sha256(Path(bound.reference_image_paths[0]).read_bytes()).hexdigest() == binding["frame_sha256"]
    assert events[0] == ("review", "clip", ["s1"])
    assert events[1] == ("review", "clip", ["s1"])
    assert events[2][0] == "submit"
    context_path = tmp_path / "workspace" / "creative_batches" / f"{batch}.json"
    context_path.unlink()
    repeated = server.get_job_status(batch, provider=provider)
    assert repeated["sha256"] == result["sha256"]
    assert repeated["final_review"]["verdict"] == "PASS"
    assert len([event for event in events if event[0] == "submit"]) == 1


def test_pinned_batch_membership_uses_latest_authorized_retry(tmp_path, monkeypatch):
    batch, store, _, _ = setup_pipeline(tmp_path, monkeypatch)
    original = store.request_from_record(store.get("op2"))
    retried = replace(original, request_id="op2-attempt-2")
    store.mark_planned(retried, render_batch_id=batch, scene_order=2)
    with store.connection:
        store.connection.execute("UPDATE video_factory_scene_operation SET attempt=2 WHERE operation_id=?",
                                 (retried.request_id,))
    rows = server._creative_batch_operations(store.connection, batch)
    assert rows[1]["operation_id"] == retried.request_id
