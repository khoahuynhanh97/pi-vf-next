from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

from video_factory.integrations.keyframe_provider import (
    KeyframeGenerationRequest,
)
from video_factory.creative.storyboard import (
    build_keyframe_recovery_request,
)
from video_factory.qc.rubric import build_keyframe_qc_prompt
from video_factory.workflows.outbox_dispatcher import OutboxDispatcher


def test_build_keyframe_recovery_request_alt_reference_and_negative_context() -> None:
    req = KeyframeGenerationRequest(
        operation_id="op_1",
        workflow_id="wf_1",
        project_id="proj_1",
        frame_id="scene_01-start",
        frame_role="start",
        prompt="A bright sunny park with a picnic blanket spread on the grass",
        product_description="Tribit XSound Go Bluetooth speaker",
        negative_prompt="blurry",
        reference_image_paths=("D:/assets/bike_lifestyle.jpg",),
        aspect_ratio="9:16",
    )

    # When QC reports bicycle context error and an alternate clean packshot is provided
    rev_req = build_keyframe_recovery_request(
        req,
        msg="The generated image fails on scene adherence: environment is a bicycle setting instead of a park with a picnic blanket",
        start_cond_p="",
        attempt_num=1,
        alt_reference_path="D:/assets/clean_packshot.png",
    )

    # Alternate reference replaces the lifestyle photo as primary
    assert rev_req.reference_image_paths[0] == "D:/assets/clean_packshot.png"
    # Prompt contains guidance and explicit bicycle exclusion
    assert "[QC REVISION GUIDANCE:" in rev_req.prompt
    assert "CRITICAL: Do NOT depict any bicycle" in rev_req.prompt


def test_build_keyframe_qc_prompt_specifies_last_image_as_candidate() -> None:
    prompt = build_keyframe_qc_prompt(
        brand="Tribit",
        model="XSound Go",
        scene_id="scene_04_vibe_upgrade-end",
        scene_prompt="Approved scene end prompt",
        candidate_dimensions=(720, 1280),
    )
    assert "The candidate keyframe to evaluate is strictly the LAST attached image" in prompt
    assert "Earlier attached images are references" in prompt


def test_outbox_dispatcher_heartbeat_default_extension_is_600s() -> None:
    from video_factory.workflows.envelope import build_envelope
    mock_repo = MagicMock()
    mock_repo.extend_lease.return_value = True
    dispatcher = OutboxDispatcher(
        repository=mock_repo,
        worker_id="test-worker",
        topic_handlers={},
    )
    called_ext = []
    def fake_extend(outbox_id, token, worker, extension_seconds=60.0):
        called_ext.append(extension_seconds)
        return True

    mock_repo.extend_lease.side_effect = fake_extend

    def fake_handler(envelope, heartbeat_fn=None):
        if heartbeat_fn:
            heartbeat_fn()

    dispatcher.topic_handlers["job.dispatch"] = fake_handler

    mock_claim = MagicMock()
    mock_claim.outbox_id = 100
    mock_claim.lease_token = "tok"
    mock_claim.message = build_envelope(
        workflow_id="wf_test",
        topic="job.dispatch",
        idempotency_key="idemp_1",
        payload={},
    )
    mock_claim.workflow_id = "wf_test"

    # Call _deliver_claim
    dispatcher._deliver_claim(mock_claim)
    assert len(called_ext) == 1
    assert called_ext[0] == 600.0
