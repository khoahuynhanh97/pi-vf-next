"""Verified manual Flow controls shared by image and video adapters."""
from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any


def unique(locator: Any, code: str, timeout_ms: int) -> Any:
    try:
        locator.first.wait_for(state='visible', timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError(code) from exc
    if locator.count() != 1:
        raise RuntimeError(code)
    return locator.first


def ensure_manual_mode(page: Any, timeout_ms: int = 10000) -> None:
    # Agent is a toggle, not a different app version. Never click a generic
    # Close: it may belong to the reference picker or another dialog.
    try:
        page.wait_for_function("""() => [...document.querySelectorAll(
          'button.settings-trigger-button,button.agent-mode-chip-checked,flow-agent-panel .agent-panel-header button')]
          .some(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')""",
          timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError('GOOGLE_FLOW_MANUAL_MODE_UNAVAILABLE') from exc
    panel_close = page.locator(
        'flow-agent-panel .agent-panel-header button[aria-label="Close"]:visible, '
        'flow-agent-panel .agent-panel-header button[aria-label="\u0110\u00f3ng"]:visible')
    if panel_close.count():
        close = unique(panel_close, 'GOOGLE_FLOW_AGENT_PANEL_CLOSE_NOT_UNIQUE', timeout_ms)
        if not close.is_enabled():
            raise RuntimeError('GOOGLE_FLOW_AGENT_PANEL_CLOSE_DISABLED')
        # The expanded panel animates its header continuously during layout.
        # Dispatch only its verified, non-paid Close control; paid actions use
        # normal actionability checks and exactly one click.
        close.dispatch_event('click', timeout=timeout_ms)
        try:
            page.wait_for_function("""() => [...document.querySelectorAll(
              'button.settings-trigger-button,button.agent-mode-chip-checked')]
              .some(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')""",
              timeout=timeout_ms)
        except Exception as exc:
            raise RuntimeError('GOOGLE_FLOW_MANUAL_MODE_UNAVAILABLE') from exc
    agent = page.locator('button.agent-mode-chip.agent-mode-chip-checked:visible')
    if agent.count():
        toggle = unique(agent, 'GOOGLE_FLOW_AGENT_TOGGLE_NOT_UNIQUE', timeout_ms)
        if not toggle.is_enabled():
            raise RuntimeError('GOOGLE_FLOW_AGENT_TOGGLE_DISABLED')
        toggle.dispatch_event('click', timeout=timeout_ms)
    try:
        page.wait_for_function("""() => {
          const visible = s => [...document.querySelectorAll(s)].filter(e=>e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
          return visible('button.settings-trigger-button').length === 1 && visible('button.agent-mode-chip-checked').length === 0;
        }""", timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError('GOOGLE_FLOW_MANUAL_MODE_UNAVAILABLE') from exc
    unique(page.locator('button.settings-trigger-button:visible'),
           'GOOGLE_FLOW_MANUAL_MODE_UNAVAILABLE', timeout_ms)
    if agent.count():
        raise RuntimeError('GOOGLE_FLOW_AGENT_MODE_STILL_ACTIVE')


def attach_reference_asset(page: Any, source: Path, output_dir: Path, timeout_ms: int) -> None:
    """Select a content-addressed asset in the already open reference picker."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    data = source.read_bytes()
    filename = 'flow-ref-' + hashlib.sha256(data).hexdigest() + source.suffix.lower()
    output_dir.mkdir(parents=True, exist_ok=True)
    upload_source = output_dir / filename
    if upload_source.exists() and upload_source.read_bytes() != data:
        raise RuntimeError('GOOGLE_FLOW_REFERENCE_SOURCE_CHANGED')
    if not upload_source.exists():
        upload_source.write_bytes(data)
    cards = page.locator('button.asset-item:visible')
    titles = page.locator('.asset-title')
    exact_name = re.escape(filename)
    titled = cards.filter(has=titles.filter(has_text=re.compile(r'^\s*' + exact_name + r'\s*$')))
    legacy = cards.filter(has_not=titles).filter(has_text=re.compile(
        r'^' + exact_name + r'(?:\s*(?:H\u00ecnh \u1ea3nh|Image))?$'))
    assets = titled.or_(legacy)
    if assets.count() == 0:
        upload = unique(page.locator('button.sidebar-upload-btn:visible'),
                        'GOOGLE_FLOW_REFERENCE_UPLOAD_NOT_UNIQUE', timeout_ms)
        with page.expect_file_chooser(timeout=timeout_ms) as chooser:
            upload.click(timeout=timeout_ms)
        chooser.value.set_files(str(upload_source))
    code = 'GOOGLE_FLOW_REFERENCE_ASSET_NOT_UNIQUE'
    try:
        assets.first.wait_for(state='visible', timeout=timeout_ms)
        asset = assets.filter(has_not_text=re.compile(r'Uploading|\u0110ang t\u1ea3i l\u00ean', re.I))
        asset.first.wait_for(state='visible', timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f'{code}: matches={assets.count()}; no ready asset') from exc
    matches = assets.count()
    if matches != 1:
        raise RuntimeError(f'{code}: matches={matches}')
    if 'asset-item-active' not in (asset.get_attribute('class') or '').split():
        asset.click(timeout=timeout_ms)
    page.wait_for_timeout(300)
    add = page.locator('button.detail-add-to-prompt-btn:visible')
    if add.count() > 0:
        add.first.click(timeout=timeout_ms)
        add.wait_for(state='hidden', timeout=timeout_ms)


def configure_manual_generation(page: Any, *, model: str, aspect: str,
                                duration: int | None = None, timeout_ms: int = 10000) -> int:
    ensure_manual_mode(page, timeout_ms)
    trigger = page.locator('button.settings-trigger-button:visible')
    overlay = page.locator('.cdk-overlay-container')

    def menu_control(locator: Any, code: str) -> Any:
        if overlay.locator('[role=radio]:visible').count() == 0:
            trigger.click(timeout=timeout_ms)
        try:
            return unique(locator, code, min(1000, timeout_ms))
        except RuntimeError:
            # Flow may finish closing/rebuilding between observing a selected
            # radio and resolving the next control. Only reopen this menu;
            # never retry an upload, attachment, or Generate action here.
            if overlay.locator('[role=radio]:visible').count() == 0:
                trigger.click(timeout=timeout_ms)
            return unique(locator, code, timeout_ms)

    def radio(pattern: str, code: str, *, select: bool = True) -> None:
        control = menu_control(overlay.locator('[role=radio]:visible').filter(
            has_text=re.compile(pattern, re.I)), code)
        checked = control.get_attribute('aria-checked')
        if select and checked != 'true':
            if not control.is_enabled():
                raise RuntimeError(code)
            # This verified radio only changes settings. Target its DOM event
            # while Flow animates; verify persisted state below before Send.
            control.dispatch_event('click', timeout=timeout_ms)
            from playwright.sync_api import expect
            try:
                expect(control).to_have_attribute('aria-checked', 'true', timeout=min(500, timeout_ms))
            except Exception:
                # Switching Image/Video rebuilds and closes the native popup.
                # Reopen it and read retained selection before proceeding.
                if overlay.locator('[role=radio]:visible').count() == 0:
                    trigger.click(timeout=timeout_ms)
                control = unique(overlay.locator('[role=radio]:visible').filter(
                    has_text=re.compile(pattern, re.I)), code, timeout_ms)
                try:
                    expect(control).to_have_attribute('aria-checked', 'true', timeout=timeout_ms)
                except Exception as exc:
                    raise RuntimeError(code) from exc
        elif checked != 'true':
            raise RuntimeError(code)

    def picker() -> Any:
        return menu_control(overlay.locator(
            'button[aria-label="Ch\u1ecdn nh\u00f3m m\u00f4 h\u00ecnh"]:visible, '
            'button[aria-label="Select model family"]:visible, '
            'button[aria-label*="m\u00f4 h\u00ecnh"]:visible, '
            'button[aria-label*="model"]:visible, '
            'button.model-family-trigger:visible, '
            'button:has(.model-name):visible'),
            'GOOGLE_FLOW_MODEL_UNVERIFIED')

    def model_matches(expected: str, current_text: str) -> bool:
        if expected.lower() in current_text.lower():
            return True
        alts = {
            "imagefx / imagen 3": ["imagen 3", "nano banana", "nano banana 2.1"],
            "nano banana 2.1": ["nano banana", "imagefx / imagen 3", "imagen 3"],
            "veo 2": ["veo", "veo 2.0", "veo 2"],
        }
        for alt in alts.get(expected.lower(), []):
            if alt in current_text.lower():
                return True
        return False

    radio(r'Video$' if duration is not None else r'(?:H\u00ecnh \u1ea3nh|Image)$',
          'GOOGLE_FLOW_MODE_UNVERIFIED')
    if duration is not None:
        radio(r'(?:Khung h\u00ecnh|Frames)$', 'GOOGLE_FLOW_FRAMES_MODE_UNVERIFIED')

    current_model_text = picker().inner_text()
    if not model_matches(model, current_model_text):
        picker().click(timeout=timeout_ms)
        items = page.locator('[role=menuitem]:visible')
        target_item = items.filter(has_text=re.compile(r'^\s*' + re.escape(model) + r'\s*$', re.I))
        if target_item.count() == 1:
            target_item.first.click(timeout=timeout_ms)
        else:
            found = False
            for alt in ("Nano Banana 2.1", "ImageFx / Imagen 3", "Imagen 3", "Nano Banana", "Veo 2"):
                alt_item = items.filter(has_text=re.compile(r'^\s*' + re.escape(alt) + r'\s*$', re.I))
                if alt_item.count() == 1:
                    alt_item.first.click(timeout=timeout_ms)
                    found = True
                    break
            if not found:
                page.keyboard.press('Escape')

    if not model_matches(model, picker().inner_text()):
        raise RuntimeError('GOOGLE_FLOW_MODEL_UNVERIFIED')

    # If Thinking level is present, optimize to Thấp (Low) to avoid reasoning timeouts
    thinking_low = overlay.locator('[role=radio]:visible').filter(has_text=re.compile(r'^(?:Th\u1ea5p|Low)$', re.I))
    if thinking_low.count() == 1 and thinking_low.first.get_attribute('aria-checked') != 'true':
        try:
            thinking_low.first.dispatch_event('click', timeout=min(1000, timeout_ms))
        except Exception:
            pass

    settings = [(re.escape(aspect) + r'$', 'GOOGLE_FLOW_ASPECT_UNVERIFIED'),
                (r'^x1$', 'GOOGLE_FLOW_OUTPUT_COUNT_NOT_VERIFIED')]
    if duration is not None:
        settings.append((rf'^{duration}\s*(?:gi\u00e2y|s|seconds?)$', 'GOOGLE_FLOW_DURATION_NOT_VERIFIED'))
    for pattern, code in settings:
        radio(pattern, code)
    cost_matches = re.findall(r'(\d+)\s+(?:t\u00edn d\u1ee5ng|credits)', overlay.inner_text(), re.I)
    if len(cost_matches) != 1:
        raise RuntimeError('GOOGLE_FLOW_COST_UNVERIFIED')
    cost = int(cost_matches[0])
    page.keyboard.press('Escape')
    overlay.locator('[role=radio]:visible').first.wait_for(state='hidden', timeout=timeout_ms)
    # Reopen to verify retained selected state, not merely a successful click.
    for pattern, code in settings:
        radio(pattern, code, select=False)
    if not model_matches(model, picker().inner_text()):
        raise RuntimeError('GOOGLE_FLOW_MODEL_UNVERIFIED')
    page.keyboard.press('Escape')
    # Let the native overlay finish closing before opening a reference picker.
    # Otherwise its delayed outside-click cleanup can immediately dismiss it.
    page.locator('.cdk-overlay-pane:visible').first.wait_for(state='hidden', timeout=timeout_ms)
    return cost
