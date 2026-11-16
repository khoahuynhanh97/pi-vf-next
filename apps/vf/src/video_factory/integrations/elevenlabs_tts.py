"""One ElevenLabs TTS request with a local receipt and durable submit guard."""

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from time import sleep

import av
import httpx

from ..storage.layout import ProductVideoWorkspaceLayout
from .composer import compute_file_sha256

MODEL_ID = 'eleven_flash_v2_5'
LANGUAGE_CODE = 'vi'
OUTPUT_FORMAT = 'mp3_44100_128'
_REJECTIONS = {
    400: 'voice_invalid_request', 401: 'voice_auth_failed', 402: 'voice_no_credit',
    403: 'voice_access_denied', 404: 'voice_not_found', 422: 'voice_invalid_request',
    429: 'voice_rate_limited',
}


def voice_id_from_env() -> str:
    voice_id = os.environ.get('ELEVENLABS_VOICE_ID', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', voice_id):
        raise RuntimeError('voice_not_configured: Cần cấu hình ELEVENLABS_VOICE_ID hợp lệ.')
    return voice_id


def audio_duration(source: Path | BytesIO) -> float:
    try:
        with av.open(str(source) if isinstance(source, Path) else source) as container:
            if not container.streams.audio:
                raise ValueError('Không có track audio')
            duration = sum(frame.samples / frame.sample_rate for frame in container.decode(audio=0))
        if duration > 0:
            return duration
    except (av.FFmpegError, ValueError, OSError, ZeroDivisionError) as exc:
        raise ValueError('voice_invalid_audio: Không giải mã được audio.') from exc
    raise ValueError('voice_invalid_audio: Audio trống.')


def _retry_delay(value: str | None) -> float:
    if not value:
        return 2.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 2.0


def _unknown() -> RuntimeError:
    return RuntimeError('voice_unknown: Lần gửi đang xử lý hoặc chưa rõ kết quả. Có thể đã trừ credit.')


def get_or_create_voice_audio(
    *, voice_root: Path, source_sha256: str, tts_text: str,
) -> tuple[Path, str | None]:
    voice_id = voice_id_from_env()
    text = unicodedata.normalize('NFC', tts_text.strip())
    if not text:
        raise ValueError('voice_invalid_script: tts_text đang trống.')
    payload = {'text': text, 'model_id': MODEL_ID, 'language_code': LANGUAGE_CODE}
    inputs = [source_sha256, voice_id, OUTPUT_FORMAT, payload]
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    folder = voice_root / 'audio' / digest
    audio_path, receipt_path = folder / 'voice.mp3', folder / 'receipt.json'
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
            if compute_file_sha256(audio_path) == receipt['audio_sha256']:
                return audio_path, receipt.get('request_id')
        except (OSError, ValueError, KeyError, TypeError):
            pass
        raise _unknown()

    claim = folder / 'submitted'
    if claim.exists():
        raise _unknown()
    key = os.environ.get('ELEVENLABS_API_KEY', '').strip()
    if not key:
        raise RuntimeError('voice_not_configured: Cần cấu hình ELEVENLABS_API_KEY.')
    folder.mkdir(parents=True, exist_ok=True)
    try:
        claim.mkdir()
    except FileExistsError:
        raise _unknown() from None

    def post() -> httpx.Response:
        try:
            return httpx.post(
                f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',
                params={'output_format': OUTPUT_FORMAT}, headers={'xi-api-key': key},
                json=payload, timeout=30.0,
            )
        except httpx.RequestError:
            raise _unknown() from None

    response = post()
    if response.status_code == 429:
        wait = _retry_delay(response.headers.get('retry-after'))
        if wait <= 5.0:
            sleep(wait)
            response = post()
    code = _REJECTIONS.get(response.status_code)
    if code:
        claim.rmdir()
        raise RuntimeError(f'{code}: ElevenLabs từ chối yêu cầu (HTTP {response.status_code}).')
    if response.status_code != 200:
        raise _unknown()

    try:
        audio_duration(BytesIO(response.content))
    except ValueError:
        raise _unknown() from None
    request_id = response.headers.get('request-id', '')
    receipt = {
        'audio_sha256': hashlib.sha256(response.content).hexdigest(),
        'request_id': request_id if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id) else None,
    }
    ProductVideoWorkspaceLayout.atomic_write_bytes(audio_path, response.content)
    ProductVideoWorkspaceLayout.atomic_write_text(receipt_path, json.dumps(receipt))
    return audio_path, receipt['request_id']
