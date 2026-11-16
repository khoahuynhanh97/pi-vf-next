import hashlib
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from automation import cli
from video_factory.integrations import nine_router
from video_factory.integrations.export_package import build_export_package


@pytest.fixture
def source_export(tmp_path, monkeypatch):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg is required for voiceover media verification')
    monkeypatch.setattr(cli, 'VF_WORKSPACE_DIR', tmp_path / 'workspace')
    import video_factory.storage.video_hub as hub
    monkeypatch.setattr(hub, 'get_video_ai_hub_root', lambda: tmp_path / 'hub')
    video = tmp_path / 'source.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'color=red:size=64x112:rate=30:duration=2', '-an', '-c:v', 'libx264', str(video)], check=True)
    return build_export_package(source_video=video, exports_root=tmp_path / 'exports',
                                project_id='pov-test', version='1', product_identity='Mứt mãng cầu',
                                verified_claims=['Hộp 500 g'])


def test_voiceover_draft_binds_source_and_rejects_tampered_input(source_export, tmp_path, monkeypatch, capsys):
    prompts = []

    def generate_json(**kwargs):
        prompts.append(json.loads(kwargs['user_prompt']))
        return SimpleNamespace(parsed={'display_text': 'Mứt mãng cầu đây.', 'tts_text': 'Mứt mãng cầu đây.'})

    monkeypatch.setattr(nine_router, 'configured_creative_model', lambda: SimpleNamespace(generate_json=generate_json))
    notes = tmp_path / 'notes.txt'
    notes.write_text('Cảnh 1: mở hộp.', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    args = ['voiceover', str(source_export.directory.relative_to(tmp_path)), '--draft']
    assert cli.main(args + ['--scene-notes', str(notes)]) == 0
    draft = Path(capsys.readouterr().out.strip())
    assert json.loads(draft.read_text(encoding='utf-8')) == {
        'source_video_sha256': source_export.video_sha256, 'product_identity': 'Mứt mãng cầu',
        'display_text': 'Mứt mãng cầu đây.', 'tts_text': 'Mứt mãng cầu đây.',
    }
    assert prompts == [{'product_identity': 'Mứt mãng cầu', 'scene_notes': 'Cảnh 1: mở hộp.'}]
    assert cli.main(args) == 0
    assert len(prompts) == 1
    capsys.readouterr()
    with source_export.video_path.open('ab') as stream:
        stream.write(b'tampered')
    assert cli.main(args) == 1
    assert 'voice_source_invalid' in capsys.readouterr().err
    assert len(prompts) == 1


def test_voiceover_draft_keeps_operator_edit_during_parallel_generation(source_export, tmp_path, monkeypatch):
    from video_factory.application.voiceover import draft_voiceover

    started, finish = Event(), Event()

    def generate_json(**kwargs):
        if 'scene_notes' in json.loads(kwargs['user_prompt']):
            started.set()
            assert finish.wait(5)
        return SimpleNamespace(parsed={'display_text': 'Lời nháp', 'tts_text': 'Lời nháp'})

    monkeypatch.setattr(nine_router, 'configured_creative_model', lambda: SimpleNamespace(generate_json=generate_json))
    notes = tmp_path / 'notes.txt'
    notes.write_text('Cảnh mở hộp.', encoding='utf-8')
    root = tmp_path / 'voiceovers'
    with ThreadPoolExecutor() as pool:
        slow = pool.submit(draft_voiceover, source_export.directory, root, notes_path=notes)
        try:
            assert started.wait(5)
            fast_path = draft_voiceover(source_export.directory, root)
            draft = json.loads(fast_path.read_text(encoding='utf-8'))
            draft['display_text'] = 'Lời đã sửa'
            fast_path.write_text(json.dumps(draft), encoding='utf-8')
        finally:
            finish.set()
        assert slow.result(timeout=5) == fast_path
    assert json.loads(fast_path.read_text(encoding='utf-8'))['display_text'] == 'Lời đã sửa'


@pytest.fixture
def tts_setup(tmp_path, monkeypatch):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-only')
    monkeypatch.setenv('ELEVENLABS_VOICE_ID', 'test-voice')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg is required for voiceover media verification')
    audio = tmp_path / 'test.mp3'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=400:duration=1',
                    '-c:a', 'libmp3lame', str(audio)], check=True)
    return audio.read_bytes()


def test_voiceover_reuses_audio_and_blocks_duplicate_after_uncertain_post(tmp_path, monkeypatch, tts_setup):
    from video_factory.integrations.elevenlabs_tts import get_or_create_voice_audio

    started, finish = Event(), Event()
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs['json']['text'])
        if len(calls) == 2:
            raise httpx.ReadTimeout('uncertain')
        started.set()
        assert finish.wait(5)
        return httpx.Response(200, content=tts_setup, headers={'request-id': 'test-id'})

    monkeypatch.setattr(httpx, 'post', post)
    args = {'voice_root': tmp_path, 'source_sha256': 'a' * 64, 'tts_text': 'Mứt mãng cầu'}
    with ThreadPoolExecutor() as pool:
        future = pool.submit(get_or_create_voice_audio, **args)
        try:
            assert started.wait(5)
            with pytest.raises(RuntimeError, match='voice_unknown'):
                get_or_create_voice_audio(**args)
        finally:
            finish.set()
        result = future.result(timeout=5)
    assert result[0].read_bytes() == tts_setup
    assert result[1] == 'test-id'
    assert get_or_create_voice_audio(**args) == result
    assert len(calls) == 1

    args['tts_text'] = 'Lời khác'
    for _ in range(2):
        with pytest.raises(RuntimeError, match='voice_unknown'):
            get_or_create_voice_audio(**args)
    assert calls == ['Mứt mãng cầu', 'Lời khác']


