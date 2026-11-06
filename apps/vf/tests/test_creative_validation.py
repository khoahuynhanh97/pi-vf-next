from __future__ import annotations

from dataclasses import replace

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
from video_factory.creative.validation import (
    CreativeValidationError,
    validate_brief,
    validate_script,
)


def _dossier() -> ResearchDossier:
    return ResearchDossier.create(
        workflow_id="pv-validation-test",
        product_identity=ProductIdentityV2("Baseus", "Bowie WM02", "Baseus Bowie WM02"),
        verified_claims=(EvidenceClaim("claim-01", "Có hộp sạc", ("evidence-01",)),),
        eligible_assets=(
            EligibleAsset("asset-1", "C:/media/one.jpg", "1" * 64, "iqc-1"),
            EligibleAsset("asset-2", "C:/media/two.jpg", "2" * 64, "iqc-1"),
        ),
    )


def _brief(dossier: ResearchDossier) -> CreativeBriefV2:
    return CreativeBriefV2.create(
        audience=AudienceSpec("Người di chuyển", "Rời bàn", "Cất tai nghe gọn"),
        consumer_insight="Việc cất tai nghe phải nhanh",
        problem="Tai nghe dễ bị bỏ quên",
        creative_angle="Theo chân thao tác cất tai nghe",
        hook="Cất tai nghe trước khi đi",
        promise="Cho thấy thao tác cất vào hộp",
        reason_to_believe=(ReasonToBelieve("Có hộp sạc", ("claim-01",)),),
        tone="Đời thường",
        story_archetype="problem_solution_demo",
        cta="Xem thêm thông tin sản phẩm.",
        restrictions=(),
        target_duration_seconds=18,
        dossier_digest=dossier.digest(),
    )


def test_brief_rejects_purchase_cta_for_manual_posting() -> None:
    dossier = _dossier()
    brief = _brief(dossier)
    invalid = replace(brief, cta="Shop now!")

    with pytest.raises(CreativeValidationError, match="manual posting"):
        validate_brief(dossier, invalid)


def _scene(
    order: int,
    *,
    subject_action: str,
    visual_change: str,
    environment: str,
    claim_refs: tuple[str, ...] = (),
) -> ScriptSceneV2:
    return ScriptSceneV2(
        scene_id=f"scene-{order:02d}",
        order=order,
        story_role=("hook", "demonstration", "payoff", "cta")[(order - 1) % 4],
        duration_seconds=6,
        narration="Có hộp sạc" if claim_refs else "Cất tai nghe trước khi đi",
        overlay_text="Cất gọn",
        visual_objective="Cho thấy hành động sử dụng sản phẩm",
        environment=environment,
        subject_action=subject_action,
        product_interaction="Tay tương tác trực tiếp với tai nghe và hộp sạc",
        start_state="Tai nghe ở ngoài hộp",
        end_state="Tai nghe nằm trong hộp",
        visual_change=visual_change,
        camera_framing="Cận cảnh dọc",
        camera_movement="Dolly nhẹ",
        transition_out="Match cut",
        claim_refs=claim_refs,
        reference_asset_ids=("asset-1",),
    )


@pytest.mark.parametrize(
    ("subject_action", "visual_change"),
    [
        ("Zoom into product", "Camera gets closer"),
        ("Pan around product", "Camera moves left"),
        ("Rotate product image", "Product image rotates"),
        ("Demonstrate product", "Product remains centered"),
    ],
)
def test_script_rejects_camera_only_or_generic_action(
    subject_action: str,
    visual_change: str,
) -> None:
    dossier = _dossier()
    script = ScriptScenePlanV2.create(
        target_duration_seconds=18,
        brief_digest=_brief(dossier).digest(),
        scenes=(
            _scene(1, subject_action=subject_action, visual_change=visual_change, environment="Bàn làm việc"),
            _scene(2, subject_action="Tay đặt tai nghe vào hộp", visual_change="Tai nghe chuyển vào hộp", environment="Bàn cạnh cửa"),
            _scene(3, subject_action="Tay cho hộp vào túi", visual_change="Hộp chuyển từ bàn vào túi", environment="Cửa ra vào"),
        ),
    )

    with pytest.raises(CreativeValidationError, match="meaningful action"):
        validate_script(dossier, _brief(dossier), script)


