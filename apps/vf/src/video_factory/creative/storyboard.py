"""Deterministic compilation of approved scenes into grounded keyframe requests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from video_factory.integrations.ports import KeyframeGenerationRequest

from .contracts import ResearchDossier, ScriptScenePlanV2


KEYFRAME_NEGATIVE_PROMPT = (
    "seller or shop branding, source watermark, marketplace UI, price badge, rating badge, "
    "social handle, shipping label, unrelated logo, copied product-listing layout, malformed text, "
    "wrong brand, wrong model, wrong variant, changed color, changed material, changed logo, "
    "changed product shape, distorted geometry, duplicate product, duplicated product, extra product, "
    "floating object, missing control, missing identity detail, cropped identity detail, extra limbs, "
    "cable entering screen, cable entering forbidden region, wrong port placement, wrong display shape, "
    "wrong control placement, black bars, letterboxing, square composition"
)


class StoryboardContractError(ValueError):
    """A deterministic input or persisted storyboard contract violation."""


def storyboard_package_digest(package: dict[str, object]) -> str:
    """Bind QC approval to every creative and generated-keyframe input."""

    payload = {
        "schema_version": package.get("schema_version"),
        "workflow_id": package.get("workflow_id"),
        "project_id": package.get("project_id"),
        "script": package.get("script"),
        "scenes": package.get("scenes"),
        "storyboard_frames": package.get("storyboard_frames"),
        "generated_keyframes": package.get("generated_keyframes"),
        "recovery_attempt": package.get("recovery_attempt", 0),
        "recovery_plan": package.get("recovery_plan"),
        "end_keyframe_omissions": package.get("end_keyframe_omissions", []),
    }
    if package.get('creative_mode') == 'four_agent':
        for key in ('scene_prompts', 'backend_profile', 'preproduction_review', 'frame_reviews'):
            payload[key] = package.get(key)
        # Board review binds this digest, so it must not be included in its own input.
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bind_recovery_reference_revisions(
    recovery_plan: dict[str, Any] | None,
    *,
    script: ScriptScenePlanV2,
    prompt_bundle: Any,
    dossier: ResearchDossier,
) -> dict[str, Any] | None:
    """Bind targeted reference reuse to the approved scene refs and their current bytes."""
    if not isinstance(recovery_plan, dict) or prompt_bundle is None:
        return recovery_plan
    assets = {asset.asset_id: asset for asset in dossier.eligible_assets}
    scenes = {scene.scene_id: scene for scene in script.scenes}
    revised = dict(recovery_plan)
    revised["frames"] = [dict(frame) for frame in recovery_plan.get("frames", [])]
    for frame in revised["frames"]:
        if not isinstance(frame, dict) or "reselect_reference" not in frame.get("actions", []):
            continue
        frame_id = str(frame.get("frame_id") or "")
        scene_id = frame_id.removesuffix("-start").removesuffix("-end")
        scene = scenes.get(scene_id)
        if scene is None:
            raise ValueError(f"reference revision names unknown scene: {scene_id}")
        bound_ids = tuple(prompt_bundle.for_scene(scene_id).reference_asset_ids)
        if not bound_ids or bound_ids != tuple(scene.reference_asset_ids):
            raise ValueError(f"reference revision has stale Director binding for {scene_id}")
        hashes = {}
        for asset_id in bound_ids:
            asset = assets.get(asset_id)
            if asset is None:
                raise ValueError(f"reference revision names non-eligible asset: {asset_id}")
            path = Path(asset.path).resolve()
            if not path.is_file() or _file_sha256(path) != asset.sha256.removeprefix("sha256:"):
                raise ValueError(f"reference revision source SHA is missing or stale: {asset_id}")
            hashes[asset_id] = asset.sha256.removeprefix("sha256:")
        binding = {"script_digest": script.digest(), "scene_id": scene_id,
                   "asset_ids": list(bound_ids), "asset_sha256": hashes}
        frame["reference_revision"] = {
            **binding,
            "revision_digest": "sha256:" + hashlib.sha256(
                json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    return revised


def select_best_keyframe_candidate(candidates: list[dict[str, object]]) -> dict[str, object] | None:
    """Choose the highest-ranked durable PASS candidate for one logical frame.

    Eligibility is fail-closed on QC identity evidence and current file SHA. Scores
    only rank candidates after these checks; ties preserve the earlier candidate.
    """
    eligible: list[tuple[int, int, dict[str, object]]] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        findings = candidate.get("qc_findings")
        receipt = candidate.get("qc_receipt")
        path = Path(str(candidate.get("image_path") or ""))
        digest = str(candidate.get("sha256") or "").removeprefix("sha256:")
        try:
            valid_bytes = path.is_file() and bool(digest) and _file_sha256(path) == digest
        except OSError:
            valid_bytes = False
        if (
            candidate.get("qc_verdict") != "pass"
            or candidate.get("qc_hard_failure") is not False
            or not isinstance(findings, dict)
            or findings.get("identity_mismatch") is not False
            or not isinstance(receipt, dict)
            or not receipt.get("report_id")
            or not receipt.get("artifact_digest")
            or not valid_bytes
        ):
            continue
        try:
            score = int(candidate.get("qc_score", 0))
        except (TypeError, ValueError):
            continue
        eligible.append((score, -index, candidate))
    return max(eligible, key=lambda item: (item[0], item[1]))[2] if eligible else None


def _reference_score(asset: object, scene: object) -> tuple[int, int, int, str]:
    scene_text = " ".join(
        str(value)
        for value in (
            getattr(scene, "story_role", ""),
            getattr(scene, "visual_objective", ""),
            getattr(scene, "subject_action", ""),
            getattr(scene, "product_interaction", ""),
            getattr(scene, "interaction_target", ""),
            getattr(scene, "contact_landmark", ""),
            *getattr(scene, "reference_requirements", ()),
        )
    ).casefold()
    tags = (
        getattr(asset, "view_angle", ""),
        getattr(asset, "shot_type", ""),
        *getattr(asset, "scene_suitability", ()),
        *getattr(asset, "visible_details", ()),
    )
    semantic_matches = sum(
        1 for tag in tags if str(tag).strip() and str(tag).casefold() in scene_text
    )
    rank = int(getattr(asset, "reference_rank", 0) or 10_000)
    return (
        semantic_matches,
        int(getattr(asset, "qc_score", 0) or 0),
        -rank,
        str(getattr(asset, "asset_id", "")),
    )


def _select_scene_assets(assets: dict[str, object], scene: object) -> list[object]:
    requested = [
        assets[asset_id]
        for asset_id in getattr(scene, "reference_asset_ids", ())
        if asset_id in assets
    ]
    if requested:
        return requested[:2]
    candidates = list(assets.values())
    candidates.sort(key=lambda asset: _reference_score(asset, scene), reverse=True)
    return candidates[: min(2, len(candidates))]


def compile_keyframe_requests(
    *,
    workflow_id: str,
    project_id: str,
    dossier: ResearchDossier,
    script: ScriptScenePlanV2,
    recovery_plan: dict[str, Any] | None = None,
    recovery_attempt: int = 0,
    previous_package: dict[str, Any] | None = None,
    prompt_bundle=None,
    backend_profile=None,
    legacy_implicit_end_recovery: bool = False,
) -> tuple[KeyframeGenerationRequest, ...]:
    if prompt_bundle is not None:
        from .agents.director import validate_bundle
        validate_bundle(prompt_bundle, script, backend_profile)
    assets = {asset.asset_id: asset for asset in dossier.eligible_assets}
    product = dossier.product_identity
    description = " ".join(
        value
        for value in (
            product.brand,
            product.model,
            product.variant,
            product.shape,
            product.color,
            product.materials,
            product.logo_placement,
            product.silhouette,
            product.main_proportions,
            product.product_count,
            product.control_placement,
            product.port_count,
            product.port_placement,
            product.display_shape,
            *product.distinctive_features,
        )
        if value
    )
    requests = []
    recovery_by_frame = {
        str(item.get("frame_id")): item
        for item in (recovery_plan or {}).get("frames", [])
        if isinstance(item, dict) and item.get("frame_id")
    }
    operator_stepwise = isinstance(recovery_plan, dict) and bool(recovery_plan.get("operator_stepwise"))
    approved_end_frames = {
        f"{scene.scene_id}-end" for scene in script.scenes
        if scene.end_keyframe_policy in {"generated", "required"}
    }
    for frame_id in tuple(recovery_by_frame):
        if frame_id.endswith("-start") and not operator_stepwise:
            end_frame_id = f"{frame_id.removesuffix('-start')}-end"
            if end_frame_id not in approved_end_frames and not legacy_implicit_end_recovery:
                continue
            recovery_by_frame.setdefault(
                end_frame_id,
                {
                    "frame_id": end_frame_id,
                    "actions": ["regenerate_keyframe", "rerun_keyframe_qc"],
                    "feedback": "START frame was regenerated; update END frame to match new START frame",
                },
            )
    previous_by_frame = {
        str(item.get("frame_id")): item
        for item in (previous_package or {}).get("generated_keyframes", [])
        if isinstance(item, dict) and item.get("frame_id")
    }
    for scene in script.scenes:
        frame_id = f"{scene.scene_id}-start"
        recovery = recovery_by_frame.get(frame_id)
        recovery_actions = tuple(str(value) for value in (recovery or {}).get("actions", []))
        recovery_feedback = str((recovery or {}).get("feedback") or "").strip()
        references = []
        unknown_ids = set(scene.reference_asset_ids) - set(assets)
        if unknown_ids:
            raise ValueError(f"scene references a non-eligible asset: {sorted(unknown_ids)[0]}")
        selected_assets = _select_scene_assets(assets, scene)
        if prompt_bundle is not None:
            selected_assets = [assets[asset_id] for asset_id in prompt_bundle.for_scene(scene.scene_id).reference_asset_ids]
        if recovery and "reselect_reference" in recovery_actions:
            if prompt_bundle is not None:
                revision = recovery.get("reference_revision")
                if (not isinstance(revision, dict)
                        or revision.get("script_digest") != script.digest()
                        or revision.get("scene_id") != scene.scene_id
                        or tuple(revision.get("asset_ids") or ()) != tuple(prompt_bundle.for_scene(scene.scene_id).reference_asset_ids)):
                    raise ValueError(f"stale or missing reference revision for {scene.scene_id}")
                expected_hashes = {asset_id: assets[asset_id].sha256.removeprefix("sha256:")
                                   for asset_id in revision["asset_ids"] if asset_id in assets}
                binding = {"script_digest": revision.get("script_digest"),
                           "scene_id": revision.get("scene_id"),
                           "asset_ids": list(revision.get("asset_ids") or ()),
                           "asset_sha256": revision.get("asset_sha256")}
                revision_digest = "sha256:" + hashlib.sha256(
                    json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if (expected_hashes != revision.get("asset_sha256")
                        or revision.get("revision_digest") != revision_digest
                        or len(expected_hashes) != len(revision["asset_ids"])):
                    raise ValueError(f"reference revision SHA mismatch for {scene.scene_id}")
                selected_assets = [assets[asset_id] for asset_id in revision["asset_ids"]]
            else:
                previous_paths = {
                    str(Path(str(value)).resolve())
                    for value in previous_by_frame.get(frame_id, {}).get(
                        "reference_image_paths", []
                    )
                }
                all_candidates = sorted(
                    assets.values(),
                    key=lambda asset: _reference_score(asset, scene),
                    reverse=True,
                )
                unseen = [
                    asset
                    for asset in all_candidates
                    if str(Path(str(getattr(asset, "path", ""))).resolve()) not in previous_paths
                ]
                selected_assets = (unseen + [asset for asset in all_candidates if asset not in unseen])[
                    : min(2, len(all_candidates))
                ]
        for asset in selected_assets:
            asset_id = asset.asset_id
            path = Path(asset.path).resolve()
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"eligible reference is missing: {asset_id}")
            if _file_sha256(path) != asset.sha256.removeprefix("sha256:"):
                raise ValueError(f"eligible reference digest changed: {asset_id}")
            references.append(str(path))
        if not references:
            raise ValueError(f"scene has no eligible product reference: {scene.scene_id}")
        operation_payload = (
            f"{workflow_id}|{project_id}|{script.digest()}|{scene.scene_id}|start-keyframe-v2"
        )
        if recovery:
            operation_payload += (
                f"|recovery:{recovery_attempt}|{','.join(recovery_actions)}|{recovery_feedback}"
            )
            revision = recovery.get("reference_revision")
            if isinstance(revision, dict):
                operation_payload += f"|reference:{revision.get('revision_digest', '')}"
        operation_id = "keyframe-" + hashlib.sha256(operation_payload.encode()).hexdigest()[:24]
        reference_profiles = "; ".join(
            f"[{index}] {asset.asset_id}: angle={asset.view_angle or 'unspecified'}, "
            f"shot={asset.shot_type or 'unspecified'}, visible={', '.join(asset.visible_details) or 'unspecified'}"
            for index, asset in enumerate(selected_assets, start=1)
        )
        identity_cues = ", ".join(
            scene.product_identity_cues
            or tuple(
                value
                for value in (
                    product.shape,
                    product.color,
                    product.materials,
                    product.logo_placement,
                    product.silhouette,
                    product.product_count,
                    product.control_placement,
                    product.port_placement,
                    product.display_shape,
                    *product.distinctive_features,
                )
                if value
            )
        )
        required_details = ", ".join(scene.required_visible_details)
        interaction_cues = []
        if scene.contact_landmark:
            interaction_cues.append(f"Contact landmark: {scene.contact_landmark}")
        if scene.approach_vector:
            interaction_cues.append(f"Approach vector: {scene.approach_vector}")
        if scene.object_orientation:
            interaction_cues.append(f"Orientation: {scene.object_orientation}")
        if scene.forbidden_contact_regions:
            interaction_cues.append(f"Forbidden contact regions: {', '.join(scene.forbidden_contact_regions)}")
        interaction_text = f"\nInteraction geometry: {'; '.join(interaction_cues)}." if interaction_cues else ""

        product_outside_start = any(
            phrase in scene.start_state.casefold()
            for phrase in ("out of frame", "outside the frame", "not visible")
        )
        start_subject = (
            f"the environment immediately before the exact product {product.display_name} ({description}) enters the shot"
            if product_outside_start
            else f"the exact product {product.display_name} ({description})"
        )
        start_presence = (
            "This START image shows the empty tabletop before the action. No speaker, hand, glass, "
            "or other object is visible on or above the table. The product reference is supplied "
            "only to identify the object that will enter in the later video; do not reproduce it in this image. "
            "The product and the hand holding it must remain outside the image at this START moment."
            if product_outside_start
            else "Show exactly one product unit; do not add a duplicate, replacement model, wrong variant or unrelated product."
        )
        prompt = f"""Create one photorealistic, native full-bleed 9:16 portrait keyframe of {start_subject}.
