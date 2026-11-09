"""Deterministic offline fake creative agent roles for automated testing and fake mode."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from video_factory.creative.ports import StructuredModelResult


def embedded(prompt: str, schema: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(prompt):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(prompt[index:])
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("schema_version") == schema:
                return value
    raise AssertionError("missing actual role input " + schema)


class FakeRoles:
    """Deterministic fake creative agent roles supporting 3, 4, or 5 scenes."""

    def __init__(self, target_scenes: int = 4, fail_stage: str | None = None) -> None:
        self.target_scenes = max(3, min(5, target_scenes))
        self.calls: list[str] = []
        self.fail_stage = fail_stage
        self.failed_once = False
        self.on_failure = lambda: None
        self.failure_generation_counts = None

    def generate_json(self, **kwargs: Any) -> StructuredModelResult:
        assert kwargs["schema_name"] == "repair-proposal-v1"
        self.calls.append(kwargs["schema_name"])
        payload = json.loads(kwargs["user_prompt"])
        review, context = payload["review"], payload["context"]
        assembly = self.fail_stage == "assembly"
        data = {
            "base_digest": context["current_digest"],
            "finding_ids": [review["findings"][0]["finding_id"]],
            "scope": "assembly" if assembly else ("scene" if review["stage"] == "preproduction" else "prompt"),
            "scene_ids": ["s2"],
            "diagnosis": "Restore documented scene order using accepted clips"
            if assembly
            else "Make the same documented handling simpler and more visible",
            "changes": {"rebuild": True} if assembly else {"action_guidance": "relaxed grip with clear hand movement"},
            "protected_invariants": context["protected_invariants"],
            "recheck_stages": ["preproduction", "storyboard", "clip", "final"],
        }
        return StructuredModelResult(data, json.dumps(data), "fake", "fake-repair-planner")

    def generate_media_json(self, **kwargs: Any) -> StructuredModelResult:
        self.calls.append(kwargs["schema_name"])
        for ref in kwargs.get("media", []):
            if hasattr(ref, "content"):
                ref.content()
        name = kwargs["schema_name"]

        if name == "creative-brief-v2":
            dossier = embedded(kwargs["user_prompt"], "research-dossier-v2")
            claim = dossier["verified_claims"][0] if dossier.get("verified_claims") else {"text": "San pham thuc te", "claim_id": "c1"}
            duration = self.target_scenes * 8
            data = {
                "schema_version": "creative-brief-v2",
                "audience": {
                    "segment": "A person evaluating home gadgets",
                    "situation": "Reviewing authentic physical product",
                    "desired_outcome": "Understand product form and usage",
                },
                "consumer_insight": "Authentic physical form factors build high shopper confidence",
                "problem": "Uncertainty regarding physical shape and exterior design",
                "creative_angle": "Neutral physical showcase presenting verified clean profile",
                "hook": f"Kham pha can canh {dossier.get('product_identity', {}).get('display_name', 'san pham')}",
                "promise": "Hinh anh thuc te, chi tiet ngoai quan chinh xac",
                "reason_to_believe": [{"text": claim["text"], "evidence_refs": [claim["claim_id"]]}],
                "tone": "Neutral, objective, observational",
                "story_archetype": "feature_focus_showcase",
                "cta": "Tim hieu them chi tiet san pham.",
                "restrictions": ["No unsupported features", "Visual-only format"],
                "target_duration_seconds": duration,
                "prompt_version": "creative-direction-v2",
                "dossier_digest": dossier["dossier_digest"],
            }
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-strategist")

        elif name == "script-scene-plan-v2":
            dossier = embedded(kwargs["user_prompt"], "research-dossier-v2")
            brief = embedded(kwargs["user_prompt"], "creative-brief-v2")
            actions_pool = [
                "A hand approaches the item",
                "The hand unboxes and lifts the item",
                "The hand demonstrates the primary controls",
                "The person carries the item across the room",
                "The person displays the documented item clearly",
            ]
            actions = actions_pool[: self.target_scenes]
            if "Finding-specific revision" in kwargs.get("user_prompt", ""):
                actions[1] = "The hand lifts the item with a relaxed grip"

            role_pool = ["hook", "demo", "demo", "proof", "cta"]
            scenes = []
            claim_id = dossier["verified_claims"][0]["claim_id"] if dossier.get("verified_claims") else "claim-sku-01"
            raw_assets = [a["asset_id"] for a in dossier.get("eligible_assets", [])]
            asset_ids = raw_assets[:1] if raw_assets else ["asset_01"]

            include_bible = "scene_bible" in kwargs.get("user_prompt", "") or "SCENE BIBLE" in kwargs.get("user_prompt", "")
            bible_env = "daylit studio"
            bible_light = "soft daylight"
            for index, action in enumerate(actions):
                role = role_pool[index] if index < len(role_pool) else "demo"
                if index == len(actions) - 1:
                    role = "cta"
                scenes.append({
                    "scene_id": f"s{index + 1}",
                    "order": index + 1,
                    "story_role": role,
                    "duration_seconds": 8,
                    "narration": "",
                    "overlay_text": "",
                    "visual_objective": action,
                    "environment": bible_env if include_bible else ("daylit desk" if index < 2 else "daylit studio"),
                    "subject_action": action,
                    "product_interaction": "The same hand handles the documented item",
                    "start_state": "The item is visible before this action",
                    "end_state": "The item stays visible after this action",
                    "visual_change": action,
                    "camera_framing": ("close-up" if index == 0 else "medium closeup") if include_bible else "medium closeup",
                    "camera_movement": "locked camera",
                    "transition_out": "cut",
                    "claim_refs": [claim_id],
                    "reference_asset_ids": asset_ids,
                    "product_identity_cues": ["same documented shape"],
                    "product_placement": "center in portrait",
                    "lighting": bible_light if include_bible else "soft daylight",
                    "continuity_from_previous": "same item and hand",
                    "continuity_to_next": "same item and hand",
                    "required_visible_details": ["same shape"],
                    "reference_requirements": ["approved source photos"],
                    "end_keyframe_policy": "omitted",
                    "end_keyframe_omission_reason": "profile uses one START frame",
                })
            total_duration = self.target_scenes * 8
            data = {
                "schema_version": "script-scene-plan-v2",
                "brief_digest": brief["brief_digest"],
                "target_duration_seconds": total_duration,
                "scenes": scenes,
            }
            if include_bible:
                data["scene_bible"] = {
                    "environment": bible_env,
                    "lighting": bible_light,
                    "pov_hand": "relaxed right hand holding item",
                    "camera_baseline": "eye-level 45-degree angle",
                }
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-director")

        elif name == "scene-prompt-bundle-v1":
            context = json.loads(kwargs["user_prompt"])
            data = {k: context[k] for k in ("script_digest", "backend_profile_digest", "playbook_digest") if k in context}
            data["prompts"] = [
                {
                    "scene_id": s["scene_id"],
                    "start_prompt": "Portrait composition: " + s["start_state"],
                    "end_prompt": "",
                    "video_prompt": s["subject_action"],
                    "negative_prompt": "",
                    "reference_asset_ids": s["reference_asset_ids"],
                }
                for s in context["script"]["scenes"]
            ]
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-prompt-author")

        elif name == "creative-review-v1":
            context = json.loads(kwargs["user_prompt"])
            data = {
                "verdict": "PASS",
                "findings": [],
                "coverage": {
                    "method": "actual_pixels",
                    "media_sha256": [r.sha256 for r in kwargs.get("media", []) if hasattr(r, "sha256")],
                    "scene_ids": context.get("scene_ids", []),
                    "timestamps": context.get("required_timestamps", []),
                    "limitations": "Fake assessment on synthetic pixels",
                },
            }
            if "preproduction_threshold" in context:
                if context.get("rubric_version") == "v2":
                    from .critic import PREPRODUCTION_WEIGHTS_V2
                    scene_pov = [{"scene_id": s_id, "kind": "experiential"} for s_id in context.get("scene_ids", [])]
                    data.update(scores=dict(PREPRODUCTION_WEIGHTS_V2), hard_failures=[], scene_pov=scene_pov)
                else:
                    from .critic import PREPRODUCTION_WEIGHTS
                    data.update(scores=dict(PREPRODUCTION_WEIGHTS), hard_failures=[])
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-critic")

        elif name == "prompt-repair-v1":
            context = json.loads(kwargs["user_prompt"])
            prompt = context["prompts"]["prompts"][0]
            data = {"repaired_prompts": [prompt], "recheck_stages": ["preproduction", "clip"]}
            return StructuredModelResult(data, json.dumps(data), "fake", "fake-repair-planner")

        data = {"status": "ok"}
        return StructuredModelResult(data, json.dumps(data), "fake", "fake-generic")
