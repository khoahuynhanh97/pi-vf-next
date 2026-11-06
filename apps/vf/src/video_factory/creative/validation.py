"""Cross-artifact validation for grounded, product-specific creative output."""

from __future__ import annotations

import re
import unicodedata

from .contracts import CreativeBriefV2, ResearchDossier, ScriptScenePlanV2
from .prompts import CREATIVE_BRIEF_V2_SCHEMA


class CreativeValidationError(ValueError):
    pass


def validate_brief_response(data: dict[str, object]) -> None:
    """Validate provider JSON before CreativeBriefV2's legacy defaulting parser runs."""
    def visit(value: object, schema: dict[str, object], path: str) -> None:
        kind = schema.get("type")
        type_matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": type(value) is int,
        }
        if kind in type_matches and not type_matches[kind]:
            raise CreativeValidationError(f"creative brief JSON {path} must be {kind}")
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise CreativeValidationError(f"creative brief JSON {path} is outside the allowed values")
        if kind == "object" and isinstance(value, dict):
            required = schema.get("required", [])
            properties = schema.get("properties", {})
            if isinstance(required, list):
                missing = [name for name in required if name not in value]
                if missing:
                    raise CreativeValidationError(
                        f"creative brief JSON {path} is missing required field(s): {', '.join(map(str, missing))}"
                    )
            if isinstance(properties, dict):
                for name, child_schema in properties.items():
                    if name in value and isinstance(child_schema, dict):
                        visit(value[name], child_schema, f"{path}.{name}")
        if kind == "array" and isinstance(value, list):
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    visit(item, item_schema, f"{path}[{index}]")

    visit(data, CREATIVE_BRIEF_V2_SCHEMA, "$")
    if data.get("schema_version") != "creative-brief-v2":
        raise CreativeValidationError("creative brief JSON schema_version must be creative-brief-v2")


_CAMERA_ONLY = (
    "zoom into",
    "zoom in",
    "pan around",
    "camera moves",
    "rotate product",
    "product image rotates",
    "demonstrate product",
    "product remains centered",
    "xoay sản phẩm",
    "phóng to sản phẩm",
    "lia máy quanh sản phẩm",
)

_CAMERA_MOTION = {
    "zoom into", "zoom in", "pan around", "camera moves",
    "phóng to sản phẩm", "lia máy quanh sản phẩm",
}

_COMMERCE_CTA = (
    "shop now",
    "buy now",
    "order now",
    "add to cart",
    "đặt hàng",
    "mua ngay",
    "thêm vào giỏ",
    "giỏ hàng",
)


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def is_commerce_cta(value: str) -> bool:
    normalized_cta = _normalized(value)
    return any(phrase in normalized_cta for phrase in _COMMERCE_CTA)


def validate_brief(dossier: ResearchDossier, brief: CreativeBriefV2) -> None:
    if brief.dossier_digest != dossier.digest():
        raise CreativeValidationError("creative brief dossier digest is stale")
    claim_ids = {claim.claim_id for claim in dossier.verified_claims}
    for reason in brief.reason_to_believe:
        missing = set(reason.evidence_refs) - claim_ids
        if missing:
            raise CreativeValidationError(
                f"reason_to_believe references unknown claims: {sorted(missing)}"
            )
    if is_commerce_cta(brief.cta):
        raise CreativeValidationError(
            "creative brief CTA must support manual posting and cannot instruct a purchase"
        )


