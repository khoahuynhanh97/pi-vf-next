"""Versioned grounded prompt templates and bounded JSON schemas."""

from __future__ import annotations


COMMON_GROUNDED_SYSTEM_PROMPT = """You are the grounded creative system for a short-form product video.
Use only the supplied ResearchDossier and previously approved artifacts.
Create content specific to the approved product, audience situation and verified evidence.

GROUNDING RULES
1. Every factual product claim must cite one or more claim_id values from verified_claims.
2. Never invent specifications, performance, price, promotions, comparisons or guarantees.
   If verified_claims contain only the product name and QC-observed visual details,
   make a neutral visual presentation using only those details. Do not demonstrate
   or narrate an unverified function, benefit, performance result or use outcome.
   A product name or visible package detail is not evidence for a feature claim.
   Comfortable light or a desk-reading context is not evidence of eye protection,
   medical benefit, blue-light safety, or reduced eye fatigue. Cite an explicit
   verified claim for any such statement; otherwise omit it entirely.
   Do not infer a control gesture such as tapping or holding from a feature claim.
   Do not assert that competing products cause glare, shadows, or discomfort
   unless the dossier contains evidence for that comparison.
3. Seller, shop and marketplace information is provenance only and must not appear in creative content.
4. Do not use generic gadget language when a product-specific observation is available.
5. Do not design scenes whose only action is zooming, rotating or panning an image.
6. Each scene must contain an observable subject action, product interaction or cause-and-effect change.
7. Preserve the approved brand, model, variant, color and physical product identity.
8. Do not ask image/video models to draw readable text, prices, ratings, logos or marketplace UI.
9. Treat eligible asset rank, view angle, visible details and scene_suitability as binding
   evidence when selecting references for a scene.
10. Return only valid JSON matching the requested schema. Do not use Markdown."""


CREATIVE_DIRECTION_PROMPT_V2 = """TASK
Create one focused CreativeBriefV2 for a vertical TikTok/Reels short-form product video (30–50s).

INPUT
research_dossier={research_dossier_json}

DEEP PRODUCT REASONING & CREATIVE DECISIONS
1. Product Sensory & Physics Analysis:
   - Carefully examine the physical structure, tactile materials (matte finish, rubberized grip, metallic hinge, mesh grille), controls (clicky buttons, touch pads, dials), and environmental resilience (waterproof IPX rating, battery endurance, compact portability).
   - Ground every creative choice in observable physical realities of the product, avoiding generic marketing fluff.

2. Story Archetype Selection (Select exactly one):
   - problem_agitation_solution: Show a verified daily friction, the product interaction, and the observable resolution.
   - day_in_the_life: Follow the same product variant through a natural routine with continuous physical state.
   - feature_focus_showcase: Demonstrate 2-3 verified features through visible product interactions.
   - unexpected_comparison: Use only a comparison explicitly supported by verified claims; otherwise do not select this archetype.
   - before_after_transformation: Contrast a verified use situation before and after the product interaction without unsupported performance claims.

3. Target Duration & Pacing:
   - Select target_duration_seconds of exactly 30 or 36 seconds optimized for TikTok/Reels watch-through rate. Use only 6-second scene blocks (5×6s=30s or 6×6s=36s).
   - The narrative must follow a strong 3-part arc:
     * Hook (4-6s): Immediate problem setup or tactile sensory hook that grabs attention.
     * Demonstrations (6-8s per scene, 3-5 scenes): Observable key features and product interactions showing value.
     * Payoff & CTA (6-8s): Resolution and clear, compliant call to action.

4. Audience, Promise & Evidence:
   - Select an audience situation only when the dossier supports it; otherwise keep the presentation neutral.
   - Craft a hook from the exact product name or a QC-observed visual detail when no verified feature claim exists.
   - When no SOURCE_BACKED feature claim exists, avoid problem/solution promises, feature demonstrations,
     performance language, and invented use cases; use a neutral visual introduction instead.
   - If verified_claims is non-empty, reason_to_believe MUST contain at least one
     object with a non-empty factual text and evidence_refs containing the exact
     claim_id from verified_claims. Example shape:
     {{"text": "specific supported product fact", "evidence_refs": ["claim_id_from_dossier"]}}.
     evidence_refs here contains claim IDs, not source evidence IDs or URLs.
   - Use only the supported factual portion of a source claim. Search-page phrases
     such as "best price" or "reviews" are not product benefits and must be ignored.
   - Describe a control action only if the verified claim states that action.
   - Use a clean, neutral, manual-posting CTA without marketplace buttons.

OUTPUT
Return one CreativeBriefV2 JSON object."""


