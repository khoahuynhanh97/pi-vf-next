"""Exercise provider controls in actual Chrome against offline DOM fixtures."""
import pytest
from video_factory.integrations.google_flow_provider import GoogleFlowBrowserPage
from video_factory.integrations.ports import VideoGenerationRequest, VideoGenerationResult
from unittest.mock import MagicMock, patch
from dataclasses import replace
from video_factory.integrations.google_flow_keyframe_provider import GoogleFlowKeyframeProvider


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True,
            proxy={'server': 'http://127.0.0.1:9', 'bypass': '<-loopback>'})
        context = browser.new_context(service_workers='block')
        context.route('**/*', lambda route: route.abort())
        page = context.new_page()
        page.set_default_timeout(500)
        yield page
        context.close()
        browser.close()


@pytest.fixture
def adapter(tmp_path):
    return GoogleFlowBrowserPage(profile_path=tmp_path, chrome_profile_directory='Default',
        output_dir=tmp_path, timeout_seconds=1)


def test_checked_agent_chip_is_disabled_without_clicking_unrelated_close(page, adapter):
    page.set_content('''<button aria-label="Close" onclick="window.wrongClose=true">Close</button>
    <button class="agent-mode-chip agent-mode-chip-checked"
      onclick="this.classList.remove('agent-mode-chip-checked');setTimeout(()=>document.querySelector('.settings-trigger-button').style.display='block',40)">Agent</button>
    <button class="settings-trigger-button" style="display:none">Video</button>''')
    adapter._close_agent_panel(page)
    assert page.locator('.settings-trigger-button').is_visible()
    assert page.evaluate('window.wrongClose === undefined')


def test_manual_mode_does_not_click_asset_dialog_close(page, adapter):
    page.set_content('''<button class="settings-trigger-button">Video</button>
      <button aria-label="Close" onclick="window.wrongClose=true">Close</button>''')
    adapter._close_agent_panel(page)
    assert page.evaluate('window.wrongClose === undefined')


def test_expanded_agent_closes_only_its_own_panel(page):
    from video_factory.integrations.flow_ui_recipe import ensure_manual_mode
    page.set_content('''<button aria-label="Close" onclick="window.wrongClose=true">Other Close</button>
    <flow-agent-panel><div class="agent-panel-header"><button aria-label="Close"
    onclick="document.querySelector('flow-agent-panel').remove();document.querySelector('.settings-trigger-button').hidden=false">Close</button></div></flow-agent-panel>
    <button class="settings-trigger-button" hidden>Video</button>''')
    ensure_manual_mode(page, timeout_ms=1000)
    assert page.locator('.settings-trigger-button').is_visible()
    assert page.evaluate('window.wrongClose === undefined')


def settings_html(*, missing_x1=False, cost=True, close_on_mode=False):
    groups = [('image', ['Image', 'Video']), ('mode', ['Frames', 'Ingredients']),
              ('aspect', ['16:9', '9:16']), ('duration', ['4 s', '8 s']),
              ('count', ['x2'] if missing_x1 else ['x2', 'x1'])]
    controls = ''
    for group, values in groups:
        controls += '<span>'
        for i, value in enumerate(values):
            prefix = '<i>icon</i>' if group in {'image', 'mode', 'aspect'} else ''
            closing = "document.querySelector('.cdk-overlay-container').hidden=true;" if close_on_mode and group == 'image' else ''
            controls += '''<button role="radio" aria-checked="{}" onclick="for(const e of this.parentElement.children)e.setAttribute('aria-checked','false');setTimeout(()=>this.setAttribute('aria-checked','true'),40);{}">{}{}</button>'''.format(str(i == 0).lower(), closing, prefix, value)
        controls += '</span>'
    return '''<button class="settings-trigger-button" onclick="document.querySelector('.cdk-overlay-container').hidden=false">Video 8s crop_9_16 x1</button>
    <div class="cdk-overlay-container" hidden>''' + controls + '''<button aria-label="Select model family">Omni 1.1 Flash</button>''' + ('Generation costs 12 credits' if cost else '') + '''</div><script>document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelector('.cdk-overlay-container').hidden=true})</script>'''


def request():
    return VideoGenerationRequest(request_id='offline', owner_user_id='owner', project_id='project',
        scene_id='scene', prompt='test', duration_seconds=8, aspect_ratio='9:16')


def test_configure_verifies_model_aspect_duration_and_output(page, adapter):
    page.set_content(settings_html())
    assert adapter._configure_project(page, request()) == 12


