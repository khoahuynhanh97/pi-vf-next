"""Model/provider boundaries for Product Video creative generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
import base64
import hashlib
from pathlib import Path
from io import BytesIO

from .contracts import CreativeBriefV2, ResearchDossier, ScriptScenePlanV2


@dataclass(frozen=True)
class MediaReference:
    path: str
    sha256: str
    role: str

    def content(self) -> list[dict[str, Any]]:
        """Validate/hash actual pixels before attaching a provider-supported data URL."""
        from PIL import Image

        if not self.role.strip():
            raise ValueError('media role is required')
        data = Path(self.path).read_bytes()
        if not data or len(data) > 20 * 1024 * 1024:
            raise ValueError('empty or oversized media')
        if hashlib.sha256(data).hexdigest() != self.sha256.removeprefix('sha256:'):
            raise ValueError('media SHA mismatch')
        with Image.open(BytesIO(data)) as im:
            mime = Image.MIME.get(im.format)
            im.verify()
        if mime not in {'image/jpeg', 'image/png', 'image/webp'}:
            raise ValueError('unsupported image MIME')
        return [{'type': 'text', 'text': f'Media role: {self.role}; SHA256: {self.sha256}'},
                {'type': 'image_url', 'image_url': {
                    'url': f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')}}]


class MediaStructuredCreativeModel(Protocol):
    def generate_media_json(self, *, system_prompt: str, user_prompt: str,
                            schema_name: str, json_schema: Mapping[str, object],
                            temperature: float, media: tuple[MediaReference, ...]) -> StructuredModelResult:
        raise NotImplementedError


@dataclass(frozen=True)
class StructuredModelResult:
    parsed: dict[str, Any] | list[dict[str, Any]]
    raw_text: str
    provider: str
    model: str
    total_tokens: int | None = 0
    parse_error: str = ""
    route_receipt: Mapping[str, Any] | None = None
    retry_allowed: bool = True


class StructuredCreativeModel(Protocol):
    def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: Mapping[str, object],
        temperature: float,
    ) -> StructuredModelResult:
        raise NotImplementedError


class CreativeGenerationPort(Protocol):
    def create_direction(self, dossier: ResearchDossier) -> CreativeBriefV2:
        raise NotImplementedError

    def create_script(
        self,
        dossier: ResearchDossier,
        brief: CreativeBriefV2,
    ) -> ScriptScenePlanV2:
        raise NotImplementedError
