from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_DEPTH = 5
MAX_STRING_LEN = 500
MAX_ITEMS = 50

SECRET_KEYWORDS = (
    "secret",
    "token",
    "password",
    "auth",
    "bearer",
    "api_key",
    "private_key",
)


@dataclass
class CapabilityError(Exception):
    code: str
    message: str
    retryable: bool
    capability_id: str
    actionable_recovery: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"CapabilityError[{self.code}]: {self.message} (capability={self.capability_id})"

    def to_dict(self) -> dict[str, Any]:
        msg = _sanitize_primitives(self.message)
        rec = (
            _sanitize_primitives(self.actionable_recovery)
            if self.actionable_recovery is not None
            else None
        )
        cleaned_details = _sanitize_primitives(self.details)
        res: dict[str, Any] = {
            "code": self.code,
            "message": msg if isinstance(msg, str) else str(msg),
            "retryable": self.retryable,
            "capability_id": self.capability_id,
            "details": cleaned_details if isinstance(cleaned_details, dict) else {},
        }
        if rec is not None:
            res["actionable_recovery"] = rec if isinstance(rec, str) else str(rec)
        return res


def _sanitize_primitives(val: Any, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        return "[TRUNCATED_DEPTH]"
    if isinstance(val, (int, float, bool, type(None))):
        return val
    if isinstance(val, str):
        if any(kw in val.lower() for kw in SECRET_KEYWORDS):
            return "[REDACTED]"
        if len(val) > MAX_STRING_LEN:
            return val[:MAX_STRING_LEN] + "..."
        return val
    if isinstance(val, dict):
        res = {}
        for idx, (k, v) in enumerate(val.items()):
            if idx >= MAX_ITEMS:
                res["_truncated"] = f"... {len(val) - MAX_ITEMS} more items"
                break
            if isinstance(k, (str, int, float, bool)):
                k_str = str(k)[:MAX_STRING_LEN]
            else:
                k_str = f"[UNSUPPORTED_KEY:{type(k).__name__}]"

            if any(kw in k_str.lower() for kw in SECRET_KEYWORDS):
                res[k_str] = "[REDACTED]"
            else:
                res[k_str] = _sanitize_primitives(v, depth + 1)
        return res
    if isinstance(val, (list, tuple, set, frozenset)):
        items = list(val)
        res_list = []
        for idx, item in enumerate(items):
            if idx >= MAX_ITEMS:
                res_list.append(f"... {len(items) - MAX_ITEMS} more items")
                break
            res_list.append(_sanitize_primitives(item, depth + 1))
        return res_list

    return f"[UNSUPPORTED_TYPE:{type(val).__name__}]"