def test_mode_switch_reopens_settings_when_flow_closes_popup(page, adapter):
    page.set_content(settings_html(close_on_mode=True))
    assert adapter._configure_project(page, request()) == 12


def test_nonpaid_settings_can_be_selected_while_popup_is_animating(page, adapter):
    page.set_content('''<style>@keyframes moving{from{transform:translateX(0)}to{transform:translateX(15px)}}
      [role=radio]{animation:moving .1s infinite alternate}</style>''' + settings_html())
    assert adapter._configure_project(page, request()) == 12


@pytest.mark.parametrize('missing_x1,cost,error', [
    (True, True, 'OUTPUT_COUNT'), (False, False, 'COST')])
def test_missing_control_or_cost_blocks_before_send(page, adapter, missing_x1, cost, error):
    adapter.timeout_ms = 3000
    page.set_content(settings_html(missing_x1=missing_x1, cost=cost))
    with pytest.raises(RuntimeError, match=error):
        adapter._configure_project(page, request())


def test_start_frame_absence_is_not_treated_as_attached(page, adapter, tmp_path):
    page.set_content('<div>No frame controls loaded</div>')
    with pytest.raises(RuntimeError, match='START'):
        adapter._attach_start_frame(page, tmp_path/'reference.png')


def test_keyframe_uses_manual_image_controls_and_single_output(page, tmp_path):
    page.set_content(settings_html().replace('Omni 1.1 Flash', 'Nano Banana 2.1'))
    provider = GoogleFlowKeyframeProvider(profile_path=tmp_path, output_dir=tmp_path,
        model='Nano Banana 2.1', timeout_seconds=1)
    provider._ensure_image_generation_settings(page)
    assert page.locator('[role=radio]').filter(has_text='Image').get_attribute('aria-checked') == 'true'
    assert page.locator('[role=radio]').filter(has_text='x1').get_attribute('aria-checked') == 'true'


def test_existing_project_without_video_never_automatically_sends_again(adapter, tmp_path):
    source = tmp_path/'source.png'
    source.write_bytes(b'reference')
    req = replace(request(), reference_image_paths=(str(source),))
    with patch.object(adapter, '_browser') as browser, \
         patch.object(adapter, '_home'), patch.object(adapter, '_project_card', return_value=object()), \
         patch.object(adapter, '_project_url', return_value='https://flow.google.com/project/00000000-0000-0000-0000-000000000001'), \
         patch.object(adapter, '_status_in_project', return_value=VideoGenerationResult(request_id=req.request_id,status='failed',error_code='RECONCILIATION_REQUIRED')), \
         patch.object(adapter, '_close_agent_panel'), patch.object(adapter, '_configure_project', return_value=12), \
         patch.object(adapter, '_attach_start_frame'), patch.object(adapter, '_generate', return_value=VideoGenerationResult(request_id=req.request_id,status='submitted')) as generate:
        browser.return_value.__enter__.return_value = MagicMock()
        result = adapter.submit(req)
    assert result.error_code == 'RECONCILIATION_REQUIRED'
    generate.assert_not_called()


_VIDEO_PENDING = '<flow-pending-tile style="display:block;width:100px;height:40px"><mat-icon>movie</mat-icon></flow-pending-tile>'
_FLOW_PROJECT_URL = 'https://flow.google.com/project/00000000-0000-0000-0000-000000000001'


def test_video_submit_reports_consent_required_before_send(adapter, tmp_path):
    from video_factory.integrations.flow_ui_recipe import FlowUploadConsentRequired

    source = tmp_path / 'reference.png'
    source.write_bytes(b'reference')
    req = replace(request(), reference_image_paths=(str(source),))
    page = MagicMock(url=_FLOW_PROJECT_URL)
    page.locator.return_value.count.return_value = 0
    page.locator.return_value.inner_text.return_value = adapter._marker(req.request_id)
    with patch('time.sleep'), patch.object(adapter, '_browser'), \
         patch.object(adapter, '_home', return_value=page), \
         patch.object(adapter, '_project_card', return_value=None), \
         patch.object(adapter, '_close_agent_panel'), \
         patch.object(adapter, '_configure_project', return_value=12), \
         patch.object(adapter, '_attach_start_frame', side_effect=FlowUploadConsentRequired('Confirm rights')), \
         patch.object(adapter, '_generate') as generate:
        result = adapter.submit(req)

    assert (result.status, result.error_code) == ('failed', 'FLOW_UPLOAD_CONSENT_REQUIRED')
    assert result.provider_operation_id == _FLOW_PROJECT_URL
    assert result.metadata['send_attempted'] is False
    generate.assert_not_called()


