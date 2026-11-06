from __future__ import annotations

import hashlib
from pathlib import Path

from video_factory.creative.contracts import (
    EligibleAsset,
    EvidenceClaim,
    ProductIdentityV2,
    ResearchDossier,
    ScriptScenePlanV2,
    ScriptSceneV2,
)
from video_factory.creative.storyboard import compile_keyframe_requests


def test_compiler_binds_scene_action_and_only_selected_eligible_references(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one.jpg"
    second = tmp_path / "two.jpg"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    dossier = ResearchDossier.create(
        workflow_id="pv-1",
        product_identity=ProductIdentityV2("Baseus", "Bowie WM02", "Baseus Bowie WM02"),
        verified_claims=(EvidenceClaim("claim-1", "Có hộp sạc", ("source-1",)),),
        eligible_assets=(
            EligibleAsset("asset-1", str(first), hashlib.sha256(b"one").hexdigest(), "qc-1"),
            EligibleAsset("asset-2", str(second), hashlib.sha256(b"two").hexdigest(), "qc-1"),
        ),
    )
    script = ScriptScenePlanV2.create(
        target_duration_seconds=18,
        brief_digest="sha256:" + "a" * 64,
        scenes=(
            ScriptSceneV2(
                scene_id="scene-1", order=1, story_role="demo", duration_seconds=6,
                narration="Cất tai nghe vào hộp", overlay_text="Cất gọn",
                visual_objective="Thể hiện thao tác cất tai nghe",
                environment="Bàn làm việc", subject_action="Đặt tai nghe vào hộp sạc",
                product_interaction="Tai nghe chạm đúng khe sạc", start_state="Hộp mở và trống",
                end_state="Tai nghe nằm trong hộp", visual_change="Từ ngoài hộp vào trong hộp",
                camera_framing="Cận cảnh", camera_movement="Máy cố định",
                transition_out="Cắt", claim_refs=("claim-1",),
                reference_asset_ids=("asset-2",),
            ),
        ),
    )

    requests = compile_keyframe_requests(
        workflow_id="pv-1",
        project_id="project-1",
        dossier=dossier,
        script=script,
    )

    assert len(requests) == 1
    assert requests[0].reference_image_paths == (str(second),)
    assert "Đặt tai nghe vào hộp sạc" in requests[0].prompt
    assert "Hộp mở và trống" in requests[0].prompt
    assert "marketplace UI" in requests[0].negative_prompt
