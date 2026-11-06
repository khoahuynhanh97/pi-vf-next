"""Durable research snapshot contracts.

A ResearchSnapshot is an immutable historical record of what Product
Intelligence observed/resolved at time T. It is not latest global truth, and it
is never a second resolution layer.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class ResearchSnapshot(BaseModel):
    snapshot_id: str
    research_id: str
    created_at: str
    status: str = ""
    request: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    research_metadata: dict = Field(default_factory=dict)


def new_snapshot_id() -> str:
    return f"snap_{uuid.uuid4().hex}"


def research_id_from_result(result) -> str:
    """Deterministic logical research id from the request seed.

    Based on the research seed only (URLs, query, brand/model/name) — never on
    transient media counts. Two research runs with the same seed share a
    research_id; each run produces a distinct immutable snapshot.
    """
    request = result.request
    parts: list[str] = []
    if request.url:
        parts.append(request.url)
    parts.extend(sorted(request.additional_urls))
    hint = request.product_identity_hint
    if hint is not None:
        for key in ("name", "brand", "model"):
            value = getattr(hint, key)
            if value:
                parts.append(f"{key}={value}")
    if request.query:
        parts.append(f"query={request.query}")
    payload = json.dumps([str(part) for part in parts], ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"research_{digest}"


def make_snapshot(result, *, research_id: str | None = None) -> ResearchSnapshot:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return ResearchSnapshot(
        snapshot_id=new_snapshot_id(),
        research_id=research_id or research_id_from_result(result),
        created_at=now,
        status=result.status.value,
        request=result.request.model_dump(mode="json"),
        result=result.model_dump(mode="json"),
        schema_version=SCHEMA_VERSION,
        research_metadata={
            "sources": result.summary.sources,
            "usable_evidence": result.summary.usable_evidence,
            "canonical_products": result.summary.canonical_products,
            "media_assets": result.summary.media_assets,
            "stop_reason": result.summary.stop_reason,
        },
    )