def _generate_page(page, *, prompt='test', on_send=None, persisted_video=True):
    page.set_default_timeout(5000)
    if on_send is None:
        on_send = "document.body.insertAdjacentHTML(`beforeend`, `" + _VIDEO_PENDING.replace('"', "'") + "`)"
    page.context.route(_FLOW_PROJECT_URL, lambda route: route.fulfill(
        content_type='text/html',
        body=_VIDEO_PENDING if persisted_video else '<flow-image-tile>Reference</flow-image-tile>',
    ))
    page.set_content('''<div class="ProseMirror" contenteditable="true" style="position:fixed;top:50px;left:50px;width:300px;height:40px">''' + prompt + '''</div>
      <button class="settings-trigger-button">Video 8s x1</button>
      <button class="generate-icon-button" onclick="window.sendCount=(window.sendCount||0)+1;''' + on_send + '''">Send</button>''')
    page.wait_for_timeout = lambda _ms: None


def test_generate_requires_exact_prompt_before_send(page, adapter):
    from unittest.mock import patch
    _generate_page(page, prompt='wrong prompt')
    page.keyboard.insert_text = lambda _text: None
    req = replace(request(), prompt='test')
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        with pytest.raises(RuntimeError, match='PROMPT_NOT_VERIFIED'):
            adapter._generate(page, req, 'https://flow.google.com/project/00000000-0000-0000-0000-000000000001', 12)
    assert page.evaluate('window.sendCount || 0') == 0


def test_generate_does_not_accept_reference_image_pending_as_video(page, adapter):
    _generate_page(page, on_send="document.body.insertAdjacentHTML(`beforeend`, `<flow-pending-tile style='display:block;width:100px;height:40px'><mat-icon>image</mat-icon></flow-pending-tile>`)")
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        with pytest.raises(RuntimeError) as exc:
            adapter._generate(page, request(), 'https://flow.google.com/project/00000000-0000-0000-0000-000000000001', 12)
    assert exc.value.code == 'GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN'
    assert page.evaluate('window.sendCount') == 1


def test_generate_does_not_accept_pending_without_icon_as_video(page, adapter):
    _generate_page(page, on_send="document.body.insertAdjacentHTML(`beforeend`, `<flow-pending-tile style='display:block;width:100px;height:40px'><span>unknown</span></flow-pending-tile>`)")
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        with pytest.raises(RuntimeError) as exc:
            adapter._generate(page, request(), 'https://flow.google.com/project/00000000-0000-0000-0000-000000000001', 12)
    assert exc.value.code == 'GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN'
    assert page.evaluate('window.sendCount') == 1


def test_generate_accepts_video_pending_after_single_send(page, adapter):
    _generate_page(page)
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        result = adapter._generate(page, request(), 'https://flow.google.com/project/00000000-0000-0000-0000-000000000001', 12)
    assert result.status == 'submitted'
    assert page.evaluate('window.sendCount') == 1


def test_generate_requires_pending_video_to_survive_project_reopen(page, adapter):
    _generate_page(page, persisted_video=False)
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        with pytest.raises(RuntimeError) as exc:
            adapter._generate(page, request(), _FLOW_PROJECT_URL, 12)
    assert exc.value.code == 'GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN'
    assert exc.value.provider_operation_id == _FLOW_PROJECT_URL
    assert page.evaluate('window.sendCount') == 1


def test_generate_observes_delayed_video_after_project_refresh(page, adapter):
    _generate_page(page)
    observations = []

    def serve_project(route):
        observations.append(route.request.url)
        route.fulfill(
            content_type='text/html',
            body=_VIDEO_PENDING if len(observations) > 1 else '<flow-image-tile>Reference</flow-image-tile>',
        )

    page.context.route(_FLOW_PROJECT_URL, serve_project)
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        result = adapter._generate(page, request(), _FLOW_PROJECT_URL, 12)
    assert result.status == 'submitted'
    assert len(observations) == 2
    assert page.evaluate('window.sendCount') == 1


def test_generate_reports_explicit_flow_rejection_in_http_200(page, adapter):
    page.route(_FLOW_PROJECT_URL, lambda route: route.fulfill(content_type='text/html', body='<html></html>'))
    page.goto(_FLOW_PROJECT_URL)
    page.route('**/batchexecute', lambda route: route.fulfill(
        status=200, content_type='text/plain', body='["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]',
    ))
    _generate_page(page, on_send="fetch('/batchexecute',{method:'POST'})", persisted_video=False)
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        result = adapter._generate(page, request(), _FLOW_PROJECT_URL, 12)
    assert result.status == 'failed'
    assert result.error_code == 'PUBLIC_ERROR_UNUSUAL_ACTIVITY'
    assert result.provider_operation_id == _FLOW_PROJECT_URL
    assert page.evaluate('window.sendCount') == 1


