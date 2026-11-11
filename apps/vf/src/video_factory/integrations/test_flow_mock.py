"""Test mock provider for Google Flow keyframe isolation tests."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from PIL import Image

from video_factory.integrations.keyframe_provider import (
    MAX_OUTPUT_RECONCILIATION_ATTEMPTS,
    KeyframeOperationStore,
)
from video_factory.integrations.ports import (
    KeyframeGenerationPort,
    KeyframeGenerationRequest,
    KeyframeGenerationResult,
)


def _create_minimal_png(path: Path, width: int = 720, height: int = 1280, color: str = "green") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=color)
    img.save(path, format="PNG")
    return path


class TestMockFlowKeyframeProvider(KeyframeGenerationPort):
    provider_kind = "google_flow"

    def __init__(self, output_dir: Path | str | None = None, store: Any | None = None) -> None:
        self.output_dir = Path(output_dir or Path.cwd() / "keyframes").resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.lease_owner: str | None = None
        self.profile_path = self.output_dir / "mock_profile"
        self.profile_path.mkdir(parents=True, exist_ok=True)

    def is_available(self) -> bool:
        return True

    def bind_operation_store(self, store: Any) -> None:
        self.store = store

    def submit(self, request: KeyframeGenerationRequest) -> KeyframeGenerationResult:
        is_a = "prod_a" in request.product_description.casefold() or "prod_a" in request.prompt.casefold() or "prod_a" in request.workflow_id.casefold()

        # If testing reusable image
        if is_a and os.environ.get("VF_TEST_FLOW_REUSE_IMAGE") == "1":
            out_img = _create_minimal_png(self.output_dir / f"{request.operation_id}.png", 720, 1280, "blue")
            project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
            if self.store:
                self.store.record_project_checkpoint(request, project_url, self.provider_kind)
            return KeyframeGenerationResult(
                operation_id=request.operation_id,
                status="completed",
                provider_operation_id=project_url,
                image_path=str(out_img),
                metadata={"provider_kind": "google_flow", "project_url": project_url, "output_reconciled": True},
            )

        # If testing replacement success
        if is_a and os.environ.get("VF_TEST_FLOW_REPLACE_SUCCESS") == "1":
            conn = getattr(self.store, "connection", None)
            prior_abandoned = False
            if conn:
                prior_abandoned = bool(conn.execute(
                    "SELECT 1 FROM video_factory_keyframe_operation WHERE status='abandoned_unknown'"
                ).fetchone())
            if prior_abandoned:
                out_img = _create_minimal_png(self.output_dir / f"{request.operation_id}.png", 720, 1280, "blue")
                project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000003"
                if self.store:
                    self.store.record_project_checkpoint(request, project_url, self.provider_kind)
                return KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="completed",
                    provider_operation_id=project_url,
                    image_path=str(out_img),
                    metadata={"provider_kind": "google_flow", "project_url": project_url, "auto_replaced": True},
                )

        if is_a:
            project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
            if self.store:
                self.store.record_project_checkpoint(request, project_url, self.provider_kind)
            return KeyframeGenerationResult(
                operation_id=request.operation_id,
                status="unknown",
                provider_operation_id=project_url,
                error_code="RECONCILIATION_REQUIRED",
                error_message="Flow post-Send timeout simulating unknown outcome",
                metadata={"provider_kind": "google_flow", "project_url": project_url, "send_attempted": True},
            )

        # Product B or others succeed
        out_img = _create_minimal_png(self.output_dir / f"{request.operation_id}.png", 720, 1280, "green")
        project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000002"
        if self.store:
            self.store.record_project_checkpoint(request, project_url, self.provider_kind)
        return KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="completed",
            provider_operation_id=project_url,
            image_path=str(out_img),
            metadata={"provider_kind": "google_flow", "project_url": project_url, "output_reconciled": True},
        )

    def reconcile_existing_output(
        self, request: KeyframeGenerationRequest, project_url: str
    ) -> KeyframeGenerationResult:
        attempt = 1
        if self.store:
            attempt = self.store.claim_output_reconciliation(
                request.operation_id,
                project_url,
                max_attempts=MAX_OUTPUT_RECONCILIATION_ATTEMPTS,
                lease_owner=self.lease_owner,
            )

        if os.environ.get("VF_TEST_FLOW_REUSE_IMAGE") == "1":
            out_img = _create_minimal_png(self.output_dir / f"{request.operation_id}.png", 720, 1280, "blue")
            return KeyframeGenerationResult(
                operation_id=request.operation_id,
                status="completed",
                provider_operation_id=project_url,
                image_path=str(out_img),
                metadata={"provider_kind": "google_flow", "project_url": project_url, "output_reconciled": True},
            )

        if attempt is None or attempt >= MAX_OUTPUT_RECONCILIATION_ATTEMPTS:
            fin = attempt or MAX_OUTPUT_RECONCILIATION_ATTEMPTS
            return KeyframeGenerationResult(
                operation_id=request.operation_id,
                status="unknown",
                provider_operation_id=project_url,
                error_code="OUTPUT_RECONCILIATION_EXHAUSTED",
                error_message="Output retrieval reached attempt limit",
                metadata={
                    "provider_kind": "google_flow",
                    "project_url": project_url,
                    "send_attempted": True,
                    "output_reconciliation_attempts": fin,
                    "output_reconciliation_finished_attempt": fin,
                },
            )

        return KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="unknown",
            provider_operation_id=project_url,
            error_code="RECONCILIATION_REQUIRED",
            error_message="Flow post-Send timeout simulating unknown outcome",
            metadata={
                "provider_kind": "google_flow",
                "project_url": project_url,
                "send_attempted": True,
                "output_reconciliation_attempts": attempt,
                "output_reconciliation_finished_attempt": attempt,
            },
        )

    def get_status(self, operation_id: str) -> KeyframeGenerationResult:
        return KeyframeGenerationResult(operation_id, "unknown", error_code="RECONCILIATION_REQUIRED")
