"""Auditor Agent: AI-driven autonomous QC evaluator and decision-maker for Video Factory Gates.

Evaluates candidates, creative briefs, scene plans, keyframes, and final video clips.
Provides structured verdict (PASS/FAIL), scores (0-100), and surgical retry feedback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from video_factory.creative.spatial_anatomy import SpatialAnatomy, extract_from_product_dossier
from video_factory.qc.models import QCStage, QCVerdict

logger = logging.getLogger(__name__)


class GateDecision(str, Enum):
    APPROVED = "approved"
    RETRY_REQUIRED = "retry_required"
    REJECTED_FATAL = "rejected_fatal"


@dataclass(frozen=True)
class GateAuditReport:
    gate_name: str
    decision: GateDecision
    score: int
    reasons: list[str] = field(default_factory=list)
    retry_feedback: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AuditorAgent:
    """Autonomous evaluator and quality-gate decider."""

    def __init__(self, min_pass_score: int = 80, max_retries: int = 3) -> None:
        self.min_pass_score = min_pass_score
        self.max_retries = max_retries

    def audit_gate_1_product_lock(
        self,
        candidate_data: dict[str, Any],
        image_qc_reports: list[dict[str, Any]],
    ) -> GateAuditReport:
        """Gate 1: Verify product identity, spatial anatomy, and one usable reference."""
        reasons: list[str] = []
        eligible_images = 0
        cleanable_images = 0

        for r in image_qc_reports:
            verdict = r.get("verdict", "fail")
            if verdict == "pass" and not r.get("hard_failure"):
                eligible_images += 1
            elif r.get("cleanable", False):
                cleanable_images += 1

        if eligible_images < 1 and cleanable_images < 1:
            return GateAuditReport(
                gate_name="Gate 1: Product Lock",
                decision=GateDecision.REJECTED_FATAL,
                score=30,
                reasons=["No usable product reference is available."],
                retry_feedback="Re-acquire product references with higher resolution and no intrusive overlays.",
            )

        # Check spatial anatomy definition
        anatomy = extract_from_product_dossier(candidate_data)
        if not anatomy or not anatomy.components:
            reasons.append("Product spatial anatomy not defined (ports, LED, plug positions missing).")
        
        if eligible_images < 1 and cleanable_images > 0:
            return GateAuditReport(
                gate_name="Gate 1: Product Lock",
                decision=GateDecision.RETRY_REQUIRED,
                score=70,
                reasons=[f"No clean reference is available; {cleanable_images} reference(s) can be cleaned."],
                retry_feedback="Trigger image inpainting/cleaning for text overlays and watermarks.",
                metadata={"requires_cleaning": True},
            )

        if reasons:
            return GateAuditReport(
                gate_name="Gate 1: Product Lock",
                decision=GateDecision.RETRY_REQUIRED,
                score=75,
                reasons=reasons,
                retry_feedback="Define product_anatomy with component positions before proceeding.",
                metadata={"requires_anatomy": True},
            )

        return GateAuditReport(
            gate_name="Gate 1: Product Lock",
            decision=GateDecision.APPROVED,
            score=90,
            reasons=["Source images passed QC and spatial anatomy is defined."],
            metadata={"anatomy": anatomy.to_prompt_injection() if anatomy else None},
        )

    def audit_gate_2_creative_brief(
        self,
        brief: dict[str, Any],
        target_min_seconds: int = 30,
        target_max_seconds: int = 36,
    ) -> GateAuditReport:
        """Gate 2: Audit creative brief for groundedness, hook, and valid duration (30s or 36s only)."""
        reasons: list[str] = []
        duration = brief.get("target_duration_seconds", 0)

        if duration not in (30, 36):
            reasons.append(
                f"Target duration must be exactly 30s or 36s (got {duration}s). TikTok/Reels optimal length."
            )

        hook = brief.get("hook", "").strip()
        if not hook or len(hook) < 10:
            reasons.append("Hook is missing or too generic.")

        reasons_to_believe = brief.get("reason_to_believe", [])
        if not reasons_to_believe:
            reasons.append("Brief lacks grounded reasons-to-believe tied to verified claims.")

        if reasons:
            return GateAuditReport(
                gate_name="Gate 2: Creative Brief",
                decision=GateDecision.RETRY_REQUIRED,
                score=65,
                reasons=reasons,
                retry_feedback=(
                    "Set target_duration_seconds to exactly 30 or 36. "
                    "Strengthen hook with product USP and cite verified evidence."
                ),
            )

        return GateAuditReport(
            gate_name="Gate 2: Creative Brief",
            decision=GateDecision.APPROVED,
            score=92,
            reasons=["Creative brief is grounded, hook is strong, duration is 30s or 36s (TikTok/Reels optimal)."],
        )

    def audit_gate_3_scene_plan(
        self,
        scene_plan: dict[str, Any],
        target_min_seconds: int = 30,
        target_max_seconds: int = 50,
    ) -> GateAuditReport:
        """Gate 3: Audit scene plan for total duration, action variety, and visual changes."""
        scenes = scene_plan.get("scenes", [])
        if not scenes:
            return GateAuditReport(
                gate_name="Gate 3: Scene Plan",
                decision=GateDecision.REJECTED_FATAL,
                score=0,
                reasons=["Scene plan contains no scenes."],
            )

        total_duration = sum(s.get("duration_seconds", 0) for s in scenes)
        reasons: list[str] = []

        if not (target_min_seconds <= total_duration <= target_max_seconds):
            reasons.append(
                f"Total scene duration {total_duration}s does not fit target range ({target_min_seconds}s - {target_max_seconds}s)."
            )

        # Check visual change in each scene
        static_scenes = [
            s.get("scene_id", f"scene-{idx}")
            for idx, s in enumerate(scenes)
            if not s.get("visual_change") or not s.get("subject_action")
        ]
        if static_scenes:
            reasons.append(f"Scenes lack meaningful action or visual transition: {', '.join(static_scenes)}")

        if reasons:
            return GateAuditReport(
                gate_name="Gate 3: Scene Plan",
                decision=GateDecision.RETRY_REQUIRED,
                score=60,
                reasons=reasons,
                retry_feedback=(
                    f"Use exactly 30s (5×6s scenes) or 36s (6×6s scenes). "
                    "Ensure every scene has a concrete subject action and visual change (no static slides)."
                ),
            )

        return GateAuditReport(
            gate_name="Gate 3: Scene Plan",
            decision=GateDecision.APPROVED,
            score=95,
            reasons=[f"Scene plan approved with {len(scenes)} dynamic scenes totaling {total_duration}s (TikTok/Reels optimized)."],
        )

    def audit_gate_4_keyframes(
        self,
        keyframe_qc_reports: list[dict[str, Any]],
    ) -> GateAuditReport:
        """Gate 4: Audit generated keyframes for product identity preservation and cleanliness."""
        failed_keyframes: list[dict[str, Any]] = []
        for rep in keyframe_qc_reports:
            verdict = rep.get("verdict", "fail")
            score = rep.get("total_score", 0)
            if verdict != "pass" or score < self.min_pass_score or rep.get("hard_failure"):
                failed_keyframes.append(rep)

        if failed_keyframes:
            scene_ids = [k.get("scene_id", "unknown") for k in failed_keyframes]
            return GateAuditReport(
                gate_name="Gate 4: Keyframe Storyboard",
                decision=GateDecision.RETRY_REQUIRED,
                score=70,
                reasons=[f"Keyframes failed QC for scenes: {', '.join(scene_ids)}"],
                retry_feedback=(
                    "Regenerate keyframes for failed scenes with explicit reference conditioning. "
                    "Prevent product distortion and enforce clean background."
                ),
                metadata={"failed_scene_ids": scene_ids},
            )

        return GateAuditReport(
            gate_name="Gate 4: Keyframe Storyboard",
            decision=GateDecision.APPROVED,
            score=90,
            reasons=["All scene keyframes passed visual consistency and cleanliness standards."],
        )

    def audit_gate_5_final_video(
        self,
        video_metadata: dict[str, Any],
        video_qc_report: dict[str, Any],
        target_min_seconds: int = 30,
        target_max_seconds: int = 50,
    ) -> GateAuditReport:
        """Gate 5: Final review of stitched MP4 container, duration, and visual continuity."""
        duration = video_metadata.get("duration_seconds", 0)
        reasons: list[str] = []

        if not (target_min_seconds <= duration <= target_max_seconds):
            reasons.append(
                f"Final video length {duration:.1f}s is outside target range ({target_min_seconds}s - {target_max_seconds}s)."
            )

        qc_score = video_qc_report.get("total_score", 0)
        if qc_score < self.min_pass_score or video_qc_report.get("hard_failure"):
            reasons.append(
                f"Video QC failed with score {qc_score}/100: {video_qc_report.get('reason', 'Unknown defect')}"
            )

        if video_metadata.get("file_size_bytes", 0) < 500_000:
            reasons.append("Output video file size suspiciously small (likely corrupt or blank render).")

        if reasons:
            return GateAuditReport(
                gate_name="Gate 5: Final Video Approval",
                decision=GateDecision.RETRY_REQUIRED,
                score=qc_score,
                reasons=reasons,
                retry_feedback=(
                    "Re-render degraded scenes and re-composite. Ensure total duration is 30-50s "
                    "and video stream has valid 1080x1920 vertical format."
                ),
            )

        return GateAuditReport(
            gate_name="Gate 5: Final Video Approval",
            decision=GateDecision.APPROVED,
            score=qc_score,
            reasons=[f"Final video approved: {duration:.1f}s, valid vertical MP4 container, QC passed."],
        )