def test_voiceover_handles_confirmed_rejections(tmp_path, monkeypatch, tts_setup):
    from video_factory.integrations import elevenlabs_tts as tts

    responses = iter([
        httpx.Response(402), httpx.Response(429), httpx.Response(429),
        httpx.Response(429, headers={'retry-after': '10'}), httpx.Response(200, content=tts_setup),
    ])
    waits = []
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: next(responses))
    monkeypatch.setattr(tts, 'sleep', waits.append)
    args = {'voice_root': tmp_path, 'source_sha256': 'a' * 64, 'tts_text': 'Mứt mãng cầu'}
    with pytest.raises(RuntimeError, match='voice_no_credit'):
        tts.get_or_create_voice_audio(**args)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='voice_rate_limited'):
            tts.get_or_create_voice_audio(**args)
    assert tts.get_or_create_voice_audio(**args)[0].is_file()
    assert waits == [2.0]


def test_voiceover_exports_new_receipt_without_touching_source_or_hub(
    source_export, tmp_path, monkeypatch, capsys, tts_setup,
):
    from video_factory.integrations.composer import probe_video

    calls = []

    def post(url, **kwargs):
        calls.append(kwargs['json'])
        return httpx.Response(200, content=tts_setup)

    monkeypatch.setattr(httpx, 'post', post)
    script = tmp_path / 'script.json'
    payload = {'source_video_sha256': source_export.video_sha256, 'product_identity': 'Mứt mãng cầu',
               'display_text': 'Mứt mãng cầu đây.', 'tts_text': 'Mứt mãng cầu đây.'}
    script.write_text(json.dumps(payload), encoding='utf-8')
    verdict = source_export.directory / 'human_verdict.json'
    verdict.write_text('{"verdict":"approve"}', encoding='utf-8')
    hub_mp4 = tmp_path / 'hub' / 'Mứt mãng cầu' / 'final.mp4'
    source_bytes = source_export.video_path.read_bytes()
    assert hub_mp4.read_bytes() == source_bytes
    args = ['voiceover', str(source_export.directory), '--script', str(script)]
    real_run = subprocess.run

    def truncate_audio(command, *args, **kwargs):
        command = [part if part != 'apad' else 'atrim=end=0.2' for part in command if part != '-shortest']
        return real_run(command, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, 'run', truncate_audio)
        assert cli.main(args) == 1
        assert 'voice_mux_invalid' in capsys.readouterr().err
    assert len(calls) == 1
    real_replace = os.replace

    def publish_race(source, target):
        real_replace(source, target)
        if Path(source).name.startswith('.export-stage-'):
            raise PermissionError('A concurrent publisher already published this package')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', publish_race)
        assert cli.main(args) == 0
    target = Path(capsys.readouterr().out.strip())
    assert target.parent.parent == tmp_path / 'workspace' / 'voiceovers' / 'exports'
    receipt = json.loads((target.parent / 'metadata.json').read_text(encoding='utf-8'))
    assert receipt['video']['sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert receipt['provenance']['voiceover']['source_video_sha256'] == source_export.video_sha256
    assert probe_video(target).audio_codec == 'aac'
    assert probe_video(target).frame_count == probe_video(source_export.video_path).frame_count
    assert cli.main(args) == 0
    assert Path(capsys.readouterr().out.strip()) == target
    assert len(calls) == 1

    assert cli.main(['human-verdict', str(target.parent), '--verdict', 'approve', '--cause', 'other',
                     '--reason', 'Giọng rõ']) == 0
    assert source_export.video_path.read_bytes() == hub_mp4.read_bytes() == source_bytes
    assert verdict.read_text(encoding='utf-8') == '{"verdict":"approve"}'
    reviewed = json.loads((target.parent / 'human_verdict.json').read_text(encoding='utf-8'))
    assert reviewed['video_sha256'] == receipt['video']['sha256']

    long_audio = tmp_path / 'long.mp3'
    subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=400:duration=3', str(long_audio)], check=True)
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: httpx.Response(200, content=long_audio.read_bytes()))
    payload['tts_text'] = 'Mứt mãng cầu, một lời khác dài hơn.'
    script.write_text(json.dumps(payload), encoding='utf-8')
    assert cli.main(args) == 1
    assert 'voice_too_long' in capsys.readouterr().err
    assert list(target.parent.parent.glob('*/final.mp4')) == [target]
