"""F1-F5 Video Factory MCP Server with canonical tools and Hermes wire compatibility."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import sqlite3
import sys
from collections.abc import Mapping

logger = logging.getLogger(__name__)
from dataclasses import asdict, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from typing_extensions import TypedDict


class RenderProviderConfigurationError(ValueError):
    """Expected, non-secret configuration failure for Product Video render."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_file_path(path_or_url: str | Path) -> Path:
    from video_factory.storage.layout import get_workspace_layout

    try:
        ws = get_workspace_layout()
        return ws.resolve_path(path_or_url)
    except Exception:
        s = str(path_or_url).strip()
        if s.startswith("file://"):
            from urllib.parse import unquote, urlparse

            parsed = urlparse(s)
            raw = unquote(parsed.path)
            netloc = unquote(parsed.netloc)
            if netloc and not raw.startswith(("/", "\\")):
                raw = f"{netloc}/{raw}"
            elif netloc:
                raw = f"{netloc}{raw}"
            if (
                len(raw) >= 3
                and raw[0] in ("/", "\\")
                and raw[2] == ":"
                and raw[1].isalpha()
            ):
                raw = raw[1:]
            return Path(raw).resolve()
        return Path(s).resolve()


try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from mcp.server import MCPServer as FastMCP

from video_factory.integrations.repository import (
    SQLiteVideoFactoryRepository,
)
from video_factory.integrations.service import VideoFactoryService
from video_factory.integrations.composer import (
    InvalidVideoArtifact,
    VideoExpectation,
    concatenate_videos,
    compute_file_sha256,
    probe_video,
)
from video_factory.integrations.export_package import (
    ExportPackageConflict,
    build_export_package,
)
from video_factory.job_contracts import TerminalJobError
from video_factory.integrations.scene_renderer import (
    SceneRenderOperationStore,
    SceneRenderRuntime,
    compile_grounded_scene_request,
)
from video_factory.integrations.capacity import (
    ProviderCapacityStore,
    configured_capacity,
    process_lease_owner,
)
from video_factory.integrations.ports import (
    OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS,
    validate_product_video_render_provider,
)
from video_factory.integrations.provider_selection import (
    ProviderSelectionError,
    VideoProviderSelector,
    validate_reconciled_result,
)
from video_factory.integrations.scene_validator import (
    validate_scene_output,
)
from video_factory.integrations.domain import (
    AssetReference,
    Claim,
    ClaimStatus,
    CreativeBrief,
    FinalApprovalStatus,
    FrameGenerationStatus,
    FramePrompt,
    GeneratedScene,
    ProjectStatus,
    RawIdea,
    ResourceIdentity,
    ResourcePack,
    Scene,
    ScenePlan,
    Storyboard,
    StoryboardApprovalStatus,
    StoryboardFrame,
    Timeline,
    TimelineClip,
    TimelineStatus,
    VideoFactoryProject,
    VideoGenerationStatus,
    VideoPrompt,
)

mcp = FastMCP("video-factory")


def _invalidate_final_approval(
    srv: VideoFactoryService,
    owner_user_id: str,
    project_id: str,
    reason: str,
) -> None:
    """Centralized downstream invalidation of Final Approval on clip or composite tampering."""
    if not owner_user_id or not project_id:
        return
    proj = srv.get_project(owner_user_id, project_id)
    if proj.final_approval == FinalApprovalStatus.APPROVED:
        srv.repository.save(
            replace(
                proj,
                final_approval=FinalApprovalStatus.PENDING,
                final_approval_notes=reason,
                updated_at=_now(),
            )
        )


# Delivery spec: 9:16 portrait (720x1280), H.264, 24fps.
# This expectation is enforced fail-closed at every export call via validate_export().
_EXPORT_VIDEO_EXPECTATION = VideoExpectation(
    width=720,
    height=1280,
    duration_seconds=60.0,
    codec="h264",
    fps=24.0,
    min_frames=1,
    max_duration_delta=60.0,
    check_black_bars=True,
)

_NO_SOURCE_BRANDING_CONSTRAINT = (
    "Do not render any readable text, watermark, seller name, shop name, marketplace UI, "
    "third-party logo, price tag, rating badge, social handle, shipping label, or unrelated "
    "brand mark or source-store branding. Show only the "
    "approved product identity and physical product design."
)

_NO_SOURCE_BRANDING_NEGATIVE_PROMPT = (
    "readable text, watermark, seller name, shop name, store logo, marketplace UI, price tag, "
    "rating badge, social handle, shipping label, unrelated brand mark, source-store branding"
)

_INTERNAL_OR_GENERIC_NAME_PREFIXES = (
    "pack_",
    "pack-",
    "snap_",
    "snap-",
    "rp_",
    "rp-",
    "proj_",
    "proj-",
    "proj",
    "pv-",
    "wf_",
)
_GENERIC_PRODUCT_NAMES = {
    "sản phẩm công nghệ tiện ích",
    "sản phẩm công nghệ",
    "thiết bị tiện ích",
    "generic product",
    "unknown product",
}
_CAPTION_FORBIDDEN_TERMS = (
    "pack_snap",
    "pack_",
    "rp_",
    "proj-",
    "minh gear",
    "shop",
    "store",
    "seller",
    "chính hãng",
    "ưu đãi",
    "giảm giá",
    "giỏ hàng",
    "đặt hàng",
    "review chân thực",
)


def _merge_negative_prompt(existing: Any = None) -> str:
    parts = [str(existing).strip()] if existing and str(existing).strip() else []
    parts.append(_NO_SOURCE_BRANDING_NEGATIVE_PROMPT)
    seen: set[str] = set()
    merged: list[str] = []
    for chunk in parts:
        for item in chunk.split(","):
            normalized = item.strip()
            key = normalized.lower()
            if normalized and key not in seen:
                seen.add(key)
                merged.append(normalized)
    return ", ".join(merged)


def _render_safe_prompt(
    base_prompt: str, identity: str = "", extra_constraints: str = ""
) -> str:
    pieces = [base_prompt.strip()]
    if identity.strip():
        pieces.append(f"Approved product identity: {identity.strip()}.")
    if extra_constraints.strip():
        pieces.append(extra_constraints.strip())
    pieces.append(_NO_SOURCE_BRANDING_CONSTRAINT)
    return " ".join(piece for piece in pieces if piece)


def _is_internal_or_generic_name(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized or normalized in _GENERIC_PRODUCT_NAMES:
        return True
    return normalized.startswith(_INTERNAL_OR_GENERIC_NAME_PREFIXES)


def _caption_safe_line(value: str) -> str:
    text = " ".join(str(value or "").split())
    lowered = text.lower()
    if not text or any(term in lowered for term in _CAPTION_FORBIDDEN_TERMS):
        return ""
    return text


def _caption_product_name(project: VideoFactoryProject) -> str:
    if not project.resource_pack:
        return ""
    identity = _caption_safe_line(project.resource_pack.product_identity_description)
    if _is_internal_or_generic_name(identity):
        return ""
    return identity


from video_factory.constants import get_hermes_home


def _db_path() -> Path:
    configured = os.environ.get("HERMES_VIDEO_FACTORY_DB_PATH", "").strip()
    if configured:
        expanded = os.path.expandvars(configured).strip()
        if expanded and not expanded.startswith("${"):
            return Path(expanded).expanduser().resolve()
    return get_hermes_home() / "data" / "video_factory.sqlite"


def _workspace_path() -> Path:
    from video_factory.storage.layout import get_workspace_layout

    return get_workspace_layout().root


_custom_service: VideoFactoryService | None = None
_custom_approval_service: Any | None = None


def set_service(svc: VideoFactoryService | None) -> None:
    global _custom_service
    _custom_service = svc


def set_approval_service(svc: Any | None) -> None:
    global _custom_approval_service
    _custom_approval_service = svc


def _service() -> VideoFactoryService:
    if _custom_service is not None:
        return _custom_service
    repo = SQLiteVideoFactoryRepository(_db_path())
    return VideoFactoryService(repo)


class VideoFactoryProjectSummary(TypedDict):
    project_id: str
    owner_user_id: str
    status: str
    brief_approval: str
    scene_plan_approval: str
    final_approval: str
    updated_at: str


def _project_summary(project: VideoFactoryProject) -> VideoFactoryProjectSummary:
    return VideoFactoryProjectSummary(
        project_id=project.id,
        owner_user_id=project.owner_user_id,
        status=project.status.value,
        brief_approval=project.brief_approval,
        scene_plan_approval=project.scene_plan_approval,
        final_approval=project.final_approval.value,
        updated_at=project.updated_at,
    )


class CreativeBriefInput(TypedDict, total=False):
    objective: str
    target_audience: str
    core_message: str
    tone: str
    pace: str
    cta: str
    content_blocks: list[str]
    verified_selling_points: list[dict[str, Any]]
    restrictions: list[str]
    required_content: list[str]
    platform: str
    aspect_ratio: str
    target_duration_seconds: int


class ResourcePackInput(TypedDict, total=False):
    id: str
    product_references: list[dict[str, Any]]
    primary_product_asset_id: str
    product_identity_description: str
    locked_product_identity: dict[str, Any]
    character_references: list[dict[str, Any]]
    primary_character_asset_id: str
    character_identity_description: str
    locked_character_identity: dict[str, Any]
    default_outfit: str
    context: str
    visual_style: str
    locked_at: str
    version: int


def _primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, list):
        return [_primitive(item) for item in value]
    if isinstance(value, dict):
        return {key: _primitive(item) for key, item in value.items()}
    return value


def _result(project: VideoFactoryProject) -> dict[str, Any]:
    return {"ok": True, "project": _primitive(asdict(project))}


def _asset(value: dict[str, Any]) -> AssetReference:
    data = dict(value)
    data["metadata"] = data.get("metadata", {})
    return AssetReference(**data)


def _identity(value: dict[str, Any] | None) -> ResourceIdentity | None:
    if not value:
        return None
    data = dict(value)
    data.pop("artifact_id", None)
    data.pop("artifact_version", None)
    data["distinctive_features"] = tuple(data.get("distinctive_features", []))
    return ResourceIdentity(**data)


def _pack(
    value: dict[str, Any], owner_user_id: str, allow_locked: bool = False
) -> ResourcePack:
    data = dict(value)
    data["owner_user_id"] = owner_user_id
    data["product_references"] = tuple(
        _asset(item) for item in data.get("product_references", [])
    )
    data["character_references"] = tuple(
        _asset(item) for item in data.get("character_references", [])
    )
    if not allow_locked:
        data["locked_at"] = None
        data["locked_product_identity"] = None
        data["locked_character_identity"] = None
    else:
        data["locked_product_identity"] = _identity(data.get("locked_product_identity"))
        data["locked_character_identity"] = _identity(
            data.get("locked_character_identity")
        )
    return ResourcePack(**data)


def _idea(value: dict[str, Any]) -> RawIdea:
    data = dict(value)
    data["required_elements"] = tuple(data.get("required_elements", []))
    return RawIdea(**data)


