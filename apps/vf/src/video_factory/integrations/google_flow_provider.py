"""Google Flow Playwright video generation provider for Hermes Personal Video Factory.

Operates as the concrete edge adapter driving Google Flow via Playwright browser
automation for AI video generation. Preserves strict fail-closed semantics without
any fallback to synthetic zoom/pan slideshow videos.
"""

from __future__ import annotations

import asyncio
import logging
import os
import hashlib
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from video_factory.integrations.ports import (
    VideoGenerationPort,
    VideoGenerationReconciliationPort,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from video_factory.job_contracts import TerminalJobError
from video_factory.integrations.flow_ui_recipe import (
    FlowUploadConsentRequired,
    configure_manual_generation,
    ensure_manual_mode,
    unique,
)

logger = logging.getLogger(__name__)

SUPPORTED_ASPECT_RATIOS = frozenset({"9:16", "16:9"})
DEFAULT_TIMEOUT_SECONDS = 300.0
FLOW_HOME = "https://flow.google.com/"
FLOW_PROJECT = re.compile(r"^https://flow\.google\.com/project/[0-9a-f-]{36}$", re.I)
FLOW_VIDEO_PENDING_ICONS = frozenset({"movie", "videocam"})


class GoogleFlowBrowserPage:
    """Flow UI boundary. One project and one video tile belong to one request.

    The project title is a deterministic, externally searchable request marker.
    An accepted project URL is usable as an operation ID only after a video tile
    is observed. If the browser closes in the paid-click window, lookup can find
    that tile without ever issuing another Generate click.
    """

    def __init__(
        self,
        *,
        profile_path: Path | str,
        chrome_profile_directory: str,
        output_dir: Path,
        model: str = "Omni 1.1 Flash",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        post_download_observation_seconds: float = 30.0,
        ui_contract_verified: bool = False,
        headless: bool = False,
    ) -> None:
        self.profile_path = Path(profile_path).expanduser().resolve()
        self.chrome_profile_directory = chrome_profile_directory
        self.output_dir = output_dir
        self.model = model
        self.ui_contract_verified = bool(ui_contract_verified)
        self.headless = headless
        self.timeout_ms = int(timeout_seconds * 1000)
        self.post_download_observation_ms = int(post_download_observation_seconds * 1000)

    @staticmethod
    def _marker(request_id: str) -> str:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
        return f"hermes-flow-{digest}"

    @contextmanager
    def _browser(self) -> Iterator[Any]:
        from playwright.sync_api import sync_playwright

        # Never remove Chrome singleton locks: another Flow worker may own this profile.
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_f = Path(self.profile_path) / lock_name
            if lock_f.exists():
                raise RuntimeError("GOOGLE_FLOW_PROFILE_BUSY")

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_path),
                headless=self.headless,
                channel="chrome",
                args=[f"--profile-directory={self.chrome_profile_directory}"],
                accept_downloads=True,
            )
            try:
                yield context
            finally:
                context.close()

    def _home(self, context: Any) -> Any:
        page = context.new_page()
        page.goto(FLOW_HOME, wait_until="domcontentloaded", timeout=self.timeout_ms)
        page.locator("button.new-project-button:visible, button:has-text('Dự án mới'):visible").first.wait_for(timeout=self.timeout_ms)
        if urlparse(page.url).hostname != "flow.google.com" or "/about" in page.url:
            raise RuntimeError("GOOGLE_FLOW_AUTH_REQUIRED")
        return page

    @staticmethod
    def _close_agent_panel(page: Any) -> None:
        """Select manual creation without dismissing unrelated dialogs."""
        ensure_manual_mode(page)

    @staticmethod
    def _project_card(page: Any, marker: str) -> Any | None:
        try:
            page.locator("flow-project-card").first.wait_for(timeout=10000)
        except Exception:
            return None
        cards = page.locator("flow-project-card").filter(has=page.locator(".project-title-label", has_text=marker))
        count = cards.count()
        if count > 1:
            raise RuntimeError("GOOGLE_FLOW_DUPLICATE_REQUEST_PROJECT")
        return cards if count == 1 else None

    @staticmethod
    def _project_url(card: Any) -> str:
        links = card.locator('a[href^="/project/"]')
        if links.count() != 1:
            raise RuntimeError("GOOGLE_FLOW_PROJECT_LINK_NOT_UNIQUE")
        href = links.get_attribute("href")
        url = f"https://flow.google.com{href or ''}"
        if not FLOW_PROJECT.fullmatch(url):
            raise RuntimeError("GOOGLE_FLOW_INVALID_PROJECT_URL")
        return url

    def is_ready(self) -> bool:
        if not self.profile_path.is_dir() or not self.chrome_profile_directory:
            return False
        if not self.ui_contract_verified:
            return False
        try:
            with self._browser() as context:
                page = self._home(context)
                page.close()
            return True
        except Exception:
            logger.info("Google Flow browser profile is not ready")
            return False

    @staticmethod
    def _credit_balance_on_page(page: Any) -> int | None:
        account = page.locator(
            '[aria-label="Thông tin về tài khoản"], [aria-label="Account information"]'
        )
        if account.count() != 1:
            return None
        account.click(timeout=5000)
        try:
            for _ in range(10):
                for frame in page.frames:
                    try:
                        body = frame.locator("body").inner_text(timeout=1000)
                    except Exception:
                        continue
                    match = re.search(
                        r"([\d.,]+)\s+(?:tín dụng Google Flow|(?:Google )?Flow credits)",
                        body, re.I,
                    )
                    if match:
                        return int(re.sub(r"\D", "", match.group(1)))
                page.wait_for_timeout(500)
            return None
        finally:
            close = page.locator("flow-account-panel button.close-btn:visible")
            if close.count() == 1:
                close.click(timeout=5000)
            else:
                page.keyboard.press("Escape")
            page.locator(".cdk-overlay-backdrop:visible").wait_for(state="hidden", timeout=5000)

    def available_credits(self) -> int | None:
        try:
            with self._browser() as context:
                page = self._home(context)
                return self._credit_balance_on_page(page)
        except Exception:
            logger.info("Google Flow credit balance is not readable")
            return None

    def _status_in_project(self, page: Any, *, request_id: str, project_url: str,
                           operation_accepted: bool = False) -> VideoGenerationResult:
        if page.url.rstrip("/") != project_url.rstrip("/"):
            page.goto(project_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if page.url.rstrip("/") != project_url.rstrip("/"):
            return VideoGenerationResult(
                request_id=request_id, status="failed", provider_operation_id=project_url,
                error_code="RECONCILIATION_REQUIRED",
                error_message="Flow project is not accessible in this account",
            )
        self._close_agent_panel(page)
        page.locator("button.settings-trigger-button:visible, div.ProseMirror").first.wait_for(timeout=self.timeout_ms)
        tiles = page.locator("flow-scene-tile, flow-video-tile")
        if tiles.count() == 0:
            tiles = page.locator("flow-tile-container")
        try:
            tiles.first.wait_for(timeout=15000)
        except Exception:
            if operation_accepted:
                return VideoGenerationResult(
                    request_id=request_id, status="running", provider_operation_id=project_url,
                    metadata={"provider_kind": "google_flow", "tile_not_visible": True},
                )
            return VideoGenerationResult(
                request_id=request_id, status="failed", provider_operation_id=project_url,
                error_code="RECONCILIATION_REQUIRED",
                error_message="Flow project has no accepted video tile",
            )
        containers = page.locator("flow-tile-container")
        if containers.count() == 0:
            if operation_accepted:
                return VideoGenerationResult(
                    request_id=request_id, status="running", provider_operation_id=project_url,
                    metadata={"provider_kind": "google_flow", "tile_not_visible": True},
                )
            return VideoGenerationResult(
                request_id=request_id, status="failed", provider_operation_id=project_url,
                error_code="RECONCILIATION_REQUIRED",
                error_message="Flow video tile container is not uniquely identifiable",
            )
        # 1. If any pending/progress tile is present, video is actively rendering
        pending = page.locator("flow-pending-tile:visible")
        pending_icons = page.locator("flow-pending-tile:visible mat-icon").all_text_contents()
        if sum(icon.strip().lower() in FLOW_VIDEO_PENDING_ICONS for icon in pending_icons) > 0:
            return VideoGenerationResult(
                request_id=request_id, status="running", provider_operation_id=project_url,
                metadata={"provider_kind": "google_flow"},
            )

        # 2. Must have a visible completed video tile (never fall back to reference image tile)
        vt = page.locator("flow-video-tile:visible")
        if vt.count() == 0:
            if operation_accepted:
                return VideoGenerationResult(
                    request_id=request_id, status="running", provider_operation_id=project_url,
                    metadata={"provider_kind": "google_flow", "tile_not_visible": True},
                )
            return VideoGenerationResult(
                request_id=request_id, status="failed", provider_operation_id=project_url,
                error_code="RECONCILIATION_REQUIRED",
                error_message="Flow project has no visible pending generation or completed video tile; outcome is ambiguous",
                metadata={"provider_kind": "google_flow"},
            )

        parent = vt.first.locator("xpath=ancestor::flow-tile-container")
        container = parent if parent.count() > 0 else vt.first
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            container.hover(timeout=5000)
        except PlaywrightTimeoutError:
            return VideoGenerationResult(
                request_id=request_id, status="running", provider_operation_id=project_url,
                metadata={"provider_kind": "google_flow"},
            )
        options = container.locator('button[aria-label="Tuỳ chọn khác"], button[aria-label*="Tuỳ chọn"]')
        if options.count() == 0:
            return VideoGenerationResult(
                request_id=request_id, status="running", provider_operation_id=project_url,
                metadata={"provider_kind": "google_flow"},
            )
        try:
            options.first.click(timeout=5000)
        except PlaywrightTimeoutError:
            return VideoGenerationResult(
                request_id=request_id, status="running", provider_operation_id=project_url,
                metadata={"provider_kind": "google_flow"},
            )
        download = page.locator("[role=menuitem]").filter(has_text="Tải xuống")
        if download.count() == 0:
            page.keyboard.press("Escape")
            return VideoGenerationResult(
                request_id=request_id, status="running", provider_operation_id=project_url,
                metadata={"provider_kind": "google_flow"},
            )
        with page.expect_download(timeout=self.timeout_ms) as download_info:
            download.first.click()
            page.wait_for_timeout(500)
            original = page.locator("[role=menuitem]").filter(has_text="720p")
            if original.count() == 1:
                original.first.click()
        download_file = download_info.value
        project_id = project_url.rsplit("/", 1)[-1]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        target = self.output_dir / f"flow-{project_id}.mp4"
        temp = self.output_dir / f"flow-{project_id}.download"
        download_file.save_as(temp)
        if download_file.failure() is not None:
            raise RuntimeError("GOOGLE_FLOW_DOWNLOAD_FAILED")
        if not temp.is_file() or temp.stat().st_size == 0:
            raise RuntimeError("GOOGLE_FLOW_EMPTY_DOWNLOAD")
        downloaded_bytes = temp.stat().st_size
        page.wait_for_timeout(self.post_download_observation_ms)
        if not temp.is_file() or temp.stat().st_size != downloaded_bytes:
            raise RuntimeError("GOOGLE_FLOW_DOWNLOAD_CHANGED_AFTER_COMPLETION")
        temp.replace(target)
        return VideoGenerationResult(
            request_id=request_id, status="completed", provider_operation_id=project_url,
            video_path=str(target), metadata={"provider_kind": "google_flow"},
        )

    def lookup_by_request_id(self, request_id: str) -> VideoGenerationResult | None:
        marker = self._marker(request_id)
        with self._browser() as context:
            page = self._home(context)
            card = self._project_card(page, marker)
            if card is None:
                return None
            project_url = self._project_url(card)
            result = self._status_in_project(page, request_id=request_id, project_url=project_url)
            return None if result.error_code == "RECONCILIATION_REQUIRED" else result

    def get_status(self, provider_operation_id: str) -> VideoGenerationResult:
        if not FLOW_PROJECT.fullmatch(provider_operation_id):
            return VideoGenerationResult(
                request_id="", status="failed", provider_operation_id=provider_operation_id,
                error_code="RECONCILIATION_REQUIRED", error_message="Invalid Flow project operation ID",
            )
        with self._browser() as context:
            page = context.new_page()
            return self._status_in_project(
                page, request_id="", project_url=provider_operation_id, operation_accepted=True,
            )

    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        import random
        import time
        delay = random.uniform(8, 15)
        time.sleep(delay)
        if request.duration_seconds not in {4, 6, 8, 10}:
            return VideoGenerationResult(
                request_id=request.request_id, status="failed", error_code="FLOW_DURATION_UNSUPPORTED",
                error_message="Configured Flow model supports 4, 6, 8 or 10 second scenes",
            )
        requested_model = request.provider_options.get("flow_model")
        if requested_model and requested_model != self.model:
            return VideoGenerationResult(
                request_id=request.request_id, status="failed", error_code="FLOW_MODEL_MISMATCH",
                error_message="Requested Flow model differs from configured model",
            )
        if request.end_keyframe is not None:
            return VideoGenerationResult(
                request_id=request.request_id, status="failed", error_code="END_KEYFRAME_UNSUPPORTED",
                error_message="Flow END frame UI has not been verified for production submission",
            )
        if len(request.reference_image_paths) != 1:
            return VideoGenerationResult(
                request_id=request.request_id, status="failed", error_code="START_FRAME_REQUIRED",
                error_message="Flow Frames mode requires exactly one START reference image",
            )
        source = Path(request.reference_image_paths[0])
        if not source.is_file():
            return VideoGenerationResult(
                request_id=request.request_id, status="failed", error_code="START_FRAME_MISSING",
                error_message="START reference image is missing",
            )
        marker = self._marker(request.request_id)
        with self._browser() as context:
            page = self._home(context)
            card = self._project_card(page, marker)
            if card is not None:
                project_url = self._project_url(card)
                # No visible tile cannot prove a previous paid click failed.
                return self._status_in_project(page, request_id=request.request_id, project_url=project_url)
            new_btn = page.locator("button.new-project-button:visible, button:has-text('Dự án mới'):visible").first
            new_btn.click()
            page.wait_for_url(re.compile(r"https://flow\.google\.com/project/[0-9a-f-]{36}"), timeout=self.timeout_ms)
            project_url = page.url
            # Dismiss any tour dialogs or backdrop overlays that may intercept clicks
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            self._close_agent_panel(page)
            more_options = page.locator("button.more-options-button:visible, button[aria-label*='lựa chọn khác cho dự án']:visible").first
            if more_options.count() == 1:
                more_options.click()
                menu_items = page.locator("[role=menuitem]")
                if menu_items.count() > 0:
                    menu_items.first.click()
                    rename = page.locator("input.editable-text-input.editing:visible")
                    unique(rename, "GOOGLE_FLOW_REQUEST_MARKER_NOT_EDITABLE", self.timeout_ms).fill(marker)
                    rename.press("Enter")
                    rename.wait_for(state="hidden", timeout=self.timeout_ms)
            if marker not in page.locator("body").inner_text():
                raise RuntimeError("GOOGLE_FLOW_REQUEST_MARKER_NOT_SAVED")
            displayed_cost = self._configure_project(page, request)
            try:
                self._attach_start_frame(page, source)
            except FlowUploadConsentRequired as exc:
                return VideoGenerationResult(
                    request_id=request.request_id, status="failed", provider_operation_id=project_url,
                    error_code=exc.code, error_message=str(exc), metadata={"send_attempted": False},
                )
            return self._generate(page, request, project_url, displayed_cost)

    def _assert_start_frame(self, page: Any) -> None:
        start = page.locator("button.empty-chip:visible").filter(has_text=re.compile(r"B\u1eaft \u0111\u1ea7u|Start", re.I))
        end = page.locator("button.empty-chip:visible").filter(has_text=re.compile(r"K\u1ebft th\u00fac|End", re.I))
        chip = page.locator("button.chip-container:visible")
        if start.count() or end.count() != 1 or chip.count() != 1:
            raise RuntimeError("GOOGLE_FLOW_START_IMAGE_NOT_ATTACHED")
        if chip.get_attribute("aria-busy") != "false":
            raise RuntimeError("GOOGLE_FLOW_START_IMAGE_NOT_READY")

    def _attach_start_frame(self, page: Any, source: Path) -> None:
        start = page.locator("button.empty-chip:visible").filter(has_text=re.compile(r"B\u1eaft \u0111\u1ea7u|Start", re.I))
        unique(start, "GOOGLE_FLOW_START_CONTROL_UNVERIFIED", self.timeout_ms).click(timeout=self.timeout_ms)
        from video_factory.integrations.flow_ui_recipe import attach_reference_asset
        attach_reference_asset(page, source, self.output_dir, self.timeout_ms)
        start.wait_for(state="detached", timeout=self.timeout_ms)
        unique(page.locator("button.chip-container[aria-busy='false']:visible"), "GOOGLE_FLOW_START_IMAGE_NOT_READY", self.timeout_ms)
        self._assert_start_frame(page)

    def _configure_project(self, page: Any, request: VideoGenerationRequest) -> int:
        return configure_manual_generation(
            page, model=self.model, aspect=request.aspect_ratio,
            duration=request.duration_seconds, timeout_ms=self.timeout_ms,
        )

    def _generate(
        self, page: Any, request: VideoGenerationRequest, project_url: str, displayed_cost: int
    ) -> VideoGenerationResult:
        prompt_inputs = page.locator("div.ProseMirror:visible").first
        prompt_inputs.wait_for(timeout=self.timeout_ms)
        prompt_inputs.click()
        prompt_inputs.focus()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(200)
        page.keyboard.insert_text(request.prompt)
        page.wait_for_timeout(500)
        if not prompt_inputs.inner_text().strip():
            prompt_inputs.click()
            prompt_inputs.focus()
            page.keyboard.insert_text(request.prompt)
            page.wait_for_timeout(500)
        actual_lines = [line.strip() for line in prompt_inputs.inner_text().replace("\r\n", "\n").splitlines() if line.strip()]
        expected_lines = [line.strip() for line in request.prompt.replace("\r\n", "\n").splitlines() if line.strip()]
        if "\n".join(actual_lines) != "\n".join(expected_lines):
            raise RuntimeError("GOOGLE_FLOW_PROMPT_NOT_VERIFIED")

        # A missing empty control alone is not proof of START attachment.
        self._assert_start_frame(page)

        # Verify settings button displays duration and x1 output
        stb = page.locator("button.settings-trigger-button:visible").first
        stb_text = stb.inner_text().strip()
        if f"{request.duration_seconds} giây" not in stb_text and f"{request.duration_seconds}s" not in stb_text:
            raise RuntimeError(f"GOOGLE_FLOW_DURATION_NOT_VERIFIED: {stb_text}")
        if "x1" not in stb_text:
            raise RuntimeError(f"GOOGLE_FLOW_OUTPUT_COUNT_NOT_VERIFIED: {stb_text}")

        available_credits = self._credit_balance_on_page(page)
        if available_credits is not None and available_credits < displayed_cost:
            return VideoGenerationResult(
                request_id=request.request_id, status="failed",
                error_code="FLOW_INSUFFICIENT_CREDITS",
                error_message=f"Flow balance ({available_credits}) is below the displayed generation cost ({displayed_cost})",
                metadata={"provider_kind": "google_flow"},
            )
        submit = page.locator('button[aria-label="Bắt đầu tạo"]:visible, button.generate-icon-button:visible').first
        try:
            from playwright.sync_api import expect
            expect(submit).to_be_enabled(timeout=min(self.timeout_ms, 60000))
        except Exception as exc:
            raise RuntimeError("GOOGLE_FLOW_GENERATE_DISABLED") from exc

        # Ensure any lingering dialogs or backdrop overlays are dismissed
        for _ in range(3):
            if page.locator(".cdk-overlay-backdrop:visible").count() > 0:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
        page.locator(".cdk-overlay-backdrop:visible").wait_for(state="hidden", timeout=5000)

        # This is the only paid action. Timeout after this point is ambiguous.
        def video_counts(current_page: Any) -> tuple[int, int]:
            return (
                current_page.locator("flow-video-tile:visible").count(),
                sum(icon.strip().lower() in FLOW_VIDEO_PENDING_ICONS for icon in
                    current_page.locator("flow-pending-tile:visible mat-icon").all_text_contents()),
            )

        baseline_video, baseline_pending_video = video_counts(page)
        rejection = None
        send_requests = set()

        def observe_request(outgoing: Any) -> None:
            target = urlparse(outgoing.url)
            if (target.hostname == "flow.google.com" and target.path.endswith("/batchexecute")
                    and outgoing.method == "POST"):
                send_requests.add(outgoing)

        def observe_rejection(response: Any) -> None:
            nonlocal rejection
            if response.request not in send_requests:
                return
            try:
                rejected = re.search(r"\bPUBLIC_ERROR_UNUSUAL_ACTIVITY\b", response.text())
            except Exception:
                return  # A missing response body is not proof of rejection.
            if rejected:
                rejection = VideoGenerationResult(
                    request_id=request.request_id, status="failed", provider_operation_id=project_url,
                    error_code="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
                    error_message="Google Flow rejected the video request as unusual activity",
                    metadata={"provider_kind": "google_flow"},
                )

        page.on("request", observe_request)
        page.on("response", observe_rejection)
        try:
            submit.click(timeout=3000)
            saw_send_tile = False
            deadline = time.monotonic() + min(20.0, self.timeout_ms / 1000)
            while time.monotonic() < deadline:
                videos, pending = video_counts(page)
                saw_send_tile = videos > baseline_video or pending > baseline_pending_video
                if saw_send_tile or rejection:
                    break
                page.wait_for_timeout(250)
            else:
                if not rejection:
                    err = TerminalJobError(
                        "Flow paid Send has no confirmed video tile; reconcile the existing project before retrying",
                        code="GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN",
                    )
                    err.provider_operation_id = project_url
                    raise err
            if saw_send_tile:
                page.wait_for_timeout(3000)
            # A pending tile in the submitting tab can be optimistic. Read
            # the same project in a fresh tab while keeping the sender open.
            observation = page.context.new_page()
            try:
                observation.goto(project_url, wait_until="domcontentloaded", timeout=min(self.timeout_ms, 15000))
                observation_seconds = min(45.0, self.timeout_ms / 1000)
                refresh_interval = min(5.0, observation_seconds / 4)
                deadline = time.monotonic() + observation_seconds
                refresh_at = time.monotonic() + refresh_interval
                while observation.url.rstrip("/") == project_url.rstrip("/") and time.monotonic() < deadline:
                    videos, pending = video_counts(observation)
                    if videos > baseline_video or pending > baseline_pending_video:
                        break
                    if rejection and not saw_send_tile:
                        return rejection
                    # A tab opened before Flow persisted the job can show a
                    # stale project snapshot. Refresh the observer, not Send.
                    remaining_ms = int((deadline - time.monotonic()) * 1000)
                    if time.monotonic() >= refresh_at and remaining_ms > 0:
                        observation.reload(wait_until="domcontentloaded", timeout=min(remaining_ms, 10000))
                        refresh_at = time.monotonic() + refresh_interval
                    observation.wait_for_timeout(250)
                else:
                    if rejection and observation.url.rstrip("/") == project_url.rstrip("/"):
                        return rejection
                    err = TerminalJobError(
                        "Flow video tile was not confirmed after reopening the project; reconcile before retrying",
                        code="GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN",
                    )
                    err.provider_operation_id = project_url
                    raise err
            finally:
                observation.close()
        except TerminalJobError:
            raise
        except Exception as exc:
            err = TerminalJobError(
                f"Flow post-send observation failed; reconcile before retrying: {exc}",
                code="GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN",
            )
            err.provider_operation_id = project_url
            raise err from exc
        finally:
            page.remove_listener("response", observe_rejection)
            page.remove_listener("request", observe_request)
        return VideoGenerationResult(
            request_id=request.request_id, status="submitted",
            provider_operation_id=project_url,
            metadata={"provider_kind": "google_flow", "model": self.model,
                      "displayed_cost_credits": displayed_cost},
        )

    def resume_prepared_project(
        self, request: VideoGenerationRequest, project_url: str, *, confirmed_no_accepted_generation: bool
    ) -> VideoGenerationResult:
        """Operator recovery after independently verifying no tile or credit charge."""
        if not confirmed_no_accepted_generation:
            raise ValueError("independent confirmation of no accepted paid generation is required")
        if not FLOW_PROJECT.fullmatch(project_url):
            raise ValueError("invalid Flow project URL")
        with self._browser() as context:
            page = self._home(context)
            card = self._project_card(page, self._marker(request.request_id))
            if card is None or self._project_url(card) != project_url:
                raise RuntimeError("GOOGLE_FLOW_PREPARED_PROJECT_NOT_FOUND")
            page.goto(project_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._close_agent_panel(page)
            page.locator("button.settings-trigger-button:visible, div.ProseMirror").first.wait_for(timeout=self.timeout_ms)
            if page.locator("flow-pending-tile, flow-scene-tile, flow-video-tile").count():
                raise RuntimeError("GOOGLE_FLOW_REQUEST_PROJECT_EXISTS_RECONCILE")
            source = Path(request.reference_image_paths[0])
            if not source.is_file():
                raise RuntimeError("GOOGLE_FLOW_PREPARED_START_FRAME_NOT_VERIFIABLE")
            source_name = f"flow-ref-{hashlib.sha256(source.read_bytes()).hexdigest()}{source.suffix.lower()}"
            if page.locator("flow-image-tile button").filter(has=page.locator("span", has_text=source_name)).count() != 1:
                raise RuntimeError("GOOGLE_FLOW_PREPARED_START_FRAME_NOT_VERIFIABLE")
            displayed_cost = self._configure_project(page, request)
            self._attach_start_frame(page, source)
            return self._generate(page, request, project_url, displayed_cost)


class GoogleFlowPlaywrightProvider(VideoGenerationPort, VideoGenerationReconciliationPort):
    """Concrete Playwright-driven Google Flow video generation provider.

    Implements VideoGenerationPort and VideoGenerationReconciliationPort with
    strict fail-closed guarantees (never falls back to synthetic zoom videos).
    """

    provider_kind: str = "google_flow"

    def __init__(
        self,
        output_dir: Path | str | None = None,
        browser_context_factory: Callable[[], Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        page_object: Any | None = None,
        profile_path: Path | str | None = None,
        chrome_profile_directory: str = "Default",
        model: str = "Omni 1.1 Flash",
        flow_project_url: str | None = None,
        ui_contract_verified: bool = False,
        headless: bool = False,
    ) -> None:
        self.provider_kind = "google_flow"
        configured_dir = output_dir or os.environ.get("HERMES_VIDEO_FACTORY_WORKSPACE") or ""
        self.output_dir = Path(configured_dir).expanduser().resolve() if configured_dir else (Path.cwd() / "workspace")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._browser_context_factory = browser_context_factory
        self.timeout = timeout
        self.profile_path = Path(profile_path).expanduser().resolve() if profile_path else None
        self.chrome_profile_directory = chrome_profile_directory
        self.model = model
        self.flow_project_url = flow_project_url
        self.ui_contract_verified = bool(ui_contract_verified)
        if page_object is None and self.profile_path:
            self._page_object = GoogleFlowBrowserPage(
                profile_path=self.profile_path,
                chrome_profile_directory=self.chrome_profile_directory,
                output_dir=self.output_dir,
                model=self.model,
                timeout_seconds=self.timeout,
                ui_contract_verified=self.ui_contract_verified,
                headless=headless,
            )
        else:
            self._page_object = page_object

    async def _upload_reference_image(self, page: Any, image_path: Path) -> bool:
        """Upload reference image to Flow's conditioning panel.

        Args:
            page: Playwright page object with Flow loaded.
            image_path: Path to reference image (must be 9:16 or will be rejected).

        Returns:
            True if upload successful, False if upload slot not found or failed.
        """
        image_path = Path(image_path)
        if not image_path.exists():
            logger.warning("Reference image not found: %s", image_path)
            return False

        try:
            file_input = page.locator('input[type="file"][accept*="image"]')
            count = await file_input.count() if hasattr(file_input, "count") and asyncio.iscoroutinefunction(file_input.count) else file_input.count()
            if count == 0:
                logger.warning("Reference image upload slot not found in Flow UI")
                return False

            if hasattr(file_input, "set_input_files"):
                if asyncio.iscoroutinefunction(file_input.set_input_files):
                    await file_input.set_input_files(str(image_path))
                else:
                    file_input.set_input_files(str(image_path))
            logger.info("Uploaded reference image: %s", image_path.name)

            wait_fn = getattr(page, "wait_for_selector", None)
            if wait_fn:
                if asyncio.iscoroutinefunction(wait_fn):
                    await page.wait_for_selector(
                        'img[src*="blob:"], div[class*="preview"], div[class*="thumbnail"]',
                        timeout=10000,
                        state="visible",
                    )
                else:
                    page.wait_for_selector(
                        'img[src*="blob:"], div[class*="preview"], div[class*="thumbnail"]',
                        timeout=10000,
                        state="visible",
                    )
            logger.info("Reference image preview confirmed")
            return True
        except Exception as e:
            logger.error("Reference image upload failed: %s", e)
            return False

    async def generate_keyframe(
        self,
        prompt: str,
        reference_image: Path | None = None,
        aspect_ratio: str = "9:16",
        model: str | None = None,
        retry: int = 0,
    ) -> Path:
        """Keyframe generation is owned by GoogleFlowKeyframeProvider."""
        raise RuntimeError(
            "GOOGLE_FLOW_KEYFRAME_UNAVAILABLE: video adapter cannot copy references or create placeholders"
        )

    @classmethod
    def from_config(cls, output_dir: Path | str | None = None) -> GoogleFlowPlaywrightProvider:
        try:
            from video_factory.config import load_config
            cfg = load_config()
        except ImportError:
            cfg = {}
        video_cfg = cfg.get("video_factory", {}) if isinstance(cfg, dict) else {}
        flow_cfg = video_cfg.get("google_flow", {}) if isinstance(video_cfg, dict) else {}
        profile_path = flow_cfg.get("profile_path") if isinstance(flow_cfg, dict) else None
        if not profile_path or str(profile_path).strip() in ("auto", "isolated", ""):
            from video_factory.integrations.chrome_profile import get_isolated_flow_profile_dir
            isolated_dir = get_isolated_flow_profile_dir()
            if isolated_dir.is_dir():
                profile_path = isolated_dir
            else:
                profile_path = isolated_dir

        if profile_path and not Path(profile_path).exists():
            for alt_drive in ("Z:", "D:"):
                alt_str = str(profile_path)
                if alt_str.startswith(("D:", "d:", "Z:", "z:")):
                    alt_p = Path(alt_drive + alt_str[2:])
                    if alt_p.exists():
                        profile_path = alt_p
                        break

        if not profile_path:
            return cls(output_dir=output_dir)
        resolved_output = Path(output_dir).expanduser().resolve() if output_dir else (Path.cwd() / "workspace")
        page = GoogleFlowBrowserPage(
            profile_path=profile_path,
            chrome_profile_directory=str(flow_cfg.get("chrome_profile_directory", "Default")),
            output_dir=resolved_output,
            model=str(flow_cfg.get("model", "Omni 1.1 Flash")),
            post_download_observation_seconds=float(flow_cfg.get("post_download_observation_seconds", 30)),
            ui_contract_verified=bool(flow_cfg.get("ui_contract_verified", False)),
            headless=bool(flow_cfg.get("headless", False)),
        )
        return cls(
            output_dir=resolved_output,
            page_object=page,
            ui_contract_verified=bool(flow_cfg.get("ui_contract_verified", False)),
        )

    def is_available(self) -> bool:
        """Check local configuration; operation methods verify the live Flow session."""
        # Provider selection runs before capacity admission, so it must not open Chrome.
        if not self.ui_contract_verified:
            return False
        page = self._page_object
        if page is None:
            return False
        if not all(
            callable(getattr(page, method, None))
            for method in ("submit", "get_status", "lookup_by_request_id")
        ):
            return False
        profile_path = getattr(page, "profile_path", self.profile_path)
        profile_directory = getattr(page, "chrome_profile_directory", self.chrome_profile_directory)
        return bool(profile_path and Path(profile_path).is_dir() and profile_directory)

    @property
    def profile_id(self) -> str:
        profile_path = getattr(self._page_object, "profile_path", self.profile_path)
        return str(Path(profile_path).resolve()) if profile_path else ""

    def can_submit(self, request: VideoGenerationRequest) -> bool:
        """Check provider availability before claiming a scene."""
        return self.is_available()

    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Submit a video generation request to Google Flow via Playwright.

        Fails closed with detailed error status if Google Flow is unavailable or unauthenticated.
        """
        if not self.is_available():
            return VideoGenerationResult(
                request_id=request.request_id,
                status="failed",
                error_message="GOOGLE_FLOW_UNAVAILABLE: Playwright or browser session is not available",
                metadata={"provider_kind": "google_flow", "code": "GOOGLE_FLOW_UNAVAILABLE"},
            )

        if not request.prompt or not str(request.prompt).strip():
            return VideoGenerationResult(
                request_id=request.request_id,
                status="failed",
                error_message="PROMPT_REQUIRED: Video generation requires a non-empty prompt",
                metadata={"provider_kind": "google_flow", "code": "PROMPT_REQUIRED"},
            )

        aspect_ratio = request.aspect_ratio or "9:16"
        if aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
            return VideoGenerationResult(
                request_id=request.request_id,
                status="failed",
                error_message=f"INVALID_ASPECT_RATIO: '{aspect_ratio}' is not supported (allowed: {sorted(SUPPORTED_ASPECT_RATIOS)})",
                metadata={"provider_kind": "google_flow", "code": "INVALID_ASPECT_RATIO"},
            )

        # The configured adapter is deliberately unavailable until a real
        # authenticated page implementation can reconcile accepted requests.
        res = self._page_object.submit(request)
        if (
            not isinstance(res, VideoGenerationResult)
            or res.request_id != request.request_id
            or (
                res.status in ("submitted", "running", "completed")
                and not str(res.provider_operation_id or "").strip()
            )
        ):
            return VideoGenerationResult(
                request_id=request.request_id,
                status="failed",
                error_code="GOOGLE_FLOW_INVALID_RESULT",
                error_message="Flow did not return a verifiable operation for this request",
                metadata={"provider_kind": "google_flow", "code": "GOOGLE_FLOW_INVALID_RESULT"},
            )
        return res

    def get_status(self, provider_operation_id: str) -> VideoGenerationResult:
        """Poll Google Flow operation status by provider_operation_id."""
        if not provider_operation_id:
            return VideoGenerationResult(
                request_id="",
                status="failed",
                error_message="OPERATION_ID_REQUIRED: provider_operation_id is required",
                metadata={"provider_kind": "google_flow"},
            )

        # The status page validates the actual account/project in the same
        # session; a separate readiness browser immediately before it is redundant.
        if self.ui_contract_verified and self._page_object is not None:
            return self._page_object.get_status(provider_operation_id)
        return VideoGenerationResult(
            request_id=provider_operation_id,
            status="failed",
            provider_operation_id=provider_operation_id,
            error_code="RECONCILIATION_REQUIRED",
            error_message="Flow operation cannot be polled without an authenticated session",
            metadata={"provider_kind": "google_flow", "code": "RECONCILIATION_REQUIRED"},
        )

    def lookup_by_request_id(self, request_id: str) -> VideoGenerationResult | None:
        """Look up existing operation result for crash reconciliation."""
        if not request_id:
            return None

        if self.ui_contract_verified and self._page_object is not None:
            return self._page_object.lookup_by_request_id(request_id)
        return None
