from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from video_factory.integrations.capacity import ProviderCapacityStore
from video_factory.integrations.google_flow_provider import (
    GoogleFlowBrowserPage,
    GoogleFlowPlaywrightProvider,
)
from video_factory.integrations.keyframe_provider import (
    KeyframeOperationStore,
    KeyframeRuntime,
)
from video_factory.integrations.ports import (
    KeyframeGenerationRequest,
    KeyframeGenerationResult,
    VideoGenerationRequest,
)


def test_video_availability_does_not_launch_chrome_before_capacity_admission(tmp_path: Path) -> None:
    page = GoogleFlowBrowserPage(
        profile_path=tmp_path,
        chrome_profile_directory="Default",
        output_dir=tmp_path,
        ui_contract_verified=True,
    )
    page.is_ready = lambda: False
    provider = GoogleFlowPlaywrightProvider(
        output_dir=tmp_path,
        page_object=page,
        ui_contract_verified=True,
    )

    assert provider.is_available() is True
    assert provider.can_submit(VideoGenerationRequest(
        request_id="video-op",
        owner_user_id="owner",
        project_id="project",
        scene_id="scene",
        prompt="prompt",
        duration_seconds=4,
    )) is True


def test_video_browser_refuses_a_busy_profile_before_launch(tmp_path: Path) -> None:
    (tmp_path / "SingletonLock").touch()
    page = GoogleFlowBrowserPage(
        profile_path=tmp_path,
        chrome_profile_directory="Default",
        output_dir=tmp_path,
        ui_contract_verified=True,
    )

    with pytest.raises(RuntimeError, match="GOOGLE_FLOW_PROFILE_BUSY"):
        with page._browser():
            pytest.fail("busy profile must be rejected before launching Chrome")


def test_video_status_requires_reconciliation_when_only_reference_tile_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
    page = MagicMock()
    page.url = project_url
    settings = MagicMock()
    reference_tiles = MagicMock()
    reference_tiles.count.return_value = 0
    reference_tiles.first.wait_for.return_value = None
    containers = MagicMock()
    containers.count.return_value = 1
    pending = MagicMock()
    pending.count.return_value = 0
    pending_icons = MagicMock()
    pending_icons.all_text_contents.return_value = []
    videos = MagicMock()
    videos.count.return_value = 0
    page.locator.side_effect = lambda selector: {
        "button.settings-trigger-button:visible, div.ProseMirror": settings,
        "flow-scene-tile, flow-video-tile": reference_tiles,
        "flow-tile-container": containers,
        "flow-pending-tile:visible": pending,
        "flow-pending-tile:visible mat-icon": pending_icons,
        "flow-video-tile:visible": videos,
    }[selector]
    monkeypatch.setattr(GoogleFlowBrowserPage, "_close_agent_panel", lambda self, current: None)
    browser_page = GoogleFlowBrowserPage(
        profile_path=tmp_path,
        chrome_profile_directory="Default",
        output_dir=tmp_path,
        ui_contract_verified=True,
    )

    result = browser_page._status_in_project(page, request_id="video-op", project_url=project_url)

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert result.provider_operation_id == project_url

    resumed = browser_page._status_in_project(
        page, request_id="video-op", project_url=project_url, operation_accepted=True,
    )
    assert resumed.status == "running"
    assert resumed.provider_operation_id == project_url
    assert resumed.metadata["tile_not_visible"] is True


def test_saved_video_operation_status_uses_accepted_poll_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
    page = GoogleFlowBrowserPage(
        profile_path=tmp_path,
        chrome_profile_directory="Default",
        output_dir=tmp_path,
        ui_contract_verified=True,
    )
    observed = {}

    class Context:
        @staticmethod
        def new_page():
            return object()

    @contextmanager
    def browser(self):
        yield Context()

    def status_in_project(self, current_page, **kwargs):
        observed.update(kwargs)
        from video_factory.integrations.ports import VideoGenerationResult
        return VideoGenerationResult(
            request_id=kwargs["request_id"], status="running",
            provider_operation_id=kwargs["project_url"],
        )

    monkeypatch.setattr(GoogleFlowBrowserPage, "_browser", browser)
    monkeypatch.setattr(GoogleFlowBrowserPage, "_status_in_project", status_in_project)
    result = page.get_status(project_url)

    assert result.status == "running"
    assert observed["operation_accepted"] is True