@pytest.mark.parametrize("optimistic_tile", [True, False])
def test_generate_keeps_confirmed_video_if_flow_also_reports_activity_marker(page, adapter, optimistic_tile):
    page.route(_FLOW_PROJECT_URL, lambda route: route.fulfill(content_type='text/html', body='<html></html>'))
    page.goto(_FLOW_PROJECT_URL)
    page.route('**/batchexecute', lambda route: route.fulfill(
        status=200, content_type='text/plain', body='["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]',
    ))
    pending = ("document.body.insertAdjacentHTML('beforeend',"
               "'<flow-pending-tile><mat-icon>movie</mat-icon></flow-pending-tile>');") if optimistic_tile else ""
    _generate_page(page, on_send=pending + "fetch('/batchexecute',{method:'POST'})")
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        result = adapter._generate(page, request(), _FLOW_PROJECT_URL, 12)
    assert result.status == 'submitted'
    assert result.provider_operation_id == _FLOW_PROJECT_URL
    assert page.evaluate('window.sendCount') == 1


def test_generate_ignores_activity_marker_from_request_started_before_send(page, adapter):
    page.route(_FLOW_PROJECT_URL, lambda route: route.fulfill(content_type='text/html', body='<html></html>'))
    page.goto(_FLOW_PROJECT_URL)
    earlier = []
    page.route('**/batchexecute', lambda route: earlier.append(route))

    def complete_after_send(route):
        earlier[0].fulfill(status=200, body='["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]')
        route.fulfill(status=200)

    page.route('**/sent', complete_after_send)
    _generate_page(page, on_send="fetch('/sent')", persisted_video=False)
    with page.expect_request('**/batchexecute'):
        page.evaluate("() => { void fetch('/batchexecute', {method:'POST'}); }")
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        with pytest.raises(RuntimeError) as exc:
            adapter._generate(page, request(), _FLOW_PROJECT_URL, 12)
    assert exc.value.code == 'GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN'
    assert page.evaluate('window.sendCount') == 1


def test_generate_accepts_multiline_paragraph_prompt(page, adapter):
    _generate_page(page)
    req = replace(request(), prompt='First line of prompt.\nSecond line of prompt.')
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        result = adapter._generate(page, req, 'https://flow.google.com/project/00000000-0000-0000-0000-000000000001', 12)
    assert result.status == 'submitted'
    assert page.evaluate('window.sendCount') == 1


@pytest.mark.parametrize('rendered_text,expected_prompt', [
    ('Action text only', 'Action text only\n{"identity_lock": "prod_123"}'),
    ('Action text\n{"identity_lock": "wrong_id"}', 'Action text\n{"identity_lock": "prod_123"}'),
    ('Altered action text\n{"identity_lock": "prod_123"}', 'Action text\n{"identity_lock": "prod_123"}'),
])
def test_generate_strictly_rejects_altered_or_missing_identity_lock(page, adapter, rendered_text, expected_prompt):
    _generate_page(page, prompt=rendered_text)
    page.keyboard.insert_text = lambda _text: None
    req = replace(request(), prompt=expected_prompt)
    with patch.object(adapter, '_assert_start_frame'), \
         patch.object(adapter, '_credit_balance_on_page', return_value=100):
        with pytest.raises(RuntimeError, match='GOOGLE_FLOW_PROMPT_NOT_VERIFIED'):
            adapter._generate(page, req, 'https://flow.google.com/project/00000000-0000-0000-0000-000000000001', 12)
    assert page.evaluate('window.sendCount || 0') == 0


@pytest.mark.parametrize('html,accepted', [
    ('<button class="chip-container" aria-busy="false">image</button><button class="empty-chip">End</button>', True),
    ('<button class="empty-chip">End</button>', False),
    ('<button class="chip-container" aria-busy="true">image</button><button class="empty-chip">End</button>', False),
])
def test_start_requires_positive_ready_chip_in_start_slot(page, adapter, html, accepted):
    page.set_content(html)
    if accepted:
        adapter._assert_start_frame(page)
    else:
        with pytest.raises(RuntimeError, match='START'):
            adapter._assert_start_frame(page)


