import json, hashlib
from dataclasses import dataclass, asdict

@dataclass(frozen=True)
class ProductResearchRef:
    owner: str = "product_intelligence"
    version: int = 1
    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

@dataclass(frozen=True)
class ResourcePackDraftRef:
    owner: str = "product_intelligence"
    version: int = 1
    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

@dataclass(frozen=True)
class ProductResourceLockRef:
    owner: str = "product_intelligence"
    version: int = 1
    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

@dataclass(frozen=True)
class ProductResearchRequest:
    owner: str = "product_intelligence"
    payload: dict | None = None
    version: int = 1
    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)