SCRIPT_SCENE_PLAN_PROMPT_V2 = """TASK
Convert the approved creative direction into a rich, cinematic ScriptScenePlanV2 (30–50s).

INPUT
research_dossier={research_dossier_json}
approved_brief={creative_brief_json}

STORY & SCENE PACING REQUIREMENTS
- Plan exactly 5 scenes (30s total) or 6 scenes (36s total). Target duration must be 30 or 36 seconds only.
- Each scene duration_seconds must be exactly 6 seconds. The sum of all scene durations must equal target_duration_seconds exactly.
  (Standard pacing: 30s = 5×6s; 36s = 6×6s).
- Narrative Arc & Progression:
  * Opening Scene (Hook): Immediate visual hook or problem setup with active interaction (NEVER a static title card or empty camera sweep).
  * Middle Scenes (Exploration & Key Demonstrations): Varied visual setups and observable product interactions supported by the supplied evidence.
  * Climax & Final Scene (Payoff & Call to Action): Satisfying resolution showcasing the product in its optimal state.
- No two scenes may repeat the same environment setup and subject action.

GROUNDING & CINEMATIC FIDELITY
- Keep narration conversational, engaging Vietnamese.
- If no SOURCE_BACKED feature claim exists, use only the product name and details explicitly observed in eligible QC PASS images. Keep narration and actions neutral; do not infer function, performance, benefit, specifications, or outcomes.
- Keep overlay_text strictly separate (never request text rendered inside generated video pixels).
- Every factual narration line must cite verified claim IDs.
- Do not turn a water-resistance rating into claims or visuals of floating, continued playback while submerged, resistance to shower jets or soap, or immunity from water damage unless the dossier explicitly verifies them.
- Do not infer bass intensity, vibration, or other physical effects from a generic sound-quality claim.
- Concretely specify start_state, end_state, and visual_change for each scene.
- MANDATORY PER-SCENE GROUNDING: Every single scene (including Scene 1 Hook/Opening) MUST contain non-empty arrays for:
  * claim_refs (at least 1 verified claim_id; describe only the observable part relevant to that claim).
  * product_identity_cues (at least 1-2 distinct physical cues of the product).
  * reference_asset_ids (must select at least 1 valid asset_id from eligible_assets matching the scene angle).
  * required_visible_details (at least 1-2 visible features).
  * reference_requirements (shot angle, detail, and scene use).
- For every scene, explicitly declare immutable product_placement, lighting, continuity_from_previous, and continuity_to_next.
- For every scene, explicitly declare interaction geometry using these exact fields:
  * interaction_actor: the hand, cable, accessory, or object performing the action.
  * interaction_target: the exact product part being touched or approached.
  * contact_landmark: the precise physical contact point; never use a generic body region.
  * approach_vector: direction of travel toward the contact landmark.
  * object_orientation: orientation of the actor and product before contact.
  * forbidden_contact_regions: visible regions the actor must never enter or touch.
  * interaction_risk: low, medium, or high. Cable insertion, docking, button presses,
    opening/closing mechanisms, and accessory attachment are always high risk.
- High-risk scenes must provide non-empty interaction_actor, interaction_target,
  contact_landmark, approach_vector, object_orientation, and forbidden_contact_regions.
- For every scene, explicitly declare end_keyframe_policy ("required", "generated", or "omitted").
  If "omitted", provide a non-empty structured end_keyframe_omission_reason (e.g. continuous_motion_single_shot, fast_action_dynamic_cut, camera_reframing).
- Maintain absolute physical continuity: same model variant, color, orientation, and lighting continuity between shots.

REJECTION CHECK
Reject the draft if camera movement is the only motion, if the product is treated as a generic gadget, if any scene omits reference_asset_ids/product_identity_cues, or if unsupported claims appear.

OUTPUT
Return one ScriptScenePlanV2 JSON object."""


CREATIVE_BRIEF_V2_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string"},
        "audience": {
            "type": "object",
            "properties": {
                "segment": {"type": "string"},
                "situation": {"type": "string"},
                "desired_outcome": {"type": "string"},
            },
            "required": ["segment", "situation", "desired_outcome"],
        },
        "consumer_insight": {"type": "string"},
        "problem": {"type": "string"},
        "creative_angle": {"type": "string"},
        "hook": {"type": "string"},
        "promise": {"type": "string"},
        "reason_to_believe": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "evidence_refs"],
            },
        },
        "tone": {"type": "string"},
        "story_archetype": {
            "type": "string",
            "enum": [
                "problem_agitation_solution",
                "day_in_the_life",
                "feature_focus_showcase",
                "unexpected_comparison",
                "before_after_transformation",
            ],
        },
        "cta": {"type": "string"},
        "restrictions": {"type": "array", "items": {"type": "string"}},
        "target_duration_seconds": {"type": "integer"},
        "dossier_digest": {"type": "string"},
    },
    "required": [
        "schema_version",
        "audience",
        "consumer_insight",
        "problem",
        "creative_angle",
        "hook",
        "promise",
        "reason_to_believe",
        "tone",
        "story_archetype",
        "cta",
        "target_duration_seconds",
        "dossier_digest",
    ],
    "additionalProperties": True,
}


