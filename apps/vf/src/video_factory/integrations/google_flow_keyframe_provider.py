"""Google Flow Playwright Keyframe Generation Provider."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from PIL import Image
from playwright.sync_api import sync_playwright

from video_factory.integrations.ports import (
    KeyframeGenerationPort,
    KeyframeGenerationRequest,
    KeyframeGenerationResult,
)

logger = logging.getLogger(__name__)


class FlowOutputAspectRatioError(RuntimeError):
    """Google Flow returned a decoded image that is not native portrait output."""

KEYFRAME_NEGATIVE_PROMPT = (
    "watermark, text, seller logo, marketplace badge, price tag, promotional frame, "
    "blurry, low quality, morphing product, distorted structural integrity, "
    "warped LCD screen, altered form factor, deformed hardware, missing components"
)



class GoogleFlowKeyframeProvider(KeyframeGenerationPort):
    provider_kind = "google_flow"
    _ASPECT_RATIO_PROMPT = (
        "Create one photorealistic, native full-bleed 9:16 portrait keyframe. "
        "Fill the entire canvas with one continuous vertical scene; the image itself "
        "must be 9:16 portrait, not a square image placed inside a portrait canvas. "
        "Do not add black bars, borders, padding, or an inset square image."
    )

    def __init__(
        self,
        profile_path: Path | str | None = None,
        chrome_profile_directory: str = "Default",
        output_dir: Path | str | None = None,
        model: str = "ImageFx / Imagen 3",
        timeout_seconds: float = 180.0,
        headless: bool = False,
        ui_contract_verified: bool = False,
        reference_upload_verified: bool = False,
        cost_contract_verified: bool = False,
        store: Any | None = None,
        on_project_created: Any | None = None,
    ) -> None:
        self.profile_path = Path(profile_path).expanduser().resolve() if profile_path else None
        self.chrome_profile_directory = chrome_profile_directory
        self.output_dir = Path(output_dir).expanduser().resolve() if output_dir else (Path.cwd() / "keyframes")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.headless = headless
        self.ui_contract_verified = bool(ui_contract_verified)
        # A UI selector check alone cannot prove that the product reference was
        # attached to the generation request. Keep this separate so an
        # accidentally enabled Image mode still fails closed before Send.
        self.reference_upload_verified = bool(reference_upload_verified)
        self.cost_contract_verified = bool(cost_contract_verified)
        self.store = store
        self.on_project_created = on_project_created

    def bind_operation_store(self, store: Any) -> None:
        self.store = store

    @property
    def profile_id(self) -> str:
        return str(self.profile_path) if self.profile_path else ""

    @classmethod
    def from_config(cls, output_dir: Path | str | None = None) -> GoogleFlowKeyframeProvider:
        try:
            from video_factory.config import load_config
            cfg = load_config()
        except ImportError:
            cfg = {}
        video_cfg = cfg.get("video_factory", {}) if isinstance(cfg, dict) else {}
        flow_cfg = (
            video_cfg.get("google_flow_keyframe")
            or video_cfg.get("google_flow")
            or {}
        )
        if not isinstance(flow_cfg, dict):
            flow_cfg = {}
        profile_path = flow_cfg.get("profile_path")
        if profile_path:
            p_cand = Path(str(profile_path))
            if not p_cand.is_dir():
                s = str(profile_path)
                if s.startswith("D:") and Path("Z:" + s[2:]).is_dir():
                    profile_path = Path("Z:" + s[2:])
                elif s.startswith("Z:") and Path("D:" + s[2:]).is_dir():
                    profile_path = Path("D:" + s[2:])
        if not profile_path or str(profile_path).strip() in ("auto", "isolated", ""):
            from video_factory.integrations.chrome_profile import get_isolated_flow_profile_dir
            profile_path = get_isolated_flow_profile_dir()

        chrome_profile_directory = str(flow_cfg.get("chrome_profile_directory", "Default"))
        model = str(flow_cfg.get("model", "ImageFx / Imagen 3"))
        timeout = float(flow_cfg.get("timeout_seconds", 180.0))
        headless = bool(flow_cfg.get("headless", False))
        ui_contract_verified = bool(flow_cfg.get("ui_contract_verified", False))
        reference_upload_verified = bool(flow_cfg.get("reference_upload_verified", False))
        cost_contract_verified = bool(flow_cfg.get("cost_contract_verified", False))
        return cls(
            profile_path=profile_path,
            chrome_profile_directory=chrome_profile_directory,
            output_dir=output_dir,
            model=model,
            timeout_seconds=timeout,
            headless=headless,
            ui_contract_verified=ui_contract_verified,
            reference_upload_verified=reference_upload_verified,
            cost_contract_verified=cost_contract_verified,
        )

    def is_available(self) -> bool:
        if not self.profile_path:
            return False
        return (
            self.profile_path.is_dir()
            and self.ui_contract_verified
            and self.reference_upload_verified
            and self.cost_contract_verified
        )

    def _output_path(self, operation_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", operation_id).strip("._") or "keyframe"
        output = (self.output_dir / f"{safe[:120]}.png").resolve()
        return output

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @classmethod
    def _compose_generation_prompt(
        cls, prompt: str, aspect_ratio: str, anatomy_text: str
    ) -> str:
        full_prompt = f"{prompt}\n{anatomy_text}".strip() if anatomy_text else prompt
        ratio_instruction = cls._ASPECT_RATIO_PROMPT.replace("9:16", aspect_ratio)
        return f"{full_prompt}\n{ratio_instruction}".strip()

    def _preflight(
        self, request: KeyframeGenerationRequest
    ) -> tuple[KeyframeGenerationResult | None, list[Path], Path]:
        """Validate request inputs, capability flags, and lock state before launching browser."""
        output_file = self._output_path(request.operation_id)
        if not request.prompt.strip() or not request.product_description.strip():
            return (
                KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="failed",
                    error_code="INVALID_INPUT",
                    error_message="prompt and product_description are required",
                ),
                [],
                output_file,
            )
        reference_paths = [Path(value).resolve() for value in request.reference_image_paths]
        if not reference_paths or any(
            not path.is_file() or path.stat().st_size == 0 for path in reference_paths
        ):
            return (
                KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="failed",
                    error_code="REFERENCE_IMAGE_NOT_FOUND",
                    error_message="at least one readable product reference is required",
                ),
                reference_paths,
                output_file,
            )

        if not self.ui_contract_verified:
            return (
                KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="failed",
                    error_code="GOOGLE_FLOW_UI_UNVERIFIED",
                    error_message="Google Flow image-mode selectors and reference conditioning have not been observed and verified",
                    metadata={"provider_kind": self.provider_kind, "model": self.model},
                ),
                reference_paths,
                output_file,
            )
        if not self.reference_upload_verified:
            return (
                KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="failed",
                    error_code="GOOGLE_FLOW_REFERENCE_UPLOAD_UNVERIFIED",
                    error_message="Google Flow reference upload and attached-image confirmation have not been observed and verified",
                    metadata={"provider_kind": self.provider_kind, "model": self.model},
                ),
                reference_paths,
                output_file,
            )
        if not self.cost_contract_verified:
            return (
                KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="failed",
                    error_code="GOOGLE_FLOW_COST_UNVERIFIED",
                    error_message="Google Flow keyframe cost and account balance have not been observed and verified",
                    metadata={"provider_kind": self.provider_kind, "model": self.model},
                ),
                reference_paths,
                output_file,
            )
        if not self.profile_path or not self.profile_path.is_dir():
            return (
                KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="failed",
                    error_code="GOOGLE_FLOW_UNAVAILABLE",
                    error_message="Google Flow browser profile directory not found or not configured",
                ),
                reference_paths,
                output_file,
            )

        if output_file.is_file() and output_file.stat().st_size > 0:
            return (
                KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="unknown",
                    error_code="RECONCILIATION_REQUIRED",
                    error_message="an output already exists for this operation; reconcile the durable operation before retrying",
                    metadata={"provider_kind": self.provider_kind, "model": self.model},
                ),
                reference_paths,
                output_file,
            )
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            if (self.profile_path / lock_name).exists():
                return (
                    KeyframeGenerationResult(
                        operation_id=request.operation_id,
                        status="unknown",
                        error_code="GOOGLE_FLOW_PROFILE_BUSY",
                        error_message="Google Flow profile is already in use; wait for the existing job",
                        metadata={"provider_kind": self.provider_kind, "model": self.model},
                    ),
                    reference_paths,
                    output_file,
                )
        return None, reference_paths, output_file

    def _prepare_ui_and_reference(
        self,
        page: Any,
        request: KeyframeGenerationRequest,
        reference_paths: list[Path],
    ) -> str:
        """Navigate to Flow, create project, configure image mode, attach references, and set prompt."""
        page.goto("https://flow.google.com/", timeout=self.timeout_seconds * 1000)

        # Do not treat the absence of a login warning as proof of a session.
        # Flow's public /about page has no such warning and used to pass this
        # check, only failing later when the workspace button was missing.
        self._assert_authenticated(page)

        # Click the visible classed action when available. The old union also
        # matched hidden responsive controls, making a valid page look
        # ambiguous before any generation request was sent.
        self._unique_visible_new_project_button(page).click()
        page.wait_for_url(re.compile(r"https://flow\.google\.com/project/[0-9a-f-]{36}"), timeout=self.timeout_seconds * 1000)
        project_url = str(page.url)

        # Dismiss any tour modal dialogs (change-log modal, what's new, etc.)
        page.keyboard.press("Escape")
        modal_close = page.locator("button.modal-close-button, mat-dialog-actions button, button[aria-label='Close'], button[aria-label='Đóng']")
        if modal_close.count() == 1 and modal_close.is_visible():
            modal_close.click()

        self._ensure_image_generation_settings(page)

        self._attach_image_references(page, reference_paths)

        # Prompt input with spatial anatomy injection
        prompt_inputs = page.locator("div.ProseMirror")
        if prompt_inputs.count() != 1:
            raise RuntimeError("GOOGLE_FLOW_PROMPT_SELECTOR_NOT_UNIQUE")
        prompt_input = prompt_inputs
        prompt_input.wait_for(timeout=10000)
        prompt_input.focus()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(200)

        opt = request.provider_options or {}
        meta = getattr(request, "metadata", {}) or {}
        anatomy_text = meta.get("anatomy_injection") or opt.get("anatomy_injection", "")
        full_prompt = self._compose_generation_prompt(
            request.prompt, request.aspect_ratio, anatomy_text
        )

        neg = request.negative_prompt or KEYFRAME_NEGATIVE_PROMPT
        anatomy_neg = meta.get("anatomy_negative") or opt.get("anatomy_negative", "")
        full_neg = f"{neg}, {anatomy_neg}".strip(", ") if anatomy_neg else neg
        prompt_text = f"{full_prompt}. Negative prompt: {full_neg}"

        page.keyboard.insert_text(prompt_text)
        page.wait_for_timeout(500)

        if not prompt_input.inner_text().strip():
            prompt_input.click()
            prompt_input.focus()
            page.keyboard.insert_text(prompt_text)
            page.wait_for_timeout(500)
        return project_url

    def _attach_image_references(self, page: Any, reference_paths: list[Path]) -> None:
        from playwright.sync_api import expect
        from video_factory.integrations.flow_ui_recipe import attach_reference_asset, unique

        timeout_ms = int(self.timeout_seconds * 1000)
        if page.locator("button.chip-container:visible").count():
            raise RuntimeError("GOOGLE_FLOW_REFERENCE_PROMPT_NOT_EMPTY")
        for count, source in enumerate(reference_paths, 1):
            unique(page.locator("button.add-menu-trigger:visible"),
                   "GOOGLE_FLOW_REFERENCE_PICKER_NOT_UNIQUE", timeout_ms).click(timeout=timeout_ms)
            attach_reference_asset(page, Path(source), self.output_dir, timeout_ms)
            expect(page.locator("button.chip-container[aria-busy='false']:visible")).to_have_count(count, timeout=timeout_ms)
        if page.locator("button.chip-container:visible").count() != len(reference_paths):
            raise RuntimeError("GOOGLE_FLOW_REFERENCE_PROMPT_ATTACH_NOT_CONFIRMED")

    @staticmethod
    def _selected_state(locator: Any) -> bool | None:
        """Read an explicit selected state from a Flow control; unknown fails closed."""
        for attribute in ("aria-pressed", "aria-checked", "aria-selected"):
            value = locator.get_attribute(attribute)
            if value is not None:
                normalized = str(value).strip().casefold()
                if normalized in {"true", "1", "selected", "checked"}:
                    return True
                if normalized in {"false", "0", "not selected", "unchecked"}:
                    return False
        classes = str(locator.get_attribute("class") or "").casefold()
        if any(token in classes for token in ("mat-button-toggle-checked", "is-selected", "selected", "active")):
            return True
        if classes and any(token in classes for token in ("mat-button-toggle", "toggle-button", "radio")):
            return False
        return None

    def _ensure_image_generation_settings(self, page: Any) -> None:
        """Use manual Image mode; agent defaults do not govern manual Send."""
        from video_factory.integrations.flow_ui_recipe import configure_manual_generation

        configure_manual_generation(
            page, model=self.model, aspect="9:16",
            timeout_ms=int(self.timeout_seconds * 1000),
        )

    @staticmethod
    def _unique_visible_new_project_button(page: Any) -> Any:
        locator = page.locator("button.new-project-button:visible")
        if locator.count() == 0:
            locator = page.locator(
                "button:has-text('Dự án mới'):visible, button:has-text('New project'):visible"
            )
        if locator.count() != 1:
            raise RuntimeError("GOOGLE_FLOW_NEW_PROJECT_SELECTOR_NOT_UNIQUE")
        return locator.first

    @staticmethod
    def _assert_authenticated(page: Any) -> None:
        authenticated = any(
            page.query_selector(selector) is not None
            for selector in (
                ".profile-button",
                ".account-button",
            )
        )
        if authenticated:
            return
        settings = page.locator("button.settings-trigger-button:visible")
        composer = page.locator("div.ProseMirror:visible")
        if "/project/" in str(getattr(page, "url", "")):
            try:
                composer.first.wait_for(state="visible", timeout=10_000)
            except Exception:
                pass
        if (settings.count() == 1 and settings.first.is_visible()
                and composer.count() == 1 and composer.first.is_visible()):
            return
        # Flow's workspace no longer consistently exposes the account/profile
        # classes above. A single visible "new project" workspace action is
        # also direct evidence that this browser reached the authenticated app.
        workspace_action = page.locator("button.new-project-button:visible")
        try:
            workspace_action.first.wait_for(state="visible", timeout=10_000)
        except Exception:
            pass
        if workspace_action.count() == 0:
            workspace_action = page.locator(
                "button:has-text('Dự án mới'):visible, button:has-text('New project'):visible"
            )
            try:
                workspace_action.first.wait_for(state="visible", timeout=5_000)
            except Exception:
                pass
        if workspace_action.count() == 1 and workspace_action.first.is_visible():
            return
        if page.query_selector(".auth-required") is not None or "/about" in str(getattr(page, "url", "")):
            raise RuntimeError("GOOGLE_FLOW_AUTH_REQUIRED")
        raise RuntimeError("GOOGLE_FLOW_AUTH_UNVERIFIED")

    def _persist_project_checkpoint(
        self, request: KeyframeGenerationRequest, project_url: str
    ) -> None:
        """Durably persist project URL, operation ID, and request digest before Generate attempt.

        Fail-closed: If saving fails or if no durable store/callback is provided, raise an exception to abort before Send.
        """
        if not project_url:
            raise RuntimeError("GOOGLE_FLOW_PROJECT_URL_MISSING")

        persisted = False
        if self.store is not None:
            if hasattr(self.store, "record_project_checkpoint"):
                self.store.record_project_checkpoint(request, project_url, self.provider_kind)
                persisted = True
            elif hasattr(self.store, "record_project_created"):
                self.store.record_project_created(request, project_url)
                persisted = True

        req_store = getattr(request, "store", None) or (getattr(request, "metadata", {}) or {}).get("store")
        if not persisted and req_store is not None and req_store is not self.store:
            if hasattr(req_store, "record_project_checkpoint"):
                req_store.record_project_checkpoint(request, project_url, self.provider_kind)
                persisted = True
            elif hasattr(req_store, "record_project_created"):
                req_store.record_project_created(request, project_url)
                persisted = True

        if not persisted and self.on_project_created is not None:
            is_configured = getattr(self, "configured_mode", False) or (self.ui_contract_verified and self.cost_contract_verified)
            try:
                receipt = self.on_project_created(project_url, request)
            except Exception as exc:
                raise RuntimeError(f"DURABLE_CHECKPOINT_FAILED: on_project_created raised {exc}") from exc

            if is_configured:
                # In configured mode, callback alone cannot satisfy durable checkpoint unless it returns an explicit durable receipt
                if isinstance(receipt, dict) and receipt.get("persisted") is True and str(receipt.get("receipt_id", "")).strip():
                    persisted = True
                elif hasattr(receipt, "persisted") and getattr(receipt, "persisted") and getattr(receipt, "receipt_id", None):
                    persisted = True
                else:
                    raise RuntimeError(
                        "DURABLE_CHECKPOINT_REQUIRED: in configured mode, on_project_created must return a verified durable receipt "
                        "(dict or CheckpointReceipt with persisted=True and receipt_id). Void/unconfirmed callback is not permitted."
                    )
            else:
                if isinstance(receipt, dict) and receipt.get("persisted") is True:
                    persisted = True
                elif hasattr(receipt, "persisted") and getattr(receipt, "persisted"):
                    persisted = True
                elif isinstance(receipt, str) and receipt.strip():
                    persisted = True
                elif receipt is True:
                    persisted = True

        if not persisted:
            raise RuntimeError(
                "DURABLE_CHECKPOINT_REQUIRED: Google Flow cannot Send without a verified durable store or durable receipt. "
                "Store must implement record_project_checkpoint / record_project_created."
            )

    def _locate_generate_button(self, page: Any) -> Any:
        """Locate and verify the Generate button selector BEFORE attempting to click.

        If the selector is invalid, missing, not unique, or not visible, this fails pre-Send.
        """
        submit_buttons = page.locator('button[aria-label="Bắt đầu tạo"], button.generate-icon-button, button:has-text("arrow_forward")')
        try:
            submit_buttons.first.wait_for(timeout=10000)
        except Exception as exc:
            raise RuntimeError("GOOGLE_FLOW_GENERATE_SELECTOR_NOT_UNIQUE") from exc
        if submit_buttons.count() != 1 or not submit_buttons.is_visible():
            raise RuntimeError("GOOGLE_FLOW_GENERATE_SELECTOR_NOT_UNIQUE")
        for _ in range(10):
            if not submit_buttons.first.is_disabled():
                break
            page.wait_for_timeout(500)
        return submit_buttons

    def _send_generation(self, submit_btn: Any) -> None:
        """Attempt one Send; a click error must reach the reconciliation path."""
        # A still-enabled button cannot prove that a previous keyboard Send was
        # rejected. Use one action only, and never retry an ambiguous click here.
        submit_btn.first.click(timeout=3000)

    @staticmethod
    def _current_image_tile_sources(page: Any) -> set[str]:
        """Return image URLs already present before sending a generation request."""
        sources: set[str] = set()
        images = page.locator("flow-image-tile img")
        for index in range(images.count()):
            src = images.nth(index).get_attribute("src")
            if src:
                sources.add(src.split("=")[0])
        return sources

    def _download_generated_output(
        self,
        page: Any,
        output_file: Path,
        *,
        initial_sources: set[str],
        expected_source: str | None = None,
    ) -> None:
        """Poll for generated image tile and download the full-res output."""
        poll_seconds = min(self.timeout_seconds, 30.0) if expected_source else self.timeout_seconds
        for _ in range(max(1, int(poll_seconds // 3))):
            page.wait_for_timeout(3000)
            tiles = page.locator("flow-image-tile")
            if tiles.count() == 0:
                tiles = page.locator("flow-tile-container")

            target_tile = None
            # Flow keeps uploaded reference tiles alongside generated results.
            # Only select a visible tile whose image URL was absent before Send.
            for i in reversed(range(tiles.count())):
                candidate = tiles.nth(i)
                img = candidate.locator("img")
                if img.count() != 1 or not img.is_visible():
                    continue
                source = (img.first.get_attribute("src") or "").split("=")[0]
                if source and source not in initial_sources:
                    if expected_source and source != expected_source:
                        continue
                    target_tile = candidate
                    break

            if target_tile is not None:
                img_candidates = target_tile.locator("img")
                if img_candidates.count() == 1 and img_candidates.is_visible():
                    raw_src = img_candidates.first.get_attribute("src") or ""
                    if isinstance(raw_src, str) and raw_src.startswith("http"):
                        page.wait_for_timeout(1000)
                        full_res_url = (
                            re.sub(r"=s\d+(?:-rw)?$", "=s0", raw_src)
                            if "?" not in raw_src else raw_src
                        )
                        try:
                            resp = page.request.get(full_res_url)
                            if hasattr(resp, "status") and resp.status == 200:
                                body = resp.body() if callable(getattr(resp, "body", None)) else b""
                                if isinstance(body, bytes) and len(body) > 1000:
                                    output_file.write_bytes(body)
                                    break
                        except Exception as e:
                            logger.info(f"Direct download note: {e}")

                    # Fallback 1: More menu download
                    if not output_file.is_file() or output_file.stat().st_size == 0:
                        try:
                            target_tile.hover(timeout=2000)
                            page.wait_for_timeout(500)
                            more_buttons = target_tile.locator('button[aria-label*="Tuỳ chọn khác" i], button[aria-label*="More" i]')
                            if more_buttons.count() == 1 and more_buttons.first.is_visible():
                                more_buttons.first.click(timeout=2000)
                                page.wait_for_timeout(500)
                                download_items = page.locator('[role=menuitem]').filter(has_text=re.compile(r"Tải xuống|Download", re.I))
                                if download_items.count() == 1:
                                    download_items.first.click(timeout=2000)
                                    page.wait_for_timeout(250)
                                    original_size = page.get_by_text(
                                        re.compile(r"^(?:Kích thước gốc|Original size)$", re.I)
                                    )
                                    visible_original_size = [
                                        original_size.nth(i)
                                        for i in range(original_size.count())
                                        if original_size.nth(i).is_visible()
                                    ]
                                    if len(visible_original_size) == 1:
                                        label = visible_original_size[0]
                                        # The visible text is often only the secondary
                                        # label inside Flow's resolution row. Click the
                                        # nearest actionable row so its handler receives
                                        # the event instead of clicking the sublabel.
                                        row = label.locator(
                                            "xpath=ancestor-or-self::*[@role='menuitem' or @role='option' or @role='button' or self::button][1]"
                                        )
                                        click_target = (
                                            row.first
                                            if row.count() == 1 and row.first.is_visible()
                                            else label
                                        )
                                        with page.expect_download(timeout=15000) as download_info:
                                            click_target.click(timeout=3000)
                                        download = download_info.value
                                        download.save_as(str(output_file))
                                        break
                                    page.keyboard.press("Escape")
                        except Exception as e:
                            logger.info(f"Fallback menu download note: {e}")
                            page.keyboard.press("Escape")

                    # Fallback 2: Preview click download
                    if not output_file.is_file() or output_file.stat().st_size == 0:
                        try:
                            page.keyboard.press("Escape")
                            img_candidates.first.click(timeout=2000)
                            page.wait_for_timeout(1000)
                            full_downloads = page.locator('button[aria-label*="Tải xuống" i], button[aria-label*="Download" i]')
                            if full_downloads.count() == 1 and full_downloads.first.is_visible():
                                with page.expect_download(timeout=15000) as download_info:
                                    full_downloads.first.click()
                                download = download_info.value
                                download.save_as(str(output_file))
                                break
                        except Exception as e:
                            logger.info(f"Fallback preview download note: {e}")

            if output_file.is_file() and output_file.stat().st_size > 0:
                break

        if not output_file.is_file() or output_file.stat().st_size == 0:
            raise RuntimeError("GOOGLE_FLOW_KEYFRAME_TIMEOUT")

    def _existing_project_sources(
        self, page: Any, request: KeyframeGenerationRequest,
    ) -> tuple[set[str], str | None]:
        """Identify a sole generated tile in the operation's dedicated Flow project."""
        expected_references = {
            "flow-ref-" + self._sha256(Path(raw_path).resolve()) + Path(raw_path).suffix.lower()
            for raw_path in request.reference_image_paths
        }
        tiles = page.locator("flow-image-tile")
        if tiles.count() == 0:
            tiles = page.locator("flow-tile-container")
        reference_sources: set[str] = set()
        generated_sources: list[str] = []
        observed_references: set[str] = set()
        for index in range(tiles.count()):
            tile = tiles.nth(index)
            images = tile.locator("img")
            if images.count() != 1 or not images.first.is_visible():
                continue
            src = str(images.first.get_attribute("src") or "").split("=")[0]
            if not src:
                continue
            footer = tile.locator("flow-tile-hover-footer button")
            label = str(footer.first.get_attribute("aria-label") or "") if footer.count() == 1 else ""
            if label in expected_references:
                observed_references.add(label)
                reference_sources.add(src)
            else:
                generated_sources.append(src)
        if observed_references != expected_references:
            return reference_sources, None
        if len(generated_sources) != 1:
            return reference_sources, None
        return reference_sources, generated_sources[0]

    def reconcile_existing_output(
        self, request: KeyframeGenerationRequest, project_url: str,
    ) -> KeyframeGenerationResult:
        """Poll/download only the already-checkpointed Flow project; never Generate."""
        project_url = str(project_url or "").strip()
        if not re.fullmatch(r"https://flow\.google\.com/project/[0-9a-fA-F-]{36}", project_url):
            return KeyframeGenerationResult(
                request.operation_id, "unknown", provider_operation_id=project_url or None,
                error_code="FLOW_PROJECT_CHECKPOINT_INVALID",
                error_message="Recorded Google Flow project URL is missing or malformed",
            )
        if self.store is None or not hasattr(self.store, "claim_output_reconciliation"):
            return KeyframeGenerationResult(
                request.operation_id, "unknown", provider_operation_id=project_url,
                error_code="OUTPUT_RECONCILIATION_UNAVAILABLE",
                error_message="Durable operation store is required for bounded output reconciliation",
            )
        attempt = None
        output_file = self._output_path(request.operation_id)
        temp_file = output_file.with_name(output_file.stem + ".download.tmp")
        try:
            if not self.profile_path or not self.profile_path.is_dir():
                raise RuntimeError("GOOGLE_FLOW_PROFILE_UNAVAILABLE")
            if any((self.profile_path / name).exists() for name in ("SingletonLock", "SingletonCookie", "SingletonSocket")):
                raise RuntimeError("GOOGLE_FLOW_PROFILE_BUSY")
            for raw_path in request.reference_image_paths:
                path = Path(raw_path).resolve()
                if not path.is_file() or not path.stat().st_size:
                    raise RuntimeError("GOOGLE_FLOW_REFERENCE_MISSING_FOR_RECONCILIATION")
            with sync_playwright() as p:
                args = []
                if self.chrome_profile_directory and self.chrome_profile_directory != "Default":
                    args.append(f"--profile-directory={self.chrome_profile_directory}")
                browser = p.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_path), headless=self.headless,
                    channel="chrome", args=args, accept_downloads=True,
                )
                try:
                    page = browser.pages[0] if browser.pages else browser.new_page()
                    page.goto(project_url, timeout=self.timeout_seconds * 1000)
                    current_url = str(getattr(page, "url", "")).rstrip("/")
                    if current_url != project_url.rstrip("/"):
                        raise RuntimeError("GOOGLE_FLOW_PROJECT_URL_MISMATCH")
                    self._assert_authenticated(page)
                    attempt = self.store.claim_output_reconciliation(
                        request.operation_id, project_url, max_attempts=3,
                    )
                    if attempt is None:
                        return KeyframeGenerationResult(
                            request.operation_id, "unknown", provider_operation_id=project_url,
                            error_code="OUTPUT_RECONCILIATION_EXHAUSTED",
                            error_message="Output retrieval reached its three-attempt limit; generation remains unreconciled",
                            metadata={"provider_kind": self.provider_kind, "project_url": project_url},
                        )
                    reference_sources: set[str] = set()
                    generated_source = None
                    for _ in range(max(1, int(self.timeout_seconds // 3))):
                        reference_sources, generated_source = self._existing_project_sources(page, request)
                        if generated_source:
                            break
                        page.wait_for_timeout(3000)
                    if not generated_source:
                        raise RuntimeError("GOOGLE_FLOW_OUTPUT_NOT_RETRIEVED")
                    self._download_generated_output(
                        page, temp_file, initial_sources=reference_sources,
                        expected_source=generated_source,
                    )
                finally:
                    browser.close()
            try:
                width, height = self._validate_and_format_output(temp_file)
            except Exception as exc:
                raise ValueError(f"GOOGLE_FLOW_OUTPUT_INVALID: {exc}") from exc
            try:
                expected_width, expected_height = (int(part) for part in request.aspect_ratio.split(":"))
            except (TypeError, ValueError):
                expected_width, expected_height = 9, 16
            expected_ratio = expected_width / expected_height
            if abs((width / height) - expected_ratio) > expected_ratio * 0.01:
                raise ValueError(f"GOOGLE_FLOW_OUTPUT_INVALID: decoded image is {width}x{height}")
            temp_file.replace(output_file)
            return KeyframeGenerationResult(
                request.operation_id, "completed", provider_operation_id=project_url,
                image_path=str(output_file), metadata={
                    "provider_kind": self.provider_kind, "model": self.model,
                    "project_url": project_url, "output_reconciled": True,
                    "download_attempt": attempt, "output_dimensions": [width, height],
                },
            )
        except Exception as exc:
            error = str(exc)
            if "GOOGLE_FLOW_OUTPUT_INVALID" in error:
                code = "GOOGLE_FLOW_OUTPUT_INVALID"
            elif attempt is None:
                known_preflight = (
                    "GOOGLE_FLOW_AUTH_UNVERIFIED", "GOOGLE_FLOW_AUTH_REQUIRED",
                    "GOOGLE_FLOW_PROJECT_URL_MISMATCH", "GOOGLE_FLOW_PROFILE_UNAVAILABLE",
                    "GOOGLE_FLOW_PROFILE_BUSY", "GOOGLE_FLOW_REFERENCE_MISSING_FOR_RECONCILIATION",
                )
                code = next((item for item in known_preflight if item in error), "GOOGLE_FLOW_PROJECT_ACCESS_FAILED")
            else:
                code = "GOOGLE_FLOW_OUTPUT_NOT_RETRIEVED"
            logger.info("Flow output reconciliation attempt %s failed for %s: %s", attempt, request.operation_id, error)
            return KeyframeGenerationResult(
                request.operation_id, "unknown", provider_operation_id=project_url,
                error_code=code, error_message=f"Existing Flow output could not be verified/downloaded: {error}",
                metadata={"provider_kind": self.provider_kind, "project_url": project_url,
                          **({"output_reconciliation_attempts": attempt} if attempt is not None else {})},
            )
        finally:
            if temp_file.exists():
                temp_file.unlink()

    def _validate_and_format_output(self, output_file: Path) -> tuple[int, int]:
        """Validate decodable Flow bytes and preserve their original dimensions for QC."""
        with Image.open(output_file) as im:
            im.verify()
            width, height = im.size
            if width < 100 or height < 100:
                raise ValueError(f"Corrupted or empty keyframe: {width}x{height}")
        return width, height

    def submit(self, request: KeyframeGenerationRequest) -> KeyframeGenerationResult:
        early_res, reference_paths, output_file = self._preflight(request)
        if early_res is not None:
            return early_res

        project_url = ""
        reference_sha256 = [self._sha256(path) for path in reference_paths]
        prompt_digest = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
        send_attempted = False
        try:
            with sync_playwright() as p:
                context_args = []
                user_data_dir = str(self.profile_path)
                if self.chrome_profile_directory and self.chrome_profile_directory != "Default":
                    context_args.append(f"--profile-directory={self.chrome_profile_directory}")

                browser = p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=self.headless,
                    channel="chrome",
                    args=context_args,
                    accept_downloads=True,
                )
                try:
                    page = browser.pages[0] if browser.pages else browser.new_page()
                    # Phase 1: Prepare UI, project, references, and prompt
                    project_url = self._prepare_ui_and_reference(page, request, reference_paths)

                    # Durable checkpoint: Persist project URL BEFORE Send attempt
                    self._persist_project_checkpoint(request, project_url)

                    # Phase 2A: Verify Generate button selector pre-Send
                    submit_btn = self._locate_generate_button(page)

                    # Phase 2B: Send attempt (only from the moment click starts)
                    initial_image_sources = self._current_image_tile_sources(page)
                    send_attempted = True
                    self._send_generation(submit_btn)

                    # Phase 3: Identify new output tile and download
                    self._download_generated_output(
                        page,
                        output_file,
                        initial_sources=initial_image_sources,
                    )
                finally:
                    browser.close()

            # Phase 4: Validate image file and standardize format
            output_dimensions = self._validate_and_format_output(output_file)
            requested_width, requested_height = (int(part) for part in request.aspect_ratio.split(":"))
            expected_ratio = requested_width / requested_height
            output_ratio = output_dimensions[0] / output_dimensions[1]
            aspect_ratio_verified = abs(output_ratio - expected_ratio) <= expected_ratio * 0.01

            return KeyframeGenerationResult(
                operation_id=request.operation_id,
                status="completed",
                provider_operation_id=project_url or None,
                image_path=str(output_file),
                metadata={
                    "provider_kind": self.provider_kind,
                    "model": self.model,
                    "aspect_ratio": request.aspect_ratio,
                    "reference_sha256": reference_sha256,
                    "reference_upload_confirmed": True,
                    "prompt_sha256": prompt_digest,
                    "project_url": project_url,
                    "image_mode_verified": True,
                    "aspect_ratio_setting_verified": True,
                    "aspect_ratio_verified": aspect_ratio_verified,
                    "output_dimensions": list(output_dimensions),
                    "output_aspect_ratio": f"{output_dimensions[0]}:{output_dimensions[1]}",
                },
            )
        except Exception as exc:
            logger.error("GoogleFlowKeyframeProvider error: %s", exc)
            if send_attempted and isinstance(exc, FlowOutputAspectRatioError):
                dimensions = None
                try:
                    with Image.open(output_file) as image:
                        dimensions = image.size
                except Exception:
                    pass
                return KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="failed",
                    provider_operation_id=project_url or request.operation_id,
                    image_path=str(output_file) if output_file.is_file() else None,
                    error_code="GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED",
                    error_message=str(exc),
                    metadata={
                        "provider_kind": self.provider_kind,
                        "model": self.model,
                        "project_url": project_url,
                        "send_attempted": True,
                        "output_received": output_file.is_file(),
                        "output_dimensions": list(dimensions) if dimensions else None,
                        "output_sha256": self._sha256(output_file) if output_file.is_file() else None,
                        "image_mode_verified": True,
                        "aspect_ratio_verified": True,
                    },
                )
            if send_attempted:
                return KeyframeGenerationResult(
                    operation_id=request.operation_id,
                    status="unknown",
                    provider_operation_id=project_url or request.operation_id,
                    error_code="RECONCILIATION_REQUIRED",
                    error_message=(
                        f"Google Flow generate request was submitted (project: {project_url or 'unknown'}), "
                        f"but output completion could not be verified: {exc}. Manual reconciliation required."
                    ),
                    metadata={
                        "provider_kind": self.provider_kind,
                        "model": self.model,
                        "project_url": project_url,
                        "send_attempted": True,
                    },
                )
            error_code = (
                "DURABLE_CHECKPOINT_REQUIRED"
                if "DURABLE_CHECKPOINT_REQUIRED" in str(exc)
                else "GOOGLE_FLOW_KEYFRAME_FAILED"
            )
            return KeyframeGenerationResult(
                operation_id=request.operation_id,
                status="failed",
                error_code=error_code,
                error_message=str(exc),
                metadata={
                    "provider_kind": self.provider_kind,
                    "model": self.model,
                    "project_url": project_url,
                    "send_attempted": False,
                },
            )

    def get_status(self, operation_id: str) -> KeyframeGenerationResult:
        """Fail closed on keyframe status queries.

        Google Flow lacks a queryable API or cryptographic provenance linking browser
        tiles to operation IDs. Automated reconciliation is intentionally not supported
        to prevent false-positive output adoption.

        Operator manual action required:
        1. Open the recorded project URL in browser to inspect the generated tile.
        2. Verify that the generated image visually matches the requested prompt and references.
        3. Save the verified image to the designated output path and record its digest.
        4. If generation failed or project is invalid, record the terminal failure to unblock workflow retry.
        """
        project_url = operation_id if "https://" in str(operation_id) else None
        return KeyframeGenerationResult(
            operation_id=operation_id,
            status="unknown",
            provider_operation_id=operation_id,
            error_code="RECONCILIATION_REQUIRED",
            error_message=(
                "Google Flow keyframe status requires manual operator reconciliation; "
                "local files, screenshots, and unverified browser tiles are not evidence of completion. "
                f"Operator must manually inspect project '{operation_id}' and confirm verified output."
            ),
            metadata={
                "provider_kind": self.provider_kind,
                "project_url": project_url,
            },
        )

    def generate_keyframe(
        self,
        prompt: Any,
        reference_image: Path | str | None = None,
        aspect_ratio: str = "9:16",
        **kwargs: Any,
    ) -> Any:
        """Generate keyframe supporting request object or prompt+reference_image."""
        if hasattr(prompt, "operation_id") and hasattr(prompt, "prompt"):
            req = KeyframeGenerationRequest(
                operation_id=prompt.operation_id,
                workflow_id=getattr(prompt, "workflow_id", "pv-legacy"),
                project_id=getattr(prompt, "project_id", "project-legacy"),
                frame_id=getattr(prompt, "frame_id", "scene-1-start"),
                prompt=prompt.prompt,
                product_description=getattr(prompt, "product_description", prompt.prompt),
                reference_image_paths=tuple(getattr(prompt, "reference_image_paths", ())),
            )
            return self.submit(req)

        # Called with prompt string and optional reference_image
        prompt_str = str(prompt)
        ref_paths = (str(reference_image),) if reference_image else ()
        op_id = hashlib.sha256(f"{prompt_str}_{reference_image}_{aspect_ratio}".encode()).hexdigest()[:16]
        req = KeyframeGenerationRequest(
            operation_id=f"kf-{op_id}",
            workflow_id="pv-direct",
            project_id="project-direct",
            frame_id="frame-1",
            prompt=prompt_str,
            product_description=prompt_str,
            reference_image_paths=ref_paths,
            aspect_ratio=aspect_ratio,
        )
        res = self.submit(req)
        if res.status == "completed" and res.image_path:
            return Path(res.image_path)
        raise RuntimeError(f"Keyframe generation failed: {res.error_message}")