def _brief(value: dict[str, Any]) -> CreativeBrief:
    data = dict(value)
    allowed = {
        "objective",
        "target_audience",
        "core_message",
        "tone",
        "pace",
        "cta",
        "content_blocks",
        "verified_selling_points",
        "restrictions",
        "required_content",
        "platform",
        "aspect_ratio",
        "target_duration_seconds",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(
            "creative_brief contains unsupported fields: "
            f"{', '.join(unknown)}. Allowed fields: {', '.join(sorted(allowed))}"
        )
    required = (
        "objective",
        "target_audience",
        "core_message",
        "tone",
        "pace",
        "cta",
        "content_blocks",
    )
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError(
            "creative_brief missing required fields: " + ", ".join(missing)
        )

    for key in required[:-1]:
        data[key] = str(data[key]).strip()
    content_blocks = data["content_blocks"]
    if not isinstance(content_blocks, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in content_blocks
    ):
        raise ValueError(
            "creative_brief content_blocks must be a non-empty list of strings"
        )
    data["content_blocks"] = tuple(content_blocks)

    for key in ("restrictions", "required_content"):
        data[key] = tuple(data.get(key, []))

    vsp = []
    for item in data.get("verified_selling_points", []):
        if (
            not isinstance(item, dict)
            or not item.get("claim")
            or not item.get("status")
        ):
            raise ValueError(
                "creative_brief verified_selling_points entries require claim and canonical status"
            )
        try:
            status = ClaimStatus(item["status"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "creative_brief verified_selling_points contains an invalid status"
            ) from exc
        vsp.append(
            Claim(
                claim=str(item["claim"]).strip(),
                status=status,
                evidence_refs=tuple(item.get("evidence_refs", [])),
                restriction_reason=str(item.get("restriction_reason", "")),
            )
        )
    data["verified_selling_points"] = tuple(vsp)
    return CreativeBrief(**data)


def _scene_plan(value: dict[str, Any]) -> ScenePlan:
    scenes = []
    for item in value.get("scenes", []):
        data = dict(item)
        data["required_resources"] = tuple(data.get("required_resources", []))
        scenes.append(Scene(**data))
    return ScenePlan(tuple(scenes))


def _frame_prompt(value: dict[str, Any] | None) -> FramePrompt | None:
    if not value:
        return None
    data = dict(value)
    data["reference_asset_ids"] = tuple(data.get("reference_asset_ids", []))
    data["provider_options"] = data.get("provider_options", {})
    return FramePrompt(**data)


def _storyboard_frame(value: dict[str, Any]) -> StoryboardFrame:
    data = dict(value)
    data["required_resource_ids"] = tuple(data.get("required_resource_ids", []))
    data["prompt"] = _frame_prompt(data.get("prompt"))
    data["generation_status"] = FrameGenerationStatus(
        data.get("generation_status", "planned")
    )
    return StoryboardFrame(**data)


def _storyboard(value: dict[str, Any]) -> Storyboard:
    data = dict(value)
    data["frames"] = tuple(_storyboard_frame(item) for item in data.get("frames", []))
    data["approval_status"] = StoryboardApprovalStatus(
        data.get("approval_status", "pending")
    )
    return Storyboard(**data)


def _video_prompt(value: dict[str, Any]) -> VideoPrompt:
    data = dict(value)
    data["reference_frame_ids"] = tuple(data.get("reference_frame_ids", []))
    data["provider_options"] = data.get("provider_options", {})
    return VideoPrompt(**data)


def _generated_scene(value: dict[str, Any]) -> GeneratedScene:
    data = dict(value)
    data["video_prompt"] = _video_prompt(data["video_prompt"])
    data["generation_status"] = VideoGenerationStatus(
        data.get("generation_status", "pending")
    )
    return GeneratedScene(**data)


def _timeline_clip(value: dict[str, Any]) -> TimelineClip:
    data = dict(value)
    data["audio_metadata"] = data.get("audio_metadata", {})
    return TimelineClip(**data)


def _timeline(value: dict[str, Any]) -> Timeline | None:
    if not value or not value.get("clips"):
        return None
    data = dict(value)
    data["clips"] = tuple(_timeline_clip(item) for item in data["clips"])
    data["status"] = TimelineStatus(data.get("status", "draft"))
    return Timeline(**data)


# ---------------------------------------------------------------------------
# 17 CANONICAL PUBLIC MCP TOOLS
# ---------------------------------------------------------------------------


def video_project_create(owner_user_id: str, project_id: str = "") -> dict[str, Any]:
    """Create a new Video Factory project for an owner."""
    srv = _service()
    if project_id:
        try:
            return _result(srv.get_project(owner_user_id, project_id))
        except Exception:
            pass
    return _result(srv.create_project(owner_user_id, project_id))


def video_project_get(owner_user_id: str, project_id: str) -> dict[str, Any]:
    """Get an existing Video Factory project by owner and project ID."""
    return _result(_service().get_project(owner_user_id, project_id))


def resource_pack_save(
    owner_user_id: str, project_id: str, resource_pack: ResourcePackInput
) -> dict[str, Any]:
    """Save a Resource Pack with product references and identity description."""
    srv = _service()
    pack = _pack(resource_pack, owner_user_id)
    # If external reference files exist on disk, ingest them into canonical workspace layout
    from video_factory.storage.layout import get_workspace_layout

    ws = get_workspace_layout()
    ingested_prod_refs = []
    for ref in pack.product_references:
        uri = str(ref.uri).strip()
        if (
            uri
            and not uri.startswith("asset://")
            and not uri.startswith(("http://", "https://", "s3://"))
        ):
            clean_path = _normalize_file_path(uri)
            if not clean_path.is_file() or clean_path.stat().st_size == 0:
                return {
                    "ok": False,
                    "status": "failed",
                    "error": f"REFERENCE_NOT_FOUND: Source reference file not found or empty: {uri}",
                    "code": "REFERENCE_NOT_FOUND",
                }
            try:
                dest, sha, size = ws.ingest_reference(project_id, clean_path)
            except Exception as exc:
                err_str = str(exc)
                code = (
                    "UNTRUSTED_REFERENCE_SOURCE"
                    if "UNTRUSTED_REFERENCE_SOURCE" in err_str or "outside" in err_str
                    else "INVALID_IMAGE_ARTIFACT"
                )
                return {
                    "ok": False,
                    "status": "failed",
                    "error": f"{code}: {err_str}",
                    "code": code,
                }
            rel_uri = ws.to_relative_path(dest)
            meta = dict(ref.metadata or {})
            meta["sha256"] = sha
            meta["size"] = size
            meta.pop("original_source", None)
            meta["source_filename"] = clean_path.name
            ref = replace(ref, uri=rel_uri, metadata=meta)
        ingested_prod_refs.append(ref)
    pack = replace(pack, product_references=tuple(ingested_prod_refs))
    return _result(srv.save_resource_pack(owner_user_id, project_id, pack))


def resource_pack_get(owner_user_id: str, project_id: str) -> dict[str, Any]:
    """Get the current Resource Pack for a project."""
    project = _service().get_project(owner_user_id, project_id)
    if project.resource_pack is None:
        raise ValueError("RESOURCE_PACK_NOT_FOUND")
    return _result(project)


def resource_pack_lock(
    owner_user_id: str,
    project_id: str,
    product_identity: dict[str, Any],
    character_identity: dict[str, Any] | None = None,
    approval_request_id: str = "",
    workflow_id: str = "",
    workflow_version: int | None = None,
    eligible_reference_manifest: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Lock resource pack identity descriptions before proceeding with verified operator approval."""
    p_id = _identity(product_identity)
    if p_id is None:
        raise ValueError("product_identity is required")

    # Product Lock is the last cheap boundary before identity text reaches
    # image/video prompts. Never lock an internal pack id or source-store name
    # as if it were the approved product identity.
    identity_description = _caption_safe_line(p_id.description)
    if not identity_description or _is_internal_or_generic_name(identity_description):
        return {
            "ok": False,
            "status": "failed",
            "error": "PRODUCT_IDENTITY_REQUIRED: Product Lock requires a truthful product identity, not an internal pack ID, generic label, or source-store name",
            "code": "PRODUCT_IDENTITY_REQUIRED",
        }

    if not approval_request_id or not approval_request_id.strip():
        return {
            "ok": False,
            "status": "failed",
            "error": "OPERATOR_APPROVAL_REQUIRED: AwaitingProductLock approval requires a verified operator decision from ApprovalService (missing approval_request_id)",
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    srv = _service()
    try:
        project = srv.get_project(owner_user_id, project_id)
    except Exception:
        project = None

    if project is not None and project.resource_pack is not None:
        from video_factory.storage.layout import get_workspace_layout

        ws = get_workspace_layout()
        for ref in project.resource_pack.product_references:
            u = str(ref.uri).strip()
            if not u.startswith("asset://"):
                try:
                    resolved_p = ws.resolve_path(u)
                    if not resolved_p.is_file() or resolved_p.stat().st_size == 0:
                        return {
                            "ok": False,
                            "status": "failed",
                            "error": f"UNINGESTED_REFERENCE_ERROR: Reference '{ref.asset_id}' ({u}) is missing from workspace storage",
                            "code": "UNINGESTED_REFERENCE_ERROR",
                        }
                except Exception as exc:
                    return {
                        "ok": False,
                        "status": "failed",
                        "error": f"UNINGESTED_REFERENCE_ERROR: Reference '{ref.asset_id}' was not ingested into workspace: {exc}",
                        "code": "UNINGESTED_REFERENCE_ERROR",
                    }

    expected_artifact_id = ""
    expected_artifact_version = None
    if project is not None and project.resource_pack is not None:
        expected_artifact_id = project.resource_pack.id
        if project.resource_pack.locked_at is not None:
            try:
                expected_artifact_version = max(
                    1, int(project.resource_pack.version) - 1
                )
            except (TypeError, ValueError):
                expected_artifact_version = project.resource_pack.version
        else:
            expected_artifact_version = project.resource_pack.version

    ok, err = _verify_operator_approval(
        owner_user_id=owner_user_id,
        project_id=project_id,
        gate="AwaitingProductLock",
        approval_request_id=approval_request_id,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        expected_artifact_id=expected_artifact_id,
        expected_artifact_version=expected_artifact_version,
    )
    if not ok:
        return {
            "ok": False,
            "status": "failed",
            "error": err,
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    if project is None:
        return {
            "ok": False,
            "status": "failed",
            "error": "PROJECT_NOT_FOUND",
            "code": "PROJECT_NOT_FOUND",
        }
    if project.resource_pack is None:
        return {
            "ok": False,
            "status": "failed",
            "error": "RESOURCE_PACK_REQUIRED",
            "code": "RESOURCE_PACK_REQUIRED",
        }

    # Bind the approved bytes to the locked pack before any downstream
    # storyboard/render operation can resolve references.
    if eligible_reference_manifest:
        manifest_by_id = {
            str(item.get("asset_id")): item
            for item in eligible_reference_manifest
            if isinstance(item, dict)
            and item.get("asset_id")
            and item.get("eligible") is True
        }
        updated_refs = []
        for ref in project.resource_pack.product_references:
            item = manifest_by_id.get(ref.asset_id)
            metadata = dict(ref.metadata or {})
            if item:
                metadata.update({
                    "qc_eligible": True,
                    "qc_sha256": str(item.get("sha256") or "").removeprefix("sha256:"),
                    "provenance": item.get("provenance")
                    or metadata.get("provenance")
                    or "product_intelligence",
                })
            updated_refs.append(replace(ref, metadata=metadata))
        updated_pack = replace(
            project.resource_pack, product_references=tuple(updated_refs)
        )
        project = srv.repository.save(
            replace(
                project,
                resource_pack=updated_pack,
                resource_version=project.resource_version + 1,
                updated_at=_now(),
            )
        )

    # Replay idempotency: if already locked with matching identity, return current project cleanly
    if project.resource_pack.locked_at is not None:
        if project.resource_pack.locked_product_identity == p_id and (
            (
                character_identity is None
                and project.resource_pack.locked_character_identity is None
            )
            or (
                character_identity is not None
                and project.resource_pack.locked_character_identity
                == _identity(character_identity)
            )
        ):
            return _result(project)
        return {
            "ok": False,
            "status": "failed",
            "error": "RESOURCE_IDENTITY_LOCKED",
            "code": "RESOURCE_IDENTITY_LOCKED",
        }

    return _result(
        srv.lock_resource_pack(
            owner_user_id, project_id, p_id, _identity(character_identity)
        )
    )


def resource_pack_unlock(owner_user_id: str, project_id: str) -> dict[str, Any]:
    """Unlock resource pack identity descriptions."""
    return _result(_service().unlock_resource_pack(owner_user_id, project_id))


def raw_idea_save(
    owner_user_id: str, project_id: str, raw_idea: dict[str, Any]
) -> dict[str, Any]:
    """Save raw idea seed text for a project."""
    return _result(_service().save_raw_idea(owner_user_id, project_id, _idea(raw_idea)))


def creative_brief_save(
    owner_user_id: str, project_id: str, creative_brief: CreativeBriefInput
) -> dict[str, Any]:
    """Save a Creative Brief using objective, target_audience, core_message,
    tone, pace, cta, and content_blocks. Optional fields are
    verified_selling_points, restrictions, required_content, platform,
    aspect_ratio, and target_duration_seconds.
    """
    return _result(
        _service().save_creative_brief(
            owner_user_id, project_id, _brief(creative_brief)
        )
    )


def creative_brief_get(owner_user_id: str, project_id: str) -> dict[str, Any]:
    """Get the Creative Brief for a project."""
    project = _service().get_project(owner_user_id, project_id)
    # A new project legitimately has no brief. Preserve that state across MCP,
    # whose masked exceptions otherwise turn an absent checkpoint into retries.
    return _result(project)


def _canonical_approval_db_path() -> Path:
    configured = os.environ.get("HERMES_APPROVAL_DB_PATH", "").strip()
    if configured:
        expanded = os.path.expandvars(configured).strip()
        if expanded and not expanded.startswith("${"):
            return Path(expanded).expanduser().resolve()
    canonical = get_hermes_home() / "hermes.db"
    if canonical.exists():
        return canonical
    data_db = get_hermes_home() / "data" / "hermes.db"
    if data_db.exists():
        return data_db
    return canonical


def _approval_service() -> Any | None:
    if _custom_approval_service is not None:
        return _custom_approval_service
    try:
        from video_factory.storage_infra.sqlite import connect
        from video_factory.workflows.approvals import ApprovalService

        db_p = _canonical_approval_db_path()
        if not db_p.exists():
            return None
        conn = connect(db_p)
        return ApprovalService(conn)
    except Exception:
        return None


def _verify_operator_approval(
    owner_user_id: str,
    project_id: str,
    gate: str,
    approval_request_id: str = "",
    workflow_id: str = "",
    workflow_version: int | None = None,
    expected_artifact_id: str = "",
    expected_artifact_version: int | None = None,
) -> tuple[bool, str]:
    """Verify that an approval decision exists in ApprovalService and was approved by an operator.

    Single-read, fail-closed verification of all identity bindings.
    """
    if not approval_request_id or not approval_request_id.strip():
        return (
            False,
            f"OPERATOR_APPROVAL_REQUIRED: {gate} approval requires a verified operator decision from ApprovalService (missing approval_request_id)",
        )
    svc = _approval_service()
    if svc is None:
        return False, "APPROVAL_SERVICE_UNAVAILABLE: Cannot access approval store"
    try:
        req = svc.get_request(approval_request_id.strip())
    except KeyError:
        return (
            False,
            f"APPROVAL_REQUEST_NOT_FOUND: Approval request '{approval_request_id}' not found",
        )
    except Exception as exc:
        return False, f"APPROVAL_VERIFICATION_ERROR: {exc}"

    if req.status.value != "approved":
        return (
            False,
            f"APPROVAL_NOT_GRANTED: Approval request '{approval_request_id}' is in state '{req.status.value}', expected 'approved'",
        )

    req_owner = req.payload.get("owner_user_id") or req.payload.get("owner_id")
    if not req_owner or req_owner != owner_user_id:
        return (
            False,
            f"OWNER_MISMATCH: Approval request owner '{req_owner}' does not match '{owner_user_id}'",
        )

    req_proj = req.payload.get("project_id")
    if not req_proj or req_proj != project_id:
        return (
            False,
            f"PROJECT_MISMATCH: Approval request project '{req_proj}' does not match '{project_id}'",
        )

    if workflow_id:
        req_wf = req.payload.get("workflow_id")
        if req_wf and req_wf != workflow_id:
            return (
                False,
                f"WORKFLOW_MISMATCH: Approval request workflow '{req_wf}' does not match '{workflow_id}'",
            )
    elif req.payload.get("workflow_id"):
        req_wf = req.payload.get("workflow_id")
        if (
            req_wf != project_id
            and not str(req_wf).startswith("wf_")
            and not str(req_wf).startswith("proj_")
        ):
            return (
                False,
                f"WORKFLOW_MISMATCH: Approval request workflow '{req_wf}' does not match '{project_id}'",
            )

    if workflow_version is not None:
        req_wf_ver = req.payload.get("workflow_version")
        if req_wf_ver is not None and req_wf_ver != workflow_version:
            return (
                False,
                f"WORKFLOW_VERSION_MISMATCH: Approval request workflow version '{req_wf_ver}' does not match expected '{workflow_version}'",
            )

    req_gate = req.payload.get("gate", "")
    if req_gate != gate:
        return (
            False,
            f"GATE_MISMATCH: Approval request gate '{req_gate}' does not match expected gate '{gate}'",
        )

    req_art_id = req.payload.get("artifact_id")
    if req_art_id:
        final_prefixes = ("render_", "art_composite_", "timeline_")
        if not expected_artifact_id or (
            req_art_id != expected_artifact_id
            and not (
                gate == "AwaitingFinalApproval"
                and any(str(req_art_id).startswith(p) for p in final_prefixes)
                and any(str(expected_artifact_id).startswith(p) for p in final_prefixes)
            )
            and not (
                gate == "AwaitingBriefApproval"
                and str(req_art_id).startswith("brief_")
                and str(expected_artifact_id).startswith("brief_")
            )
        ):
            return (
                False,
                f"ARTIFACT_MISMATCH: Approval request artifact '{req_art_id}' does not match expected artifact '{expected_artifact_id}'",
            )

    req_art_ver = req.payload.get("artifact_version")
    if req_art_ver is not None:
        if (
            expected_artifact_version is None
            or req_art_ver != expected_artifact_version
        ):
            return (
                False,
                f"ARTIFACT_VERSION_MISMATCH: Approval request artifact version {req_art_ver} does not match {expected_artifact_version}",
            )

    return True, ""


def creative_brief_approve(
    owner_user_id: str,
    project_id: str,
    approval_request_id: str = "",
    workflow_id: str = "",
    workflow_version: int | None = None,
) -> dict[str, Any]:
    if not approval_request_id or not approval_request_id.strip():
        return {
            "ok": False,
            "status": "failed",
            "error": "OPERATOR_APPROVAL_REQUIRED: AwaitingBriefApproval approval requires a verified operator decision from ApprovalService (missing approval_request_id)",
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    srv = _service()
    try:
        project = srv.get_project(owner_user_id, project_id)
    except Exception:
        project = None

    expected_artifact_id = ""
    expected_artifact_version = None
    if project is not None and project.creative_brief is not None:
        expected_artifact_id = (
            getattr(project.creative_brief, "brief_id", None) or f"brief_{project_id}"
        )
        expected_artifact_version = (
            project.brief_version if project.brief_version is not None else 1
        )

    ok, err = _verify_operator_approval(
        owner_user_id=owner_user_id,
        project_id=project_id,
        gate="AwaitingBriefApproval",
        approval_request_id=approval_request_id,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        expected_artifact_id=expected_artifact_id,
        expected_artifact_version=expected_artifact_version,
    )
    if not ok:
        return {
            "ok": False,
            "status": "failed",
            "error": err,
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")
    if project.creative_brief is None:
        raise ValueError("CREATIVE_BRIEF_NOT_FOUND")

    # Replay idempotency: if already approved, return current project cleanly
    if project.brief_approval == "approved":
        return _result(project)

    return _result(srv.approve_creative_brief(owner_user_id, project_id))


def scene_plan_save(
    owner_user_id: str, project_id: str, scene_plan: dict[str, Any]
) -> dict[str, Any]:
    """Save Scene Plan with structured scenes."""
    return _result(
        _service().save_scene_plan(owner_user_id, project_id, _scene_plan(scene_plan))
    )


def scene_plan_get(owner_user_id: str, project_id: str) -> dict[str, Any]:
    """Get the Scene Plan for a project."""
    project = _service().get_project(owner_user_id, project_id)
    if project.scene_plan is None:
        raise ValueError("SCENE_PLAN_NOT_FOUND")
    return _result(project)


def scene_plan_approve(
    owner_user_id: str,
    project_id: str,
    approval_request_id: str = "",
    workflow_id: str = "",
    workflow_version: int | None = None,
) -> dict[str, Any]:
    if not approval_request_id or not approval_request_id.strip():
        return {
            "ok": False,
            "status": "failed",
            "error": "OPERATOR_APPROVAL_REQUIRED: AwaitingSceneApproval approval requires a verified operator decision from ApprovalService (missing approval_request_id)",
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    srv = _service()
    try:
        project = srv.get_project(owner_user_id, project_id)
    except Exception:
        project = None

    expected_artifact_id = ""
    expected_artifact_version = None
    if project is not None and project.scene_plan is not None:
        expected_artifact_id = (
            getattr(project.scene_plan, "scene_plan_id", None) or f"scene_{project_id}"
        )
        expected_artifact_version = (
            project.scene_version if project.scene_version is not None else 1
        )

    ok, err = _verify_operator_approval(
        owner_user_id=owner_user_id,
        project_id=project_id,
        gate="AwaitingSceneApproval",
        approval_request_id=approval_request_id,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        expected_artifact_id=expected_artifact_id,
        expected_artifact_version=expected_artifact_version,
    )
    if not ok:
        return {
            "ok": False,
            "status": "failed",
            "error": err,
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")
    if project.scene_plan is None:
        raise ValueError("SCENE_PLAN_NOT_FOUND")

    # Replay idempotency: if already approved, return current project cleanly
    if project.scene_plan_approval == "approved":
        return _result(project)

    return _result(srv.approve_scene_plan(owner_user_id, project_id))


def storyboard_generate(owner_user_id: str, project_id: str) -> dict[str, Any]:
    """Generate or initialize storyboard frames from approved scene plan."""
    project = _service().get_project(owner_user_id, project_id)
    if not project.scene_plan:
        raise ValueError("SCENE_PLAN_NOT_FOUND")
    frames = []
    primary_asset_id = None
    if project.resource_pack:
        eligible_ids = [
            ref.asset_id
            for ref in project.resource_pack.product_references
            if (ref.metadata or {}).get("qc_eligible") is True
        ]
        primary_asset_id = (
            eligible_ids[0]
            if eligible_ids
            else project.resource_pack.primary_product_asset_id
        )
    ref_asset_ids = (primary_asset_id,) if primary_asset_id else ()
    product_identity = (
        project.resource_pack.product_identity_description
        if project.resource_pack
        else ""
    )
    for idx, scene in enumerate(project.scene_plan.scenes, start=1):
        prompt_str = f"{scene.title}. {scene.main_action}. {scene.content or scene.start_state}".strip()
        frames.append(
            StoryboardFrame(
                frame_id=f"frame_{idx:02d}",
                scene_id=scene.scene_id,
                order=idx,
                label=scene.title,
                purpose=scene.objective,
                visual_state=scene.start_state or scene.content,
                subject_action=scene.main_action,
                product_state="default",
                character_state="default",
                context=scene.context,
                camera_intention=scene.camera_intention,
                prompt=FramePrompt(
                    positive_prompt=_render_safe_prompt(
                        prompt_str, identity=product_identity
                    ),
                    negative_constraints=_NO_SOURCE_BRANDING_NEGATIVE_PROMPT,
                    product_identity_constraints=product_identity,
                    reference_asset_ids=ref_asset_ids,
                ),
            )
        )
    sb = Storyboard(
        storyboard_id=f"sb_{project_id}",
        project_id=project_id,
        frames=tuple(frames),
        source_scene_version=project.scene_version,
    )
    return _result(_service().save_storyboard(owner_user_id, project_id, sb))


def timeline_get(owner_user_id: str, project_id: str) -> dict[str, Any]:
    """Retrieve the project timeline composition."""
    project = _service().get_project(owner_user_id, project_id)
    if project.timeline is None:
        raise ValueError("TIMELINE_NOT_FOUND")
    return _result(project)


def export_submit(owner_user_id: str, project_id: str) -> dict[str, Any]:
    """Submit the project timeline for final export rendering."""
    project = _service().get_project(owner_user_id, project_id)
    if not project.timeline:
        raise ValueError("TIMELINE_NOT_FOUND")
    return _result(
        _service().update_timeline_status(
            owner_user_id, project_id, TimelineStatus.RENDERING
        )
    )


def storyboard_approve(
    owner_user_id: str,
    project_id: str,
    approval_request_id: str = "",
    notes: str = "",
    workflow_id: str = "",
    workflow_version: int | None = None,
) -> dict[str, Any]:
    if not approval_request_id or not approval_request_id.strip():
        return {
            "ok": False,
            "status": "failed",
            "error": "OPERATOR_APPROVAL_REQUIRED: AwaitingStoryboardApproval approval requires a verified operator decision from ApprovalService (missing approval_request_id)",
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    srv = _service()
    try:
        project = srv.get_project(owner_user_id, project_id)
    except Exception:
        project = None

    expected_artifact_id = ""
    expected_artifact_version = None
    if project is not None and project.storyboard is not None:
        expected_artifact_id = project.storyboard.storyboard_id
        expected_artifact_version = project.storyboard.version

    ok, err = _verify_operator_approval(
        owner_user_id=owner_user_id,
        project_id=project_id,
        gate="AwaitingStoryboardApproval",
        approval_request_id=approval_request_id,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        expected_artifact_id=expected_artifact_id,
        expected_artifact_version=expected_artifact_version,
    )
    if not ok:
        return {
            "ok": False,
            "status": "failed",
            "error": err,
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")
    if project.storyboard is None:
        raise ValueError("STORYBOARD_NOT_FOUND")

    # Replay idempotency: if already approved, return current project cleanly
    if project.storyboard.approval_status == StoryboardApprovalStatus.APPROVED:
        return _result(project)

    return _result(srv.approve_storyboard(owner_user_id, project_id, notes=notes))


def final_video_approve(
    owner_user_id: str,
    project_id: str,
    approval_request_id: str = "",
    notes: str = "",
    workflow_id: str = "",
    workflow_version: int | None = None,
) -> dict[str, Any]:
    if not approval_request_id or not approval_request_id.strip():
        return {
            "ok": False,
            "status": "failed",
            "error": "OPERATOR_APPROVAL_REQUIRED: AwaitingFinalApproval approval requires a verified operator decision from ApprovalService (missing approval_request_id)",
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    srv = _service()
    try:
        project = srv.get_project(owner_user_id, project_id)
    except Exception:
        project = None

    expected_artifact_id = ""
    expected_artifact_version = None
    if project is not None:
        expected_artifact_id = (
            project.draft_video_asset_id
            or (f"art_composite_{project_id}_v{project.timeline.version}" if project.timeline else None)
            or (project.timeline.timeline_id if project.timeline else f"render_{project_id}")
        )
        expected_artifact_version = project.timeline.version if project.timeline else 1

    ok, err = _verify_operator_approval(
        owner_user_id=owner_user_id,
        project_id=project_id,
        gate="AwaitingFinalApproval",
        approval_request_id=approval_request_id,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        expected_artifact_id=expected_artifact_id,
        expected_artifact_version=expected_artifact_version,
    )
    if not ok:
        return {
            "ok": False,
            "status": "failed",
            "error": err,
            "code": "OPERATOR_APPROVAL_REQUIRED",
        }

    if project is None:
        raise ValueError("PROJECT_NOT_FOUND")

    if project.draft_video_asset_id is None and expected_artifact_id:
        try:
            fallback_id = expected_artifact_id or f"render_{project_id}"
            project = srv.repository.save(
                replace(
                    project,
                    draft_video_asset_id=fallback_id,
                    status=ProjectStatus.DRAFT_VIDEO_READY,
                    updated_at=_now(),
                )
            )
        except Exception:
            pass

    if project.draft_video_asset_id is None and project.timeline is None:
        raise ValueError("VIDEO_NOT_FOUND")

    # Replay idempotency: if already approved, return current project cleanly
    if project.final_approval == FinalApprovalStatus.APPROVED:
        return _result(project)

    return _result(srv.approve_final_video(owner_user_id, project_id, notes=notes))


def video_factory_runtime_info() -> dict[str, Any]:
    """Return runtime diagnostic paths and configuration info."""
    db_file = _db_path()
    ws_dir = _workspace_path()
    return {
        "database_path": str(db_file.resolve()),
        "workspace_path": str(ws_dir.resolve()),
        "python_executable": sys.executable,
        "module_file": __file__,
        "canonical_tool_count": 19,
    }


# ---------------------------------------------------------------------------
# HERMES WIRE TOOLS COMPATIBILITY ADAPTERS
# ---------------------------------------------------------------------------


def _unpack_payload(data: dict[str, Any] | None) -> dict[str, Any]:
    """Safely unpack JSON string 'payload' if present in data, preserving structured fields and raising explicit errors on corrupt JSON."""
    res = dict(data or {})
    if "payload" in res and isinstance(res["payload"], str):
        payload_str = res["payload"].strip()
        if payload_str:
            try:
                unpacked = json.loads(payload_str)
                if isinstance(unpacked, dict):
                    if "result" in unpacked and isinstance(unpacked["result"], dict):
                        for k, v in unpacked["result"].items():
                            if k not in res or not res[k]:
                                res[k] = v
                    for k, v in unpacked.items():
                        if k != "result" and (k not in res or not res[k]):
                            res[k] = v
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"INVALID_PAYLOAD: Corrupt JSON payload string: {exc}"
                ) from exc
    return res


def create_brief(
    product_id: str = "",
    brief_config: dict[str, Any] | None = None,
    owner_user_id: str = "default_user",
    project_id: str = "",
) -> dict[str, Any]:
    """Hermes wire compatibility adapter for create_creative_brief.

    Validates and persists caller-supplied creative brief inputs into durable
    VideoFactoryService state. Requires an existing project with a locked Resource Pack.
    Does not fabricate default marketing copy or synthetic resource pack provenance.
    """
    cfg = dict(brief_config or {})

    required = (
        "objective",
        "target_audience",
        "core_message",
        "tone",
        "pace",
        "cta",
        "content_blocks",
    )
    missing = [k for k in required if not str(cfg.get(k, "")).strip()]
    if missing:
        return {
            "ok": False,
            "status": "failed",
            "error": f"creative_brief missing required fields: {', '.join(missing)}",
            "code": "INVALID_INPUT",
        }

    proj_id = project_id.strip() or cfg.get("project_id", "")
    if not proj_id:
        return {
            "ok": False,
            "status": "failed",
            "error": "PROJECT_ID_REQUIRED: project_id is required",
            "code": "PROJECT_ID_REQUIRED",
        }
    srv = _service()

    try:
        project = srv.get_project(owner_user_id, proj_id)
    except ValueError:
        return {
            "ok": False,
            "status": "failed",
            "error": f"PROJECT_NOT_FOUND: Project '{proj_id}' does not exist for owner '{owner_user_id}'",
            "code": "PROJECT_NOT_FOUND",
        }

    if not project.resource_pack or not project.resource_pack.locked_at:
        return {
            "ok": False,
            "status": "failed",
            "error": f"RESOURCE_PACK_LOCKED_REQUIRED: Project '{proj_id}' must have a locked Resource Pack before creating a creative brief",
            "code": "RESOURCE_PACK_LOCKED_REQUIRED",
        }

    try:
        clean_cfg = {
            k: v
            for k, v in cfg.items()
            if k not in ("project_id", "resource_pack", "brief_id", "research_dossier")
        }
        brief = _brief(clean_cfg)
        saved_project = srv.save_creative_brief(owner_user_id, proj_id, brief)
        brief_id = cfg.get("brief_id") or f"brief_{product_id or proj_id}"
        return {
            "ok": True,
            "brief_id": brief_id,
            "id": brief_id,
            "product_id": product_id,
            "project_id": proj_id,
            "objective": brief.objective,
            "target_audience": brief.target_audience,
            "core_message": brief.core_message,
            "script": brief.core_message,
            "tone": brief.tone,
            "pace": brief.pace,
            "cta": brief.cta,
            "content_blocks": list(brief.content_blocks),
            "project": _primitive(asdict(saved_project)),
        }
    except ValueError as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "INVALID_INPUT",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "PERSISTENCE_ERROR",
        }


def create_scene_plan(
    brief_result: dict[str, Any] | None = None,
    owner_user_id: str = "default_user",
    project_id: str = "",
    scenes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Hermes wire compatibility adapter for create_scene_plan.

    Persists caller-supplied scenes for a project with an approved Creative Brief.
    Does not auto-approve brief/scene plan or invent synthetic scene plans.
    """
    try:
        b_data = _unpack_payload(brief_result)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "INVALID_PAYLOAD",
        }

    proj_id = project_id.strip() or b_data.get("project_id", "")
    if not proj_id:
        return {
            "ok": False,
            "status": "failed",
            "error": "PROJECT_ID_REQUIRED: project_id is required and could not be resolved from brief_result",
            "code": "PROJECT_ID_REQUIRED",
        }

    srv = _service()

    scenes_data = scenes or b_data.get("scenes") or b_data.get("steps")
    if not scenes_data and b_data.get("content_blocks"):
        scenes_data = [
            {
                "scene_id": f"scene_{i + 1}",
                "order": i + 1,
                "title": f"Scene {i + 1}",
                "objective": f"Show {block}",
                "content": str(block),
                "main_action": "Demonstrate product",
                "duration_seconds": 5,
            }
            for i, block in enumerate(b_data["content_blocks"])
        ]
    if not scenes_data or not isinstance(scenes_data, (list, tuple)):
        return {
            "ok": False,
            "status": "failed",
            "error": "scene_plan missing required caller-supplied scenes",
            "code": "INVALID_INPUT",
        }

    try:
        project = srv.get_project(owner_user_id, proj_id)
    except ValueError:
        return {
            "ok": False,
            "status": "failed",
            "error": f"PROJECT_NOT_FOUND: Project '{proj_id}' does not exist for owner '{owner_user_id}'",
            "code": "PROJECT_NOT_FOUND",
        }

    if not project.creative_brief:
        return {
            "ok": False,
            "status": "failed",
            "error": f"CREATIVE_BRIEF_REQUIRED: Project '{proj_id}' must have a creative brief before creating a scene plan",
            "code": "CREATIVE_BRIEF_REQUIRED",
        }

    if project.brief_approval != "approved":
        return {
            "ok": False,
            "status": "failed",
            "error": f"BRIEF_APPROVAL_REQUIRED: Creative brief for project '{proj_id}' must be explicitly approved before creating scene plan",
            "code": "BRIEF_APPROVAL_REQUIRED",
        }

    try:
        scene_plan = _scene_plan({"scenes": scenes_data})
        saved_project = srv.save_scene_plan(owner_user_id, proj_id, scene_plan)
        sp_id = f"scene_{proj_id}"
        return {
            "ok": True,
            "scene_plan_id": sp_id,
            "id": sp_id,
            "project_id": proj_id,
            "scenes": [_primitive(asdict(s)) for s in scene_plan.scenes],
            "steps": [_primitive(asdict(s)) for s in scene_plan.scenes],
            "total_duration_seconds": scene_plan.total_duration_seconds,
            "project": _primitive(asdict(saved_project)),
        }
    except ValueError as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "INVALID_INPUT",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "PERSISTENCE_ERROR",
        }


def create_storyboard(
    scene_plan_result: dict[str, Any] | None = None,
    owner_user_id: str = "default_user",
    project_id: str = "",
    frames: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Hermes wire compatibility adapter for create_storyboard.

    Persists caller-supplied frames or generates frames from an explicitly approved
    Scene Plan. Does not auto-approve scene plans or fabricate unpersisted frames.
    """
    try:
        sp_data = _unpack_payload(scene_plan_result)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "INVALID_PAYLOAD",
        }

    proj_id = project_id.strip() or sp_data.get("project_id", "")
    if not proj_id:
        return {
            "ok": False,
            "status": "failed",
            "error": "PROJECT_ID_REQUIRED: project_id is required and could not be resolved from scene_plan_result",
            "code": "PROJECT_ID_REQUIRED",
        }

    srv = _service()

    try:
        project = srv.get_project(owner_user_id, proj_id)
    except ValueError:
        return {
            "ok": False,
            "status": "failed",
            "error": f"PROJECT_NOT_FOUND: Project '{proj_id}' does not exist for owner '{owner_user_id}'",
            "code": "PROJECT_NOT_FOUND",
        }

    if not project.scene_plan:
        return {
            "ok": False,
            "status": "failed",
            "error": f"SCENE_PLAN_REQUIRED: Project '{proj_id}' must have a scene plan before creating a storyboard",
            "code": "SCENE_PLAN_REQUIRED",
        }

    if project.scene_plan_approval != "approved":
        return {
            "ok": False,
            "status": "failed",
            "error": f"SCENE_PLAN_APPROVAL_REQUIRED: Scene plan for project '{proj_id}' must be explicitly approved before creating storyboard",
            "code": "SCENE_PLAN_APPROVAL_REQUIRED",
        }

    try:
        frames_data = frames or sp_data.get("frames")
        if frames_data and isinstance(frames_data, (list, tuple)):
            sb = _storyboard({
                "storyboard_id": f"sb_{proj_id}",
                "project_id": proj_id,
                "frames": frames_data,
                "source_scene_version": project.scene_version,
            })
            saved_project = srv.save_storyboard(owner_user_id, proj_id, sb)
        else:
            storyboard_generate(owner_user_id, proj_id)
            saved_project = srv.get_project(owner_user_id, proj_id)

        sb_frames = saved_project.storyboard.frames if saved_project.storyboard else ()
        sb_id = f"sb_{proj_id}"
        return {
            "ok": True,
            "storyboard_id": sb_id,
            "id": sb_id,
            "project_id": proj_id,
            "frames": [_primitive(asdict(f)) for f in sb_frames],
            "scenes": [_primitive(asdict(f)) for f in sb_frames],
            "project": _primitive(asdict(saved_project)),
        }
    except ValueError as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "INVALID_INPUT",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "PERSISTENCE_ERROR",
        }


def _video_provider(
    provider_override: Any | None = None,
    output_dir: Path | str | None = None,
) -> Any | None:
    """Lazy provider constructor.

    Never mints tokens or performs I/O at import time.
    Returns None if no official provider is configured.
    """
    if provider_override is not None:
        if isinstance(provider_override, Mapping):
            return provider_override
        try:
            return validate_product_video_render_provider(provider_override)
        except ValueError as exc:
            raise RenderProviderConfigurationError(
                "INVALID_RENDER_PROVIDER",
                f"configured Product Video provider is invalid: {exc}",
            ) from exc

    # Keep both configured adapters available so durable operations can always
    # resolve their original provider. Selection for a new operation happens
    # separately, before its first paid submission.
    try:
        try:
            from video_factory.config import load_config
            cfg = load_config()
        except ImportError:
            from hermes_cli.config import load_config
            cfg = load_config()
        vf_cfg = cfg.get("video_factory") if isinstance(cfg, dict) else {}
        vf_cfg = vf_cfg if isinstance(vf_cfg, dict) else {}
    except Exception:
        vf_cfg = {}

    configured_order = vf_cfg.get("render_providers")
    if configured_order is not None:
        if (
            not isinstance(configured_order, list)
            or not configured_order
            or any(not isinstance(kind, str) or not kind.strip() for kind in configured_order)
        ):
            raise RenderProviderConfigurationError(
                "INVALID_RENDER_PROVIDER_ORDER",
                "render_providers must be a nonempty list of provider names",
            )
        provider_names = [kind.strip().lower() for kind in configured_order]
        if len(provider_names) != len(set(provider_names)):
            raise RenderProviderConfigurationError(
                "INVALID_RENDER_PROVIDER_ORDER", "render_providers contains a duplicate",
            )
    else:
        provider_name = str(vf_cfg.get("render_provider", "")).strip().lower()
        provider_names = [provider_name] if provider_name else []

    if not provider_names:
        return None
    if any(name not in OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS for name in provider_names):
        allowed = ", ".join(sorted(OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS))
        raise RenderProviderConfigurationError(
            "UNSUPPORTED_RENDER_PROVIDER",
            f"render providers must be one of: {allowed}",
        )
    providers: dict[str, Any] = {}
    ws = Path(output_dir) if output_dir else _workspace_path()
    for name in provider_names:
        if name == "google_flow":
            try:
                from video_factory.integrations.google_flow_provider import GoogleFlowPlaywrightProvider

                providers[name] = validate_product_video_render_provider(
                    GoogleFlowPlaywrightProvider.from_config(output_dir=ws)
                )
            except ModuleNotFoundError:
                raise RenderProviderConfigurationError(
                    "GOOGLE_FLOW_ADAPTER_NOT_INSTALLED", "Google Flow adapter is not installed"
                ) from None
        elif name == 'muse':
            if provider_names != ['muse']:
                raise RenderProviderConfigurationError('MUSE_PROFILE_REQUIRED','Muse must be selected alone with no fallback')
            from video_factory.integrations.muse_adapter import configured_muse_adapter
            try:
                providers[name]=configured_muse_adapter(vf_cfg['muse'])
            except (KeyError,ValueError):
                raise RenderProviderConfigurationError('MUSE_PROFILE_REQUIRED','Explicit Muse execution/profile configuration required') from None
        else:
            try:
                from video_factory.integrations.vertex_provider import VertexVideoProvider

                vertex_cfg = vf_cfg.get("vertex_ai") or {}
                vertex_cfg = vertex_cfg if isinstance(vertex_cfg, dict) else {}
                providers[name] = validate_product_video_render_provider(
                    VertexVideoProvider(
                        location=str(vertex_cfg.get("location") or "us-central1"),
                        model=str(vertex_cfg.get("model") or "veo-3.1-generate-001"),
                        output_dir=ws,
                    )
                )
            except ModuleNotFoundError:
                raise RenderProviderConfigurationError(
                    "VERTEX_AI_ADAPTER_NOT_INSTALLED", "Vertex AI video adapter is not installed"
                ) from None
    if configured_order is not None:
        return providers
    selected = providers[provider_names[0]]
    if provider_names[0] == "google_flow" and not selected.is_available():
        raise RenderProviderConfigurationError(
            "GOOGLE_FLOW_UNAVAILABLE",
            "Google Flow requires an authenticated, reconcilable browser session",
        )
    return selected


def _provider_for_operation(
    provider_source: Any,
    operation: dict[str, Any],
    *,
    provider_kind: str | None = None,
) -> tuple[Any | None, str | None]:
    """Resolve the exact official provider pinned to a durable operation."""
    if provider_source is None:
        return None, "no live video render provider configured or active"
    durable_kind = str(operation.get("provider_kind") or "").strip()
    if durable_kind and durable_kind not in OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS:
        return None, f"unsupported durable Product Video provider pin {durable_kind!r}"
    if isinstance(provider_source, Mapping):
        if durable_kind and durable_kind in provider_source:
            provider_source = provider_source[durable_kind]
        elif not durable_kind and provider_kind in provider_source:
            provider_source = provider_source[provider_kind]
        elif not durable_kind and len(provider_source) == 1:
            provider_source = next(iter(provider_source.values()))
        else:
            return None, (
                "configured Product Video provider registry is missing the durable "
                f"provider {durable_kind!r}"
                if durable_kind
                else "durable provider kind is required when multiple providers are configured"
            )

    known_kind = getattr(provider_source, "provider_kind", None)
    if isinstance(known_kind, str):
        known_kind = known_kind.strip()
    if durable_kind and known_kind and known_kind != durable_kind:
        return (
            None,
            f"configured Product Video provider kind {known_kind!r} does not match durable pin {durable_kind!r}",
        )
    if provider_kind is not None and known_kind and str(provider_kind).strip() != known_kind:
        return None, "explicit provider_kind does not match resolved provider identity"

    try:
        provider_source = validate_product_video_render_provider(provider_source)
    except ValueError as exc:
        return None, f"configured Product Video provider is invalid: {exc}"

    if operation.get("provider_operation_id") and not durable_kind:
        return (
            None,
            "legacy operation has a provider operation ID but no durable provider kind",
        )
    return provider_source, None


def _select_render_provider(provider_source: Any, requested_kind: str = "") -> Any:
    """Select only before submission; persisted operations use _provider_for_operation."""
    if not isinstance(provider_source, Mapping):
        selected_kind = str(getattr(provider_source, "provider_kind", "") or "").strip()
        if requested_kind and requested_kind != selected_kind:
            raise ProviderSelectionError(f"requested provider {requested_kind!r} is not configured")
        return provider_source
    selector = VideoProviderSelector(
        provider_source,
        preferred_provider_kind=requested_kind or None,
        allow_pre_submit_fallback=not bool(requested_kind),
    )
    selected = selector.select_initial()
    if requested_kind and selected.kind != requested_kind:
        raise ProviderSelectionError(f"requested provider {requested_kind!r} is unavailable")
    return selected.provider


def render(
    storyboard_result: dict[str, Any] | None = None,
    render_params: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    owner_user_id: str = "default_user",
    project_id: str = "",
    provider: Any | None = None,
    admission_context: dict[str, Any] | None = None,
    approval_request_id: str = "",
    kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hermes wire compatibility adapter for video rendering.

    Validates approved project storyboard and reference image before submitting
    to the configured video provider. Supports active_job_id for no-resubmit polling.
    """
    try:
        sb_data = _unpack_payload(storyboard_result)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "INVALID_PAYLOAD",
        }
    params = dict(render_params or {})
    if admission_context:
        params.setdefault("admission_context", admission_context)
    proj_id = (
        project_id.strip()
        or sb_data.get("project_id", "")
        or params.get("project_id", "")
    )

    # 1. Check for active_job_id resume (no-resubmit guarantee)
    active_job_id = params.get("active_job_id") or params.get("job_id")
    if active_job_id:
        if proj_id and proj_id != "proj_default":
            srv = _service()
            try:
                srv.get_project(owner_user_id, proj_id)
            except ValueError:
                return {
                    "ok": False,
                    "status": "failed",
                    "error": f"PROJECT_NOT_FOUND: Project '{proj_id}' does not exist for owner '{owner_user_id}'",
                    "code": "PROJECT_NOT_FOUND",
                }
        return {
            "ok": True,
            "status": "pending",
            "job_id": active_job_id,
            "id": active_job_id,
            "project_id": proj_id,
        }
    if not proj_id or proj_id == "proj_default":
        return {
            "ok": False,
            "status": "failed",
            "error": "PROJECT_ID_REQUIRED: A valid project_id is required for render",
            "code": "PROJECT_ID_REQUIRED",
        }

    # 2. Validate project state & storyboard approval
    srv = _service()
    try:
        project = srv.get_project(owner_user_id, proj_id)
    except ValueError:
        return {
            "ok": False,
            "status": "failed",
            "error": f"PROJECT_NOT_FOUND: Project '{proj_id}' does not exist for owner '{owner_user_id}'",
            "code": "PROJECT_NOT_FOUND",
        }

    if not project.storyboard or not project.storyboard.frames:
        return {
            "ok": False,
            "status": "failed",
            "error": f"STORYBOARD_REQUIRED: Project '{proj_id}' must have a valid storyboard before rendering",
            "code": "STORYBOARD_REQUIRED",
        }

    if project.storyboard.approval_status != StoryboardApprovalStatus.APPROVED:
        return {
            "ok": False,
            "status": "failed",
            "error": f"STORYBOARD_APPROVAL_REQUIRED: Storyboard for project '{proj_id}' must be approved before rendering",
            "code": "STORYBOARD_APPROVAL_REQUIRED",
        }

    # The request may confirm the package mode, but cannot select a cheaper branch.
    package_mode = sb_data.get("creative_mode") or "legacy"
    if "creative_mode" in params and params["creative_mode"] != package_mode:
        return {"ok": False, "status": "failed", "code": "CREATIVE_MODE_MISMATCH",
                "error": "Render mode conflicts with the storyboard package"}
    is_four_agent = package_mode == "four_agent"
    workflow_id = str(sb_data.get("workflow_id") or "")
    requested_workflow_id = str(params.get("workflow_id") or
                                (params.get("admission_context") or {}).get("workflow_id") or "")
    if is_four_agent and requested_workflow_id and requested_workflow_id != workflow_id:
        return {"ok": False, "status": "failed", "code": "CREATIVE_MODE_MISMATCH",
                "error": "Render workflow conflicts with the storyboard package"}
    if is_four_agent:
        from video_factory.creative.storyboard import storyboard_package_digest

        if (sb_data.get("schema_version") != "storyboard-package-v2"
                or not workflow_id
                or sb_data.get("package_digest") != storyboard_package_digest(sb_data)):
            return {"ok": False, "status": "failed", "code": "STORYBOARD_PACKAGE_CHANGED",
                    "error": "Four-agent render requires an unchanged storyboard package"}
    workflow = {}
    db_path = _canonical_approval_db_path()
    if workflow_id and db_path.is_file():
        try:
            with sqlite3.connect(db_path) as workflow_conn:
                row = workflow_conn.execute(
                    "SELECT aggregate_json FROM workflow WHERE workflow_id = ?", (workflow_id,)
                ).fetchone()
            workflow = json.loads(row[0]) if row else {}
        except (sqlite3.Error, ValueError, TypeError):
            workflow = {}
    workflow_mode = workflow.get("creative_policy", {}).get("creative_mode")
    if workflow_mode and workflow_mode != package_mode:
        return {"ok": False, "status": "failed", "code": "CREATIVE_MODE_MISMATCH",
                "error": "Storyboard mode conflicts with the persisted workflow"}
    if is_four_agent:
        sb_qc = sb_data.get('qc_level') or (workflow.get('creative_policy') or {}).get('qc_level') or 'middle'
        wf_qc = (workflow.get('creative_policy') or {}).get('qc_level', 'middle')
        if sb_qc != wf_qc:
            return {"ok":False, "status":"failed", "code":"QC_POLICY_MISMATCH",
                    "error":"Storyboard QC level conflicts with the persisted workflow"}
    if is_four_agent:
        if (workflow_mode != "four_agent"
                or (workflow.get("storyboard") or {}).get("package_digest") != sb_data["package_digest"]):
            return {"ok": False, "status": "failed", "code": "CREATIVE_MODE_MISMATCH",
                    "error": "Storyboard package does not match the persisted four-agent workflow"}
        if (not sb_data.get("project_id")
                or proj_id != sb_data["project_id"]
                or proj_id != workflow.get("project_id")
                or owner_user_id != workflow.get("owner_user_id")):
            return {"ok": False, "status": "failed", "code": "CREATIVE_CONTEXT_MISMATCH",
                    "error": "Render project or owner conflicts with the storyboard package or workflow"}

    # 3. Verify worker admission context fail-closed: direct MCP render cannot allocate budget or submit provider
    admitted_workflow_id = str(
        (params.get("admission_context") or {}).get("workflow_id")
        or sb_data.get("workflow_id")
        or params.get("workflow_id")
        or ""
    )
    if not is_four_agent:
        admission_ctx = params.get("admission_context") or {}
        if not isinstance(admission_ctx, dict):
            admission_ctx = {}
        reservation_id = (
            admission_ctx.get("reservation_id")
            or admission_ctx.get("budget_reservation_id")
            or params.get("budget_reservation_id")
            or params.get("reservation_id")
        )
        if not reservation_id:
            return {
                "ok": False,
                "status": "failed",
                "error": "ADMISSION_REQUIRED: Direct MCP render requires a valid worker admission context and budget reservation",
                "code": "ADMISSION_REQUIRED",
            }

        # Look up budget reservation from in-memory engine or directly from hermes.db
        alloc_state: str | None = None
        alloc_owner: str | None = None
        alloc_scope: str | None = None
        try:
            from video_factory.workflows.budgets import get_reservation

            alloc = get_reservation(reservation_id)
            if alloc is not None:
                alloc_state = alloc.state
                alloc_owner = alloc.owner_user_id
                alloc_scope = alloc.scope_id
        except Exception:
            pass

        if alloc_state is None:
            try:
                _budget_db_path = _canonical_approval_db_path()
                if _budget_db_path.exists():
                    from video_factory.storage_infra.sqlite import connect as _connect_sqlite

                    _bconn = _connect_sqlite(_budget_db_path)
                    _bconn.row_factory = sqlite3.Row
                    _brow = _bconn.execute(
                        "SELECT state, owner_user_id, scope_id FROM budget_reservation WHERE reservation_id = ?",
                        (reservation_id,),
                    ).fetchone()
                    if _brow:
                        alloc_state = _brow["state"]
                        alloc_owner = _brow["owner_user_id"]
                        alloc_scope = _brow["scope_id"]
                    _bconn.close()
            except Exception:
                alloc_state = None
        if alloc_state is None or alloc_state != "reserved":
            return {
                "ok": False,
                "status": "failed",
                "error": f"ADMISSION_REQUIRED: Invalid or expired budget reservation '{reservation_id}'",
                "code": "ADMISSION_REQUIRED",
            }
        if alloc_owner != owner_user_id:
            return {
                "ok": False,
                "status": "failed",
                "error": f"OWNER_MISMATCH: Budget reservation owner '{alloc_owner}' does not match '{owner_user_id}'",
                "code": "ADMISSION_REQUIRED",
            }
        if alloc_scope and alloc_scope not in (
            proj_id,
            admitted_workflow_id,
            "default-scope",
        ):
            return {
                "ok": False,
                "status": "failed",
                "error": f"PROJECT_MISMATCH: Budget reservation project '{alloc_scope}' does not match '{proj_id}'",
                "code": "ADMISSION_REQUIRED",
            }

    def _reference_manifest() -> dict[str, dict[str, Any]]:
        manifest: dict[str, dict[str, Any]] = {}
        if not project.resource_pack:
            return manifest
        for ref in project.resource_pack.product_references:
            metadata = dict(ref.metadata or {})
            if not any(
                key in metadata for key in ("qc_eligible", "qc_sha256", "sha256")
            ):
                continue
            digest = str(
                metadata.get("qc_sha256") or metadata.get("sha256") or ""
            ).removeprefix("sha256:")
            eligible = (
                metadata.get("qc_eligible") is True or metadata.get("eligible") is True
            )
            manifest[ref.asset_id] = {
                "asset_id": ref.asset_id,
                "uri": ref.uri,
                "sha256": digest,
                "eligible": eligible,
                "metadata": metadata,
            }
        return manifest

    approved_manifest = _reference_manifest()

    # 3. Resolve default reference images from the resource pack. A local
    # reference must be backed by the immutable QC manifest; asset:// remains
    # a legacy symbolic fixture for offline provider tests only.
    fallback_ref_images: list[str] = []
    if project.resource_pack and project.resource_pack.product_references:
        for prod_ref in project.resource_pack.product_references:
            if hasattr(prod_ref, "uri") and prod_ref.uri:
                fallback_ref_images.append(str(prod_ref.uri))
            elif isinstance(prod_ref, dict) and prod_ref.get("uri"):
                fallback_ref_images.append(str(prod_ref["uri"]))

    # 4. Resolve video provider
    try:
        provider_source = _video_provider(provider_override=provider)
    except RenderProviderConfigurationError as exc:
        return {"ok": False, "status": "failed", "error": str(exc), "code": exc.code}
    if provider_source is None:
        return {
            "ok": False,
            "status": "failed",
            "error": "PROVIDER_UNAVAILABLE: No live video render provider configured or active",
            "code": "VF_PROVIDER_UNAVAILABLE",
        }
    try:
        video_prov = _select_render_provider(
            provider_source, str(params.get("provider_kind") or "").strip()
        )
    except (ProviderSelectionError, ValueError) as exc:
        return {
            "ok": False, "status": "failed", "code": "PROVIDER_UNAVAILABLE",
            "error": f"PROVIDER_UNAVAILABLE: {exc}",
        }

    from video_factory.integrations.ports import VideoGenerationRequest

    duration = int(params.get("duration_seconds", 4))
    if duration not in (4, 6, 8):
        return {
            "ok": False,
            "status": "failed",
            "error": f"UNSUPPORTED_SCENE_DURATION: default duration {duration}s is not one of 4, 6, 8",
            "code": "UNSUPPORTED_SCENE_DURATION",
        }
    aspect_ratio = str(params.get("aspect_ratio", "9:16"))
    if aspect_ratio not in ("9:16", "16:9"):
        aspect_ratio = "9:16"

    # Multi-scene render submission across all storyboard frames
    submitted_op_ids: list[str] = []
    frames_to_render = project.storyboard.frames
    orders = [frame.order for frame in frames_to_render]
    if len(orders) != len(set(orders)):
        return {
            "ok": False,
            "status": "failed",
            "error": "INVALID_SCENE_ORDER: Storyboard frames have duplicate or ambiguous orders",
            "code": "INVALID_SCENE_ORDER",
        }
    if project.scene_plan and project.scene_plan.scenes:
        if len(frames_to_render) != len(project.scene_plan.scenes):
            return {
                "ok": False,
                "status": "failed",
                "error": f"SCENE_COUNT_MISMATCH: Storyboard has {len(frames_to_render)} frames but scene plan requires {len(project.scene_plan.scenes)} scenes",
                "code": "SCENE_COUNT_MISMATCH",
            }
    frames_to_render = tuple(sorted(project.storyboard.frames, key=lambda f: f.order))
    product_identity = (
        project.resource_pack.product_identity_description
        if project.resource_pack
        else ""
    )
    package_keyframes: dict[tuple[str, str], dict[str, Any]] = {}
    for item in sb_data.get("generated_keyframes", []):
        if not isinstance(item, dict) or not item.get("scene_id"):
            continue
        role = str(item.get("frame_role") or "start").lower()
        if role not in {"start", "end"}:
            return {"ok": False, "status": "failed", "code": "INVALID_KEYFRAME_ROLE", "error": "INVALID_KEYFRAME_ROLE"}
        binding = (str(item["scene_id"]), role)
        if binding in package_keyframes:
            return {"ok": False, "status": "failed", "code": "DUPLICATE_KEYFRAME_ROLE", "error": "DUPLICATE_KEYFRAME_ROLE"}
        package_keyframes[binding] = item
    script_payload = (
        sb_data.get("script") if isinstance(sb_data.get("script"), dict) else {}
    )
    script_scenes = script_payload.get("scenes") or sb_data.get("scenes") or []
    script_by_scene = {
        str(item.get("scene_id")): item
        for item in script_scenes
        if isinstance(item, dict) and item.get("scene_id")
    }
    package_v2 = sb_data.get("schema_version") == "storyboard-package-v2"
    keyframe_qc_receipt = sb_data.get("keyframe_qc_receipt")
    render_qc_level = "middle"
    persisted_storyboard = workflow.get("storyboard") or {}
    if ((workflow.get("creative_policy") or {}).get("qc_level") == "low"
            and persisted_storyboard.get("package_digest") == sb_data.get("package_digest")
            and isinstance(persisted_storyboard.get("keyframe_qc_receipt"), dict)):
        render_qc_level = "low"
        keyframe_qc_receipt = persisted_storyboard["keyframe_qc_receipt"]
    if sb_data.get('creative_mode') == 'four_agent':
        from video_factory.creative.agents.gates import require_storyboard_qc_pass
        try:
            require_storyboard_qc_pass(
                {**sb_data, "keyframe_qc_receipt": keyframe_qc_receipt}, qc_level=render_qc_level,
            )
        except (KeyError, ValueError, OSError):
            return {'ok':False, 'status':'failed', 'code':'KEYFRAME_QC_REQUIRED',
                    'error':'Current keyframe QC PASS for the storyboard package is required'}
    if package_v2:
        from video_factory.creative.storyboard import (
            storyboard_package_digest,
        )

        if str(sb_data.get("package_digest") or "") != storyboard_package_digest(
            sb_data
        ):
            return {
                "ok": False,
                "status": "failed",
                "error": "STORYBOARD_PACKAGE_CHANGED: script, prompt, reference, or keyframe changed after QC",
                "code": "STORYBOARD_PACKAGE_CHANGED",
            }
        scene_ids = {frame.scene_id for frame in frames_to_render}
        if any((scene_id, "start") not in package_keyframes for scene_id in scene_ids) or any(
            scene_id not in scene_ids for scene_id, _role in package_keyframes
        ):
            return {
                "ok": False,
                "status": "failed",
                "error": "KEYFRAME_PACKAGE_INCOMPLETE: every storyboard scene requires one generated keyframe",
                "code": "KEYFRAME_PACKAGE_INCOMPLETE",
            }
        if not isinstance(keyframe_qc_receipt, dict):
            return {
                "ok": False,
                "status": "failed",
                "error": (
                    "KEYFRAME_QC_REQUIRED: storyboard-package-v2 requires an immutable "
                    "keyframe_qc_receipt before paid scene rendering"
                ),
                "code": "KEYFRAME_QC_REQUIRED",
            }
    else:
        legacy_references = list(
            params.get("reference_image_paths") or fallback_ref_images
        )
        if any(not str(value).startswith("asset://") for value in legacy_references):
            return {
                "ok": False,
                "status": "failed",
                "error": (
                    "KEYFRAME_PACKAGE_REQUIRED: local seller/listing images cannot condition "
                    "video render; use an approved storyboard-package-v2 generated keyframe"
                ),
                "code": "KEYFRAME_PACKAGE_REQUIRED",
            }

    batch_hash = hashlib.sha256(
        f"{proj_id}|{project.storyboard_version}|{idempotency_key or sb_data.get('package_digest') or ''}".encode()
    ).hexdigest()[:12]
    durable_batch_id = f"batch_{proj_id}_v{project.storyboard_version}_{batch_hash}"

    # Pre-persist RenderBatch (status='running') and all planned scene operations before paid provider calls
    from video_factory.integrations.domain import RenderBatch

    render_batch = RenderBatch(
        render_batch_id=durable_batch_id,
        owner_user_id=owner_user_id,
        workflow_id=admitted_workflow_id,
        project_id=proj_id,
        version=project.storyboard_version,
        status="running",
        scene_count=len(frames_to_render),
        completed_scene_count=0,
        created_at=_now(),
        updated_at=_now(),
    )
    # Pre-compile requests and pre-persist planned operations
    compiled_requests: list[tuple[Any, VideoGenerationRequest]] = []
    for frame in frames_to_render:
        scene_spec = script_by_scene.get(frame.scene_id, {})
        f_prompt = (
            getattr(frame.prompt, "positive_prompt", "")
            or getattr(frame.prompt, "prompt", "")
            if frame.prompt
            else ""
        )
        if scene_spec:
            f_prompt = (
                f"Scene action: {scene_spec.get('subject_action', '')}. "
                f"Product interaction: {scene_spec.get('product_interaction', '')}. "
                f"Start state: {scene_spec.get('start_state', '')}. "
                f"End state: {scene_spec.get('end_state', '')}. "
                f"Visible change: {scene_spec.get('visual_change', '')}. "
                f"Environment: {scene_spec.get('environment', '')}. "
                f"Camera framing: {scene_spec.get('camera_framing', '')}. "
                f"Camera movement: {scene_spec.get('camera_movement', '')}. "
                "Animate the described physical action and state change; do not merely zoom, pan, "
                "rotate, or hold on a static product image. "
                f"Show exactly one {product_identity or 'approved target product'} and no unrelated product. "
                "Do not render any readable text, captions, CTA, watermark, seller name, shop name, "
                "marketplace UI, URL, price, or unrelated logo; text will be added in post-production."
            )
            narration = str(scene_spec.get("narration") or "").strip()
            if narration:
                f_prompt += (
                    f' If audio is supported, speak this exact Vietnamese narration as an off-screen voiceover: "{narration}". '
                    "Do not add subtitles, extra claims, or other spoken words."
                )
        if not f_prompt:
            f_prompt = (
                f"{frame.label}. {frame.subject_action}. {frame.visual_state}".strip()
            )
        if not f_prompt and project.creative_brief:
            f_prompt = project.creative_brief.core_message

        f_refs: list[str] = []
        selected_asset_ids: list[str] = []
        generated_keyframe = package_keyframes.get((frame.scene_id, "start"))
        generated_end_keyframe = (
            package_keyframes.get((frame.scene_id, "end"))
            if scene_spec.get("end_keyframe_policy") in {"generated", "required"}
            else None
        )
        generated_keyframe_sha = ""
        if generated_keyframe:
            generated_keyframe_sha = str(
                generated_keyframe.get("sha256") or ""
            ).removeprefix("sha256:")
            f_refs.append(str(generated_keyframe.get("image_path") or ""))
        elif params.get("reference_image_paths"):
            f_refs.extend(params["reference_image_paths"])
        elif frame.prompt and getattr(frame.prompt, "reference_asset_ids", None):
            selected_asset_ids = [
                str(asset_id) for asset_id in frame.prompt.reference_asset_ids
            ]
            missing_ids = [
                asset_id
                for asset_id in selected_asset_ids
                if asset_id not in approved_manifest
            ]
            if missing_ids:
                if approved_manifest:
                    return {
                        "ok": False,
                        "status": "failed",
                        "error": f"REFERENCE_NOT_APPROVED: Storyboard references are absent from the eligible QC manifest: {missing_ids}",
                        "code": "REFERENCE_NOT_APPROVED",
                    }
                # Compatibility for old symbolic asset:// fixtures. A real
                # local file still must match an eligible manifest below.
                refs_by_id = (
                    {
                        ref.asset_id: ref.uri
                        for ref in project.resource_pack.product_references
                        if ref.asset_id in missing_ids
                    }
                    if project.resource_pack
                    else {}
                )
                f_refs.extend(
                    refs_by_id[asset_id]
                    for asset_id in selected_asset_ids
                    if asset_id in refs_by_id
                )
            else:
                f_refs.extend(
                    approved_manifest[asset_id]["uri"]
                    for asset_id in selected_asset_ids
                )
        if not f_refs:
            f_refs.extend(fallback_ref_images)

        # Validate file references exist
        valid_refs: list[str] = []
        missing_refs: list[str] = []
        for img_p in f_refs:
            if img_p.startswith("asset://"):
                if approved_manifest:
                    missing_refs.append(img_p)
                else:
                    valid_refs.append(img_p)
            else:
                p = _normalize_file_path(img_p)
                current_sha = (
                    compute_file_sha256(p)
                    if p.is_file() and p.stat().st_size > 0
                    else ""
                )
                if (
                    generated_keyframe
                    and current_sha
                    and current_sha == generated_keyframe_sha
                ):
                    valid_refs.append(str(p))
                    continue
                matched = next(
                    (
                        item
                        for item in approved_manifest.values()
                        if item["eligible"]
                        and item["sha256"] == current_sha
                        and _normalize_file_path(item["uri"]) == p
                    ),
                    None,
                )
                if matched:
                    selected_asset_ids.append(matched["asset_id"])
                    valid_refs.append(str(p))
                else:
                    missing_refs.append(img_p)

        if missing_refs:
            return {
                "ok": False,
                "status": "failed",
                "error": f"REFERENCE_IMAGE_NOT_FOUND: Declared reference image(s) do not exist or are empty on disk: {missing_refs}",
                "code": "REFERENCE_IMAGE_NOT_FOUND",
            }

        if len(valid_refs) > 1:
            valid_refs = valid_refs[:1]
            selected_asset_ids = selected_asset_ids[:1]

        provider_model = "configured-multi-provider" if isinstance(provider_source, Mapping) else getattr(video_prov, "model", "")
        if not isinstance(provider_model, str) or not provider_model.strip():
            provider_model = video_prov.__class__.__name__
        operation_material = "|".join((
            str(idempotency_key or ""),
            str(sb_data.get("package_digest") or ""),
            proj_id,
            frame.scene_id,
            provider_model,
        ))
        req_id = (
            "video-scene-"
            + hashlib.sha256(operation_material.encode()).hexdigest()[:24]
        )

        frame_negative = (
            getattr(frame.prompt, "negative_constraints", "") if frame.prompt else ""
        )
        frame_identity = (
            getattr(frame.prompt, "product_identity_constraints", "")
            if frame.prompt
            else ""
        )
        provider_options = dict(params.get("provider_options") or {})
        if sb_data.get('creative_mode')=='four_agent':
            from video_factory.creative.agents.contracts import BackendProfile
            prof_dict = sb_data.get('backend_profile') or {}
            prof_digest = prof_dict.get('digest') or (BackendProfile.from_dict(prof_dict).digest if prof_dict else '')
            provider_options.update(creative_profile_digest=prof_digest,
                product_truth_digest=(sb_data.get('research_dossier') or {}).get('dossier_digest', ''))
        provider_options["negativePrompt"] = _merge_negative_prompt(
            provider_options.get("negativePrompt") or frame_negative
        )
        if selected_asset_ids:
            provider_options["selected_reference_assets"] = [
                {
                    "asset_id": asset_id,
                    "sha256": approved_manifest[asset_id]["sha256"],
                }
                for asset_id in selected_asset_ids
                if asset_id in approved_manifest
            ]
        if generated_keyframe:
            provider_options["selected_keyframe"] = {
                "frame_id": generated_keyframe.get("frame_id"),
                "sha256": generated_keyframe_sha,
                "package_digest": sb_data.get("package_digest"),
            }

        scene_duration = int(scene_spec.get("duration_seconds") or duration)
        if scene_duration not in (4, 6, 8):
            return {
                "ok": False,
                "status": "failed",
                "error": f"UNSUPPORTED_SCENE_DURATION: {frame.scene_id} requests {scene_duration}s; allowed values are 4, 6, 8",
                "code": "UNSUPPORTED_SCENE_DURATION",
            }

        req = VideoGenerationRequest(
            request_id=req_id,
            owner_user_id=owner_user_id,
            project_id=proj_id,
            scene_id=frame.scene_id,
            prompt=_render_safe_prompt(
                f_prompt or "Cinematic product showcase video",
                identity=frame_identity or product_identity,
                extra_constraints=frame_negative,
            ),
            duration_seconds=scene_duration,
            aspect_ratio=aspect_ratio,
            reference_image_paths=tuple(valid_refs),
            provider_options=provider_options,
            request_audio=bool(str(scene_spec.get("narration") or "").strip()),
        )

        if package_v2:
            try:
                req = compile_grounded_scene_request(
                    owner_user_id=owner_user_id,
                    workflow_id=str(
                        sb_data.get("workflow_id")
                        or params.get("workflow_id")
                        or (kwargs or {}).get("workflow_id")
                        or ""
                    ).strip(),
                    project_id=proj_id,
                    scene=scene_spec,
                    keyframe=generated_keyframe or {},
                    end_keyframe=generated_end_keyframe,
                    keyframe_qc_receipt=keyframe_qc_receipt,
                    package_digest=str(sb_data.get("package_digest") or ""),
                    qc_level=render_qc_level,
                    product_identity=product_identity,
                    provider_model=provider_model,
                    aspect_ratio=aspect_ratio,
                    provider_options=provider_options,
                    scene_prompt=next((p for p in sb_data.get('scene_prompts', {}).get('prompts', [])
                        if p['scene_id'] == frame.scene_id), None) if sb_data.get('creative_mode') == 'four_agent' else None,
                )
                if sb_data.get('creative_mode') == 'four_agent':
                    req = replace(req, provider_options={**req.provider_options, 'creative_package':sb_data,
                        'creative_video_call_limit':min(12,int(sb_data.get('video_call_limit',12))),
                        'creative_profile_digest':prof_digest})
            except ValueError as exc:
                message = str(exc)
                code = (
                    "KEYFRAME_QC_REQUIRED"
                    if "QC" in message or "keyframe" in message
                    else "INVALID_SCENE_RENDER_REQUEST"
                )
                if "allowed values are 4, 6, 8" in message:
                    code = "UNSUPPORTED_SCENE_DURATION"
                return {
                    "ok": False,
                    "status": "failed",
                    "error": f"{code}: {message}",
                    "code": code,
                }

        compiled_requests.append((frame, req))

    if sb_data.get('creative_mode')=='four_agent':
        from video_factory.creative.agents.continuity import continues_previous_clip
        continuous = [continues_previous_clip(script_by_scene[frame.scene_id]) for frame, _ in compiled_requests]
        if continuous and continuous[0]:
            return {'ok':False, 'status':'failed', 'code':'INVALID_CONTINUITY',
                    'error':'The first scene cannot continue a previous clip'}
        low_qc_cut = any(continuous) and render_qc_level == 'low'
        if any(continuous) and not low_qc_cut and sb_data.get('qc_level', 'middle') != 'high':
            return {'ok':False, 'status':'failed', 'code':'CONTINUITY_REQUIRES_HIGH_QC',
                    'error':'Continuous scenes require high clip and endpoint QC'}
        # Immutable batch membership permits reuse of an unaffected completed scene
        # without mutating that operation's original batch/history.
        context_path = _workspace_path()/'creative_batches'/(durable_batch_id+'.json')
        context_path.parent.mkdir(parents=True,exist_ok=True)
        context={**sb_data,'_batch_operation_ids':[req.request_id for _,req in compiled_requests]}
        if low_qc_cut:
            context['_continuity_mode'] = 'independent_keyframes'
        if context_path.exists():
            if json.loads(context_path.read_text(encoding='utf8'))!=json.loads(json.dumps(context)):
                return {'ok':False,'status':'failed','code':'CREATIVE_BATCH_INPUT_CHANGED',
                        'error':'Batch is already bound to a different board or scene requests'}
        else:
            with context_path.open('x',encoding='utf8') as handle:
                json.dump(context,handle,ensure_ascii=False,indent=2)

    # Persist only after every scene and both role bindings have validated.
    if hasattr(srv.repository, "save_render_batch"):
        srv.repository.save_render_batch(render_batch)

    # Transactionally pre-persist all operations in 'planned' status
    op_conn = sqlite3.connect(_db_path())
    try:
        op_store = SceneRenderOperationStore(op_conn)
        for frame, req in compiled_requests:
            op_store.mark_planned(
                req, render_batch_id=durable_batch_id, scene_order=frame.order
            )
        batch_operations = _creative_batch_operations(op_conn, durable_batch_id) if is_four_agent else None
    finally:
        op_conn.close()

    # Now execute submissions via runtime
    waiting_for_capacity = False
    pending_scene_fallback = False
    for request_index, (frame, req) in enumerate(compiled_requests):
        if request_index and sb_data.get('creative_mode') == 'four_agent' and sb_data.get('qc_level') == 'high':
            break
        try:
            operation_connection = sqlite3.connect(_db_path())
            try:
                op_store = SceneRenderOperationStore(operation_connection)
                if batch_operations and batch_operations[request_index]["operation_id"] != req.request_id:
                    req = op_store.request_from_record(batch_operations[request_index])
                runtime = SceneRenderRuntime(
                    store=op_store,
                    provider=provider_source,
                    preferred_provider_kind=str(params.get("provider_kind") or "").strip() or None,
                    allow_pre_submit_fallback=not bool(params.get("provider_kind")),
                    capacity_store=ProviderCapacityStore(operation_connection),
                    capacity_limit=None,
                )
                result = runtime.run(req)
                if (is_four_agent
                        and (sb_data.get("backend_profile") or {}).get("provider_kind") == "google_flow"
                        and op_store.is_flow_activity_rejection(op_store.get(req.request_id))):
                    # Let status handling plan the replacement before another scene is submitted.
                    pending_scene_fallback = True
                    break
            finally:
                operation_connection.close()
            if (
                result.status in ("submitted", "running", "completed")
                and result.provider_operation_id
            ):
                submitted_op_ids.append(result.provider_operation_id)
            elif result.error_code == "PROVIDER_CAPACITY_WAIT":
                waiting_for_capacity = True
            elif (
                result.status == "unknown"
                or result.error_code in ("RECONCILIATION_REQUIRED", "GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN", "NETWORK_UNKNOWN_OUTCOME")
            ):
                return {
                    "ok": False,
                    "status": "reconciliation_required",
                    "code": result.error_code or "RECONCILIATION_REQUIRED",
                    "error": result.error_message or "provider outcome is uncertain; reconcile before retrying",
                }
            else:
                return {
                    "ok": False,
                    "status": "waiting_capacity" if result.error_code == "PROVIDER_CAPACITY_WAIT" else "failed",
                    "error": f"RENDER_SUBMIT_FAILED: Frame {frame.frame_id} failed: {result.error_message or result.error_code}",
                    "code": result.error_code or "RENDER_SUBMIT_FAILED",
                }
        except TerminalJobError as exc:
            if exc.code in {"GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN", "RECONCILIATION_REQUIRED", "UNKNOWN_PAID_OUTCOME"}:
                return {
                    "ok": False,
                    "status": "reconciliation_required",
                    "code": exc.code or "RECONCILIATION_REQUIRED",
                    "error": str(exc),
                }
            return {
                "ok": False,
                "status": "failed",
                "error": f"RENDER_SUBMIT_FAILED: Frame {frame.frame_id} exception: {exc}",
                "code": exc.code or "RENDER_SUBMIT_FAILED",
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "error": f"RENDER_SUBMIT_FAILED: Frame {frame.frame_id} exception: {exc}",
                "code": "RENDER_SUBMIT_FAILED",
            }

    if not submitted_op_ids and not waiting_for_capacity and not pending_scene_fallback:
        return {
            "ok": False,
            "status": "failed",
            "error": "RENDER_SUBMIT_FAILED: No frames were submitted for rendering",
            "code": "RENDER_SUBMIT_FAILED",
        }

    # Persist every scene in project.generated_scenes with VideoGenerationStatus.GENERATING
    # Note: We do NOT set ProjectStatus.DRAFT_VIDEO_READY yet; status transitions to
    # SCENES_GENERATED and TIMELINE_READY / DRAFT_VIDEO_READY during polling completion
    new_generated_scenes = []
    for frame in frames_to_render:
        scene_spec = script_by_scene.get(frame.scene_id, {})
        new_generated_scenes.append(
            GeneratedScene(
                scene_id=frame.scene_id,
                video_prompt=VideoPrompt(
                    scene_id=frame.scene_id,
                    duration_seconds=int(
                        scene_spec.get("duration_seconds") or duration
                    ),
                    start_visual_state=scene_spec.get("start_state", ""),
                    end_visual_state=scene_spec.get("end_state", ""),
                    subject_action=scene_spec.get("subject_action", ""),
                    product_action=scene_spec.get("product_interaction", ""),
                    camera_movement=scene_spec.get("camera_movement", ""),
                    camera_framing=scene_spec.get("camera_framing", ""),
                    environment_motion=scene_spec.get("environment", ""),
                ),
                generation_status=VideoGenerationStatus.GENERATING,
                generation_job_id=durable_batch_id,
                version=project.storyboard_version,
                created_at=_now(),
                updated_at=_now(),
            )
        )

    srv.repository.save(
        replace(
            project,
            generated_scenes=tuple(new_generated_scenes),
            draft_video_asset_id=durable_batch_id,
            updated_at=_now(),
        )
    )

    return {
        "ok": True,
        "status": "pending",
        "job_id": durable_batch_id,
        "id": durable_batch_id,
        "project_id": proj_id,
        "render_id": durable_batch_id,
    }


def _creative_batch_operations(connection, job_id):
    path=_workspace_path()/'creative_batches'/(job_id+'.json')
    if not path.is_file():
        return None
    context=json.loads(path.read_text(encoding='utf8'))
    ids=context['_batch_operation_ids']
    if not 1<=len(ids)<=5 or len(set(ids))!=len(ids):
        raise ValueError('invalid creative batch operation membership')
    operations=[]
    for order,operation_id in enumerate(ids,1):
        row=connection.execute('SELECT * FROM video_factory_scene_operation WHERE operation_id=?',(operation_id,)).fetchone()
        if row is None or row['project_id']!=context['project_id']:
            raise ValueError('creative batch operation missing or wrong project')
        latest = connection.execute(
            'SELECT * FROM video_factory_scene_operation WHERE render_batch_id=? AND scene_id=? '
            'AND project_id=? AND owner_user_id=? AND attempt>=? ORDER BY attempt DESC LIMIT 1',
            (row['render_batch_id'] or job_id, row['scene_id'], row['project_id'], row['owner_user_id'], row['attempt']),
        ).fetchone()
        if latest is not None:
            row = latest
        operations.append({**dict(row),'scene_order':order})
    return operations


def get_job_status(
    job_id: str,
    provider: Any | None = None,
    provider_kind: str | None = None,
    force_recompose: bool = False,
) -> dict[str, Any]:
    """Hermes wire compatibility adapter for polling asynchronous video job status."""
    if not job_id or not str(job_id).strip():
        return {
            "ok": False,
            "status": "failed",
            "error": "MISSING_JOB_ID: job_id is required",
            "code": "MISSING_JOB_ID",
        }

    # Handle durable render batch
    if job_id.startswith("batch_"):
        srv = _service()
        batch = (
            srv.repository.get_render_batch(job_id)
            if hasattr(srv.repository, "get_render_batch")
            else None
        )
        from video_factory.storage.layout import get_workspace_layout
        from video_factory.integrations.domain import (
            VideoFactoryArtifact,
            Timeline,
            TimelineClip,
            TimelineStatus,
        )

        ws = get_workspace_layout()
        op_store = SceneRenderOperationStore(_db_path())
        capacity_store = ProviderCapacityStore(op_store.connection)

        def acquire_poll_capacity(
            operation_id: str, provider_instance: Any, kind: str, *, lookup_only: bool = False
        ) -> bool:
            return capacity_store.try_acquire(
                provider_kind=kind,
                operation_id=operation_id,
                lease_owner=process_lease_owner(),
                limit=configured_capacity(kind),
                profile_id=str(getattr(provider_instance, "profile_id", "") or ""),
                reconciled_takeover=not lookup_only,
                reconciliation_only=lookup_only,
            )

        def fail_batch(
            *,
            code: str,
            error: str,
            operation_id: str = "",
            persist_operation_failure: bool = False,
            creative_failure: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if operation_id and persist_operation_failure:
                op_store.mark_integrity_failed(
                    operation_id,
                    error_code=code,
                    error_message=error,
                )
                failed_op = op_store.get(operation_id)
                if failed_op and failed_op.get("provider_kind"):
                    capacity_store.release_terminal(failed_op["provider_kind"], operation_id)
            durable_batch = (
                srv.repository.get_render_batch(job_id)
                if hasattr(srv.repository, "get_render_batch")
                else None
            )
            if durable_batch is not None and hasattr(
                srv.repository, "save_render_batch"
            ):
                srv.repository.save_render_batch(
                    replace(durable_batch, status="failed", updated_at=_now())
                )
            proj_id = (
                durable_batch.project_id
                if durable_batch
                else (batch.project_id if batch else "")
            )
            owner_id = (
                durable_batch.owner_user_id
                if durable_batch
                else (batch.owner_user_id if batch else "default-owner")
            )
            if proj_id:
                try:
                    _invalidate_final_approval(
                        srv, owner_id, proj_id, f"Batch {job_id} failed: {code}"
                    )
                except Exception as exc:
                    return {
                        "ok": False,
                        "status": "failed",
                        "job_id": job_id,
                        "error": f"FINAL_APPROVAL_INVALIDATION_FAILED: Failed to invalidate approval: {exc}",
                        "code": "FINAL_APPROVAL_INVALIDATION_FAILED",
                    }

            return {
                "ok": False,
                "status": "failed",
                "job_id": job_id,
                "error": error,
                "code": code,
                **({'creative_failure':creative_failure} if creative_failure else {}),
            }

        if batch is not None and batch.status == "completed":
            comp_path = (
                ws.get_composite_dir(batch.project_id, batch.version) / "draft.mp4"
            )
            valid_reuse = False
            if comp_path.is_file() and comp_path.stat().st_size > 0:
                comp_sha = compute_file_sha256(comp_path)
                if batch.composite_artifact_id and hasattr(
                    srv.repository, "get_artifact"
                ):
                    try:
                        art = srv.repository.get_artifact(
                            batch.composite_artifact_id,
                            owner_user_id=batch.owner_user_id,
                        )
                        if (
                            art
                            and art.owner_user_id == batch.owner_user_id
                            and art.project_id == batch.project_id
                            and art.version == batch.version
                            and art.sha256 == comp_sha
                        ):
                            prov = (art.provider_metadata or {}).get("provenance") or {}
                            op_conn = sqlite3.connect(_db_path())
                            op_conn.row_factory = sqlite3.Row
                            try:
                                rows = op_conn.execute(
                                    "SELECT * "
                                    "FROM video_factory_scene_operation AS op WHERE op.render_batch_id = ? "
                                    "AND op.attempt = (SELECT MAX(other.attempt) FROM video_factory_scene_operation AS other "
                                    "WHERE other.render_batch_id = op.render_batch_id AND other.scene_id = op.scene_id) "
                                    "ORDER BY op.scene_order ASC",
                                    (job_id,),
                                ).fetchall()
                                cur_ops = [dict(r) for r in rows]
                                cur_ops = _creative_batch_operations(op_conn, job_id) or cur_ops
                            finally:
                                op_conn.close()

                            prov_scene_ids = prov.get("scene_ids")
                            prov_req_digests = prov.get("request_digests")
                            prov_clip_digests = prov.get("clip_digests")
                            cur_scene_ids = [o["scene_id"] for o in cur_ops]
                            cur_req_digests = [
                                o.get("request_digest") for o in cur_ops
                            ]
                            cur_clip_digests = [
                                o.get("output_sha256") for o in cur_ops
                            ]

                            if (
                                cur_ops
                                and isinstance(prov_scene_ids, list)
                                and isinstance(prov_req_digests, list)
                                and isinstance(prov_clip_digests, list)
                                and len(prov_scene_ids) == len(cur_scene_ids)
                                and len(prov_scene_ids) > 0
                                and prov_scene_ids == cur_scene_ids
                                and prov_req_digests == cur_req_digests
                                and prov_clip_digests == cur_clip_digests
                            ):
                                clips_intact = True
                                for o in cur_ops:
                                    p_clip = Path(o.get("output_path", ""))
                                    if (
                                        not p_clip.is_file()
                                        or p_clip.stat().st_size == 0
                                        or compute_file_sha256(p_clip)
                                        != (o.get("output_sha256") or "").removeprefix("sha256:")
                                    ):
                                        clips_intact = False
                                        break
                                if clips_intact:
                                    comp_probe = probe_video(
                                        comp_path, decode_all=True
                                    )
                                    if (
                                        comp_probe.frame_count >= 1
                                        and abs(
                                            (comp_probe.width / comp_probe.height)
                                            - (9.0 / 16.0)
                                        )
                                        <= 0.08
                                    ):
                                        valid_reuse = True
                    except Exception:
                        valid_reuse = False

            if valid_reuse and not force_recompose:
                creative_assessment={}
                context_path=_workspace_path()/'creative_batches'/(job_id+'.json')
                current_context = json.loads(context_path.read_text(encoding='utf8')) if context_path.is_file() else None
                if not current_context:
                    stored = op_store.request_from_record(cur_ops[0]) if cur_ops[0].get('request_json') not in (None, '{}') else None
                    current_context = stored.provider_options.get('creative_package') if stored else None
                if (prov.get('qc_level') == 'high') and not current_context:
                    return fail_batch(code='FINAL_CONTENT_REQUIRED', error='High-QC render context is missing')
                if current_context:
                    try:
                        if not current_context.get('package_digest'):
                            raise ValueError('render package lineage is missing')
                        from video_factory.creative.agents.video_review import (
                            require_final_pass, review_production_final, technical_failure_code)
                        require_final_pass({'file_path':str(comp_path),'sha256':comp_sha})
                        creative_assessment = review_production_final(
                            path=comp_path, package=current_context, operations=cur_ops,
                            receipt_root=_workspace_path()/'creative_reviews')
                        failure_code = technical_failure_code('final',
                            content_verdict=(creative_assessment.get('final_review') or {}).get('verdict'),
                            technical_verdict=creative_assessment['technical_qc']['verdict'])
                        if failure_code:
                            return fail_batch(code=failure_code, error='Reused final failed current QC',
                                creative_failure={'assessment':creative_assessment,'package':current_context})
                    except (KeyError,ValueError,OSError):
                        return fail_batch(code='FINAL_TECHNICAL_FAIL',error='Completed bytes failed current final technical verification')
                return {
                    "ok": True,
                    "status": "completed",
                    "job_id": job_id,
                    "id": job_id,
                    "render_id": job_id,
                    "video_url": f"file://{comp_path.resolve()}",
                    "output_url": f"file://{comp_path.resolve()}",
                    "file_path": str(comp_path.resolve()),
                    "file_size": comp_path.stat().st_size,
                    "owner_user_id": batch.owner_user_id if batch else owner_id,
                    "project_id": batch.project_id if batch else proj_id,
                    "workflow_id": batch.workflow_id if batch else "",
                    "artifact_id": (
                        batch.composite_artifact_id
                        if batch and batch.composite_artifact_id
                        else comp_art_id
                    ),
                    "artifact_version": batch.version if batch else version,
                    "version": batch.version if batch else version,
                    "sha256": comp_sha,
                    "scene_provider_kinds": [o["provider_kind"] for o in cur_ops],
                    **creative_assessment,
                }

        # Query all scene operations for this batch
        op_conn = sqlite3.connect(_db_path())
        op_conn.row_factory = sqlite3.Row
        try:
            cur = op_conn.execute(
                "SELECT * FROM video_factory_scene_operation AS op WHERE op.render_batch_id = ? "
                "AND op.attempt = (SELECT MAX(other.attempt) FROM video_factory_scene_operation AS other "
                "WHERE other.render_batch_id = op.render_batch_id AND other.scene_id = op.scene_id) "
                "ORDER BY op.scene_order ASC",
                (job_id,),
            )
            scene_ops = [dict(r) for r in cur.fetchall()]
            scene_ops = _creative_batch_operations(op_conn, job_id) or scene_ops
            creative_package = None
            batch_provider_kind = None
            is_four_agent_batch = False
            if scene_ops:
                first_request_payload = json.loads(scene_ops[0].get("request_json") or "{}")
                if first_request_payload != {}:
                    first_request = op_store.request_from_record(scene_ops[0])
                    creative_package = first_request.provider_options.get('creative_package')
                    is_four_agent_batch = (
                        first_request.provider_options.get('creative_mode') == 'four_agent'
                    )
                context_path = _workspace_path()/'creative_batches'/(job_id+'.json')
                if context_path.is_file():
                    creative_package=json.loads(context_path.read_text(encoding='utf8'))
                if creative_package:
                    is_four_agent_batch = is_four_agent_batch or creative_package.get('creative_mode') == 'four_agent'
                    pinned_kind = str(
                        (creative_package.get('backend_profile') or {}).get('provider_kind') or ''
                    ).strip()
                    if is_four_agent_batch:
                        batch_provider_kind = pinned_kind
            if is_four_agent_batch:
                if batch_provider_kind not in OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS:
                    return {
                        'ok': False, 'status': 'failed', 'job_id': job_id,
                        'code': 'PROVIDER_BINDING_MISSING',
                        'error': 'PROVIDER_BINDING_MISSING: Four-agent batch has no durable backend provider pin',
                    }
                if provider_kind and provider_kind != batch_provider_kind:
                    return {
                        'ok': False, 'status': 'failed', 'job_id': job_id,
                        'code': 'PROVIDER_BINDING_MISMATCH',
                        'error': (
                            'PROVIDER_BINDING_MISMATCH: caller provider '
                            f'{provider_kind!r} differs from pinned batch provider {batch_provider_kind!r}'
                        ),
                    }
            elif batch is not None and not creative_package and provider_kind:
                batch_provider_kind = provider_kind
        finally:
            op_conn.close()

        if not scene_ops:
            return fail_batch(
                code="VF_NO_OPERATIONS",
                error=f"NO_OPERATIONS: No scene operations registered for batch {job_id}",
            )

        strict_content = bool(creative_package and creative_package.get('qc_level') == 'high')
        creative_critic = None
        clip_assessments = {}
        if strict_content:
            from video_factory.creative.agents.video_review import configured_content_critic
            creative_critic = configured_content_critic(creative_package, _workspace_path()/'creative_reviews')

        # Validate batch operation count, unique scene IDs, and contiguous scene_order 1..N
        if batch and batch.scene_count > 0 and len(scene_ops) != batch.scene_count:
            return fail_batch(
                code="OPERATION_COUNT_MISMATCH",
                error=(
                    f"OPERATION_COUNT_MISMATCH: Batch expects {batch.scene_count} "
                    f"scene operations, found {len(scene_ops)}"
                ),
            )
        scene_ids = [op["scene_id"] for op in scene_ops]
        if len(scene_ids) != len(set(scene_ids)):
            return fail_batch(
                code="DUPLICATE_SCENE_ID",
                error="DUPLICATE_SCENE_ID: Batch contains duplicate scene IDs",
            )
        orders = [op.get("scene_order", 0) for op in scene_ops]
        if sorted(orders) != list(range(1, len(orders) + 1)):
            return fail_batch(
                code="NON_CONTIGUOUS_SCENE_ORDER",
                error="NON_CONTIGUOUS_SCENE_ORDER: Scene operations must have contiguous orders 1..N",
            )

        completed_paths: list[Path] = []
        video_provider_source: Any | None = None
        allow_flow_fallback = is_four_agent_batch and batch_provider_kind == "google_flow"

        def handle_flow_rejection(operation: dict[str, Any] | None) -> dict[str, Any] | None:
            if operation is None or not allow_flow_fallback or not op_store.is_flow_activity_rejection(operation):
                return None
            try:
                source = video_provider_source
                if source is None:
                    source = _video_provider(provider_override=provider)
                vertex, error = _provider_for_operation(source, {"provider_kind": "vertex_ai"})
                if vertex is None or not VideoProviderSelector.is_available(vertex):
                    return fail_batch(
                        code=operation["error_code"],
                        error=f"{operation['error_message']}; Vertex fallback unavailable: {error or 'provider not ready'}",
                    )
                retry = op_store.fallback_rejected_flow_scene(operation["operation_id"])
                capacity_store.release_terminal("google_flow", operation["operation_id"])
            except (ValueError, RenderProviderConfigurationError) as exc:
                return fail_batch(
                    code="VERTEX_FALLBACK_UNAVAILABLE",
                    error=f"Flow rejected scene {operation['scene_id']}; could not prepare Vertex fallback: {exc}",
                )
            logger.info("Flow rejected %s (%s); Vertex fallback %s",
                        operation["operation_id"], operation["error_code"], retry["operation_id"])
            return {"ok": True, "status": "running", "job_id": job_id, "id": job_id}

        for op_index, op in enumerate(scene_ops):
            if op.get("error_code") == "OPERATOR_QUARANTINED_UNCERTAIN":
                return {
                    "ok": False, "status": "reconciliation_required", "job_id": job_id,
                    "id": job_id, "code": "RECONCILIATION_REQUIRED",
                    "error": "Inactive submission is quarantined with an unknown provider outcome; no redispatch",
                }
            op_status = op["status"]
            op_output = op.get("output_path") or ""
            stored_sha = op.get("output_sha256") or ""
            prov_op_id = op.get("provider_operation_id") or ""
            op_id = op.get("operation_id", "")
            scene_id = op.get("scene_id", "")

            durable_scene_provider = str(op.get("provider_kind") or "").strip()
            if (
                op_status == "planned"
                and batch_provider_kind
                and durable_scene_provider
                and durable_scene_provider != batch_provider_kind
                and not (allow_flow_fallback and op_store.is_rejected_flow_fallback(op))
            ):
                return {
                    "ok": False, "status": "failed", "job_id": job_id,
                    "code": "PROVIDER_BINDING_MISMATCH",
                    "error": (
                        f"PROVIDER_BINDING_MISMATCH: planned scene {scene_id} is pinned to "
                        f"{durable_scene_provider!r}, batch is pinned to {batch_provider_kind!r}"
                    ),
                }

            if op_status == "completed":
                # A terminal row without a validation digest is corrupt. Do not
                # attempt to repair it through a terminal no-op finalizer.
                if not stored_sha:
                    return fail_batch(
                        code="SCENE_DIGEST_MISSING",
                        error=f"SCENE_DIGEST_MISSING: Completed scene {scene_id} has empty digest",
                        operation_id=op_id,
                        persist_operation_failure=True,
                    )
                if not op_output:
                    return fail_batch(
                        code="SCENE_OUTPUT_MISSING",
                        error=f"SCENE_OUTPUT_MISSING: Completed scene {scene_id} is missing output path",
                        operation_id=op_id,
                        persist_operation_failure=True,
                    )
                p_out = Path(op_output)
                if not p_out.is_file() or p_out.stat().st_size == 0:
                    return fail_batch(
                        code="SCENE_OUTPUT_MISSING",
                        error=f"SCENE_OUTPUT_MISSING: Completed scene file missing or empty: {op_output}",
                        operation_id=op_id,
                        persist_operation_failure=True,
                    )
                disk_sha = compute_file_sha256(p_out)
                if disk_sha != stored_sha.removeprefix("sha256:"):
                    _invalidate_final_approval(
                        srv,
                        op.get("owner_user_id")
                        or (batch.owner_user_id if batch else "default-owner"),
                        op.get("project_id") or (batch.project_id if batch else ""),
                        f"Invalidated due to clip digest mismatch on {scene_id}",
                    )
                    return fail_batch(
                        code="SCENE_DIGEST_MISMATCH",
                        error=f"SCENE_DIGEST_MISMATCH: Scene clip {scene_id} was modified on disk",
                        operation_id=op_id,
                        persist_operation_failure=True,
                    )
                expected_duration = float(op.get("expected_duration_seconds") or 0)
                expected_aspect = str(op.get("expected_aspect_ratio") or "")
                if expected_duration <= 0 or not expected_aspect:
                    return fail_batch(
                        code="VALIDATION_CONTRACT_MISSING",
                        error=(
                            f"VALIDATION_CONTRACT_MISSING: Completed scene {scene_id} "
                            "has no durable validation contract"
                        ),
                        operation_id=op_id,
                        persist_operation_failure=True,
                    )
                try:
                    validation = validate_scene_output(
                        output_path=p_out,
                        expected_duration=expected_duration,
                        expected_aspect_ratio=expected_aspect,
                        duration_tolerance=0.5,
                    )
                except ValueError as exc:
                    _invalidate_final_approval(
                        srv,
                        op.get("owner_user_id")
                        or (batch.owner_user_id if batch else "default-owner"),
                        op.get("project_id") or (batch.project_id if batch else ""),
                        f"Invalidated due to validation failure on {scene_id}",
                    )
                    return fail_batch(
                        code="VALIDATION_FAILED",
                        error=f"SCENE_VALIDATION_FAILED: {scene_id} - {exc}",
                        operation_id=op_id,
                        persist_operation_failure=True,
                    )
                if strict_content:
                    from video_factory.creative.agents.video_review import review_video, technical_failure_code
                    from video_factory.creative.agents.continuity import scene_start_bindings
                    assessment = review_video(path=p_out, package=creative_package, critic=creative_critic,
                        stage='clip', scene_id=scene_id, start_bindings=scene_start_bindings(scene_ops))
                    code = technical_failure_code('clip',
                        content_verdict=assessment['clip_review']['verdict'],
                        technical_verdict=assessment['technical_qc']['verdict'])
                    if code:
                        return fail_batch(code=code, error=f'{scene_id} did not pass current clip QC',
                            creative_failure={'assessment':assessment,'package':creative_package})
                    clip_assessments[scene_id] = assessment
                completed_paths.append(p_out)
                continue

            if op_status == "failed":
                fallback = handle_flow_rejection(op)
                if fallback is not None:
                    return fallback
                return fail_batch(
                    code=op.get("error_code") or "SCENE_FAILED",
                    error=(
                        f"SCENE_FAILED: {scene_id} - "
                        f"{op.get('error_message') or 'durable scene operation is failed'}"
                    ),
                )

            if op_status == "planned":
                try:
                    planned_request = op_store.request_from_record(op)
                    if strict_content:
                        from video_factory.creative.agents.continuity import (
                            continues_previous_clip, prepare_continuity_start)
                        scene = next(scene for scene in creative_package['scenes'] if scene['scene_id'] == scene_id)
                        if continues_previous_clip(scene):
                            if not op_index:
                                raise ValueError('First scene cannot continue a predecessor')
                            previous = scene_ops[op_index - 1]
                            planned_request, endpoint_failure = prepare_continuity_start(
                                package=creative_package, scene=scene, operation=op, previous=previous,
                                store=op_store, critic=creative_critic,
                                output_dir=_workspace_path()/'continuity',
                                clip_assessment=clip_assessments[previous['scene_id']])
                            if endpoint_failure:
                                verdict = endpoint_failure['clip_review']['verdict']
                                return fail_batch(code=f'CLIP_CONTENT_{verdict}',
                                    error=f'{scene_id} continuity endpoint did not pass QC',
                                    creative_failure={'assessment':endpoint_failure,'package':creative_package})
                            scene_ops[op_index] = op_store.get(op_id)
                    if video_provider_source is None:
                        video_provider_source = _video_provider(provider_override=provider)
                    if video_provider_source is None:
                        raise ValueError("selected provider is unavailable")
                    planned_result = SceneRenderRuntime(
                        store=op_store,
                        provider=video_provider_source,
                        preferred_provider_kind=durable_scene_provider or batch_provider_kind or provider_kind,
                        allow_pre_submit_fallback=not bool(batch_provider_kind or provider_kind),
                        capacity_store=capacity_store,
                        capacity_limit=None,
                    ).run(planned_request)
                except TerminalJobError as exc:
                    if exc.code in {"GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN", "RECONCILIATION_REQUIRED", "UNKNOWN_PAID_OUTCOME"}:
                        return {
                            "ok": False,
                            "status": "reconciliation_required",
                            "job_id": job_id,
                            "id": job_id,
                            "code": exc.code or "RECONCILIATION_REQUIRED",
                            "error": str(exc),
                        }
                    return fail_batch(
                        code=exc.code or "SCENE_FAILED",
                        error=str(exc),
                    )
                except (ValueError, RenderProviderConfigurationError) as exc:
                    return {
                        "ok": False, "status": "reconciliation_required", "job_id": job_id,
                        "code": "RECONCILIATION_REQUIRED", "error": str(exc),
                    }
                if planned_result.error_code == "PROVIDER_CAPACITY_WAIT":
                    return {"ok": True, "status": "running", "job_id": job_id, "id": job_id}
                if planned_result.status == "completed":
                    return get_job_status(job_id, provider=provider, provider_kind=provider_kind)
                if planned_result.status in {"submitted", "running"} and planned_result.provider_operation_id:
                    return {"ok": True, "status": "running", "job_id": job_id, "id": job_id}
                if (
                    planned_result.status == "unknown"
                    or planned_result.error_code in ("RECONCILIATION_REQUIRED", "GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN", "NETWORK_UNKNOWN_OUTCOME")
                ):
                    return {
                        "ok": False,
                        "status": "reconciliation_required",
                        "job_id": job_id,
                        "id": job_id,
                        "code": planned_result.error_code or "RECONCILIATION_REQUIRED",
                        "error": planned_result.error_message or "provider outcome is uncertain; reconcile before retrying",
                    }
                fallback = handle_flow_rejection(op_store.get(op_id))
                if fallback is not None:
                    return fallback
                return fail_batch(
                    code=planned_result.error_code or "SCENE_FAILED",
                    error=planned_result.error_message or "planned scene submit failed",
                )

            if video_provider_source is None:
                try:
                    video_provider_source = _video_provider(provider_override=provider)
                except RenderProviderConfigurationError as exc:
                    return {
                        "ok": False,
                        "status": "reconciliation_required",
                        "job_id": job_id,
                        "id": job_id,
                        "error": str(exc),
                        "code": "RECONCILIATION_REQUIRED",
                    }

            # A submitting checkpoint without an operation ID is the crash
            # window between provider acceptance and durable checkpointing. It
            # must be reconciled through the pinned provider, never reported as
            # indefinitely running and never resubmitted here.
            if not prov_op_id:
                if op_status != "submitting":
                    return {
                        "ok": False,
                        "status": "reconciliation_required",
                        "job_id": job_id,
                        "id": job_id,
                        "error": (
                            f"RECONCILIATION_REQUIRED: Scene {scene_id} is in "
                            f"status {op_status!r} without a provider operation ID"
                        ),
                        "code": "RECONCILIATION_REQUIRED",
                    }
                video_prov, provider_error = _provider_for_operation(
                    video_provider_source, op,
                    provider_kind=None if op.get("provider_kind") else provider_kind,
                )
                if video_prov is None:
                    return {
                        "ok": False,
                        "status": "reconciliation_required",
                        "job_id": job_id,
                        "id": job_id,
                        "error": (
                            "RECONCILIATION_REQUIRED: "
                            f"{provider_error or 'pinned provider is unavailable'}"
                        ),
                        "code": "RECONCILIATION_REQUIRED",
                    }
                lookup = getattr(video_prov, "lookup_by_request_id", None)
                if not callable(lookup):
                    return {
                        "ok": False,
                        "status": "reconciliation_required",
                        "job_id": job_id,
                        "id": job_id,
                        "error": (
                            "RECONCILIATION_REQUIRED: pinned provider does not "
                            "support request-id reconciliation"
                        ),
                        "code": "RECONCILIATION_REQUIRED",
                    }
                if not acquire_poll_capacity(
                    op_id, video_prov, str(op.get("provider_kind") or ""), lookup_only=True
                ):
                    return {"ok": True, "status": "running", "job_id": job_id,
                            "id": job_id, "code": "PROVIDER_CAPACITY_WAIT"}
                try:
                    reconciled = lookup(op_id)
                except Exception as exc:
                    reconciled = None
                    provider_error = str(exc)
                try:
                    reconciled = validate_reconciled_result(
                        expected_request_id=op_id,
                        durable_provider_kind=str(op.get("provider_kind") or ""),
                        result=reconciled,
                    )
                except ProviderSelectionError as exc:
                    return {
                        "ok": False,
                        "status": "reconciliation_required",
                        "job_id": job_id,
                        "id": job_id,
                        "error": (
                            "RECONCILIATION_REQUIRED: "
                            f"{exc}"
                        ),
                        "code": "RECONCILIATION_REQUIRED",
                    }
                try:
                    op_store.bind_provider_operation_id(
                        op_id, reconciled.provider_operation_id
                    )
                    capacity_store.bind_provider_operation(
                        str(op.get("provider_kind") or ""), op_id,
                        reconciled.provider_operation_id,
                    )
                    final_state = op_store.finalize_provider_result(op_id, reconciled)
                except ValueError as exc:
                    return {
                        "ok": False,
                        "status": "reconciliation_required",
                        "job_id": job_id,
                        "id": job_id,
                        "error": f"RECONCILIATION_REQUIRED: {exc}",
                        "code": "RECONCILIATION_REQUIRED",
                    }
                if final_state.get("status") == "completed":
                    capacity_store.release_terminal(str(op.get("provider_kind") or ""), op_id)
                    return get_job_status(
                        job_id,
                        provider=provider,
                        provider_kind=provider_kind,
                    )
                if final_state.get("status") == "failed":
                    capacity_store.release_terminal(str(op.get("provider_kind") or ""), op_id)
                    fallback = handle_flow_rejection(final_state)
                    if fallback is not None:
                        return fallback
                    return fail_batch(
                        code=final_state.get("error_code") or "SCENE_FAILED",
                        error=(
                            f"SCENE_FAILED: {scene_id} - "
                            f"{final_state.get('error_message') or 'reconciled provider operation failed'}"
                        ),
                    )
                return {
                    "ok": True,
                    "status": "running",
                    "job_id": job_id,
                    "id": job_id,
                }

            video_prov, provider_error = _provider_for_operation(
                video_provider_source, op,
                provider_kind=None if op.get("provider_kind") else provider_kind,
            )
            if video_prov is None:
                legacy_provider = bool(
                    provider_error
                    and provider_error.startswith(
                        "LEGACY_PROVIDER_RECONCILIATION_REQUIRED"
                    )
                )
                return {
                    "ok": False,
                    "status": (
                        "reconciliation_required" if legacy_provider else "failed"
                    ),
                    "job_id": job_id,
                    "error": f"PROVIDER_UNAVAILABLE: {provider_error or 'No live video render provider configured or active'}",
                    "code": (
                        "LEGACY_PROVIDER_RECONCILIATION_REQUIRED"
                        if legacy_provider
                        else "VF_PROVIDER_SELECTION_MISSING"
                    ),
                }

            if not acquire_poll_capacity(
                op_id, video_prov, str(op.get("provider_kind") or "")
            ):
                return {"ok": True, "status": "running", "job_id": job_id,
                        "id": job_id, "code": "PROVIDER_CAPACITY_WAIT"}
            capacity_store.bind_provider_operation(
                str(op.get("provider_kind") or ""), op_id, prov_op_id
            )
            sub_res = video_prov.get_status(prov_op_id)
            if sub_res.status == "running":
                return {
                    "ok": True,
                    "status": "running",
                    "job_id": job_id,
                    "id": job_id,
                }

            if sub_res.status == "failed":
                if sub_res.error_code in {"RECONCILIATION_REQUIRED", "NETWORK_UNKNOWN_OUTCOME"}:
                    return {
                        "ok": False, "status": "reconciliation_required", "job_id": job_id,
                        "id": job_id, "code": "RECONCILIATION_REQUIRED",
                        "error": sub_res.error_message or "Provider session cannot poll the pinned operation",
                    }
                final_state = op_store.finalize_provider_result(op_id, sub_res)
                capacity_store.release_terminal(str(op.get("provider_kind") or ""), op_id)
                durable_state = op_store.get(op_id) or final_state
                fallback = handle_flow_rejection(durable_state)
                if fallback is not None:
                    return fallback
                return fail_batch(
                    code=durable_state.get("error_code")
                    or sub_res.error_code
                    or "SCENE_FAILED",
                    error=(
                        f"SCENE_FAILED: {scene_id} - "
                        f"{durable_state.get('error_message') or sub_res.error_message or 'unknown error'}"
                    ),
                )

            if sub_res.status == "completed":
                try:
                    op_store.finalize_provider_result(op_id, sub_res)
                    capacity_store.release_terminal(str(op.get("provider_kind") or ""), op_id)
                except ValueError as exc:
                    return fail_batch(
                        code="FINALIZATION_FAILED",
                        error=f"FINALIZATION_FAILED: {scene_id} - {exc}",
                        operation_id=op_id,
                        persist_operation_failure=True,
                    )
                durable_state = op_store.get(op_id)
                if durable_state is None:
                    return fail_batch(
                        code="OPERATION_NOT_FOUND",
                        error=f"OPERATION_NOT_FOUND: {op_id}",
                    )
                scene_ops[op_index] = durable_state
                durable_sha = durable_state.get("output_sha256") or ""
                if durable_state.get("status") != "completed" or not durable_sha:
                    return fail_batch(
                        code=durable_state.get("error_code") or "FINALIZATION_FAILED",
                        error=(
                            f"SCENE_VALIDATION_FAILED: {scene_id} - "
                            f"{durable_state.get('error_message') or 'validation did not produce completed state'}"
                        ),
                    )
                durable_output = durable_state.get("output_path") or ""
                p_out = Path(durable_output)
                if (
                    not p_out.is_file()
                    or p_out.stat().st_size == 0
                    or compute_file_sha256(p_out) != durable_sha.removeprefix("sha256:")
                ):
                    return fail_batch(
                        code="SCENE_DIGEST_MISMATCH",
                        error=f"SCENE_DIGEST_MISMATCH: Finalized scene {scene_id} is not intact",
                        operation_id=op_id,
                        persist_operation_failure=True,
                    )
                if strict_content:
                    return get_job_status(job_id, provider=provider, provider_kind=provider_kind)
                completed_paths.append(p_out)
                continue

            if (
                sub_res.status == "unknown"
                or sub_res.error_code in {"RECONCILIATION_REQUIRED", "NETWORK_UNKNOWN_OUTCOME"}
            ):
                return {
                    "ok": False,
                    "status": "reconciliation_required",
                    "job_id": job_id,
                    "id": job_id,
                    "code": sub_res.error_code or "RECONCILIATION_REQUIRED",
                    "error": sub_res.error_message or "provider outcome is uncertain; reconcile before retrying",
                }

            # Any other status (unknown, etc.)
            return fail_batch(
                code="SCENE_UNKNOWN_STATUS",
                error=f"SCENE_UNKNOWN_STATUS: {scene_id} - {sub_res.status}",
                operation_id=op_id,
                persist_operation_failure=True,
            )

        # All scenes completed -> build timeline and composite in canonical workspace layout
        proj_id = batch.project_id if batch else scene_ops[0]["project_id"]
        version = batch.version if batch else 1
        owner_id = batch.owner_user_id if batch else scene_ops[0]["owner_user_id"]

        comp_dir = ws.get_composite_dir(proj_id, version)
        comp_dir.mkdir(parents=True, exist_ok=True)
        comp_path = comp_dir / "draft.mp4"

        # Ingest scene clips into project layout with validation
        canonical_clips: list[Path] = []
        timeline_clips: list[TimelineClip] = []
        updated_scenes_list: list[GeneratedScene] = []
        for idx, (op, raw_clip_path) in enumerate(
            zip(scene_ops, completed_paths), start=1
        ):
            scene_id = op["scene_id"]
            attempt = int(op.get("attempt") or 1)
            dest_clip_path = ws.get_rendered_clip_path(
                proj_id, version, scene_id, attempt=attempt
            )
            if not dest_clip_path.is_file() or compute_file_sha256(
                dest_clip_path
            ) != compute_file_sha256(raw_clip_path):
                ws.atomic_copy(raw_clip_path, dest_clip_path)

            clip_sha = compute_file_sha256(dest_clip_path)
            # Validate clip decodability and aspect ratio
            try:
                probe = probe_video(dest_clip_path, decode_all=True)
                clip_ratio = probe.width / probe.height if probe.height > 0 else 0
                if abs(clip_ratio - (9.0 / 16.0)) > 0.08:
                    return {
                        "ok": False,
                        "status": "failed",
                        "job_id": job_id,
                        "error": f"INVALID_CLIP_GEOMETRY: Clip for {scene_id} has invalid aspect ratio {clip_ratio:.3f} (must be 9:16)",
                        "code": "INVALID_CLIP_GEOMETRY",
                    }
                if probe.duration_seconds < 1.0:
                    return {
                        "ok": False,
                        "status": "failed",
                        "job_id": job_id,
                        "error": f"INVALID_CLIP_DURATION: Clip for {scene_id} duration {probe.duration_seconds:.2f}s is less than 1.0s",
                        "code": "INVALID_CLIP_DURATION",
                    }
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "failed",
                    "job_id": job_id,
                    "error": f"CORRUPT_CLIP: Failed to decode clip for {scene_id}: {exc}",
                    "code": "CORRUPT_CLIP",
                }

            canonical_clips.append(dest_clip_path)
            clip_art_id = f"art_clip_{proj_id}_v{version}_{scene_id}"
            if attempt > 1:
                clip_art_id += f"_a{attempt}"
            if hasattr(srv.repository, "save_artifact"):
                srv.repository.save_artifact(
                    VideoFactoryArtifact(
                        artifact_id=clip_art_id,
                        owner_user_id=owner_id,
                        workflow_id=batch.workflow_id if batch else "",
                        project_id=proj_id,
                        kind="clip",
                        scene_id=scene_id,
                        version=version,
                        attempt=attempt,
                        relative_path=ws.to_relative_path(dest_clip_path),
                        sha256=clip_sha,
                        size=dest_clip_path.stat().st_size,
                        mime_type="video/mp4",
                        status="active",
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )

            timeline_clips.append(
                TimelineClip(
                    clip_id=f"tclip_{scene_id}",
                    order=idx,
                    source_asset_id=clip_art_id,
                    duration_seconds=probe.duration_seconds,
                )
            )

            clip_dur = int(round(probe.duration_seconds))
            if clip_dur <= 0:
                clip_dur = 4

            proj = srv.get_project(owner_id, proj_id)
            existing_gs_map = {gs.scene_id: gs for gs in (proj.generated_scenes or ())}
            existing_gs = existing_gs_map.get(scene_id)
            v_prompt = (
                existing_gs.video_prompt
                if (existing_gs and existing_gs.video_prompt)
                else VideoPrompt(scene_id=scene_id, duration_seconds=clip_dur)
            )

            updated_scenes_list.append(
                GeneratedScene(
                    scene_id=scene_id,
                    video_prompt=v_prompt,
                    generated_asset_id=clip_art_id,
                    generation_status=VideoGenerationStatus.COMPLETED,
                    generation_job_id=job_id,
                    provider_operation_id=op.get("provider_operation_id"),
                    version=version,
                    created_at=existing_gs.created_at
                    if existing_gs and existing_gs.created_at
                    else _now(),
                    updated_at=_now(),
                )
            )

        comp_art_id = f"art_composite_{proj_id}_v{version}"
        stored_comp_art = (
            srv.repository.get_artifact(comp_art_id, owner_user_id=owner_id)
            if hasattr(srv.repository, "get_artifact")
            else None
        )
        need_rebuild = not comp_path.is_file() or comp_path.stat().st_size == 0
        if not need_rebuild:
            current_comp_sha = compute_file_sha256(comp_path)
            if (
                stored_comp_art is None
                or stored_comp_art.owner_user_id != owner_id
                or stored_comp_art.project_id != proj_id
                or stored_comp_art.version != version
                or stored_comp_art.sha256 != current_comp_sha
            ):
                need_rebuild = True
            else:
                prov = (stored_comp_art.provider_metadata or {}).get("provenance") or {}
                prov_scene_ids = prov.get("scene_ids")
                prov_req_digests = prov.get("request_digests")
                prov_clip_digests = prov.get("clip_digests")
                cur_scene_ids = [o["scene_id"] for o in scene_ops]
                cur_req_digests = [o.get("request_digest") for o in scene_ops]
                cur_clip_digests = [o.get("output_sha256") for o in scene_ops]
                if (
                    not isinstance(prov_scene_ids, list)
                    or not isinstance(prov_req_digests, list)
                    or not isinstance(prov_clip_digests, list)
                    or len(prov_scene_ids) != len(cur_scene_ids)
                    or len(prov_scene_ids) == 0
                    or prov_scene_ids != cur_scene_ids
                    or prov_req_digests != cur_req_digests
                    or prov_clip_digests != cur_clip_digests
                ):
                    need_rebuild = True
                else:
                    try:
                        comp_probe = probe_video(comp_path, decode_all=True)
                        if (
                            comp_probe.frame_count < 1
                            or abs((comp_probe.width / comp_probe.height) - (9.0 / 16.0))
                            > 0.08
                        ):
                            need_rebuild = True
                    except Exception:
                        need_rebuild = True

        if force_recompose:
            need_rebuild=True
        if need_rebuild:
            staging_comp = comp_dir / ".draft_staging.mp4"
            try:
                concatenate_videos(
                    canonical_clips,
                    staging_comp,
                    target_width=1080 if creative_package else 720,
                    target_height=1920 if creative_package else 1280,
                    target_fps=30 if creative_package else 24,
                    **({'silent':True} if creative_package else {}),
                )
                # Verify staging composite decodability before promotion
                comp_probe = probe_video(staging_comp, decode_all=True)
                if (
                    comp_probe.frame_count < 1
                    or abs((comp_probe.width / comp_probe.height) - (9.0 / 16.0)) > 0.08
                ):
                    if staging_comp.exists():
                        staging_comp.unlink(missing_ok=True)
                    return fail_batch(
                        code="INVALID_COMPOSITE_GEOMETRY",
                        error="INVALID_COMPOSITE_GEOMETRY: Composite failed 9:16 geometry or decode validation",
                    )
            except Exception as exc:
                if staging_comp.exists():
                    staging_comp.unlink(missing_ok=True)
                return fail_batch(
                    code="COMPOSITION_FAILED",
                    error=f"COMPOSITION_FAILED: {exc}",
                )

            # Invalidate Final Approval BEFORE promoting replacement composite
            try:
                _invalidate_final_approval(
                    srv, owner_id, proj_id, "Invalidated due to composite rebuild"
                )
            except Exception as exc:
                if staging_comp.exists():
                    staging_comp.unlink(missing_ok=True)
                return fail_batch(
                    code="FINAL_APPROVAL_INVALIDATION_FAILED",
                    error=f"FINAL_APPROVAL_INVALIDATION_FAILED: Failed to persist approval invalidation: {exc}",
                )

            # Atomic promote only after decodability and invalidation succeed
            try:
                os.replace(staging_comp, comp_path)
            except Exception as exc:
                if staging_comp.exists():
                    staging_comp.unlink(missing_ok=True)
                return fail_batch(
                    code="COMPOSITE_PROMOTION_FAILED",
                    error=f"COMPOSITE_PROMOTION_FAILED: Failed to promote staging composite: {exc}",
                )

        composite_sha = compute_file_sha256(comp_path)
        creative_assessment = {}
        if creative_package:
            from video_factory.creative.agents.video_review import review_production_final, technical_failure_code
            creative_assessment = review_production_final(path=comp_path, package=creative_package,
                operations=scene_ops, receipt_root=_workspace_path()/'creative_reviews')
            failure_code=technical_failure_code('final',
                content_verdict=(creative_assessment.get('final_review') or {}).get('verdict'),
                technical_verdict=creative_assessment['technical_qc']['verdict'])
            if failure_code:
                return fail_batch(code=failure_code,
                    error='Final content or technical QC blocked export',
                    creative_failure={'assessment':creative_assessment,'package':creative_package})
        comp_provenance = {
            "scene_ids": [o["scene_id"] for o in scene_ops],
            "request_digests": [o.get("request_digest") for o in scene_ops],
            "clip_digests": [o.get("output_sha256") for o in scene_ops],
            "provider_kinds": [o.get("provider_kind") for o in scene_ops],
        }
        if creative_package:
            comp_provenance['qc_level'] = creative_package.get('qc_level', 'middle')
            comp_provenance['package_digest'] = creative_package['package_digest']
        try:
            if hasattr(srv.repository, "save_artifact"):
                srv.repository.save_artifact(
                    VideoFactoryArtifact(
                        artifact_id=comp_art_id,
                        owner_user_id=owner_id,
                        workflow_id=batch.workflow_id if batch else "",
                        project_id=proj_id,
                        kind="composite",
                        version=version,
                        attempt=1,
                        relative_path=ws.to_relative_path(comp_path),
                        sha256=composite_sha,
                        size=comp_path.stat().st_size,
                        mime_type="video/mp4",
                        status="active",
                        provider_metadata={"provenance": comp_provenance},
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )

            timeline_art_id = f"art_timeline_{proj_id}_v{version}"
            timeline_obj = Timeline(
                timeline_id=f"timeline_{proj_id}_v{version}",
                project_id=proj_id,
                clips=tuple(timeline_clips),
                status=TimelineStatus.COMPLETED,
                version=version,
                created_at=_now(),
                updated_at=_now(),
            )

            # Update project timeline and generated scenes BEFORE marking batch completed
            proj = srv.get_project(owner_id, proj_id)
            srv.repository.save(
                replace(
                    proj,
                    generated_scenes=tuple(updated_scenes_list),
                    timeline=timeline_obj,
                    draft_video_asset_id=comp_art_id,
                    status=ProjectStatus.DRAFT_VIDEO_READY,
                    updated_at=_now(),
                )
            )

            if batch is not None and hasattr(srv.repository, "save_render_batch"):
                srv.repository.save_render_batch(
                    replace(
                        batch,
                        status="completed",
                        completed_scene_count=len(completed_paths),
                        timeline_artifact_id=timeline_art_id,
                        composite_artifact_id=comp_art_id,
                        updated_at=_now(),
                    )
                )
        except Exception as exc:
            return fail_batch(
                code="METADATA_PERSISTENCE_FAILED",
                error=f"METADATA_PERSISTENCE_FAILED: Failed to persist completion metadata: {exc}",
            )

        return {
            "ok": True,
            "status": "completed",
            "job_id": job_id,
            "id": job_id,
            "render_id": job_id,
            "video_url": f"file://{comp_path.resolve()}",
            "output_url": f"file://{comp_path.resolve()}",
            "file_path": str(comp_path.resolve()),
            "file_size": comp_path.stat().st_size,
            "owner_user_id": batch.owner_user_id if batch else owner_id,
            "project_id": batch.project_id if batch else proj_id,
            "workflow_id": batch.workflow_id if batch else "",
            "artifact_id": (
                batch.composite_artifact_id
                if batch and batch.composite_artifact_id
                else comp_art_id
            ),
            "artifact_version": batch.version if batch else version,
            "version": batch.version if batch else version,
            "sha256": composite_sha,
            "scene_provider_kinds": [o.get("provider_kind") for o in scene_ops],
            **creative_assessment,
        }

    # Ungoverned single-scene job path is DISABLED.
    return {
        "ok": False,
        "status": "failed",
        "job_id": job_id,
        "error": "UNGOVERNED_POLL: Single-scene polling without batch context is disabled for production integrity.",
        "code": "VF_UNGOVERNED_POLL",
    }


def export(
    render_result: dict[str, Any] | None = None,
    export_params: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    owner_user_id: str = "default_user",
    project_id: str = "",
    workflow_id: str | None = None,
    approval_request_id: str | None = None,
    target_workflow_id: str | None = None,
) -> dict[str, Any]:
    """Hermes wire compatibility adapter for deterministic local video export.

    Promotes a completed, verified local MP4 from render output to workspace exports.
    Performs zero provider calls and is strictly idempotent.
    """
    try:
        rend_data = _unpack_payload(render_result)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": str(exc),
            "code": "INVALID_PAYLOAD",
        }

    params = dict(export_params or {})
    if rend_data.get('creative_mode')=='four_agent':
        from video_factory.creative.agents.video_review import require_final_pass, require_final_content_pass
        try:
            require_final_pass(rend_data)
        except (KeyError, ValueError, OSError):
            return {'ok':False,'status':'failed','code':'FINAL_TECHNICAL_REQUIRED',
                    'error':'Export requires decoded video, current technical profile and matching final SHA'}
        lineage = rend_data.get('creative_lineage') or {}
        if rend_data.get('qc_level', lineage.get('qc_level')) == 'high':
            try:
                require_final_content_pass(rend_data)
            except (KeyError, ValueError, OSError):
                return {'ok':False,'status':'failed','code':'FINAL_CONTENT_REQUIRED',
                        'error':'High-QC export requires current final content PASS'}

    required_lineage = (
        "owner_user_id",
        "project_id",
        "workflow_id",
        "artifact_id",
        "artifact_version",
        "sha256",
    )
    missing_lineage = [
        field
        for field in required_lineage
        if field not in rend_data
        or rend_data[field] is None
        or (isinstance(rend_data[field], str) and not rend_data[field].strip())
    ]
    if missing_lineage:
        return {
            "ok": False,
            "status": "failed",
            "error": "RENDER_LINEAGE_REQUIRED: render_result is missing canonical fields: "
            + ", ".join(missing_lineage),
            "code": "RENDER_LINEAGE_REQUIRED",
        }

    owner = str(rend_data["owner_user_id"]).strip()
    proj_id = str(rend_data["project_id"]).strip()
    target_wf_id = str(rend_data["workflow_id"]).strip()
    artifact_id = str(rend_data["artifact_id"]).strip()
    export_version_raw = rend_data["artifact_version"]
    if (
        isinstance(export_version_raw, bool)
        or not isinstance(export_version_raw, int)
        or export_version_raw < 1
    ):
        return {
            "ok": False,
            "status": "failed",
            "error": "RENDER_LINEAGE_INVALID: artifact_version must be a positive integer",
            "code": "RENDER_LINEAGE_INVALID",
        }
    export_version = export_version_raw
    compatibility_version = rend_data.get("version")
    if compatibility_version is not None and compatibility_version != export_version:
        return {
            "ok": False,
            "status": "failed",
            "error": "RENDER_LINEAGE_MISMATCH: version does not match artifact_version",
            "code": "RENDER_LINEAGE_MISMATCH",
        }

    rend_sha = str(rend_data["sha256"]).strip().removeprefix("sha256:").lower()
    if len(rend_sha) != 64 or any(ch not in "0123456789abcdef" for ch in rend_sha):
        return {
            "ok": False,
            "status": "failed",
            "error": "RENDER_LINEAGE_INVALID: sha256 must be a 64-character hexadecimal digest",
            "code": "RENDER_LINEAGE_INVALID",
        }

    supplied_expectations: list[tuple[str, Any]] = [
        (
            "owner_user_id",
            owner_user_id if owner_user_id and owner_user_id != "default_user" else None,
        ),
        ("project_id", project_id or None),
        (
            "workflow_id",
            workflow_id
            or target_workflow_id
            or params.get("workflow_id"),
        ),
        ("artifact_id", params.get("artifact_id")),
        (
            "artifact_version",
            params.get("artifact_version")
            if params.get("artifact_version") is not None
            else params.get("version"),
        ),
        ("sha256", params.get("sha256")),
    ]
    canonical_values: dict[str, Any] = {
        "owner_user_id": owner,
        "project_id": proj_id,
        "workflow_id": target_wf_id,
        "artifact_id": artifact_id,
        "artifact_version": export_version,
        "sha256": rend_sha,
    }
    for field, expected in supplied_expectations:
        if expected is None or (isinstance(expected, str) and not expected.strip()):
            continue
        normalized_expected: Any = expected
        if field == "sha256":
            normalized_expected = str(expected).strip().removeprefix("sha256:").lower()
        elif field != "artifact_version":
            normalized_expected = str(expected).strip()
        if normalized_expected != canonical_values[field]:
            return {
                "ok": False,
                "status": "failed",
                "error": f"RENDER_LINEAGE_MISMATCH: caller {field} does not match render_result",
                "code": "RENDER_LINEAGE_MISMATCH",
            }

    # Extract source video path from render result FIRST
    raw_video_url = (
        rend_data.get("video_url")
        or rend_data.get("output_url")
        or rend_data.get("file_path")
    )
    if not raw_video_url:
        return {
            "ok": False,
            "status": "failed",
            "error": "MISSING_RENDER_OUTPUT: render_result must contain video_url or file_path",
            "code": "MISSING_RENDER_OUTPUT",
        }

    src_path = _normalize_file_path(raw_video_url)

    if not src_path.is_file() or src_path.stat().st_size == 0:
        return {
            "ok": False,
            "status": "failed",
            "error": f"FILE_NOT_FOUND: Source video file does not exist or is empty: {src_path}",
            "code": "FILE_NOT_FOUND",
        }

    if src_path.suffix.lower() != ".mp4":
        return {
            "ok": False,
            "status": "failed",
            "error": f"INVALID_FORMAT: Video file must be an .mp4 file, got '{src_path.suffix}'",
            "code": "INVALID_FORMAT",
        }

    # Verify project final approval and ownership via canonical service
    srv = _service()
    try:
        project = srv.get_project(owner, proj_id)
    except ValueError:
        return {
            "ok": False,
            "status": "failed",
            "error": f"PROJECT_NOT_FOUND: Project '{proj_id}' does not exist for owner '{owner}'",
            "code": "PROJECT_NOT_FOUND",
        }

    if project.final_approval != FinalApprovalStatus.APPROVED:
        return {
            "ok": False,
            "status": "failed",
            "error": f"FINAL_APPROVAL_REQUIRED: Project '{proj_id}' must have Final approval status 'APPROVED' before export",
            "code": "FINAL_APPROVAL_REQUIRED",
        }

    # Fail before promotion when identity cannot support a truthful caption.
    prod_name = _caption_product_name(project)
    if not prod_name:
        return {
            "ok": False,
            "status": "failed",
            "error": "PRODUCT_IDENTITY_REQUIRED: Export caption requires a truthful product name, not a pack/project ID or generic fallback",
            "code": "PRODUCT_IDENTITY_REQUIRED",
        }

    # Ensure source video is decodable before proceeding with QC lookup or packaging
    try:
        probe_video(src_path, decode_all=True)
    except InvalidVideoArtifact as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"INVALID_VIDEO_ARTIFACT: Source MP4 cannot be fully decoded: {exc}",
            "code": "INVALID_VIDEO_ARTIFACT",
        }

    # Verify Video QC from QCReportRepository directly (fail-closed)
    source_sha256 = compute_file_sha256(src_path)
    if rend_sha != source_sha256:
        return {
            "ok": False,
            "status": "failed",
            "error": f"LINEAGE_SHA_MISMATCH: Render SHA ({rend_sha}) does not match source video file SHA ({source_sha256})",
            "code": "LINEAGE_SHA_MISMATCH",
        }

    canonical_artifact = srv.repository.get_artifact(artifact_id, owner)
    if canonical_artifact is None:
        return {
            "ok": False,
            "status": "failed",
            "error": "RENDER_LINEAGE_ARTIFACT_NOT_FOUND: canonical composite artifact is missing",
            "code": "RENDER_LINEAGE_ARTIFACT_NOT_FOUND",
        }
    artifact_mismatches = {
        "project_id": canonical_artifact.project_id != proj_id,
        "workflow_id": canonical_artifact.workflow_id != target_wf_id,
        "artifact_version": canonical_artifact.version != export_version,
        "sha256": canonical_artifact.sha256.removeprefix("sha256:") != source_sha256,
        "kind": canonical_artifact.kind != "composite",
        "status": canonical_artifact.status != "active",
    }
    failed_artifact_fields = [
        field for field, mismatched in artifact_mismatches.items() if mismatched
    ]
    canonical_path = (_workspace_path() / canonical_artifact.relative_path).resolve()
    if canonical_path != src_path.resolve():
        failed_artifact_fields.append("relative_path")
    if failed_artifact_fields:
        return {
            "ok": False,
            "status": "failed",
            "error": "RENDER_LINEAGE_MISMATCH: source does not match canonical active composite fields: "
            + ", ".join(failed_artifact_fields),
            "code": "RENDER_LINEAGE_MISMATCH",
        }

    provenance = (canonical_artifact.provider_metadata or {}).get('provenance') or {}
    if provenance.get('qc_level') == 'high':
        from video_factory.creative.agents.video_review import require_final_content_pass
        try:
            require_final_content_pass(rend_data)
            if (rend_data.get('review_context') or {}).get('package_digest') != provenance.get('package_digest'):
                raise ValueError('final review package differs from canonical render')
        except (KeyError, ValueError, OSError):
            return {'ok':False, 'status':'failed', 'code':'FINAL_CONTENT_REQUIRED',
                    'error':'Canonical high-QC render requires its current final content PASS'}

    matching_report = None
    if provenance.get('qc_level') == 'low':
        from video_factory.creative.agents.video_review import require_final_pass

        technical = rend_data.get("technical_qc")
        try:
            if (not isinstance(technical, dict) or technical.get("verdict") != "PASS"
                    or technical.get("sha256") != source_sha256):
                raise ValueError("Technical receipt is missing or bound to different bytes")
            require_final_pass(rend_data)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return {"ok": False, "status": "failed", "code": "FINAL_TECHNICAL_REQUIRED",
                    "error": f"Export requires current technical evidence: {exc}"}
    else:
        try:
            from video_factory.qc.repository import QCReportRepository
            from video_factory.qc.models import QCStage

            appr_svc = _approval_service()
            db_candidates: list[tuple[sqlite3.Connection, bool]] = []
            if appr_svc is not None and getattr(appr_svc, "conn", None) is not None:
                db_candidates.append((appr_svc.conn, False))
            elif appr_svc is not None and getattr(appr_svc, "connection", None) is not None:
                db_candidates.append((appr_svc.connection, False))
            elif (
                appr_svc is not None and getattr(appr_svc, "_connection", None) is not None
            ):
                db_candidates.append((appr_svc._connection, False))

            appr_p = _canonical_approval_db_path()
            if appr_p.exists():
                db_candidates.append((sqlite3.connect(appr_p), True))
            vf_p = _db_path()
            if vf_p.exists() and vf_p != appr_p:
                db_candidates.append((sqlite3.connect(vf_p), True))

            matching_report = None
            for conn, should_close in db_candidates:
                try:
                    cur = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_video_qc_reports'"
                    )
                    if not cur.fetchone():
                        continue
                    qc_repo = QCReportRepository(conn)
                    rep = qc_repo.get_verified_pass_report(
                        owner_user_id=owner,
                        project_id=proj_id,
                        workflow_id=target_wf_id,
                        artifact_id=artifact_id,
                        artifact_version=export_version,
                        stage=QCStage.VIDEO,
                        expected_sha256=source_sha256,
                        artifact_path=str(src_path),
                    )
                    if rep is not None:
                        matching_report = rep
                        break
                except Exception:
                    continue
                finally:
                    if should_close:
                        try:
                            conn.close()
                        except Exception:
                            pass

            if not matching_report:
                return {
                    "ok": False,
                    "status": "failed",
                    "error": f"VIDEO_QC_REQUIRED: No verified PASS Video QC report bound to exact composite lineage (owner={owner}, project={proj_id}, workflow={target_wf_id}, artifact={artifact_id}, version={export_version}, sha={source_sha256})",
                    "code": "VIDEO_QC_REQUIRED",
                }
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "error": f"VIDEO_QC_REQUIRED: Could not verify Video QC report: {exc}",
                "code": "VIDEO_QC_REQUIRED",
            }
    brief_obj = project.creative_brief
    claims_list = [
        safe_claim
        for c in (
            brief_obj.verified_selling_points
            if brief_obj and brief_obj.verified_selling_points
            else []
        )
        if (safe_claim := _caption_safe_line(c.claim))
    ]

    # Gather provenance for the canonical package (no secrets).
    selected_references = []
    if project.resource_pack:
        for ref in project.resource_pack.product_references:
            metadata = dict(ref.metadata or {})
            selected_references.append({
                "asset_id": ref.asset_id,
                "uri": ref.uri,
                "sha256": str(
                    metadata.get("qc_sha256") or metadata.get("sha256") or ""
                ).removeprefix("sha256:"),
                "eligible": metadata.get("qc_eligible") is True
                or metadata.get("eligible") is True,
                "provenance": metadata.get("provenance") or "video_factory",
            })

    approval_ids: dict[str, str | None] = {
        "product_lock": None,
        "brief": None,
        "scene_plan": None,
        "storyboard": None,
        "final_video": None,
    }
    try:
        appr_svc = _approval_service()
        approval_conn = None
        should_close_appr = False
        if appr_svc is not None and getattr(appr_svc, "conn", None) is not None:
            approval_conn = appr_svc.conn
        elif appr_svc is not None and getattr(appr_svc, "connection", None) is not None:
            approval_conn = appr_svc.connection
        elif (
            appr_svc is not None and getattr(appr_svc, "_connection", None) is not None
        ):
            approval_conn = appr_svc._connection
        else:
            approval_db = _canonical_approval_db_path()
            if approval_db.exists():
                from video_factory.storage_infra.sqlite import connect as _connect_sqlite

                approval_conn = _connect_sqlite(approval_db)
                should_close_appr = True

        if approval_conn is not None:
            rows = approval_conn.execute(
                "SELECT request_id, payload_json FROM approval_request "
                "WHERE status = 'approved' ORDER BY updated_at ASC"
            ).fetchall()
            for row in rows:
                payload = json.loads(row[1] or "{}")
                if (
                    payload.get("project_id") != proj_id
                    or payload.get("workflow_id") != target_wf_id
                    or (payload.get("owner_user_id") or payload.get("owner_id")) != owner
                ):
                    continue
                gate = "".join(char for char in str(payload.get("gate") or "").lower() if char.isalnum())
                for key, marker in (
                    ("product_lock", "productlock"),
                    ("brief", "brief"),
                    ("scene_plan", "sceneapproval"),
                    ("storyboard", "storyboard"),
                    ("final_video", "final"),
                ):
                    if marker in gate:
                        if key == "final_video" and (
                            payload.get("artifact_id") != artifact_id
                            or payload.get("artifact_version") != export_version
                        ):
                            continue
                        approval_ids[key] = row[0]
            if should_close_appr:
                approval_conn.close()
    except Exception:
        logger.debug("Could not load approval provenance for export", exc_info=True)

    render_plan_payload = [
        {
            "frame_id": frame.frame_id,
            "scene_id": frame.scene_id,
            "order": frame.order,
            "purpose": frame.purpose,
            "subject_action": frame.subject_action,
        }
        for frame in (project.storyboard.frames if project.storyboard else ())
    ]
    render_plan_hash = hashlib.sha256(
        json.dumps(
            render_plan_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    scene_operations = [
        {
            "scene_id": scene.scene_id,
            "provider_operation_id": scene.provider_operation_id,
            "asset_id": scene.generated_asset_id,
        }
        for scene in project.generated_scenes
    ]
    
    # Collect scene video clips for export package
    scenes_list = []
    for scene in project.generated_scenes:
        scene_video_path = None
        # Try to find the scene's video file from the generated asset
        if scene.generated_asset_id:
            # Look for video file in workspace
            workspace = _workspace_path().resolve()
            potential_paths = [
                workspace / "scenes" / f"{scene.scene_id}.mp4",
                workspace / "renders" / f"{scene.scene_id}.mp4",
                workspace / "videos" / f"{scene.scene_id}.mp4",
            ]
            for path in potential_paths:
                if path.is_file():
                    scene_video_path = str(path)
                    break
        
        if scene_video_path:
            scenes_list.append({
                "scene_id": scene.scene_id,
                "video_path": scene_video_path,
                "asset_id": scene.generated_asset_id,
            })
    
    provider_operations = scene_operations
    artifact_digests = {"composite": source_sha256}
    artifact_digests.update({
        str(ref["asset_id"]): str(ref["sha256"])
        for ref in selected_references
        if ref.get("asset_id") and ref.get("sha256")
    })
    qc_reports = {
        "image": None,
        "video": matching_report.report_id if matching_report else None,
    }
    # A reservation ceiling is not an invoice. Keep caller-supplied spend out
    # of immutable provenance until provider usage has been reconciled.
    spend = {"actual_cost_status": "unavailable"}
    export_version = str(
        params.get("version")
        or params.get("export_version")
        or rend_data.get("version")
        or rend_data.get("export_version")
        or f"v-{source_sha256[:12]}"
    )
    provenance = {
        "owner_user_id": owner,
        "approvals": approval_ids,
        "artifact_digests": artifact_digests,
        "qc": qc_reports,
        "spend": spend,
        "references": selected_references,
        "provider_operations": provider_operations,
        "scenes": scenes_list,
        "render_plan_sha256": render_plan_hash,
        "resource_pack_id": project.resource_pack.id if project.resource_pack else None,
        "brief_approval": project.brief_approval,
        "scene_plan_approval": project.scene_plan_approval,
        "final_approval": project.final_approval.value,
        "storyboard_frames_count": len(project.storyboard.frames)
        if project.storyboard
        else 0,
        "source_video_sha256": source_sha256,
        "end_keyframe_omissions": rend_data.get("end_keyframe_omissions")
        or params.get("end_keyframe_omissions")
        or [],
    }
    export_expectation = _EXPORT_VIDEO_EXPECTATION
    if rend_data.get('creative_mode')=='four_agent':
        export_expectation = VideoExpectation(width=1080,height=1920,fps=30,
            duration_seconds=float(rend_data['technical_qc']['probe']['duration_seconds']))
        provenance['creative'] = {key:rend_data[key] for key in (
            'technical_qc','final_review','content_assessment','human_review','creative_lineage')
            if key in rend_data}
    try:
        package = build_export_package(
            source_video=src_path,
            exports_root=_workspace_path().resolve() / "exports",
            project_id=proj_id,
            version=export_version,
            product_identity=prod_name,
            verified_claims=claims_list,
            cta=brief_obj.cta if brief_obj else None,
            provenance=provenance,
            expected=export_expectation,
        )
    except InvalidVideoArtifact as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"INVALID_VIDEO_ARTIFACT: Source MP4 cannot be fully decoded: {exc}",
            "code": "INVALID_VIDEO_ARTIFACT",
        }
    except ExportPackageConflict as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"EXPORT_VERSION_CONFLICT: {exc}",
            "code": "EXPORT_VERSION_CONFLICT",
        }
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"EXPORT_PACKAGE_ERROR: {exc}",
            "code": "EXPORT_PACKAGE_ERROR",
        }

    export_id = idempotency_key or package.directory.name
    final_url = f"file://{package.video_path.resolve()}"

    # Persist final export state to service/repository
    try:
        srv.save_final_export(owner, proj_id, asset_id=export_id)
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "error": f"PERSISTENCE_ERROR: Failed to save final export: {exc}",
            "code": "PERSISTENCE_ERROR",
        }

    return {
        "ok": True,
        "status": "completed",
        "export_id": export_id,
        "id": export_id,
        "project_id": proj_id,
        "final_video_url": final_url,
        "video_url": final_url,
        "output_url": final_url,
        "file_path": str(package.video_path.resolve()),
        "file_size": package.video_path.stat().st_size,
        "caption_path": str(package.caption_path.resolve()),
        "metadata_path": str(package.metadata_path.resolve()),
        "export_directory": str(package.directory.resolve()),
        "version": export_version,
        "sha256": package.video_sha256,
    }


# ---------------------------------------------------------------------------
# TOOL REGISTRATION
# ---------------------------------------------------------------------------

CANONICAL_PUBLIC_TOOLS = (
    video_project_create,
    video_project_get,
    resource_pack_save,
    resource_pack_get,
    resource_pack_lock,
    resource_pack_unlock,
    raw_idea_save,
    creative_brief_save,
    creative_brief_get,
    creative_brief_approve,
    scene_plan_save,
    scene_plan_get,
    scene_plan_approve,
    storyboard_generate,
    storyboard_approve,
    timeline_get,
    export_submit,
    final_video_approve,
    video_factory_runtime_info,
)

HERMES_WIRE_COMPATIBILITY_TOOLS = (
    create_brief,
    create_scene_plan,
    create_storyboard,
    render,
    export,
    get_job_status,
)

ALL_VIDEO_FACTORY_TOOLS = CANONICAL_PUBLIC_TOOLS + HERMES_WIRE_COMPATIBILITY_TOOLS

for _tool in ALL_VIDEO_FACTORY_TOOLS:
    mcp.tool()(_tool)


if __name__ == "__main__":
    mcp.run()