SCRIPT_SCENE_PLAN_V2_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string"},
        "target_duration_seconds": {"type": "integer"},
        "brief_digest": {"type": "string"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"}, "order": {"type": "integer"},
                    "story_role": {"type": "string"}, "duration_seconds": {"type": "integer"},
                    "narration": {"type": "string"}, "overlay_text": {"type": "string"},
                    "visual_objective": {"type": "string"}, "environment": {"type": "string"},
                    "subject_action": {"type": "string"}, "product_interaction": {"type": "string"},
                    "start_state": {"type": "string"}, "end_state": {"type": "string"},
                    "visual_change": {"type": "string"}, "camera_framing": {"type": "string"},
                    "camera_movement": {"type": "string"}, "transition_out": {"type": "string"},
                    "claim_refs": {"type": "array", "items": {"type": "string"}},
                    "reference_asset_ids": {"type": "array", "items": {"type": "string"}},
                    "product_identity_cues": {"type": "array", "items": {"type": "string"}},
                    "product_placement": {"type": "string"},
                    "lighting": {"type": "string"},
                    "continuity_from_previous": {"type": "string"},
                    "continuity_to_next": {"type": "string"},
                    "required_visible_details": {"type": "array", "items": {"type": "string"}},
                    "reference_requirements": {"type": "array", "items": {"type": "string"}},
                    "interaction_actor": {"type": "string"},
                    "interaction_target": {"type": "string"},
                    "contact_landmark": {"type": "string"},
                    "approach_vector": {"type": "string"},
                    "object_orientation": {"type": "string"},
                    "forbidden_contact_regions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "interaction_risk": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "end_keyframe_policy": {
                        "type": "string",
                        "enum": ["required", "generated", "omitted"],
                    },
                    "end_keyframe_omission_reason": {
                        "type": "string",
                    },
                },
                "required": ["scene_id", "order", "story_role", "duration_seconds", "narration", "overlay_text", "visual_objective", "environment", "subject_action", "product_interaction", "start_state", "end_state", "visual_change", "camera_framing", "camera_movement", "transition_out", "claim_refs", "product_identity_cues", "product_placement", "lighting", "continuity_from_previous", "continuity_to_next", "required_visible_details", "reference_requirements", "reference_asset_ids", "interaction_actor", "interaction_target", "contact_landmark", "approach_vector", "object_orientation", "forbidden_contact_regions", "interaction_risk", "end_keyframe_policy"],
            },
        },
    },
    "required": ["schema_version", "target_duration_seconds", "brief_digest", "scenes"],
    "additionalProperties": True,
}

SEVEN_PART_SCENE_COMPILER_PROMPT = """TASK: You are video prompt architect. Convert scene description into 7-part structured prompt.

OUTPUT 7 BLOCKS (JSON format):
{
  "shot": "...",
  "subject": "...",
  "action": "...",
  "setting": "...",
  "light_palette": "...",
  "look": "...",
  "sound": "..."
}

RULES:
- SHOT: Camera framing (close-up/medium/wide), angle (eye-level/high/low), movement (static/handheld/push-in).
- SUBJECT: Identity anchor locked across shots (e.g., "the woman in cream knit sweater holding {product_name}").
- ACTION: ONE camera-legible physical verb cause-and-effect.
- SETTING: Real-world room/location with 2-3 physical objects.
- LIGHT_PALETTE: Light direction + temperature + 3-color palette (e.g., "window left, warm morning sunlight, cream beige sage").
- LOOK: Camera texture (35mm film or high-end smartphone camera, fine organic grain, natural skin rendering). NEVER "photorealistic", "8k", or "ultra-detailed".
- SOUND: Ambient background sounds + physical contact SFX + dialogue language tag. State "No music" explicitly.

CONTAINER ISOLATION:
DO NOT include duration ('8 seconds'), aspect ratio ('9:16'), or resolution in the prose prompt blocks.
Those are handled strictly via API container parameters.

CATEGORY-SPECIFIC:
- BEAUTY: All 7 blocks mandatory. Focus on skin/product texture, natural human motion.
- TECH: SHOT+SUBJECT+ACTION mandatory. SETTING/LIGHT_PALETTE/LOOK/SOUND optional (use "" if skipped).
- FASHION: SHOT+SUBJECT+ACTION+SETTING+LOOK mandatory.
- FMCG: SHOT+SUBJECT+ACTION+SETTING mandatory.
"""