def test_image_references_select_exact_content_instead_of_first_asset(page, tmp_path):
    import hashlib
    source = tmp_path/'product.png'
    source.write_bytes(b'product reference')
    filename = 'flow-ref-' + hashlib.sha256(source.read_bytes()).hexdigest() + '.png'
    page.set_content('''<button class="add-menu-trigger" onclick="document.querySelector('#picker').hidden=false">Add</button>
    <div id="picker" hidden><button class="sidebar-upload-btn">Upload</button>
    <button class="asset-item" onclick="window.wrongAsset=true">another-product.png</button>
    <button class="asset-item" onclick="this.classList.add('asset-item-active')">''' + filename + '''<span>Image</span></button>
    <button class="detail-add-to-prompt-btn" onclick="attach()">Add to prompt</button></div>
    <script>function attach(){document.querySelector('#picker').hidden=true;
    const chip=document.createElement('button');chip.className='chip-container';chip.setAttribute('aria-busy','false');chip.textContent='image';document.body.append(chip)}</script>''')
    provider = GoogleFlowKeyframeProvider(profile_path=tmp_path,output_dir=tmp_path,timeout_seconds=1)
    provider._attach_image_references(page,[source])
    assert page.locator('.chip-container').count() == 1
    assert page.evaluate('window.wrongAsset === undefined')


def test_reference_picker_waits_for_exact_title_to_finish_uploading(page, tmp_path):
    import hashlib
    from video_factory.integrations.flow_ui_recipe import attach_reference_asset

    source = tmp_path / 'product.webp'
    source.write_bytes(b'product reference')
    filename = 'flow-ref-' + hashlib.sha256(source.read_bytes()).hexdigest() + '.webp'
    page.set_content(f'''
        <button class="sidebar-upload-btn" onclick="window.uploadedAgain=true">Upload</button>
        <button class="asset-item" onclick="window.wrongAsset=true">
            <span class="asset-title">other-{filename}</span>
        </button>
        <button class="asset-item" onclick="window.pickedWhileUploading=!!document.querySelector('#progress')">
            <span id="progress">Đang tải lên</span><mat-icon>image</mat-icon>
            <span class="asset-title">{filename}</span><span class="type-subtitle">Hình ảnh</span>
        </button>
        <script>setTimeout(() => document.querySelector('#progress').remove(), 200)</script>
    ''')
    attach_reference_asset(page, source, tmp_path / 'uploads', 1000)
    assert page.evaluate('window.pickedWhileUploading === false')
    assert page.evaluate('window.wrongAsset === undefined && window.uploadedAgain === undefined')


def test_reference_picker_reports_consent_required_without_accepting(page, tmp_path):
    from video_factory.integrations.flow_ui_recipe import FlowUploadConsentRequired, attach_reference_asset

    source = tmp_path / 'product.png'
    source.write_bytes(b'product reference')
    page.set_content('''
        <input type="file" hidden onchange="document.querySelector('[role=dialog]').hidden=false">
        <button class="sidebar-upload-btn" onclick="document.querySelector('input').click()">Upload</button>
        <div role="dialog" hidden>
            <h2>Quyền sử dụng hình ảnh này</h2>
            <button onclick="window.accepted=true">Tôi đồng ý</button>
        </div>
    ''')

    with pytest.raises(FlowUploadConsentRequired):
        attach_reference_asset(page, source, tmp_path / 'uploads', 1000)
    assert page.get_by_role('dialog').is_visible()
    assert page.evaluate('window.accepted === undefined')


def test_reference_picker_rejects_duplicate_exact_titles(page, tmp_path):
    import hashlib
    from video_factory.integrations.flow_ui_recipe import attach_reference_asset

    source = tmp_path / 'product.webp'
    source.write_bytes(b'product reference')
    filename = 'flow-ref-' + hashlib.sha256(source.read_bytes()).hexdigest() + '.webp'
    page.set_content(f'''
        <button class="sidebar-upload-btn" onclick="window.clicked=true">Upload</button>
        <button class="asset-item" onclick="window.clicked=true">
            <mat-icon>image</mat-icon><span class="asset-title">{filename}</span>
        </button>
        <button class="asset-item" onclick="window.clicked=true">
            <mat-icon>image</mat-icon><span class="asset-title">{filename}</span>
        </button>
    ''')
    with pytest.raises(RuntimeError, match='GOOGLE_FLOW_REFERENCE_ASSET_NOT_UNIQUE.*matches=2'):
        attach_reference_asset(page, source, tmp_path / 'uploads', 1000)
    assert page.evaluate('window.clicked === undefined')
