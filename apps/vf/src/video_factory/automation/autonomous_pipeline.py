"""DEPRECATED synthetic prototype retained for offline tests."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from video_factory.qc.auditor_agent import AuditorAgent, GateDecision
from video_factory.workflow import ProductVideoWorkflowState

logger = logging.getLogger(__name__)


@dataclass
class PipelineRunResult:
    success: bool
    state: ProductVideoWorkflowState
    final_video_path: Path | None = None
    duration_seconds: float = 0.0
    gate_reports: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class AutonomousVideoPipeline:
    """DEPRECATED test fixture. Use ProductVideoCoordinator for real work."""

    def __init__(
        self,
        workspace_root: Path | str,
        target_min_seconds: int = 30,
        target_max_seconds: int = 50,
        max_retries: int = 3,
        provider_mode: str = "configured",
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.target_min_seconds = target_min_seconds
        self.target_max_seconds = target_max_seconds
        self.max_retries = max_retries
        if provider_mode not in {"configured", "fake"}:
            raise ValueError("provider_mode must be 'configured' or 'fake'")
        self.provider_mode = provider_mode
        self.auditor = AuditorAgent(min_pass_score=80, max_retries=max_retries)

    def run_pipeline(
        self,
        product_data: dict[str, Any],
        source_image_paths: list[Path],
    ) -> PipelineRunResult:
        """Execute the synthetic fixture only inside pytest."""
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            raise RuntimeError("DEPRECATED_AUTONOMOUS_PIPELINE: test fixture only")
        if self.provider_mode == "configured":
            # This legacy orchestrator only contains deterministic scaffolding
            # below. Never let that scaffolding claim a real Product Video
            # result; configured runs must use the durable coordinator/runtime.
            return PipelineRunResult(
                success=False,
                state=ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
                errors=[
                    "CONFIGURED_PIPELINE_UNAVAILABLE: use the durable Product Video coordinator; "
                    "the legacy autonomous runner has no real Flow/Vertex submission boundary"
                ],
            )
        run_id = f"run_{int(time.time())}"
        work_dir = self.workspace_root / run_id
        work_dir.mkdir(parents=True, exist_ok=True)
        reports: dict[str, Any] = {}

        logger.info("🎬 [Step 1/5] Auditing Candidate and Image References...")
        # Simulating initial image QC run
        image_reports = [
            {"asset_path": str(p), "verdict": "pass", "score": 85, "cleanable": False}
            for p in source_image_paths
        ]
        g1_audit = self.auditor.audit_gate_1_product_lock(product_data, image_reports)
        reports["gate_1"] = g1_audit.__dict__

        if g1_audit.decision != GateDecision.APPROVED:
            return PipelineRunResult(
                success=False,
                state=ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
                gate_reports=reports,
                errors=g1_audit.reasons,
            )

        logger.info("✨ Gate 1 Passed: Product Locked.")

        logger.info("📝 [Step 2/5] Synthesizing Creative Brief (Target: 35s)...")
        brief = self._generate_creative_brief_with_retry(product_data)
        g2_audit = self.auditor.audit_gate_2_creative_brief(
            brief,
            target_min_seconds=self.target_min_seconds,
            target_max_seconds=self.target_max_seconds,
        )
        reports["gate_2"] = g2_audit.__dict__
        if g2_audit.decision != GateDecision.APPROVED:
            return PipelineRunResult(
                success=False,
                state=ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL,
                gate_reports=reports,
                errors=g2_audit.reasons,
            )

        logger.info("✨ Gate 2 Passed: Creative Brief Approved.")

        logger.info("🎬 [Step 3/5] Constructing Scene Plan...")
        scene_plan = self._generate_scene_plan_with_retry(brief)
        g3_audit = self.auditor.audit_gate_3_scene_plan(
            scene_plan,
            target_min_seconds=self.target_min_seconds,
            target_max_seconds=self.target_max_seconds,
        )
        reports["gate_3"] = g3_audit.__dict__
        if g3_audit.decision != GateDecision.APPROVED:
            return PipelineRunResult(
                success=False,
                state=ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL,
                gate_reports=reports,
                errors=g3_audit.reasons,
            )

        logger.info(f"✨ Gate 3 Passed: Scene Plan Approved ({len(scene_plan.get('scenes', []))} scenes).")

        logger.info("🎨 [Step 4/5] Generating & Auditing Keyframes...")
        keyframes = self._generate_keyframes_with_retry(scene_plan, source_image_paths)
        g4_audit = self.auditor.audit_gate_4_keyframes(keyframes.get("qc_reports", []))
        reports["gate_4"] = g4_audit.__dict__
        if g4_audit.decision != GateDecision.APPROVED:
            return PipelineRunResult(
                success=False,
                state=ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL,
                gate_reports=reports,
                errors=g4_audit.reasons,
            )

        logger.info("✨ Gate 4 Passed: Storyboard & Keyframes Verified.")

        logger.info("🎥 [Step 5/5] Rendering Video Scenes & Final Composition...")
        video_output = self._render_and_compose_video(work_dir, scene_plan, keyframes)
        g5_audit = self.auditor.audit_gate_5_final_video(
            video_metadata=video_output.get("metadata", {}),
            video_qc_report=video_output.get("qc_report", {}),
            target_min_seconds=self.target_min_seconds,
            target_max_seconds=self.target_max_seconds,
        )
        reports["gate_5"] = g5_audit.__dict__
        if g5_audit.decision != GateDecision.APPROVED:
            return PipelineRunResult(
                success=False,
                state=ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL,
                gate_reports=reports,
                errors=g5_audit.reasons,
            )

        final_mp4 = video_output.get("mp4_path")
        duration = video_output.get("metadata", {}).get("duration_seconds", 35.0)

        logger.info(f"🎉 Pipeline Complete! Exported to: {final_mp4} (Duration: {duration}s)")
        return PipelineRunResult(
            success=True,
            state=ProductVideoWorkflowState.COMPLETED,
            final_video_path=final_mp4,
            duration_seconds=duration,
            gate_reports=reports,
        )

    def _generate_creative_brief_with_retry(self, product_data: dict[str, Any]) -> dict[str, Any]:
        """Synthesize brief targeted for 35s."""
        title = product_data.get("title", "Sản phẩm")
        features = product_data.get("features", [])
        return {
            "title": title,
            "target_duration_seconds": 35,
            "hook": f"Đây là lý do bạn cần {title} ngay hôm nay!",
            "consumer_insight": "Người dùng muốn tiết kiệm thời gian và có trải nghiệm mượt mà.",
            "reason_to_believe": [{"text": str(f)} for f in features] or [{"text": "Sản phẩm chính hãng chất lượng cao"}],
            "tone": "Trẻ trung, lôi cuốn, thực tế",
        }

    def _generate_scene_plan_with_retry(self, brief: dict[str, Any]) -> dict[str, Any]:
        """Build 6-scene storyboard totaling 35 seconds."""
        return {
            "target_duration_seconds": 35,
            "scenes": [
                {
                    "scene_id": "scene-01",
                    "duration_seconds": 5,
                    "story_role": "hook",
                    "subject_action": "Cận cảnh người dùng đang tương tác với sản phẩm",
                    "visual_change": "Mở hộp hoặc khởi động thiết bị",
                },
                {
                    "scene_id": "scene-02",
                    "duration_seconds": 6,
                    "story_role": "problem",
                    "subject_action": "Thể hiện vấn đề bất tiện thường gặp",
                    "visual_change": "Chuyển từ bối cảnh khó khăn sang giải pháp",
                },
                {
                    "scene_id": "scene-03",
                    "duration_seconds": 6,
                    "story_role": "feature_1",
                    "subject_action": "Thao tác tính năng chủ đạo",
                    "visual_change": "Hiệu quả rõ rệt khi sử dụng sản phẩm",
                },
                {
                    "scene_id": "scene-04",
                    "duration_seconds": 6,
                    "story_role": "feature_2",
                    "subject_action": "Góc quay chi tiết thiết kế tinh tế",
                    "visual_change": "Xoay góc quay 45 độ làm nổi bật thiết kế",
                },
                {
                    "scene_id": "scene-05",
                    "duration_seconds": 6,
                    "story_role": "result",
                    "subject_action": "Người dùng hài lòng với kết quả",
                    "visual_change": "Gương mặt tươi vui và sản phẩm hoạt động hoàn hảo",
                },
                {
                    "scene_id": "scene-06",
                    "duration_seconds": 6,
                    "story_role": "cta",
                    "subject_action": "Hiển thị sản phẩm nguyên vẹn kèm lời kêu gọi hành động",
                    "visual_change": "Đóng khung kết thúc sản phẩm",
                },
            ],
        }

    def _generate_keyframes_with_retry(
        self,
        scene_plan: dict[str, Any],
        source_images: list[Path],
    ) -> dict[str, Any]:
        """Generate and evaluate keyframes for all scenes."""
        scenes = scene_plan.get("scenes", [])
        qc_reports = [
            {
                "scene_id": s["scene_id"],
                "verdict": "pass",
                "total_score": 88,
                "hard_failure": False,
            }
            for s in scenes
        ]
        return {"qc_reports": qc_reports}

    def _render_and_compose_video(
        self,
        work_dir: Path,
        scene_plan: dict[str, Any],
        keyframes: dict[str, Any],
    ) -> dict[str, Any]:
        """Compose final video and perform technical/visual QC."""
        output_mp4 = work_dir / "final_video_vertical_1080x1920.mp4"
        total_duration = sum(s.get("duration_seconds", 0) for s in scene_plan.get("scenes", []))
        
        # Placeholder simulation for container metadata
        output_mp4.touch()
        return {
            "mp4_path": output_mp4,
            "metadata": {
                "duration_seconds": float(total_duration),
                "resolution": "1080x1920",
                "file_size_bytes": 12_500_000,
            },
            "qc_report": {
                "verdict": "pass",
                "total_score": 90,
                "hard_failure": False,
            },
        }
