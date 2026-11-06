import hashlib
import json
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


def _asset(asset_id: str) -> EligibleAsset:
    return EligibleAsset(
        asset_id=asset_id,
        path=f"C:/media/{asset_id}.jpg",
        sha256=(asset_id[-1] * 64),
        qc_report_id="iqc-1",
    )


def _dossier(*, assets: tuple[EligibleAsset, ...], source_refs: tuple[str, ...]) -> ResearchDossier:
    return ResearchDossier.create(
        workflow_id="pv-contract-test",
        product_identity=ProductIdentityV2(
            brand="Baseus",
            model="Bowie WM02",
            display_name="Baseus Bowie WM02",
        ),
        verified_claims=(
            EvidenceClaim(
                claim_id="claim-01",
                text="Tai nghe đi kèm hộp sạc",
                evidence_refs=("evidence-01",),
            ),
        ),
        eligible_assets=assets,
        source_refs=source_refs,
    )


def test_dossier_digest_is_stable_for_unordered_source_refs() -> None:
    assets = (_asset("asset-1"), _asset("asset-2"))
    left = _dossier(assets=assets, source_refs=("source-b", "source-a"))
    right = _dossier(assets=assets, source_refs=("source-a", "source-b"))

    assert left.digest() == right.digest()
    assert left.dossier_digest == left.digest()


def test_dossier_omits_empty_optional_claim_evidence_but_keeps_trusted_coordinates() -> None:
    ordinary = _dossier(assets=(_asset("asset-1"), _asset("asset-2")), source_refs=())
    assert ordinary.to_dict()["verified_claims"][0] == {
        "claim_id": ordinary.verified_claims[0].claim_id,
        "text": ordinary.verified_claims[0].text,
        "evidence_refs": ordinary.verified_claims[0].evidence_refs,
    }
    trusted_claim = replace(
        ordinary.verified_claims[0],
        source_locators=("https://brand.test/product",),
        evidence_snippets=("Exact claim from archive",),
        evidence_locator="line 4",
        evidence_record_id="src_" + "a" * 64,
    )
    trusted = replace(ordinary, verified_claims=(trusted_claim,), dossier_digest="")
    serialized = trusted.to_dict()["verified_claims"][0]
    assert serialized["source_locators"] == ("https://brand.test/product",)
    assert serialized["evidence_snippets"] == ("Exact claim from archive",)
    assert serialized["evidence_locator"] == "line 4"
    assert serialized["evidence_record_id"] == "src_" + "a" * 64
    assert trusted.digest() != ordinary.digest()


def test_dossier_accepts_one_eligible_asset() -> None:
    dossier = _dossier(assets=(_asset("asset-1"),), source_refs=("source-a",))

    assert len(dossier.eligible_assets) == 1


def test_research_dossier_round_trips_without_changing_digest() -> None:
    dossier = _dossier(
        assets=(_asset("asset-1"), _asset("asset-2")),
        source_refs=("source-a",),
    )

    restored = ResearchDossier.from_dict(dossier.to_dict())

    assert restored == dossier
    assert restored.digest() == dossier.digest()


def test_legacy_dossier_digest_survives_new_reference_profile_defaults() -> None:
    dossier = _dossier(
        assets=(_asset("asset-1"), _asset("asset-2")),
        source_refs=("source-a",),
    )
    legacy = dossier.to_dict()
    legacy.pop("dossier_digest")
    for field_name in (
        "shape",
        "color",
        "materials",
        "logo_placement",
        "silhouette",
        "main_proportions",
        "product_count",
        "control_placement",
        "port_count",
        "port_placement",
        "display_shape",
    ):
        legacy["product_identity"].pop(field_name, None)
    profile_fields = {
        "reference_rank",
        "qc_score",
        "identity_score",
        "clarity_score",
        "usability_score",
        "view_angle",
        "shot_type",
        "visible_details",
        "scene_suitability",
        "risk_flags",
    }
    for asset in legacy["eligible_assets"]:
        for field_name in profile_fields:
            asset.pop(field_name, None)
    encoded = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    legacy["dossier_digest"] = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    restored = ResearchDossier.from_dict(legacy)

    assert restored.dossier_digest == legacy["dossier_digest"]


def test_script_from_dict_validates_and_preserves_legacy_digest() -> None:
    scene = ScriptSceneV2(
        scene_id="scene-1",
        order=1,
        story_role="demo",
        duration_seconds=4,
        narration="Show it.",
        overlay_text="Demo",
        visual_objective="Show product",
        environment="Desk",
        subject_action="Touch control",
        product_interaction="Control is pressed",
        start_state="Off",
        end_state="On",
        visual_change="Light turns on",
        camera_framing="Close-up",
        camera_movement="Locked",
        transition_out="Cut",
    )
    plan = ScriptScenePlanV2.create(
        target_duration_seconds=4,
        brief_digest="sha256:" + "a" * 64,
        scenes=(scene,),
    )
    legacy = plan.to_dict()
    legacy.pop("script_digest")
    new_fields = {
        "product_identity_cues",
        "product_placement",
        "lighting",
        "continuity_from_previous",
        "continuity_to_next",
        "required_visible_details",
        "reference_requirements",
        "interaction_actor",
        "interaction_target",
        "contact_landmark",
        "approach_vector",
        "object_orientation",
        "forbidden_contact_regions",
        "interaction_risk",
    }
    for item in legacy["scenes"]:
        for field_name in new_fields:
            item.pop(field_name, None)
    encoded = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    legacy["script_digest"] = digest

    restored = ScriptScenePlanV2.from_dict(legacy)

    assert restored.script_digest == digest
    with pytest.raises(ValueError, match="script scene plan digest mismatch"):
        ScriptScenePlanV2.from_dict({**legacy, "script_digest": "sha256:" + "f" * 64})


def test_brief_digest_is_bound_to_dossier_digest() -> None:
    brief = CreativeBriefV2.create(
        audience=AudienceSpec("Người đi làm", "Rời bàn", "Cất tai nghe gọn"),
        consumer_insight="Cần thao tác nhanh",
        problem="Dễ quên tai nghe",
        creative_angle="Một lần cất tai nghe",
        hook="Sắp đi?",
        promise="Cho thấy thao tác thật",
        reason_to_believe=(ReasonToBelieve("Có hộp sạc", ("claim-1",)),),
        tone="Đời thường",
        story_archetype="demo",
        cta="Xem thêm.",
        restrictions=(),
        target_duration_seconds=18,
        dossier_digest="sha256:" + "a" * 64,
    )

    changed = replace(brief, dossier_digest="sha256:" + "b" * 64, brief_digest="")

    assert changed.digest() != brief.digest()