def test_script_rejects_unknown_claim_reference() -> None:
    dossier = _dossier()
    brief = _brief(dossier)
    script = ScriptScenePlanV2.create(
        target_duration_seconds=18,
        brief_digest=brief.digest(),
        scenes=(
            _scene(1, subject_action="Tay mở hộp", visual_change="Nắp hộp mở", environment="Bàn", claim_refs=("missing-claim",)),
            _scene(2, subject_action="Tay đặt tai nghe vào hộp", visual_change="Tai nghe chuyển vào hộp", environment="Bàn cạnh cửa"),
            _scene(3, subject_action="Tay cho hộp vào túi", visual_change="Hộp chuyển vào túi", environment="Cửa ra vào"),
        ),
    )

    with pytest.raises(CreativeValidationError, match="missing-claim"):
        validate_script(dossier, brief, script)


def test_validate_v3_script_requires_claim_reference_for_every_scene() -> None:
    dossier = _dossier()
    brief = _brief(dossier)

    def grounded_scene(order: int, claim_refs: tuple[str, ...]) -> ScriptSceneV2:
        return replace(
            _scene(
                order,
                subject_action=f"Tay thực hiện thao tác {order}",
                visual_change=f"Trạng thái sản phẩm đổi lần {order}",
                environment="Bàn" if order % 2 else "Cạnh cửa",
                claim_refs=claim_refs,
            ),
            duration_seconds=4,
            product_identity_cues=("Hình dáng hộp sạc",),
            product_placement="Chính giữa khung hình",
            lighting="Ánh sáng trung tính",
            continuity_from_previous="Giữ nguyên mẫu và màu",
            continuity_to_next="Giữ nguyên mẫu và màu",
            required_visible_details=("Nắp hộp sạc",),
            reference_requirements=("Góc cận chính diện",),
        )

    script = ScriptScenePlanV2.create(
        target_duration_seconds=16,
        brief_digest=brief.digest(),
        prompt_version="script-scene-plan-v3",
        scenes=(
            grounded_scene(1, ()),
            grounded_scene(2, ("claim-01",)),
            grounded_scene(3, ("claim-01",)),
            grounded_scene(4, ("claim-01",)),
        ),
    )

    with pytest.raises(CreativeValidationError, match="requires a verified claim reference"):
        validate_script(dossier, brief, script)


def test_script_rejects_duration_not_supported_by_video_provider() -> None:
    dossier = _dossier()
    brief = _brief(dossier)
    script = ScriptScenePlanV2.create(
        target_duration_seconds=17,
        brief_digest=brief.digest(),
        scenes=(
            replace(_scene(1, subject_action="Tay mở hộp", visual_change="Nắp hộp mở", environment="Bàn"), duration_seconds=5),
            _scene(2, subject_action="Tay đặt tai nghe vào hộp", visual_change="Tai nghe vào hộp", environment="Bàn cạnh cửa"),
            _scene(3, subject_action="Tay cho hộp vào túi", visual_change="Hộp vào túi", environment="Cửa ra vào"),
        ),
    )

    with pytest.raises(CreativeValidationError, match="provider-supported"):
        validate_script(dossier, brief, script)


def test_script_rejects_keyword_detected_high_risk_interaction_without_geometry() -> None:
    dossier = _dossier()
    brief = _brief(dossier)
    risky_scene = replace(
        _scene(
            1,
            subject_action="Tay cắm cáp USB-C vào cổng sạc",
            visual_change="Cáp chuyển từ ngoài vào cổng sạc",
            environment="Bàn làm việc",
            claim_refs=("claim-01",),
        ),
        product_interaction="Cắm cáp vào cổng sạc của sản phẩm",
        interaction_risk="low",
    )
    script = ScriptScenePlanV2.create(
        target_duration_seconds=18,
        brief_digest=brief.digest(),
        scenes=(
            risky_scene,
            _scene(
                2,
                subject_action="Tay đặt tai nghe vào hộp",
                visual_change="Tai nghe vào hộp",
                environment="Bàn cạnh cửa",
            ),
            _scene(
                3,
                subject_action="Tay cho hộp vào túi",
                visual_change="Hộp vào túi",
                environment="Cửa ra vào",
            ),
        ),
    )

    with pytest.raises(CreativeValidationError, match="requires interaction_actor"):
        validate_script(dossier, brief, script)