def test_video_status_does_not_treat_pending_reference_image_as_running_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
    page = MagicMock()
    page.url = project_url
    settings = MagicMock()
    tiles = MagicMock()
    tiles.count.return_value = 1
    tiles.first.wait_for.return_value = None
    containers = MagicMock()
    containers.count.return_value = 1
    pending = MagicMock()
    pending.count.return_value = 1
    pending_icon = MagicMock()
    pending_icon.all_text_contents.return_value = ["image"]
    videos = MagicMock()
    videos.count.return_value = 0
    page.locator.side_effect = lambda selector: {
        "button.settings-trigger-button:visible, div.ProseMirror": settings,
        "flow-scene-tile, flow-video-tile": tiles,
        "flow-tile-container": containers,
        "flow-pending-tile:visible": pending,
        "flow-pending-tile:visible mat-icon": pending_icon,
        "flow-video-tile:visible": videos,
    }[selector]
    monkeypatch.setattr(GoogleFlowBrowserPage, "_close_agent_panel", lambda self, current: None)
    browser_page = GoogleFlowBrowserPage(
        profile_path=tmp_path,
        chrome_profile_directory="Default",
        output_dir=tmp_path,
        ui_contract_verified=True,
    )

    result = browser_page._status_in_project(page, request_id="video-op", project_url=project_url)

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert result.provider_operation_id == project_url


def test_video_status_does_not_treat_pending_without_icon_as_running_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
    page = MagicMock()
    page.url = project_url
    settings = MagicMock()
    tiles = MagicMock()
    tiles.count.return_value = 1
    tiles.first.wait_for.return_value = None
    containers = MagicMock()
    containers.count.return_value = 1
    pending = MagicMock()
    pending.count.return_value = 1
    pending_icon = MagicMock()
    pending_icon.all_text_contents.return_value = []
    videos = MagicMock()
    videos.count.return_value = 0
    page.locator.side_effect = lambda selector: {
        "button.settings-trigger-button:visible, div.ProseMirror": settings,
        "flow-scene-tile, flow-video-tile": tiles,
        "flow-tile-container": containers,
        "flow-pending-tile:visible": pending,
        "flow-pending-tile:visible mat-icon": pending_icon,
        "flow-video-tile:visible": videos,
    }[selector]
    monkeypatch.setattr(GoogleFlowBrowserPage, "_close_agent_panel", lambda self, current: None)
    browser_page = GoogleFlowBrowserPage(
        profile_path=tmp_path,
        chrome_profile_directory="Default",
        output_dir=tmp_path,
        ui_contract_verified=True,
    )

    result = browser_page._status_in_project(page, request_id="video-op", project_url=project_url)

    assert result.status == "failed"
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert result.provider_operation_id == project_url


def test_keyframe_runtime_leases_the_configured_profile(tmp_path: Path) -> None:
    class Provider:
        provider_kind = "google_flow"
        profile_id = str(tmp_path.resolve())

        def __init__(self) -> None:
            self.submit_calls = 0
            self.status_calls = 0

        def submit(self, request: KeyframeGenerationRequest) -> KeyframeGenerationResult:
            self.submit_calls += 1
            return KeyframeGenerationResult(
                operation_id=request.operation_id,
                status="submitted",
                provider_operation_id=f"flow:{request.operation_id}",
            )

        def get_status(self, operation_id: str) -> KeyframeGenerationResult:
            self.status_calls += 1
            return KeyframeGenerationResult(
                operation_id=operation_id,
                status="unknown",
                error_code="RECONCILIATION_REQUIRED",
            )

    connection = sqlite3.connect(tmp_path / "operations.sqlite")
    capacity = ProviderCapacityStore(connection)
    provider = Provider()
    runtime = KeyframeRuntime(
        store=KeyframeOperationStore(connection),
        provider=provider,
        capacity_store=capacity,
        capacity_limit=2,
    )
    def request(operation_id: str) -> KeyframeGenerationRequest:
        return KeyframeGenerationRequest(
            operation_id=operation_id,
            workflow_id="workflow",
            project_id="project",
            frame_id=operation_id,
            prompt="prompt",
            product_description="product",
            reference_image_paths=(str(tmp_path / "unused.png"),),
        )

    first = runtime.run(request("op-1"))
    second = runtime.run(request("op-2"))

    assert first.status == "submitted"
    assert second.status == "unknown"
    assert second.error_code == "PROVIDER_CAPACITY_WAIT"
    assert capacity.active("google_flow")[0]["profile_id"] == str(tmp_path.resolve())

    resumed_worker = KeyframeRuntime(
        store=runtime.store,
        provider=provider,
        capacity_store=capacity,
        capacity_limit=2,
    )
    busy_resume = resumed_worker.run(request("op-1"))
    assert busy_resume.error_code == "PROVIDER_CAPACITY_WAIT"
    assert provider.status_calls == 0

    with connection:
        connection.execute(
            "UPDATE video_factory_provider_capacity SET lease_expires_at=0 WHERE operation_id='op-1'"
        )
    reconciled = resumed_worker.run(request("op-1"))
    assert reconciled.error_code == "RECONCILIATION_REQUIRED"
    assert provider.status_calls == 1
    assert provider.submit_calls == 1
    connection.close()