def validate_script(
    dossier: ResearchDossier,
    brief: CreativeBriefV2,
    script: ScriptScenePlanV2,
    *,
    allow_camera_motion: bool = False,
) -> None:
    if script.brief_digest != brief.digest():
        raise CreativeValidationError("script brief digest is stale")
    # Three-scene plans remain readable for legacy workflows; newly generated
    # plans are instructed to use 4 to 10 scenes for durations between 16 and 50 seconds.
    minimum_scenes = 4 if script.prompt_version == "script-scene-plan-v3" else 3
    if not minimum_scenes <= len(script.scenes) <= 10:
        raise CreativeValidationError("script requires between 3 and 10 scenes")
    if not 16 <= script.target_duration_seconds <= 50:
        raise CreativeValidationError("script duration must be between 16 and 50 seconds")
    actual_duration = sum(scene.duration_seconds for scene in script.scenes)
    if abs(actual_duration - script.target_duration_seconds) > 0.5:
        raise CreativeValidationError("scene durations do not match target duration")

    claim_ids = {claim.claim_id for claim in dossier.verified_claims}
    asset_ids = {asset.asset_id for asset in dossier.eligible_assets}
    environments: set[str] = set()
    actions: set[str] = set()
    for scene in script.scenes:
        if scene.duration_seconds not in {4, 6, 8}:
            raise CreativeValidationError(
                f"scene {scene.scene_id} duration must be provider-supported (4, 6, or 8 seconds)"
            )
        action = _normalized(scene.subject_action)
        change = _normalized(scene.visual_change)
        risk = str(scene.interaction_risk or "low").strip().lower()
        rejected_actions = (
            tuple(phrase for phrase in _CAMERA_ONLY if phrase not in _CAMERA_MOTION)
            if allow_camera_motion and risk == "low" else _CAMERA_ONLY
        )
        if (allow_camera_motion and risk == "low" and
                any(phrase in action for phrase in _CAMERA_MOTION)):
            rejected_actions = tuple(phrase for phrase in rejected_actions
                                     if phrase != "product remains centered")
        if any(phrase in action or phrase in change for phrase in rejected_actions):
            raise CreativeValidationError(
                f"scene {scene.scene_id} requires meaningful action beyond camera movement"
            )
        missing_claims = set(scene.claim_refs) - claim_ids
        if missing_claims:
            raise CreativeValidationError(
                f"scene {scene.scene_id} references unknown claims: {sorted(missing_claims)}"
            )
        missing_assets = set(scene.reference_asset_ids) - asset_ids
        if missing_assets:
            raise CreativeValidationError(
                f"scene {scene.scene_id} references ineligible assets: {sorted(missing_assets)}"
            )
        if script.prompt_version == "script-scene-plan-v3":
            if not scene.claim_refs:
                raise CreativeValidationError(
                    f"scene {scene.scene_id} requires a verified claim reference"
                )
            if not scene.product_identity_cues:
                raise CreativeValidationError(
                    f"scene {scene.scene_id} requires product identity cues"
                )
            required_scene_text = {
                "product placement": scene.product_placement,
                "lighting": scene.lighting,
                "continuity from previous": scene.continuity_from_previous,
                "continuity to next": scene.continuity_to_next,
            }
            missing_text = [name for name, value in required_scene_text.items() if not value.strip()]
            if missing_text:
                raise CreativeValidationError(
                    f"scene {scene.scene_id} requires {', '.join(missing_text)}"
                )
            if not scene.required_visible_details or not scene.reference_requirements:
                raise CreativeValidationError(
                    f"scene {scene.scene_id} requires visible details and reference requirements"
                )
            if not scene.reference_asset_ids:
                raise CreativeValidationError(
                    f"scene {scene.scene_id} requires scene-specific references"
                )
        if risk not in {"low", "medium", "high"}:
            raise CreativeValidationError(
                f"scene {scene.scene_id} interaction_risk must be 'low', 'medium', or 'high'"
            )
        high_risk_keywords = ("plug", "insert", "cắm", "đút", "tra vào", "tháo", "gắn", "cradle", "port", "cổng sạc", "bấm nút", "nhấn nút", "press button")
        is_high_risk = (risk == "high") or any(kw in action or kw in _normalized(scene.product_interaction) for kw in high_risk_keywords)
        if is_high_risk:
            required_geometry = {
                "interaction_actor": scene.interaction_actor,
                "interaction_target": scene.interaction_target,
                "contact_landmark": scene.contact_landmark,
                "approach_vector": scene.approach_vector,
                "object_orientation": scene.object_orientation,
            }
            missing_geometry = [
                name for name, value in required_geometry.items() if not value.strip()
            ]
            if not scene.forbidden_contact_regions:
                missing_geometry.append("forbidden_contact_regions")
            if missing_geometry:
                raise CreativeValidationError(
                    f"high-risk interaction scene {scene.scene_id} requires "
                    f"{', '.join(missing_geometry)}"
                )
        environments.add(_normalized(scene.environment))
        actions.add(action)
    if len(environments) < 2:
        raise CreativeValidationError("script requires at least two distinct visual setups")
    if len(actions) < 2:
        raise CreativeValidationError("script requires at least two distinct meaningful actions")
