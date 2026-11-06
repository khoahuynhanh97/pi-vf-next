from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class VideoArtifactRef:
    owner: str = "video_factory"
    version: int = 1

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True)
class CreativeBriefRequest:
    product_id: str = ""
    brief_config: dict = field(default_factory=dict)
    owner: str = "video_factory"
    version: int = 1

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True)
class ScenePlanRequest:
    owner: str = "video_factory"
    version: int = 1

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True)
class StoryboardRequest:
    owner: str = "video_factory"
    version: int = 1

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True)
class RenderRequest:
    owner: str = "video_factory"
    version: int = 1

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True)
class ExportRequest:
    owner: str = "video_factory"
    version: int = 1

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)
