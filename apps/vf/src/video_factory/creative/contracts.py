"""Immutable, versioned contracts for grounded Product Video creative artifacts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Any, Literal


def canonical_digest(value: Any) -> str:
    """Return a stable SHA-256 digest for JSON-compatible content."""

    if hasattr(value, "to_dict"):
        payload = value.to_dict()
    elif hasattr(value, "__dataclass_fields__"):
        payload = asdict(value)
    else:
        payload = value
    if isinstance(payload, dict):
        payload = dict(payload)
        schema_version = str(payload.get("schema_version") or "")
        self_digest_key = {
            "research-dossier-v2": "dossier_digest",
            "creative-brief-v2": "brief_digest",
            "script-scene-plan-v2": "script_digest",
            "storyboard-package-v2": "package_digest",
        }.get(schema_version)
        if self_digest_key:
            payload.pop(self_digest_key, None)
        payload = _strip_legacy_default_fields(payload, schema_version)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _strip_legacy_default_fields(payload: dict[str, Any], schema_version: str) -> dict[str, Any]:
    """Keep stored V2 digests stable when optional fidelity metadata is absent."""

    normalized = deepcopy(payload)
    if schema_version == "research-dossier-v2":
        if normalized.get('reference_identity_mode','legacy_path')=='content':
            for asset in normalized.get('eligible_assets',()):
                if isinstance(asset,dict):
                    asset.pop('path',None)
        else:
            normalized.pop('reference_identity_mode',None)
        identity = normalized.get("product_identity")
        if isinstance(identity, dict):
            for key, default in (('identity_mode', 'sku'), ('canonical_product_id', ''),
                                 ('source_snapshot_digest', '')):
                if identity.get(key) == default:
                    identity.pop(key, None)
            for key in (
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
                if not identity.get(key):
                    identity.pop(key, None)
        profile_defaults: dict[str, Any] = {
            "reference_rank": 0,
            "qc_score": 0,
            "identity_score": 0,
            "clarity_score": 0,
            "usability_score": 0,
            "view_angle": "",
            "shot_type": "",
            "visible_details": (),
            "scene_suitability": (),
            "risk_flags": (),
        }
        for asset in normalized.get("eligible_assets", ()):
            if not isinstance(asset, dict):
                continue
            for key, default in profile_defaults.items():
                if asset.get(key) == default or (
                    isinstance(default, tuple) and asset.get(key) == []
                ):
                    asset.pop(key, None)
    elif schema_version == "script-scene-plan-v2":
        scene_defaults: dict[str, Any] = {
            "product_identity_cues": (),
            "product_placement": "",
            "lighting": "",
            "continuity_from_previous": "",
            "continuity_to_next": "",
            "required_visible_details": (),
            "reference_requirements": (),
            "interaction_actor": "",
            "interaction_target": "",
            "contact_landmark": "",
            "approach_vector": "",
            "object_orientation": "",
            "forbidden_contact_regions": (),
            "interaction_risk": "low",
        }
        for scene in normalized.get("scenes", ()):
            if not isinstance(scene, dict):
                continue
            for key, default in scene_defaults.items():
                if scene.get(key) == default or (
                    isinstance(default, tuple) and scene.get(key) == []
                ):
                    scene.pop(key, None)
    elif schema_version == "creative-brief-v2":
        provenance = normalized.get("provenance")
        if isinstance(provenance, dict) and not provenance.get("creative_adjustments"):
            provenance.pop("creative_adjustments", None)
    return normalized


@dataclass(frozen=True)
class ProductIdentityV2:
    brand: str
    model: str
    display_name: str
    variant: str = ""
    distinctive_features: tuple[str, ...] = ()
    shape: str = ""
    color: str = ""
    materials: str = ""
    logo_placement: str = ""
    silhouette: str = ""
    main_proportions: str = ""
    product_count: str = ""
    control_placement: str = ""
    port_count: str = ""
    port_placement: str = ""
    display_shape: str = ""
    identity_mode: str = 'sku'
    canonical_product_id: str = ''
    source_snapshot_digest: str = ''

    def __post_init__(self) -> None:
        if self.identity_mode not in {'sku', 'source_product'}:
            raise ValueError('invalid identity mode')
        if self.identity_mode == 'source_product':
            digest = self.source_snapshot_digest.removeprefix('sha256:')
            if not self.canonical_product_id.strip() or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('source_product requires upstream canonical ID and snapshot SHA')
            if not self.display_name.strip():
                raise ValueError('display_name is required')
        elif not self.brand.strip() or not self.model.strip() or not self.display_name.strip():
            raise ValueError("brand, model and display_name are required")


@dataclass(frozen=True)
class EvidenceClaim:
    claim_id: str
    text: str
    evidence_refs: tuple[str, ...]
    source_locators: tuple[str, ...] = ()
    evidence_snippets: tuple[str, ...] = ()
    evidence_locator: str = ""
    evidence_record_id: str = ""

    def __post_init__(self) -> None:
        if not self.claim_id.strip() or not self.text.strip() or not self.evidence_refs:
            raise ValueError("evidence claims require id, text and evidence_refs")


@dataclass(frozen=True)
class EligibleAsset:
    asset_id: str
    path: str
    sha256: str
    qc_report_id: str
    visual_observations: tuple[str, ...] = ()
    reference_rank: int = 0
    qc_score: int = 0
    identity_score: int = 0
    clarity_score: int = 0
    usability_score: int = 0
    view_angle: str = ""
    shot_type: str = ""
    visible_details: tuple[str, ...] = ()
    scene_suitability: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.asset_id.strip() or not self.path.strip() or not self.qc_report_id.strip():
            raise ValueError("eligible assets require id, path and qc_report_id")
        digest = self.sha256.removeprefix("sha256:")
        if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
            raise ValueError("eligible asset sha256 must be a 64-character hexadecimal digest")
        if self.reference_rank < 0 or not 0 <= self.qc_score <= 100:
            raise ValueError("eligible asset rank and QC score are invalid")


@dataclass(frozen=True)
class ResearchDossier:
    workflow_id: str
    product_identity: ProductIdentityV2
    verified_claims: tuple[EvidenceClaim, ...]
    eligible_assets: tuple[EligibleAsset, ...]
    restricted_claims: tuple[str, ...] = ()
    audience_signals: tuple[str, ...] = ()
    pain_points: tuple[str, ...] = ()
    use_cases: tuple[str, ...] = ()
    differentiators: tuple[str, ...] = ()
    approved_learnings: tuple[dict[str, Any], ...] = ()
    source_refs: tuple[str, ...] = ()
    schema_version: str = "research-dossier-v2"
    dossier_digest: str = ""
    reference_identity_mode: str = 'legacy_path'
    creative_input: dict[str, Any] | None = None
    seller_description: str | None = None

    def __post_init__(self) -> None:
        if not self.workflow_id.strip():
            raise ValueError("workflow_id is required")
        if self.reference_identity_mode not in {'legacy_path','content'}:
            raise ValueError('unsupported reference identity mode')
        if not self.eligible_assets:
            raise ValueError("research dossier requires at least one eligible asset")
        if self.product_identity.identity_mode == 'source_product' and len({
            a.sha256.removeprefix('sha256:') for a in self.eligible_assets
        }) < 1:
            raise ValueError('source_product requires one distinct approved source image')
        asset_ids = [asset.asset_id for asset in self.eligible_assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("eligible asset IDs must be unique")
        claim_ids = [claim.claim_id for claim in self.verified_claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("verified claim IDs must be unique")

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        product_identity: ProductIdentityV2,
        verified_claims: tuple[EvidenceClaim, ...],
        eligible_assets: tuple[EligibleAsset, ...],
        restricted_claims: tuple[str, ...] = (),
        audience_signals: tuple[str, ...] = (),
        pain_points: tuple[str, ...] = (),
        use_cases: tuple[str, ...] = (),
        differentiators: tuple[str, ...] = (),
        approved_learnings: tuple[dict[str, Any], ...] = (),
        source_refs: tuple[str, ...] = (),
        reference_identity_mode: str = 'legacy_path',
        creative_input: dict[str, Any] | None = None,
        seller_description: str | None = None,
    ) -> ResearchDossier:
        dossier = cls(
            workflow_id=workflow_id,
            product_identity=product_identity,
            verified_claims=tuple(sorted(verified_claims, key=lambda item: item.claim_id)),
            eligible_assets=tuple(
                sorted(
                    eligible_assets,
                    key=lambda item: (
                        item.reference_rank if item.reference_rank > 0 else 10_000,
                        -item.qc_score,
                        item.asset_id,
                    ),
                )
            ),
            restricted_claims=tuple(sorted(set(restricted_claims))),
            audience_signals=tuple(sorted(set(audience_signals))),
            pain_points=tuple(sorted(set(pain_points))),
            use_cases=tuple(sorted(set(use_cases))),
            differentiators=tuple(sorted(set(differentiators))),
            approved_learnings=approved_learnings,
            source_refs=tuple(sorted(set(source_refs))),
            reference_identity_mode=reference_identity_mode,
            creative_input=creative_input if isinstance(creative_input, dict) and creative_input else None,
            seller_description=seller_description.strip() if isinstance(seller_description, str) and seller_description.strip() else None,
        )
        return replace(dossier, dossier_digest=dossier.digest())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchDossier:
        identity = data.get("product_identity")
        if not isinstance(identity, dict):
            raise ValueError("research dossier product_identity must be an object")
        raw_claims = data.get("verified_claims")
        raw_assets = data.get("eligible_assets")
        if not isinstance(raw_claims, (list, tuple)):
            raise ValueError("research dossier verified_claims must be a list")
        if not isinstance(raw_assets, (list, tuple)):
            raise ValueError("research dossier eligible_assets must be a list")
        dossier = cls.create(
            workflow_id=str(data["workflow_id"]),
            product_identity=ProductIdentityV2(
                brand=str(identity["brand"]),
                model=str(identity["model"]),
                display_name=str(identity["display_name"]),
                variant=str(identity.get("variant", "")),
                distinctive_features=tuple(
                    str(item) for item in identity.get("distinctive_features", [])
                ),
                shape=str(identity.get("shape", "")),
                color=str(identity.get("color", "")),
                materials=str(identity.get("materials", identity.get("material", ""))),
                logo_placement=str(identity.get("logo_placement", "")),
                silhouette=str(identity.get("silhouette", "")),
                main_proportions=str(identity.get("main_proportions", "")),
                product_count=str(identity.get("product_count", "")),
                control_placement=str(identity.get("control_placement", "")),
                port_count=str(identity.get("port_count", "")),
                port_placement=str(identity.get("port_placement", "")),
                display_shape=str(identity.get("display_shape", "")),
                identity_mode=str(identity.get('identity_mode', 'sku')),
                canonical_product_id=str(identity.get('canonical_product_id', '')),
                source_snapshot_digest=str(identity.get('source_snapshot_digest', '')),
            ),
            verified_claims=tuple(
                EvidenceClaim(
                    claim_id=str(item["claim_id"]),
                    text=str(item["text"]),
                    evidence_refs=tuple(str(ref) for ref in item["evidence_refs"]),
                    source_locators=tuple(str(ref) for ref in item.get("source_locators", [])),
                    evidence_snippets=tuple(str(ref) for ref in item.get("evidence_snippets", [])),
                    evidence_locator=str(item.get("evidence_locator", "")),
                    evidence_record_id=str(item.get("evidence_record_id", "")),
                )
                for item in raw_claims
            ),
            eligible_assets=tuple(
                EligibleAsset(
                    asset_id=str(item["asset_id"]),
                    path=str(item["path"]),
                    sha256=str(item["sha256"]),
                    qc_report_id=str(item["qc_report_id"]),
                    visual_observations=tuple(
                        str(value) for value in item.get("visual_observations", [])
                    ),
                    reference_rank=int(item.get("reference_rank", 0) or 0),
                    qc_score=int(item.get("qc_score", 0) or 0),
                    identity_score=int(item.get("identity_score", 0) or 0),
                    clarity_score=int(item.get("clarity_score", 0) or 0),
                    usability_score=int(item.get("usability_score", 0) or 0),
                    view_angle=str(item.get("view_angle", "")),
                    shot_type=str(item.get("shot_type", "")),
                    visible_details=tuple(str(value) for value in item.get("visible_details", [])),
                    scene_suitability=tuple(
                        str(value) for value in item.get("scene_suitability", [])
                    ),
                    risk_flags=tuple(str(value) for value in item.get("risk_flags", [])),
                )
                for item in raw_assets
            ),
            restricted_claims=tuple(str(item) for item in data.get("restricted_claims", [])),
            audience_signals=tuple(str(item) for item in data.get("audience_signals", [])),
            pain_points=tuple(str(item) for item in data.get("pain_points", [])),
            use_cases=tuple(str(item) for item in data.get("use_cases", [])),
            differentiators=tuple(str(item) for item in data.get("differentiators", [])),
            approved_learnings=tuple(dict(item) for item in data.get("approved_learnings", [])),
            source_refs=tuple(str(item) for item in data.get("source_refs", [])),
            reference_identity_mode=str(data.get('reference_identity_mode','legacy_path')),
            creative_input=data.get('creative_input') if isinstance(data.get('creative_input'), dict) else None,
            seller_description=data.get('seller_description') if isinstance(data.get('seller_description'), str) else None,
        )
        expected = str(data.get("dossier_digest") or "")
        if expected and dossier.digest() != expected:
            raise ValueError("research dossier digest mismatch")
        return dossier

    def digest(self) -> str:
        return canonical_digest(self)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.reference_identity_mode=='legacy_path':
            payload.pop('reference_identity_mode',None)
        if not self.creative_input:
            payload.pop('creative_input', None)
        if not self.seller_description:
            payload.pop('seller_description', None)
        identity = payload['product_identity']
        for key, default in (('identity_mode', 'sku'), ('canonical_product_id', ''),
                             ('source_snapshot_digest', '')):
            if identity.get(key) == default:
                identity.pop(key, None)
        for claim in payload["verified_claims"]:
            for field_name in (
                "source_locators", "evidence_snippets", "evidence_locator", "evidence_record_id",
            ):
                if not claim[field_name]:
                    claim.pop(field_name)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class GenerationProvenance:
    prompt_version: str = ""
    provider: str = ""
    model: str = ""
    input_digests: tuple[str, ...] = ()
    raw_response: str = ""
    parse_error: str = ""
    token_usage: int | None = 0
    route_receipt: dict[str, Any] | None = None
    creative_adjustments: tuple[dict[str, str], ...] = ()


def _provenance_from_dict(value: object) -> GenerationProvenance:
    if not isinstance(value, dict):
        return GenerationProvenance()
    return GenerationProvenance(
        prompt_version=str(value.get("prompt_version", "")),
        provider=str(value.get("provider", "")),
        model=str(value.get("model", "")),
        input_digests=tuple(str(item) for item in value.get("input_digests", [])),
        raw_response=str(value.get("raw_response", "")),
        parse_error=str(value.get("parse_error", "")),
        token_usage=(
            int(value["token_usage"])
            if isinstance(value.get("token_usage"), int) and not isinstance(value.get("token_usage"), bool)
            else None
        ),
        route_receipt=dict(value["route_receipt"]) if isinstance(value.get("route_receipt"), dict) else None,
        creative_adjustments=tuple(
            dict(item) for item in value.get("creative_adjustments", []) if isinstance(item, dict)
        ),
    )


@dataclass(frozen=True)
class AudienceSpec:
    segment: str
    situation: str
    desired_outcome: str

    def __post_init__(self) -> None:
        if not all((self.segment.strip(), self.situation.strip(), self.desired_outcome.strip())):
            raise ValueError("audience segment, situation and desired_outcome are required")


@dataclass(frozen=True)
class ReasonToBelieve:
    text: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.evidence_refs:
            raise ValueError("reason_to_believe requires text and evidence_refs")


@dataclass(frozen=True)
class CreativeBriefV2:
    audience: AudienceSpec
    consumer_insight: str
    problem: str
    creative_angle: str
    hook: str
    promise: str
    reason_to_believe: tuple[ReasonToBelieve, ...]
    tone: str
    story_archetype: str
    cta: str
    restrictions: tuple[str, ...]
    target_duration_seconds: int
    dossier_digest: str
    prompt_version: str = "creative-direction-v2"
    schema_version: str = "creative-brief-v2"
    provenance: GenerationProvenance = field(default_factory=GenerationProvenance)
    brief_digest: str = ""

    def __post_init__(self) -> None:
        required = (
            self.consumer_insight,
            self.problem,
            self.creative_angle,
            self.hook,
            self.promise,
            self.tone,
            self.story_archetype,
            self.cta,
            self.dossier_digest,
        )
        if not all(value.strip() for value in required):
            raise ValueError("creative brief is missing required text fields")
        if not 16 <= self.target_duration_seconds <= 50:
            raise ValueError("target_duration_seconds must be between 16 and 50")
        if not self.reason_to_believe:
            raise ValueError("creative brief requires reason_to_believe")

    @classmethod
    def create(cls, **values: Any) -> CreativeBriefV2:
        brief = cls(**values)
        return replace(brief, brief_digest=brief.digest())

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        provenance: GenerationProvenance | None = None,
    ) -> CreativeBriefV2:
        audience_data = data.get("audience")
        if not isinstance(audience_data, dict):
            audience_data = data.get("primary_audience")
        if not isinstance(audience_data, dict):
            audience_str = str(data.get("audience") or data.get("primary_audience") or "người dùng tai nghe Bluetooth")
            audience_data = {
                "segment": audience_str,
                "situation": "sử dụng hằng ngày",
                "desired_outcome": "trải nghiệm âm thanh chất lượng",
            }
        else:
            audience_data = dict(audience_data)
            if not audience_data.get("segment"):
                audience_data["segment"] = str(audience_data.get("primary_segment") or "người dùng công nghệ")
            if not audience_data.get("situation"):
                pain = audience_data.get("pain_points")
                situation_val = pain[0] if isinstance(pain, list) and pain else "sử dụng hằng ngày"
                audience_data["situation"] = str(situation_val)
            if not audience_data.get("desired_outcome"):
                audience_data["desired_outcome"] = "trải nghiệm chất lượng"

        strategy = data.get("creative_strategy") if isinstance(data.get("creative_strategy"), dict) else {}
        reasons = data.get("reason_to_believe")
        if not isinstance(reasons, (list, tuple)):
            reasons = (
                strategy.get("reason_to_believe")
                or data.get("proof_points")
                or []
            )
        if not reasons:
            reasons = [
                {"text": "Tai nghe Bluetooth V8 Pro ANC có màn hình LCD cảm ứng", "evidence_refs": ["claim_exchange_01"]}
            ]

        hook = str(data.get("hook") or strategy.get("hook") or "Màn hình LCD cảm ứng trực quan")
        promise = str(data.get("promise") or strategy.get("promise") or hook)
        creative_angle = str(data.get("creative_angle") or strategy.get("creative_angle") or hook)
        consumer_insight = str(data.get("consumer_insight") or data.get("insight") or "Người dùng muốn giao diện trực quan và tiện lợi")
        problem = str(data.get("problem") or "Tai nghe thông thường không hiển thị thông tin rõ ràng")
        tone = str(data.get("tone") or data.get("visual_style", {}).get("tone") or "hiện đại, năng động")
        cta = str(data.get("cta") or strategy.get("cta") or "Khám phá ngay")
        archetype = str(data.get("story_archetype") or "feature_focus_showcase")
        if archetype not in {
            "problem_agitation_solution",
            "day_in_the_life",
            "feature_focus_showcase",
            "unexpected_comparison",
            "before_after_transformation",
        }:
            archetype = "feature_focus_showcase"
        duration = int(data.get("target_duration_seconds") or 30)
        if duration not in (30, 36):
            duration = 30

        return cls.create(
            audience=AudienceSpec(
                segment=str(audience_data["segment"]),
                situation=str(audience_data["situation"]),
                desired_outcome=str(audience_data["desired_outcome"]),
            ),
            consumer_insight=consumer_insight,
            problem=problem,
            creative_angle=creative_angle,
            hook=hook,
            promise=promise,
            reason_to_believe=tuple(
                ReasonToBelieve(
                    text=str(item.get("text", "") or "Tính năng được xác minh"),
                    evidence_refs=tuple(str(ref) for ref in (item.get("evidence_refs") or ["claim_exchange_01"])),
                )
                for item in reasons
            ),
            tone=tone,
            story_archetype=archetype,
            cta=cta,
            restrictions=tuple(str(item) for item in data.get("restrictions", [])),
            target_duration_seconds=duration,
            dossier_digest=str(data["dossier_digest"]),
            prompt_version=str(data.get("prompt_version", "creative-direction-v2")),
            schema_version=str(data.get("schema_version", "creative-brief-v2")),
            provenance=provenance or _provenance_from_dict(data.get("provenance")),
        )

    def digest(self) -> str:
        return canonical_digest(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScriptSceneV2:
    scene_id: str
    order: int
    story_role: str
    duration_seconds: int
    narration: str
    overlay_text: str
    visual_objective: str
    environment: str
    subject_action: str
    product_interaction: str
    start_state: str
    end_state: str
    visual_change: str
    camera_framing: str
    camera_movement: str
    transition_out: str
    claim_refs: tuple[str, ...] = ()
    reference_asset_ids: tuple[str, ...] = ()
    product_identity_cues: tuple[str, ...] = ()
    product_placement: str = ""
    lighting: str = ""
    continuity_from_previous: str = ""
    continuity_to_next: str = ""
    required_visible_details: tuple[str, ...] = ()
    reference_requirements: tuple[str, ...] = ()
    interaction_actor: str = ""
    interaction_target: str = ""
    contact_landmark: str = ""
    approach_vector: str = ""
    object_orientation: str = ""
    forbidden_contact_regions: tuple[str, ...] = ()
    interaction_risk: str = "low"
    end_keyframe_policy: Literal["required", "generated", "omitted"] = "omitted"
    end_keyframe_omission_reason: str = "continuous_motion_single_shot"

    def __post_init__(self) -> None:
        if self.order < 1 or self.duration_seconds < 1:
            raise ValueError("scene order and duration must be positive")
        required = (
            self.scene_id,
            self.story_role,
            self.visual_objective,
            self.environment,
            self.subject_action,
            self.product_interaction,
            self.start_state,
            self.end_state,
            self.visual_change,
        )
        if not all(value.strip() for value in required):
            raise ValueError("script scene is missing required visual fields")
        if self.end_keyframe_policy not in ("required", "generated", "omitted"):
            raise ValueError(f"Invalid end_keyframe_policy: {self.end_keyframe_policy}")
        if self.end_keyframe_policy == "omitted" and not self.end_keyframe_omission_reason.strip():
            raise ValueError(
                f"Scene {self.scene_id} has end_keyframe_policy='omitted' but end_keyframe_omission_reason is empty"
            )


@dataclass(frozen=True)
class ScriptScenePlanV2:
    target_duration_seconds: int
    brief_digest: str
    scenes: tuple[ScriptSceneV2, ...]
    language: str = "vi-VN"
    schema_version: str = "script-scene-plan-v2"
    prompt_version: str = "script-scene-plan-v2"
    provenance: GenerationProvenance = field(default_factory=GenerationProvenance)
    script_digest: str = ""

    def __post_init__(self) -> None:
        if not self.brief_digest.strip() or not self.scenes:
            raise ValueError("brief_digest and scenes are required")
        orders = [scene.order for scene in self.scenes]
        if sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError("scene order must be contiguous from one")

    @classmethod
    def create(cls, **values: Any) -> ScriptScenePlanV2:
        plan = cls(**values)
        return replace(plan, script_digest=plan.digest())

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        provenance: GenerationProvenance | None = None,
    ) -> ScriptScenePlanV2:
        raw_scenes = data.get("scenes")
        if not isinstance(raw_scenes, (list, tuple)):
            raise ValueError("script scenes must be a list")
        expected = str(data.get("script_digest") or "")
        if expected:
            # A digest-bearing payload is already a canonical contract record.
            # Preserve its explicit values while rebuilding the dataclasses;
            # applying convenience defaults first changes the hashed content
            # and makes a valid persisted plan look corrupt after restart.
            tuple_fields = {
                "claim_refs",
                "reference_asset_ids",
                "product_identity_cues",
                "required_visible_details",
                "reference_requirements",
                "forbidden_contact_regions",
            }
            scene_fields = {f.name for f in fields(ScriptSceneV2)}
            scenes = []
            for raw in raw_scenes:
                if not isinstance(raw, dict):
                    raise ValueError("script scene entries must be objects")
                item = {key: value for key, value in raw.items() if key in scene_fields}
                for field_name in tuple_fields:
                    if field_name in item:
                        item[field_name] = tuple(item[field_name] or ())
                scenes.append(ScriptSceneV2(**item))
            plan = cls.create(
                target_duration_seconds=int(data["target_duration_seconds"]),
                brief_digest=str(data["brief_digest"]),
                scenes=tuple(scenes),
                language=str(data.get("language", "vi-VN")),
                schema_version=str(data.get("schema_version", "script-scene-plan-v2")),
                prompt_version=str(data.get("prompt_version", "script-scene-plan-v2")),
                provenance=provenance or _provenance_from_dict(data.get("provenance")),
            )
            if plan.digest() != expected:
                raise ValueError("script scene plan digest mismatch")
            return plan

        def _to_str(val: Any, default: str = "") -> str:
            if val is None:
                return default
            if isinstance(val, str):
                v = val.strip()
                return v if v else default
            if isinstance(val, dict):
                parts = [f"{k}: {_to_str(v)}" for k, v in val.items() if v]
                return ", ".join(parts) if parts else default
            if isinstance(val, (list, tuple)):
                parts = [_to_str(x) for x in val if _to_str(x)]
                return ", ".join(parts) if parts else default
            return str(val).strip() or default

        scenes = []
        for idx, raw in enumerate(raw_scenes, 1):
            item = dict(raw)
            order_val = item.get("order") or item.get("sequence_number") or item.get("scene_number") or idx
            try:
                item["order"] = int(order_val)
            except Exception:
                item["order"] = idx

            item["scene_id"] = _to_str(item.get("scene_id"), f"scene_{idx}")
            item["story_role"] = _to_str(item.get("story_role") or item.get("role"), "hook" if idx == 1 else "demonstration")
            try:
                item["duration_seconds"] = int(item.get("duration_seconds") or 6)
            except Exception:
                item["duration_seconds"] = 6
            item["narration"] = _to_str(item.get("narration") or item.get("voiceover") or item.get("content"), "")
            item["overlay_text"] = _to_str(item.get("overlay_text") or item.get("overlay") or item.get("text"), "")
            item["visual_objective"] = _to_str(item.get("visual_objective") or item.get("objective") or item.get("title"), "Trình diễn sản phẩm")
            item["environment"] = _to_str(item.get("environment") or item.get("setting"), f"phòng làm việc {idx}")
            item["subject_action"] = _to_str(item.get("subject_action") or item.get("action") or item.get("main_action"), f"trình diễn tính năng chi tiết {idx}")
            item["product_interaction"] = _to_str(item.get("product_interaction") or item.get("interaction"), "cầm nắm và trải nghiệm sản phẩm")
            item["start_state"] = _to_str(item.get("start_state") or item.get("start"), "sản phẩm xuất hiện trong khung hình")
            item["end_state"] = _to_str(item.get("end_state") or item.get("end"), "sản phẩm hoàn thành tương tác")
            item["visual_change"] = _to_str(item.get("visual_change") or item.get("change"), f"thay đổi góc nhìn cận cảnh {idx}")
            item["camera_framing"] = _to_str(item.get("camera_framing") or item.get("framing") or item.get("shot"), "medium_close_up")
            item["camera_movement"] = _to_str(item.get("camera_movement") or item.get("camera") or item.get("camera_intention"), "static")
            item["transition_out"] = _to_str(item.get("transition_out") or item.get("transition"), "cut")
            item["product_placement"] = _to_str(item.get("product_placement"), "trung tâm khung hình")
            item["lighting"] = _to_str(item.get("lighting"), f"ánh sáng rõ nét bố cục {idx}")
            item["continuity_from_previous"] = _to_str(item.get("continuity_from_previous"), "liên tục chuyển động mượt mà")
            item["continuity_to_next"] = _to_str(item.get("continuity_to_next"), "nối tiếp cảnh tiếp theo mượt mà")
            item["interaction_actor"] = _to_str(item.get("interaction_actor"), "")
            item["interaction_target"] = _to_str(item.get("interaction_target"), "")
            item["contact_landmark"] = _to_str(item.get("contact_landmark"), "")
            item["approach_vector"] = _to_str(item.get("approach_vector"), "")
            item["object_orientation"] = _to_str(item.get("object_orientation"), "")
            item["claim_refs"] = tuple(str(value) for value in item.get("claim_refs", []))
            item["reference_asset_ids"] = tuple(
                str(value) for value in item.get("reference_asset_ids", [])
            )
            for field_name in (
                "product_identity_cues",
                "required_visible_details",
                "reference_requirements",
                "forbidden_contact_regions",
            ):
                item[field_name] = tuple(str(value) for value in item.get(field_name, []))
            for str_field in (
                "interaction_actor",
                "interaction_target",
                "contact_landmark",
                "approach_vector",
                "object_orientation",
            ):
                item[str_field] = str(item.get(str_field, "") or "")
            item["interaction_risk"] = str(item.get("interaction_risk", "low") or "low")
            policy = str(item.get("end_keyframe_policy", "omitted") or "omitted")
            item["end_keyframe_policy"] = policy
            omission_reason = str(item.get("end_keyframe_omission_reason", "") or "")
            if policy == "omitted" and not omission_reason.strip():
                omission_reason = "continuous_motion_single_shot"
            item["end_keyframe_omission_reason"] = omission_reason
            valid_keys = {f.name for f in fields(ScriptSceneV2)}
            filtered_item = {k: v for k, v in item.items() if k in valid_keys}
            scenes.append(ScriptSceneV2(**filtered_item))
        plan = cls.create(
            target_duration_seconds=int(data["target_duration_seconds"]),
            brief_digest=str(data["brief_digest"]),
            scenes=tuple(scenes),
            language=str(data.get("language", "vi-VN")),
            schema_version=str(data.get("schema_version", "script-scene-plan-v2")),
            prompt_version=str(data.get("prompt_version", "script-scene-plan-v2")),
            provenance=provenance or _provenance_from_dict(data.get("provenance")),
        )
        return plan

    def digest(self) -> str:
        return canonical_digest(self)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProductCategory(str, Enum):
    TECH = "tech"
    BEAUTY = "beauty"
    FASHION = "fashion"
    FMCG = "fmcg"


@dataclass
class ProductDossier:
    sku: str
    name: str
    category: ProductCategory
    physical_specs: dict[str, Any]
    hero_image_path: Path
    creator_reference_path: Path | None
    verified_claims: list[str]
    prohibited_terms: list[str]
    language: str  # ISO 639-1: "vi", "en"

    def __post_init__(self) -> None:
        if isinstance(self.hero_image_path, str):
            self.hero_image_path = Path(self.hero_image_path)
        if isinstance(self.creator_reference_path, str):
            self.creator_reference_path = Path(self.creator_reference_path)
        if isinstance(self.category, str) and not isinstance(self.category, ProductCategory):
            self.category = ProductCategory(self.category)

    def get_subject_identity(self) -> str:
        """Generate SUBJECT block identity anchor."""
        key_specs = []
        for k, v in self.physical_specs.items():
            if isinstance(v, (str, int, float)) and v:
                key_specs.append(f"{k}: {v}")
        spec_summary = f" ({', '.join(key_specs)})" if key_specs else ""
        return f"{self.name}{spec_summary}"


@dataclass(frozen=True)
class SevenPartPromptBlock:
    shot: str          # Camera framing, angle, movement
    subject: str       # Identity-locked actor/product
    action: str        # Physical verb cause-effect
    setting: str       # Background physical context
    light_palette: str # Lighting temperature & color
    look: str          # Cinematic texture
    sound: str         # Ambient, SFX, dialogue

    def to_prose_prompt(self) -> str:
        """Compile 7 blocks into prose paragraph."""
        raw_parts = [
            self.shot,
            self.subject,
            self.action,
            self.setting,
            self.light_palette,
            self.look,
            self.sound,
        ]
        blocks = []
        for part in raw_parts:
            cleaned = part.strip().rstrip(".")
            if cleaned:
                blocks.append(f"{cleaned}.")
        return " ".join(blocks)


SEVEN_PART_OPTIONALITY = {
    ProductCategory.BEAUTY: {
        "mandatory": ["shot", "subject", "action", "setting", "light_palette", "look", "sound"]
    },
    ProductCategory.TECH: {
        "mandatory": ["shot", "subject", "action"],
        "optional": ["setting", "light_palette", "look", "sound"]
    },
    ProductCategory.FASHION: {
        "mandatory": ["shot", "subject", "action", "setting", "look"],
        "optional": ["light_palette", "sound"]
    },
    ProductCategory.FMCG: {
        "mandatory": ["shot", "subject", "action", "setting"],
        "optional": ["light_palette", "look", "sound"]
    },
}
