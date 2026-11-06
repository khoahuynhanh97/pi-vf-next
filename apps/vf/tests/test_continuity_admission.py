import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from video_factory.integrations.ports import VideoGenerationRequest
from video_factory.integrations.scene_renderer import SceneRenderOperationStore, stable_scene_request_digest


def request(tmp_path):
    frame = tmp_path / "start.png"
    frame.write_bytes(b"approved initial still")
    return VideoGenerationRequest("scene-op", "owner", "project", "s2", "Continue", 4,
                                  (str(frame),), provider_options={"creative_mode": "four_agent"})


def bound_request(original, tmp_path):
    frame = tmp_path / "tail.png"
    frame.write_bytes(b"QC checked previous tail")
    options = {**original.provider_options, "continuity_binding": {
        "base_request_digest": stable_scene_request_digest(original),
        "previous_operation_id": "previous-op",
        "previous_output_sha256": "previous-sha",
        "frame_sha256": hashlib.sha256(frame.read_bytes()).hexdigest(),
        "frame_path": str(frame),
        "frame_timestamp": 3.958333,
        "clip_review_digest": "clip-review",
        "endpoint_review_digest": "endpoint-review",
    }}
    return replace(original, reference_image_paths=(str(frame),), provider_options=options)


def test_continuity_binding_is_atomic_repeatable_and_survives_original_plan_replay(tmp_path):
    store = SceneRenderOperationStore(tmp_path / "ops.db")
    original = request(tmp_path)
    store.mark_planned(original, render_batch_id="batch", scene_order=2)
    bound = bound_request(original, tmp_path)
    row = store.bind_continuity_start(bound, expected_digest=stable_scene_request_digest(original))
    assert store.request_from_record(row) == bound
    store.mark_planned(original, render_batch_id="batch", scene_order=2)
    assert store.request_from_record(store.get(original.request_id)) == bound
    other = SceneRenderOperationStore(tmp_path / "ops.db")
    assert other.bind_continuity_start(bound, expected_digest=stable_scene_request_digest(original)) == row


def test_continuity_binding_cannot_change_accepted_request(tmp_path):
    store = SceneRenderOperationStore(sqlite3.connect(":memory:"))
    original = request(tmp_path)
    store.mark_planned(original)
    assert store.mark_submitting(original)
    with pytest.raises(ValueError, match="accepted|claimed"):
        store.bind_continuity_start(bound_request(original, tmp_path),
                                    expected_digest=stable_scene_request_digest(original))
    assert store.request_from_record(store.get(original.request_id)) == original


def test_high_clip_gate_enforces_technical_verdict(monkeypatch):
    from video_factory.creative.agents import video_review
    assessment = {"technical_qc": {"verdict": "FAIL"}, "clip_review": {"verdict": "PASS"}}
    monkeypatch.setattr(video_review, "review_video", lambda **kwargs: assessment)
    assert video_review.check_completed_clips({"qc_level": "high"}, [("s1", Path("unused"))],
                                             object()) == ("s1", "CLIP_TECHNICAL_FAIL", assessment)


@pytest.mark.parametrize("verdict", ["FAIL", "UNKNOWN"])
def test_endpoint_failure_never_rebinds_or_admits_next_scene(tmp_path, monkeypatch, verdict):
    from video_factory.creative.agents import continuity
    from video_factory.creative.agents.contracts import CreativeReview
    from test_production_integrity import _write_valid_video
    store = SceneRenderOperationStore(tmp_path / "ops.db")
    original = request(tmp_path)
    store.mark_planned(original)
    clip = _write_valid_video(tmp_path / "previous.mp4", width=90, height=160, duration_frames=96)
    previous = {"operation_id": "previous-op", "scene_id": "s1", "status": "completed",
                "output_path": str(clip), "output_sha256": hashlib.sha256(clip.read_bytes()).hexdigest()}
    review = {"sha256": previous["output_sha256"], "technical_qc": {"verdict": "PASS"},
              "clip_review": {"verdict": "PASS", "input_digest": "current-clip-review"}}
    monkeypatch.setattr(continuity, "board_media", lambda package: ())

    class Critic:
        def review(self, **kwargs):
            assert kwargs["context"]["next_scene"]["scene_id"] == "s2"
            assert kwargs["media"][-1].content()
            return CreativeReview("clip", verdict, "endpoint-review", (), {}, {})

    bound, failure = continuity.prepare_continuity_start(
        package={"package_digest": "package"}, scene={"scene_id": "s2"},
        operation=store.get(original.request_id), previous=previous, store=store,
        critic=Critic(), output_dir=tmp_path / "continuity", clip_assessment=review)
    assert bound is None and failure["clip_review"]["verdict"] == verdict
    assert store.request_from_record(store.get(original.request_id)) == original


def test_high_final_cannot_fall_back_to_technical_only(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from video_factory.creative.agents import video_review
    monkeypatch.setattr("video_factory.integrations.composer.probe_video",
        lambda *args, **kwargs: SimpleNamespace(sha256="sha"))
    with pytest.raises(ValueError, match="high QC requires"):
        video_review.review_video(path=tmp_path / "final.mp4",
            package={"qc_level": "high", "scenes": []}, critic=None, stage="final")