Fill the entire canvas with one continuous photographic scene: no black bars, padding, borders, inset square image, collage, or split screen.
This is the START frame for scene {scene.order} ({scene.story_role}).
Approved reference profiles: {reference_profiles}.
Immutable identity cues: {identity_cues or 'match the approved references exactly'}.
Environment: {scene.environment}.
Visual objective: {scene.visual_objective}.
Subject action that is about to happen: {scene.subject_action}.
Product interaction: {scene.product_interaction}.{interaction_text}
Exact visible start state: {scene.start_state}.
The later end state will be: {scene.end_state}.
Frame as: {scene.camera_framing}. Camera intent: {scene.camera_movement}.
Product placement: {('outside the frame at this start moment' if product_outside_start else scene.product_placement or 'keep the complete product clearly visible and unobstructed')}.
Lighting: {scene.lighting or 'natural commercial lighting that preserves true product color and material'}.
Continuity into this scene: {scene.continuity_from_previous or 'preserve the approved product unit'}.
Continuity out of this scene: {scene.continuity_to_next or 'keep product identity and orientation stable'}.
Required visible details: {('the environment and the empty placement area; product details appear only after entry' if product_outside_start else required_details or identity_cues or 'all identity-critical product details')}.
Preserve the exact product identity from approved references: package shape, primary colors, clearly readable large logo/product/variant text, readable weight/size, and distinguishing marks. Do not invent a variant, claim, product, or new physical detail.
Preserve exact proportions, variant, color, material, controls, logo placement and distinctive details from the references.
Reproduce only large product text that is clearly readable in the approved front reference. Do not invent, approximate, or try to reconstruct small unreadable package text, ingredients, or barcodes; leave such print naturally indistinct.
Create a believable real-use situation with people/hands only when required by the action.
Render one continuous photographic moment in a single portrait frame; never create a collage, panels, split screen, or a sequence of views.
Do not place promotional copy, captions, prices, badges or UI inside the image.
{start_presence}
This image is a visual storyboard reference for the later scene video, not a final frame with text."""
        if prompt_bundle is not None:
            prompt = prompt_bundle.for_scene(scene.scene_id).start_prompt
            from .contracts import canonical_digest
            operation_id = 'keyframe-' + canonical_digest({
                'workflow': workflow_id, 'project': project_id, 'frame':frame_id,
                'prompt':prompt, 'references':[(a.asset_id, a.sha256) for a in selected_assets],
                'profile':backend_profile.digest, 'playbook':prompt_bundle.playbook_digest,
                'source':dossier.digest(),'start_state':scene.start_state,'action':scene.subject_action,
                'negative_prompt':prompt_bundle.for_scene(scene.scene_id).negative_prompt,
                'recovery':recovery_attempt if recovery else 0,
            }).removeprefix('sha256:')[:24]
        if recovery:
            prompt += _keyframe_recovery_instructions(
                recovery_feedback,
                recovery_actions,
                recovery_attempt=recovery_attempt,
                frame_role="start",
            )
        requests.append(
            KeyframeGenerationRequest(
                operation_id=operation_id,
                workflow_id=workflow_id,
                project_id=project_id,
                frame_id=frame_id,
                prompt=prompt,
                product_description=description,
                reference_image_paths=tuple(references),
                aspect_ratio="9:16",
                negative_prompt=(prompt_bundle.for_scene(scene.scene_id).negative_prompt if prompt_bundle else KEYFRAME_NEGATIVE_PROMPT),
                frame_role="start",
                provider_options=({'creative_mode':'four_agent','profile_digest':backend_profile.digest}
                                  if prompt_bundle else {}),
            )
        )

        # Explicitly model end keyframe or structured safety omission
        # If end keyframe is requested or planned, generate end keyframe request; otherwise record safety reason
        end_frame_id = f"{scene.scene_id}-end"
        end_recovery = recovery_by_frame.get(end_frame_id)
        end_requested = (
            scene.end_keyframe_policy in ("generated", "required")
            or (recovery and "generate_end_keyframe" in recovery_actions)
            or end_recovery is not None
        )
        if end_requested:
            end_op_payload = f"{workflow_id}|{project_id}|{script.digest()}|{scene.scene_id}|end-keyframe-v2"
            if end_recovery or (recovery and "generate_end_keyframe" in recovery_actions):
                end_op_payload += f"|recovery:{recovery_attempt}"
            end_op_id = "keyframe-" + hashlib.sha256(end_op_payload.encode()).hexdigest()[:24]
            end_prompt = f"""Create one photorealistic, native full-bleed 9:16 portrait keyframe.
