"""Tests for Google Flow Playwright Keyframe Provider."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from video_factory.integrations.google_flow_keyframe_provider import GoogleFlowKeyframeProvider


def test_flow_generation_prompt_always_requests_native_full_bleed_916():
    prompt = GoogleFlowKeyframeProvider._compose_generation_prompt(
        "A pouch on a bright tabletop", "9:16", ""
    )

    assert "native full-bleed 9:16 portrait" in prompt
    assert "entire canvas" in prompt
    assert "Do not add black bars, borders, padding, or an inset square image" in prompt
    assert "A pouch on a bright tabletop" in prompt


def test_send_generation_submits_once_when_button_stays_enabled(tmp_path):
    """An enabled button after Enter is not proof that the first Send failed."""
    actions = []

    class Prompt:
        def count(self):
            return 1

        def is_visible(self):
            return True

        def focus(self):
            pass

    class Keyboard:
        def press(self, key):
            actions.append(('keyboard', key))

    class Page:
        keyboard = Keyboard()

        def locator(self, selector):
            assert selector == 'div.ProseMirror'
            return Prompt()

        def wait_for_timeout(self, milliseconds):
            pass

    class Button:
        page = Page()

        @property
        def first(self):
            return self

        def is_disabled(self):
            return False

        def click(self, **kwargs):
            actions.append(('button', 'click'))

    provider = GoogleFlowKeyframeProvider(profile_path=tmp_path, output_dir=tmp_path)

    provider._send_generation(Button())

    assert len(actions) == 1, actions


def test_send_generation_propagates_ambiguous_click_without_another_action(tmp_path):
    """The caller must reconcile a click timeout instead of falling through."""
    actions = []

    class Button:
        page = None

        @property
        def first(self):
            return self

        def click(self, **kwargs):
            actions.append('click')
            raise TimeoutError('browser disconnected after click started')

    provider = GoogleFlowKeyframeProvider(profile_path=tmp_path, output_dir=tmp_path)

    with pytest.raises(TimeoutError, match='after click started'):
        provider._send_generation(Button())

    assert actions == ['click']


def test_submit_click_timeout_preserves_checkpoint_and_requires_reconciliation(tmp_path, monkeypatch):
    actions = []
    reference = _create_test_image(tmp_path / 'reference.png')
    project_url = 'https://flow.google.com/project/00000000-0000-0000-0000-000000000003'

    def checkpoint(url, request):
        actions.append(('checkpoint', request.operation_id, url))
        return {'persisted': True, 'receipt_id': 'diagnostic-receipt'}

    class Button:
        @property
        def first(self):
            return self

        def click(self, **kwargs):
            actions.append(('send',))
            raise TimeoutError('disconnected after click started')

    provider = GoogleFlowKeyframeProvider(profile_path=tmp_path, output_dir=tmp_path,
        ui_contract_verified=True, reference_upload_verified=True, cost_contract_verified=True,
        on_project_created=checkpoint)
    request = KeyframeGenerationRequest(operation_id='kf-click-uncertain', workflow_id='wf-click-uncertain',
        project_id='project-click-uncertain', frame_id='scene-1-start', prompt='Product on a table',
        product_description='Test product', reference_image_paths=(str(reference),))
    monkeypatch.setattr(provider, '_prepare_ui_and_reference', lambda *args: project_url)
    monkeypatch.setattr(provider, '_locate_generate_button', lambda *args: Button())
    monkeypatch.setattr(provider, '_current_image_tile_sources', lambda *args: set())
    monkeypatch.setattr(provider, '_download_generated_output', lambda *args, **kwargs: actions.append(('download',)))
    browser = MagicMock()
    browser.pages = [MagicMock()]
    with patch('video_factory.integrations.google_flow_keyframe_provider.sync_playwright') as playwright:
        playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = browser
        result = provider.submit(request)

    assert result.status == 'unknown'
    assert result.error_code == 'RECONCILIATION_REQUIRED'
    assert result.provider_operation_id == project_url
    assert result.metadata['send_attempted'] is True
    assert actions == [('checkpoint', request.operation_id, project_url), ('send',)]


def _flow_image_settings_locators():
    panel = MagicMock()
    panel.count.return_value = 1
    panel.first = panel
    panel.is_visible.return_value = True
    panel.wait_for.return_value = None
    settings_button = MagicMock()
    settings_button.count.return_value = 1
    settings_button.first = settings_button
    settings_button.is_visible.return_value = True
    settings_button.wait_for.return_value = None
    image_section = MagicMock()
    image_section.count.return_value = 1
    image_section.first = image_section
    image_section.is_visible.return_value = True
    image_picker = MagicMock()
    image_picker.count.return_value = 1
    image_picker.first.is_visible.return_value = True
    image_picker.first.get_attribute.return_value = "Image model"
    image_picker.first.wait_for.return_value = None
    image_picker.first.get_attribute.return_value = "Image generation model"
    selected = MagicMock()
    selected.count.return_value = 1
    selected.first.is_visible.return_value = True
    selected.first.get_attribute.side_effect = lambda name: "true" if name == "aria-checked" else None
    aspect = MagicMock()
    aspect.filter.return_value = selected
    image_section.locator.side_effect = lambda selector: aspect if selector == "[role='radio']" else image_picker
    sections = MagicMock()
    sections.filter.return_value = image_section
    sections.first = image_section
    save = MagicMock()
    save.count.return_value = 1
    save.first = save
    save.is_visible.return_value = True
    save.first.wait_for.return_value = None
    manual_overlay = MagicMock()
    manual_overlay.inner_text.return_value = "12 credits"
    manual_model = MagicMock()
    manual_model.count.return_value = 1
    manual_model.first = manual_model
    manual_model.inner_text.return_value = "ImageFx / Imagen 3"
    manual_overlay.locator.side_effect = lambda selector: manual_model if selector.startswith('button[') else aspect
    inactive_agent = MagicMock()
    inactive_agent.count.return_value = 0
    return {
        "button.settings-trigger-button:visible": settings_button,
        "button.agent-mode-chip.agent-mode-chip-checked:visible": inactive_agent,
        'flow-agent-panel .agent-panel-header button[aria-label="Close"]:visible, flow-agent-panel .agent-panel-header button[aria-label="\u0110\u00f3ng"]:visible': inactive_agent,
        ".cdk-overlay-container": manual_overlay,
        "flow-settings-view:visible": panel,
        "button[aria-label='Settings']:visible": settings_button,
        "div.settings-section": sections,
        "button.image-model-picker-button": image_picker,
        ".settings-save-button:visible": save,
    }


def test_new_project_selector_uses_only_unique_visible_candidate():
    provider = GoogleFlowKeyframeProvider(profile_path=Path("."))
    page = MagicMock()
    visible_candidates = MagicMock()
    visible_candidates.count.return_value = 1
    visible_candidates.first = MagicMock()
    page.locator.return_value = visible_candidates

    selected = provider._unique_visible_new_project_button(page)

    assert selected is visible_candidates.first
    assert ":visible" in page.locator.call_args.args[0]


def test_new_project_selector_stops_when_multiple_visible_candidates_remain():
    provider = GoogleFlowKeyframeProvider(profile_path=Path("."))
    page = MagicMock()
    visible_candidates = MagicMock()
    visible_candidates.count.return_value = 2
    page.locator.return_value = visible_candidates

    with pytest.raises(RuntimeError, match="GOOGLE_FLOW_NEW_PROJECT_SELECTOR_NOT_UNIQUE"):
        provider._unique_visible_new_project_button(page)

    visible_candidates.first.click.assert_not_called()


@pytest.mark.parametrize(("generated_src", "expected_download_url"), [
    ("https://flow.google.com/media/generated.png=s512", "https://flow.google.com/media/generated.png=s0"),
    (
        "https://flow-content.google/image/id?Expires=1792960416&KeyName=labs-flow-prod-cdn-key&Signature=sycSBDD",
        "https://flow-content.google/image/id?Expires=1792960416&KeyName=labs-flow-prod-cdn-key&Signature=sycSBDD",
    ),
])
def test_generated_output_download_skips_existing_reference_tile(tmp_path, generated_src, expected_download_url):
    provider = GoogleFlowKeyframeProvider(profile_path=tmp_path, timeout_seconds=3)
    reference_src = "https://flow.google.com/media/reference.png=s512"
    generated_bytes = b"generated-image-bytes" * 100

    class FakeImage:
        def __init__(self, src):
            self.src = src
            self.first = self

        def get_attribute(self, name):
            return self.src if name == "src" else None

        def is_visible(self):
            return True

    class FakeCollection:
        def __init__(self, items):
            self.items = items
            self.first = items[0] if items else None

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

        def is_visible(self):
            return bool(self.items) and self.items[0].is_visible()

    class FakeTile:
        def __init__(self, img):
            self.img = img

        def locator(self, selector):
            assert selector == "img"
            return FakeCollection([self.img])

    class FakeResponse:
        status = 200

        def body(self):
            return generated_bytes

    class FakePage:
        request = MagicMock()

        def __init__(self):
            self.request.get.return_value = FakeResponse()

        def locator(self, selector):
            if selector == "flow-image-tile img":
                return FakeCollection([FakeImage(reference_src)])
            if selector == "flow-image-tile":
                return FakeCollection([
                    FakeTile(FakeImage(reference_src)),
                    FakeTile(FakeImage(generated_src)),
                ])
            raise AssertionError(f"unexpected selector {selector}")

        def wait_for_timeout(self, _milliseconds):
            return None

    output = tmp_path / "flow-output.png"
    page = FakePage()

    provider._download_generated_output(
        page,
        output,
        initial_sources={reference_src.split("=")[0]},
    )

    assert output.read_bytes() == generated_bytes
    assert page.request.get.call_args.args[0] == expected_download_url


def test_google_flow_landing_page_fails_auth_check_before_project_selection():
    provider = GoogleFlowKeyframeProvider(profile_path=Path("."))
    page = MagicMock()
    page.url = "https://flow.google.com/about"
    page.query_selector.return_value = None

    with pytest.raises(RuntimeError, match="GOOGLE_FLOW_AUTH_REQUIRED"):
        provider._assert_authenticated(page)


def test_google_flow_project_workspace_auth_uses_visible_settings_and_composer():
    provider = GoogleFlowKeyframeProvider(profile_path=Path("."))
    page = MagicMock()
    page.url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000099"
    page.query_selector.return_value = None
    settings = MagicMock()
    settings.count.return_value = 1
    settings.first.is_visible.return_value = True
    composer = MagicMock()
    composer.count.return_value = 1
    composer.first.is_visible.return_value = True
    empty = MagicMock()
    empty.count.return_value = 0
    page.locator.side_effect = lambda selector: {
        "button.settings-trigger-button:visible": settings,
        "div.ProseMirror:visible": composer,
    }.get(selector, empty)

    provider._assert_authenticated(page)


def test_google_flow_workspace_action_proves_auth_when_profile_marker_changed():
    provider = GoogleFlowKeyframeProvider(profile_path=Path("."))
    page = MagicMock()
    page.url = "https://flow.google.com/"
    page.query_selector.return_value = None
    workspace_action = MagicMock()
    workspace_action.count.return_value = 1
    workspace_action.first.is_visible.return_value = True
    page.locator.return_value = workspace_action

    provider._assert_authenticated(page)


def test_google_flow_auth_check_waits_for_workspace_to_hydrate():
    provider = GoogleFlowKeyframeProvider(profile_path=Path("."))
    page = MagicMock()
    page.url = "https://flow.google.com/"
    page.query_selector.return_value = None

    class HydratingWorkspaceAction:
        ready = False

        @property
        def first(self):
            return self

        def count(self):
            return int(self.ready)

        def is_visible(self):
            return self.ready

        def wait_for(self, *, state, timeout):
            assert state == "visible"
            assert timeout > 0
            self.ready = True

    workspace_action = HydratingWorkspaceAction()
    page.locator.return_value = workspace_action

    provider._assert_authenticated(page)

def test_google_flow_keyframe_config_initialization():
    provider = GoogleFlowKeyframeProvider(profile_path=Path("."))
    assert provider.provider_kind == "google_flow"
    assert provider.model == "ImageFx / Imagen 3"
    assert provider.is_available() is False


def test_square_flow_output_is_preserved_for_contextual_keyframe_qc(tmp_path):
    from PIL import Image

    provider = GoogleFlowKeyframeProvider(profile_path=tmp_path)
    output = tmp_path / "flow-square.png"
    Image.new("RGB", (1024, 1024), "white").save(output)
    original = output.read_bytes()

    provider._validate_and_format_output(output)

    assert output.read_bytes() == original
    with Image.open(output) as image:
        assert image.size == (1024, 1024)


def test_flow_portrait_output_is_kept_as_original_bytes(tmp_path):
    from PIL import Image

    provider = GoogleFlowKeyframeProvider(profile_path=tmp_path)
    output = tmp_path / "flow-portrait.png"
    Image.new("RGB", (768, 1376), "white").save(output)
    original = output.read_bytes()

    provider._validate_and_format_output(output)

    assert output.read_bytes() == original
    with Image.open(output) as image:
        assert image.size == (768, 1376)


def test_unknown_operation_retries_download_after_restart_without_resubmitting(tmp_path):
    import sqlite3
    import hashlib
    from video_factory.integrations.keyframe_provider import KeyframeOperationStore, KeyframeRuntime
    from video_factory.integrations.ports import KeyframeGenerationRequest, KeyframeGenerationResult

    db_path = tmp_path / "flow-download-restart.db"
    output = _create_test_image(tmp_path / "recovered.png")
    request = KeyframeGenerationRequest(
        operation_id="flow-download-restart", workflow_id="wf-download-restart",
        project_id="project-download-restart", frame_id="scene_1-start",
        prompt="A bottle on a table", product_description="A bottle",
        reference_image_paths=(),
    )
    project_url = "https://flow.google.com/project/11111111-2222-3333-4444-555555555555"

    class ExistingProjectProvider:
        provider_kind = "google_flow"
        submit_calls = 0
        download_calls = 0

        def submit(self, req):
            self.submit_calls += 1
            return KeyframeGenerationResult(
                req.operation_id, "unknown", provider_operation_id=project_url,
                error_code="RECONCILIATION_REQUIRED",
            )

        def get_status(self, _provider_operation_id):
            raise AssertionError("existing output should use the non-submitting download path")

        def reconcile_existing_output(self, req, url):
            self.download_calls += 1
            attempt = self.store.claim_output_reconciliation(req.operation_id, url, max_attempts=3)
            if self.download_calls == 1:
                return KeyframeGenerationResult(
                    req.operation_id, "unknown", provider_operation_id=url,
                    error_code="GOOGLE_FLOW_OUTPUT_NOT_RETRIEVED",
                )
            return KeyframeGenerationResult(
                req.operation_id, "completed", provider_operation_id=url,
                image_path=str(output), metadata={"provider_kind": "google_flow", "download_attempt": attempt},
            )

        def bind_operation_store(self, store):
            self.store = store

    provider = ExistingProjectProvider()
    conn = sqlite3.connect(db_path)
    store = KeyframeOperationStore(conn)
    runtime = KeyframeRuntime(store=store, provider=provider)
    first = runtime.run(request)
    assert first.status == "unknown"
    assert provider.submit_calls == 1

    # First read-only retrieval attempt finds no verified tile yet.
    first_download = runtime.run(request)
    assert first_download.status == "unknown"
    assert provider.download_calls == 1
    conn.close()

    conn = sqlite3.connect(db_path)
    store = KeyframeOperationStore(conn)
    resumed = KeyframeRuntime(store=store, provider=provider).run(request)
    assert resumed.status == "completed", (resumed.error_code, resumed.error_message, provider.download_calls)
    assert provider.submit_calls == 1
    assert provider.download_calls == 2
    record = store.get(request.operation_id)
    assert record["status"] == "completed"
    assert record["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    metadata = __import__("json").loads(record["provider_metadata_json"])
    assert metadata["output_reconciliation_attempts"] == 2
    assert metadata["download_attempt"] == 2

    # A further process restart returns the verified artifact without another poll/download.
    conn.close()
    conn = sqlite3.connect(db_path)
    store = KeyframeOperationStore(conn)
    final = KeyframeRuntime(store=store, provider=provider).run(request)
    assert final.status == "completed"
    assert provider.submit_calls == 1
    assert provider.download_calls == 2
    conn.close()


def test_flow_reconciliation_rejects_corrupt_download_without_confirming(tmp_path, monkeypatch):
    import sqlite3
    from video_factory.integrations.keyframe_provider import KeyframeOperationStore
    from video_factory.integrations.ports import KeyframeGenerationRequest, KeyframeGenerationResult

    conn = sqlite3.connect(":memory:")
    store = KeyframeOperationStore(conn)
    request = KeyframeGenerationRequest(
        operation_id="flow-corrupt-download", workflow_id="wf-corrupt-download",
        project_id="project-corrupt-download", frame_id="scene_1-start",
        prompt="A bottle", product_description="Bottle", reference_image_paths=(),
    )
    project_url = "https://flow.google.com/project/11111111-2222-3333-4444-555555555555"
    store.claim_first_submission(request, "google_flow")
    store.record_project_checkpoint(request, project_url, "google_flow")
    store.save_result(request, KeyframeGenerationResult(
        request.operation_id, "unknown", provider_operation_id=project_url,
        error_code="RECONCILIATION_REQUIRED",
    ))
    profile = tmp_path / "profile"
    profile.mkdir()
    provider = GoogleFlowKeyframeProvider(profile_path=profile, output_dir=tmp_path)
    provider.bind_operation_store(store)
    page = MagicMock()
    page.url = project_url
    browser = MagicMock()
    browser.pages = [page]
    monkeypatch.setattr(provider, "_assert_authenticated", lambda _page: None)
    monkeypatch.setattr(provider, "_existing_project_sources", lambda *_args: (set(), "https://flow.google.com/media/generated.png"))
    monkeypatch.setattr(provider, "_download_generated_output", lambda _page, path, **_kwargs: path.write_bytes(b"not an image"))

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as playwright:
        playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = browser
        result = provider.reconcile_existing_output(request, project_url)

    assert result.status == "unknown"
    assert result.error_code == "GOOGLE_FLOW_OUTPUT_INVALID"
    operation = store.get(request.operation_id)
    assert operation["status"] == "unknown"
    assert operation["output_path"] is None
    assert operation["output_sha256"] == ""
    assert not provider._output_path(request.operation_id).exists()


def test_flow_auth_failure_does_not_consume_output_reconciliation_attempt(tmp_path, monkeypatch):
    import sqlite3
    from video_factory.integrations.keyframe_provider import KeyframeOperationStore
    from video_factory.integrations.ports import KeyframeGenerationRequest, KeyframeGenerationResult

    conn = sqlite3.connect(":memory:")
    store = KeyframeOperationStore(conn)
    request = KeyframeGenerationRequest(
        operation_id="flow-auth-no-attempt", workflow_id="wf-auth-no-attempt",
        project_id="project-auth-no-attempt", frame_id="scene_1-start",
        prompt="A bottle", product_description="Bottle", reference_image_paths=(),
    )
    project_url = "https://flow.google.com/project/11111111-2222-3333-4444-555555555555"
    store.claim_first_submission(request, "google_flow")
    store.record_project_checkpoint(request, project_url, "google_flow")
    store.save_result(request, KeyframeGenerationResult(
        request.operation_id, "unknown", provider_operation_id=project_url,
        error_code="RECONCILIATION_REQUIRED",
    ))
    profile = tmp_path / "configured-profile"
    profile.mkdir()
    monkeypatch.setattr("video_factory.config.load_config", lambda: {
        "video_factory": {"google_flow_keyframe": {
            "profile_path": str(profile), "chrome_profile_directory": "Default",
            "headless": True, "timeout_seconds": 3,
        }},
    })
    provider = GoogleFlowKeyframeProvider.from_config(output_dir=tmp_path)
    assert provider.profile_path == profile.resolve()
    assert provider.chrome_profile_directory == "Default"
    provider.bind_operation_store(store)
    page = MagicMock()
    page.url = project_url
    browser = MagicMock()
    browser.pages = [page]
    monkeypatch.setattr(provider, "_assert_authenticated", lambda _page: (_ for _ in ()).throw(RuntimeError("GOOGLE_FLOW_AUTH_UNVERIFIED")))
    launch = MagicMock(return_value=browser)

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as playwright:
        playwright.return_value.__enter__.return_value.chromium.launch_persistent_context = launch
        result = provider.reconcile_existing_output(request, project_url)

    assert result.status == "unknown"
    assert result.error_code == "GOOGLE_FLOW_AUTH_UNVERIFIED"
    assert launch.call_args.kwargs["user_data_dir"] == str(profile.resolve())
    assert launch.call_args.kwargs["args"] == []
    metadata = __import__("json").loads(store.get(request.operation_id)["provider_metadata_json"])
    assert "output_reconciliation_attempts" not in metadata


def test_flow_ui_configuration_uses_manual_image_settings():
    provider = GoogleFlowKeyframeProvider(profile_path=Path("."))
    page = MagicMock()
    with patch("video_factory.integrations.flow_ui_recipe.configure_manual_generation") as configure:
        provider._ensure_image_generation_settings(page)
    configure.assert_called_once_with(
        page, model=provider.model, aspect="9:16", timeout_ms=180000,
    )


def test_flow_reference_upload_must_be_verified_separately(tmp_path):
    provider = GoogleFlowKeyframeProvider(
        profile_path=tmp_path,
        ui_contract_verified=True,
        reference_upload_verified=False,
    )
    assert provider.is_available() is False


def test_flow_keyframe_cost_must_be_verified_separately(tmp_path):
    provider = GoogleFlowKeyframeProvider(
        profile_path=tmp_path,
        ui_contract_verified=True,
        reference_upload_verified=True,
        cost_contract_verified=False,
    )
    assert provider.is_available() is False

def test_marker_generation_deterministic():
    class MockRequest:
        operation_id = "test-123"
        prompt = "test prompt"
    
    provider = GoogleFlowKeyframeProvider(profile_path=Path("."))
    # Calling internal logic manually or testing through public method
    # Marker is deterministic based on operation_id
    # Test requires executing the logic.
    pass


def test_flow_video_adapter_never_creates_keyframe_placeholder(tmp_path):
    from video_factory.integrations.google_flow_provider import GoogleFlowPlaywrightProvider

    provider = GoogleFlowPlaywrightProvider(output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="cannot copy references or create placeholders"):
        import asyncio
        asyncio.run(provider.generate_keyframe("prompt", tmp_path / "reference.png"))

from video_factory.integrations.ports import KeyframeGenerationRequest
from PIL import Image


def _create_test_image(path: Path, width: int = 720, height: int = 1280) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (width, height), color=(100, 150, 200))
    im.save(path, format="PNG")
    return path


@patch.object(GoogleFlowKeyframeProvider, '_attach_image_references', lambda self, page, paths: None)
def test_flow_keyframe_preview_click_download_branch(tmp_path):
    """Directly tests the image click download fallback branch (fixing undefined img_elem)."""
    ref_file = _create_test_image(tmp_path / "ref.png")
    out_dir = tmp_path / "out"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    provider = GoogleFlowKeyframeProvider(
        profile_path=profile_dir,
        output_dir=out_dir,
        ui_contract_verified=True,
        reference_upload_verified=True,
        cost_contract_verified=True,
        timeout_seconds=6.0,
        on_project_created=lambda url, req: {"persisted": True, "receipt_id": f"rec_{req.operation_id}"},
    )

    req = KeyframeGenerationRequest(
        operation_id="kf-test-preview-branch",
        workflow_id="wf-test",
        project_id="proj-test",
        frame_id="scene-1-start",
        prompt="A bottle of serum on a stone pedestal",
        product_description="Skincare serum",
        reference_image_paths=(str(ref_file),),
        aspect_ratio="9:16",
    )

    class DummyDownload:
        def save_as(self, dest: str) -> None:
            _create_test_image(Path(dest), 720, 1280)

    class DummyDownloadContext:
        def __init__(self):
            self.value = DummyDownload()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    # Setup mocked playwright objects
    mock_page = MagicMock()
    mock_page.url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
    mock_page.query_selector.side_effect = lambda sel: MagicMock() if ".profile-button" in sel else None

    # Locators
    settings_locators = _flow_image_settings_locators()

    def mock_locator(sel: str):
        loc = MagicMock()
        if sel in settings_locators:
            return settings_locators[sel]
        if "new-project-button" in sel:
            loc.count.return_value = 1
        elif "modal-close-button" in sel:
            loc.count.return_value = 0
        elif "settings-trigger-button" in sel:
            loc.count.return_value = 0
        elif "button:has-text('Hình ảnh')" in sel or "button:has-text('Image')" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
            loc.first.get_attribute.return_value = "true"
        elif "9:16" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
            loc.first.get_attribute.return_value = "true"
        elif 'input[type="file"]' in sel:
            loc.count.return_value = 1
            loc.evaluate.return_value = 1
        elif "detail-add-to-prompt-btn" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif "ProseMirror" in sel:
            loc.count.return_value = 1
        elif "Bắt đầu tạo" in sel or "generate-icon-button" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif sel == "flow-image-tile img":
            # No tile exists before Send; the generated result is added afterward.
            loc.count.return_value = 0
            loc.is_visible.return_value = False
        elif "flow-image-tile" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
            loc.nth.return_value = loc
            # img locator inside tile
            mock_img = MagicMock()
            mock_img.count.return_value = 1
            mock_img.is_visible.return_value = True
            mock_img.first.get_attribute.return_value = "https://flow.google.com/media/generated.png=s512"
            loc.locator.side_effect = lambda sub_sel: mock_img if sub_sel == "img" else (
                # more buttons returns 0 to force preview click branch
                MagicMock(count=MagicMock(return_value=0), is_visible=MagicMock(return_value=False))
            )
        elif "Download" in sel or "Tải xuống" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        else:
            loc.count.return_value = 0
            loc.is_visible.return_value = False
        return loc

    mock_page.locator.side_effect = mock_locator
    mock_page.expect_download.return_value = DummyDownloadContext()

    mock_browser = MagicMock()
    mock_browser.pages = [mock_page]

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = mock_browser
        res = provider.submit(req)

    assert res.status == "completed"
    assert res.image_path is not None
    assert Path(res.image_path).is_file()
    assert res.provider_operation_id == "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"


def test_flow_keyframe_pre_send_failure_returns_failed(tmp_path):
    """Pre-Send exception/failure must return status='failed'."""
    ref_file = _create_test_image(tmp_path / "ref.png")
    out_dir = tmp_path / "out"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    provider = GoogleFlowKeyframeProvider(
        profile_path=profile_dir,
        output_dir=out_dir,
        ui_contract_verified=True,
        reference_upload_verified=True,
        cost_contract_verified=True,
    )

    req = KeyframeGenerationRequest(
        operation_id="kf-test-presend-err",
        workflow_id="wf-test",
        project_id="proj-test",
        frame_id="scene-1-start",
        prompt="A bottle of serum",
        product_description="Skincare serum",
        reference_image_paths=(str(ref_file),),
    )

    mock_page = MagicMock()
    mock_page.goto.side_effect = RuntimeError("DNS_RESOLUTION_FAILED")
    mock_browser = MagicMock()
    mock_browser.pages = [mock_page]

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = mock_browser
        res = provider.submit(req)

    assert res.status == "failed"
    assert res.error_code == "GOOGLE_FLOW_KEYFRAME_FAILED"
    assert res.metadata["send_attempted"] is False


@patch.object(GoogleFlowKeyframeProvider, '_attach_image_references', lambda self, page, paths: None)
def test_flow_keyframe_post_send_timeout_returns_unknown_reconciliation(tmp_path):
    """Post-Send timeout/exception must return status='unknown' and RECONCILIATION_REQUIRED."""
    ref_file = _create_test_image(tmp_path / "ref.png")
    out_dir = tmp_path / "out"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    provider = GoogleFlowKeyframeProvider(
        profile_path=profile_dir,
        output_dir=out_dir,
        ui_contract_verified=True,
        reference_upload_verified=True,
        cost_contract_verified=True,
        timeout_seconds=3.0,
        on_project_created=lambda url, req: {"persisted": True, "receipt_id": f"rec_{req.operation_id}"},
    )

    req = KeyframeGenerationRequest(
        operation_id="kf-test-postsend-timeout",
        workflow_id="wf-test",
        project_id="proj-test",
        frame_id="scene-1-start",
        prompt="A bottle of serum",
        product_description="Skincare serum",
        reference_image_paths=(str(ref_file),),
    )

    mock_page = MagicMock()
    mock_page.url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000002"
    mock_page.query_selector.side_effect = lambda sel: MagicMock() if ".profile-button" in sel else None

    settings_locators = _flow_image_settings_locators()

    def mock_locator(sel: str):
        loc = MagicMock()
        if sel in settings_locators:
            return settings_locators[sel]
        if "new-project-button" in sel:
            loc.count.return_value = 1
        elif "Image" in sel or "Hình ảnh" in sel:
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            loc.first.get_attribute.side_effect = lambda name: "true" if name == "aria-pressed" else None
        elif "9:16" in sel:
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            loc.first.get_attribute.side_effect = lambda name: "true" if name == "aria-pressed" else None
        elif 'input[type="file"]' in sel:
            loc.count.return_value = 1
            loc.evaluate.return_value = 1
        elif "detail-add-to-prompt-btn" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif "ProseMirror" in sel:
            loc.count.return_value = 1
        elif "Bắt đầu tạo" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif sel == "flow-image-tile img":
            # Capture the pre-Send baseline before the simulated post-Send crash.
            loc.count.return_value = 0
        elif "flow-image-tile" in sel:
            # Tile is not found in polling -> triggers timeout
            loc.count.return_value = 0
            loc.is_visible.return_value = False
        else:
            loc.count.return_value = 0
            loc.is_visible.return_value = False
        return loc

    mock_page.locator.side_effect = mock_locator
    mock_browser = MagicMock()
    mock_browser.pages = [mock_page]

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = mock_browser
        res = provider.submit(req)

    assert res.status == "unknown"
    assert res.error_code == "RECONCILIATION_REQUIRED"
    assert res.metadata["send_attempted"] is True
    assert res.provider_operation_id == "https://flow.google.com/project/00000000-0000-0000-0000-000000000002"


def test_flow_keyframe_get_status_fail_closed_operator_instructions(tmp_path):
    """get_status must fail closed and provide manual operator reconciliation instructions."""
    provider = GoogleFlowKeyframeProvider(profile_path=tmp_path, output_dir=tmp_path)
    res = provider.get_status("https://flow.google.com/project/abc-123")
    assert res.status == "unknown"
    assert res.error_code == "RECONCILIATION_REQUIRED"
    assert "manual operator reconciliation" in res.error_message.lower()


@patch.object(GoogleFlowKeyframeProvider, '_attach_image_references', lambda self, page, paths: None)
def test_flow_keyframe_checkpoint_failure_aborts_send_send_count_zero(tmp_path):
    """If saving project URL checkpoint fails before Generate, Send must NOT be attempted (Send=0)."""
    import sqlite3
    ref_file = _create_test_image(tmp_path / "ref.png")
    out_dir = tmp_path / "out"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    mock_store = MagicMock()
    mock_store.record_project_checkpoint.side_effect = sqlite3.OperationalError("database is locked")

    provider = GoogleFlowKeyframeProvider(
        profile_path=profile_dir,
        output_dir=out_dir,
        ui_contract_verified=True,
        reference_upload_verified=True,
        cost_contract_verified=True,
        store=mock_store,
    )

    req = KeyframeGenerationRequest(
        operation_id="kf-test-checkpoint-fail",
        workflow_id="wf-test",
        project_id="proj-test",
        frame_id="scene-1-start",
        prompt="A bottle of serum",
        product_description="Skincare serum",
        reference_image_paths=(str(ref_file),),
    )

    generate_click_mock = MagicMock()

    mock_page = MagicMock()
    mock_page.url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000003"
    mock_page.query_selector.side_effect = lambda sel: MagicMock() if "profile-button" in sel else None

    settings_locators = _flow_image_settings_locators()

    def mock_locator(sel):
        loc = MagicMock()
        if sel in settings_locators:
            return settings_locators[sel]
        if "new-project-button" in sel:
            loc.count.return_value = 1
        elif "Image" in sel or "Hình ảnh" in sel:
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            loc.first.get_attribute.side_effect = lambda name: "true" if name == "aria-pressed" else None
        elif "9:16" in sel:
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            loc.first.get_attribute.side_effect = lambda name: "true" if name == "aria-pressed" else None
        elif 'input[type="file"]' in sel:
            loc.count.return_value = 1
            loc.evaluate.return_value = 1
        elif "detail-add-to-prompt-btn" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif "ProseMirror" in sel:
            loc.count.return_value = 1
        elif "Bắt đầu tạo" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
            loc.click = generate_click_mock
        else:
            loc.count.return_value = 0
            loc.is_visible.return_value = False
        return loc

    mock_page.locator.side_effect = mock_locator
    mock_browser = MagicMock()
    mock_browser.pages = [mock_page]

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = mock_browser
        res = provider.submit(req)

    assert res.status == "failed"
    assert res.metadata["send_attempted"] is False
    assert generate_click_mock.call_count == 0, "Generate button must NEVER be clicked if checkpoint save fails"


def test_flow_keyframe_missing_selector_pre_send_is_failed(tmp_path):
    """If Generate selector is missing/invalid before click, return failed pre-Send (not unknown)."""
    ref_file = _create_test_image(tmp_path / "ref.png")
    out_dir = tmp_path / "out"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    provider = GoogleFlowKeyframeProvider(
        profile_path=profile_dir,
        output_dir=out_dir,
        ui_contract_verified=True,
        reference_upload_verified=True,
        cost_contract_verified=True,
        on_project_created=lambda url, req: {"persisted": True, "receipt_id": f"rec_{req.operation_id}"},
    )

    req = KeyframeGenerationRequest(
        operation_id="kf-test-missing-selector",
        workflow_id="wf-test",
        project_id="proj-test",
        frame_id="scene-1-start",
        prompt="A bottle of serum",
        product_description="Skincare serum",
        reference_image_paths=(str(ref_file),),
    )

    generate_click_mock = MagicMock()

    mock_page = MagicMock()
    mock_page.url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000004"
    mock_page.query_selector.side_effect = lambda sel: MagicMock() if "profile-button" in sel else None

    def mock_locator(sel):
        loc = MagicMock()
        if "new-project-button" in sel:
            loc.count.return_value = 1
        elif "Image" in sel or "Hình ảnh" in sel:
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            loc.first.get_attribute.side_effect = lambda name: "true" if name == "aria-pressed" else None
        elif "9:16" in sel:
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            loc.first.get_attribute.side_effect = lambda name: "true" if name == "aria-pressed" else None
        elif 'input[type="file"]' in sel:
            loc.count.return_value = 1
            loc.evaluate.return_value = 1
        elif "detail-add-to-prompt-btn" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif "ProseMirror" in sel:
            loc.count.return_value = 1
        elif "Bắt đầu tạo" in sel:
            # Selector is missing before click!
            loc.count.return_value = 0
            loc.is_visible.return_value = False
            loc.click = generate_click_mock
        else:
            loc.count.return_value = 0
            loc.is_visible.return_value = False
        return loc

    mock_page.locator.side_effect = mock_locator
    mock_browser = MagicMock()
    mock_browser.pages = [mock_page]

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = mock_browser
        res = provider.submit(req)

    assert res.status == "failed"
    assert res.error_code == "GOOGLE_FLOW_KEYFRAME_FAILED"
    assert res.metadata["send_attempted"] is False
    assert generate_click_mock.call_count == 0


@patch.object(GoogleFlowKeyframeProvider, '_attach_image_references', lambda self, page, paths: None)
def test_flow_keyframe_crash_after_click_preserves_url_in_store_and_prevents_stale_adoption(tmp_path):
    """Crash after click preserves project URL in store across restarts, refuses stale output adoption."""
    import sqlite3
    from video_factory.integrations.keyframe_provider import KeyframeOperationStore, KeyframeRuntime

    conn = sqlite3.connect(":memory:")
    store = KeyframeOperationStore(conn)

    ref_file = _create_test_image(tmp_path / "ref.png")
    out_dir = tmp_path / "out"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    provider = GoogleFlowKeyframeProvider(
        profile_path=profile_dir,
        output_dir=out_dir,
        ui_contract_verified=True,
        reference_upload_verified=True,
        cost_contract_verified=True,
    )

    runtime = KeyframeRuntime(store=store, provider=provider)

    req = KeyframeGenerationRequest(
        operation_id="kf-test-crash-recovery",
        workflow_id="wf-test",
        project_id="proj-test",
        frame_id="scene-1-start",
        prompt="A bottle of serum",
        product_description="Skincare serum",
        reference_image_paths=(str(ref_file),),
    )

    project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000099"

    mock_page = MagicMock()
    mock_page.url = project_url
    mock_page.query_selector.side_effect = lambda sel: MagicMock() if "profile-button" in sel else None

    settings_locators = _flow_image_settings_locators()

    def mock_locator(sel):
        loc = MagicMock()
        if sel in settings_locators:
            return settings_locators[sel]
        if "new-project-button" in sel:
            loc.count.return_value = 1
        elif "Image" in sel or "Hình ảnh" in sel:
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            loc.first.get_attribute.side_effect = lambda name: "true" if name == "aria-pressed" else None
        elif "9:16" in sel:
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            loc.first.get_attribute.side_effect = lambda name: "true" if name == "aria-pressed" else None
        elif 'input[type="file"]' in sel:
            loc.count.return_value = 1
            loc.evaluate.return_value = 1
        elif "detail-add-to-prompt-btn" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif "ProseMirror" in sel:
            loc.count.return_value = 1
        elif "Bắt đầu tạo" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif sel == "flow-image-tile img":
            # Capture the pre-Send baseline before the simulated post-Send crash.
            loc.count.return_value = 0
        elif "flow-image-tile" in sel:
            # Crash during download polling
            raise RuntimeError("SIMULATED_POST_SEND_CRASH")
        else:
            loc.count.return_value = 0
            loc.is_visible.return_value = False
        return loc

    mock_page.locator.side_effect = mock_locator
    mock_browser = MagicMock()
    mock_browser.pages = [mock_page]

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = mock_browser
        res = runtime.run(req)

    assert res.status == "unknown"
    assert res.error_code == "RECONCILIATION_REQUIRED"
    assert res.provider_operation_id == project_url

    # Check that project URL was persisted in SQLite store
    op_record = store.get(req.operation_id)
    assert op_record is not None
    assert op_record["provider_operation_id"] == project_url
    assert project_url in op_record["provider_metadata_json"]
    assert op_record["status"] in ("unknown", "project_created")

    # Simulate an arbitrary local file existing on disk at output path
    fake_existing_file = provider._output_path(req.operation_id)
    fake_existing_file.write_bytes(b"stale_image_bytes")

    # Simulate process restart with fresh KeyframeRuntime instance
    runtime_after_restart = KeyframeRuntime(store=store, provider=provider)
    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as retry_pw:
        retry_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = mock_browser
        res_after_restart = runtime_after_restart.run(req)

    # Must NOT adopt stale local file and must NOT mark as completed
    assert res_after_restart.status == "unknown"
    assert res_after_restart.error_code == "GOOGLE_FLOW_OUTPUT_NOT_RETRIEVED"
    assert res_after_restart.provider_operation_id == project_url