Use the attached product photos for product identity and the attached START frame for scene continuity. The entire canvas is one continuous photographic scene: no black bars, padding, borders, inset square image, collage, or split screen.
This is the END moment of scene {scene.scene_id} ({scene.story_role}), in the same setting, background, lighting, and camera direction as START.
The approved visible change is: {scene.visual_change}.
The exact END state is: {scene.end_state}.
Show the exact approved product {product.display_name} ({description}) only as required by the END state.
Approved reference profiles: {reference_profiles}.
Immutable identity cues: {identity_cues or 'match the approved references exactly'}.
Environment: {scene.environment}.
Frame as: {scene.camera_framing}.
Lighting: {scene.lighting or 'natural commercial lighting'}.
Required visible details: {required_details or identity_cues or 'all identity-critical product details'}.
Keep the START frame's environment, background, lighting, camera direction, and product placement; change only the state/action expressly required above.
Preserve the package shape, primary colors, clearly readable large logo/product/variant text, readable weight/size, and other identity-critical details from the product photos. Do not invent a variant, product claim, physical detail, or prominent unreadable label text."""
            if prompt_bundle is not None:
                end_prompt = prompt_bundle.for_scene(scene.scene_id).end_prompt
                end_op_id = 'keyframe-' + canonical_digest({'start_operation':operation_id,
                    'prompt':end_prompt, 'frame':end_frame_id, 'profile':backend_profile.digest,
                    'end_state':scene.end_state,
                    'negative_prompt':prompt_bundle.for_scene(scene.scene_id).negative_prompt,
                    'recovery':recovery_attempt if end_recovery else 0,
                }).removeprefix('sha256:')[:24]
            if end_recovery:
                end_prompt += _keyframe_recovery_instructions(
                    str(end_recovery.get("feedback") or "correct the failed scene continuity"),
                    tuple(str(value) for value in end_recovery.get("actions", [])),
                    recovery_attempt=recovery_attempt,
                    frame_role="end",
                )
            requests.append(
                KeyframeGenerationRequest(
                    operation_id=end_op_id,
                    workflow_id=workflow_id,
                    project_id=project_id,
                    frame_id=end_frame_id,
                    prompt=end_prompt,
                    product_description=description,
                    reference_image_paths=tuple(references),
                    aspect_ratio="9:16",
                    negative_prompt=(prompt_bundle.for_scene(scene.scene_id).negative_prompt if prompt_bundle else KEYFRAME_NEGATIVE_PROMPT),
                    frame_role="end",
                    provider_options=({'creative_mode':'four_agent','profile_digest':backend_profile.digest}
                                      if prompt_bundle else {}),
                )
            )
    return tuple(requests)


def _keyframe_recovery_instructions(
    feedback: str,
    actions: tuple[str, ...],
    *,
    recovery_attempt: int,
    frame_role: str,
) -> str:
    """Turn a persisted QC finding into a targeted new-image prompt, not a QC rerun."""
    normalized_feedback = " ".join(str(feedback or "").split())
    lowered = normalized_feedback.casefold()
    guidance = [
        f"\nREGENERATION — Recovery attempt {recovery_attempt}: create a new image for this exact {frame_role.upper()} frame; do not reuse or merely reformat the failed image.",
        "Use the attached approved product photo(s) as the visual identity reference, not merely the text description. Preserve the approved SKU, visible package shape, primary colors, large readable logo/product/variant text, and clearly readable size markings.",
        f"The previous QC finding is diagnostic feedback only: {normalized_feedback or 'keyframe did not meet the approved scene/QC requirements'}. Correct that specific issue while retaining the approved scene plan, exact frame moment, and all identity/evidence constraints above.",
        f"Required correction actions: {', '.join(actions) or 'rewrite_prompt, regenerate_keyframe'}. Do not add product claims, physical details, text, people, props, or scene changes that are not supported by the approved plan and references.",
    ]
    aspect_or_bars = any(
        marker in lowered
        for marker in (
            "black bar", "letterbox", "pillarbox", "border", "padding", "inset square",
            "square image", "square frame", "aspect ratio", "9:16", "vertical frame",
        )
    )
    if aspect_or_bars:
        guidance.append(
            "FORMAT CORRECTION: create a native full-frame 9:16 vertical portrait image. Fill the entire canvas with one continuous photographic scene; no black bars, borders, padding, frame, inset square image, collage, or split screen. Do not place a square image on a vertical canvas."
        )
    if frame_role == "end":
        guidance.append(
            "For this END frame, use both the approved product photo(s) for identity and the attached START frame for continuity. Keep the START frame's setting, background, lighting, camera direction, and product placement; change only the visual state expressly required by the approved scene plan."
        )
    return "\n" + "\n".join(guidance)


def plan_initial_storyboard_recovery(
    recovery_plan: dict[str, Any] | None,
) -> tuple[set[str], dict[str, str]]:
    """Extract recovery frame IDs and feedback map with START->END cascade."""
    recovery_frame_ids = {
        str(item.get("frame_id"))
        for item in (recovery_plan or {}).get("frames", [])
        if isinstance(item, dict) and item.get("frame_id")
    } if isinstance(recovery_plan, dict) else set()
    feedback_map = {
        str(item.get("frame_id")): str(item.get("feedback") or item.get("reason") or "")
        for item in (recovery_plan or {}).get("frames", [])
        if isinstance(item, dict) and item.get("frame_id")
    } if isinstance(recovery_plan, dict) else {}
    # Cascade: if START is in recovery_frame_ids, dependent END must also be regenerated
    if isinstance(recovery_plan, dict) and recovery_plan.get("operator_stepwise"):
        return recovery_frame_ids, feedback_map
    for fid in list(recovery_frame_ids):
        if fid.endswith("-start"):
            sc_id = fid.removesuffix("-start")
            end_fid = f"{sc_id}-end"
            recovery_frame_ids.add(end_fid)
            if end_fid not in feedback_map:
                feedback_map[end_fid] = "START frame was regenerated; update END frame to match new START frame"
    return recovery_frame_ids, feedback_map


def prepare_keyframe_generation_request(
    request: KeyframeGenerationRequest, *, feedback: str = "", conditioning_start_path: str = "",
) -> KeyframeGenerationRequest:
    """Apply the same conditioning when submitting and reconciling a keyframe."""
    if conditioning_start_path:
        request = replace(
            request,
            prompt=(
                request.prompt
                + "\nThe first attached image is the approved START frame of this same scene. "
                "Keep its exact table material, shape, furniture layout, camera context, "
                "product surface and pool position. Show only the specified action and end state. "
                "If water is specified near but not on the product, keep the product completely dry."
            ),
            reference_image_paths=(conditioning_start_path, *request.reference_image_paths)[:4],
        )
    if feedback:
        request = replace(request, prompt=request.prompt + f"\n[QC REVISION GUIDANCE: {feedback}]")
    return request


def resolve_reusable_storyboard_frames(
    previous_package: dict[str, Any] | None,
    *,
    workflow_id: str,
    project_id: str,
    script: ScriptScenePlanV2,
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """Identify unchanged scenes and valid reusable frames from previous storyboard package."""
    previous_frames: dict[str, dict[str, Any]] = {}
    unchanged_scene_ids: set[str] = set()
    if isinstance(previous_package, dict) and (
        previous_package.get("workflow_id") == workflow_id
        and previous_package.get("project_id") == project_id
        and previous_package.get("package_digest") == storyboard_package_digest(previous_package)
        and isinstance(previous_package.get("script"), dict)
    ):
        old_script = ScriptScenePlanV2.from_dict(previous_package["script"])
        old_scenes = {scene.scene_id: scene for scene in old_script.scenes}
        unchanged_scene_ids = {
            scene.scene_id for scene in script.scenes
            if old_scenes.get(scene.scene_id) == scene
        }
        previous_frames = {
            str(item["frame_id"]): item
            for item in previous_package.get("generated_keyframes", [])
            if isinstance(item, dict) and item.get("frame_id")
        }
    return unchanged_scene_ids, previous_frames


def cascade_keyframe_qc_failures(
    failing_frame_ids: set[str] | list[str],
    feedback_by_frame: dict[str, str],
) -> tuple[set[str], dict[str, str]]:
    """Cascade START frame failures to dependent END frames."""
    cascaded_ids = set(failing_frame_ids)
    cascaded_feedback = dict(feedback_by_frame)
    for fid in list(cascaded_ids):
        if fid.endswith("-start"):
            sc_id = fid.removesuffix("-start")
            end_fid = f"{sc_id}-end"
            cascaded_ids.add(end_fid)
            if end_fid not in cascaded_feedback:
                cascaded_feedback[end_fid] = "START frame was regenerated; update END frame to match new START frame"
    return cascaded_ids, cascaded_feedback


def build_keyframe_recovery_request(
    target_req: KeyframeGenerationRequest,
    *,
    msg: str,
    start_cond_p: str,
    attempt_num: int,
    alt_reference_path: str = "",
) -> KeyframeGenerationRequest:
    """Construct a deterministic recovery request with augmented prompt and operation ID."""
    from dataclasses import replace

    aug_prompt = target_req.prompt + (f"\n[QC REVISION GUIDANCE: {msg}]" if msg else "")
    if "bicycle" in msg.lower() or "bike" in msg.lower() or "handlebar" in msg.lower():
        aug_prompt += "\nCRITICAL: Do NOT depict any bicycle, handlebars, or cycling elements. Setting must be strictly a bright sunny park with a picnic blanket on grass."

    rec_op_payload = (
        f"{target_req.workflow_id}|{target_req.project_id}|{target_req.frame_id}|recovery:{attempt_num}|{start_cond_p}|{alt_reference_path}|{aug_prompt}"
    )
    rec_op_id = "keyframe-" + hashlib.sha256(rec_op_payload.encode()).hexdigest()[:24]

    ref_paths = list(target_req.reference_image_paths)
    if alt_reference_path:
        ref_paths = [alt_reference_path] + [p for p in ref_paths if p != alt_reference_path]
    if start_cond_p:
        ref_paths = [start_cond_p] + [p for p in ref_paths if p != start_cond_p]

    return replace(
        target_req,
        operation_id=rec_op_id,
        prompt=aug_prompt,
        reference_image_paths=tuple(ref_paths[:4]),
    )
