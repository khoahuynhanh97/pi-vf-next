"""Immutable, fail-closed export packages for product-video deliverables."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .composer import VideoExpectation, probe_video, validate_export


class ExportPackageConflict(RuntimeError):
    """Raised when callers try to reuse an immutable version for new inputs."""


@dataclass(frozen=True)
class ExportPackage:
    directory: Path
    video_path: Path
    caption_path: Path
    metadata_path: Path
    video_sha256: str
    caption_sha256: str


_BLOCKED_CAPTION_PATTERNS = (
    "minh gear",
    "marketplace",
    "seller",
    "shop",
    "store",
    "watermark",
    "đặt hàng",
    "giỏ hàng",
    "ưu đãi",
    "chính hãng",
    "review chân thực",
    "#review",
)
_GENERIC_IDENTITIES = {
    "sản phẩm",
    "sản phẩm công nghệ",
    "sản phẩm công nghệ tiện ích",
    "product",
    "unknown product",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_segment(value: str, *, field: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.")
    if not segment:
        raise ValueError(f"{field} is required")
    return segment


def _contains_blocked_caption_text(value: str) -> bool:
    lowered = value.casefold()
    return any(pattern in lowered for pattern in _BLOCKED_CAPTION_PATTERNS)


def _validate_identity(value: str) -> str:
    identity = " ".join(value.split()).strip()
    lowered = identity.casefold()
    if (
        not identity
        or lowered in _GENERIC_IDENTITIES
        or lowered.startswith(("pack_", "pack-", "proj_", "proj-"))
        or _contains_blocked_caption_text(identity)
    ):
        raise ValueError("A canonical product identity without seller/source branding is required")
    return identity


def _build_caption(
    product_identity: str,
    verified_claims: Sequence[str],
    cta: str | None,
) -> str:
    lines = [product_identity, ""]
    safe_claims = [
        " ".join(str(claim).split()).strip()
        for claim in verified_claims
        if str(claim).strip() and not _contains_blocked_caption_text(str(claim))
    ]
    if safe_claims:
        lines.append("Điểm nổi bật đã xác thực:")
        lines.extend(f"- {claim}" for claim in safe_claims[:3])
        lines.append("")

    safe_cta = " ".join((cta or "").split()).strip()
    if not safe_cta or _contains_blocked_caption_text(safe_cta):
        safe_cta = "Xem thêm thông tin sản phẩm trước khi quyết định."
    lines.extend((safe_cta, "", "#gadget #technology #productvideo"))
    return "\n".join(lines) + "\n"


def _existing_package(target: Path, *, expected_input_digest: str) -> ExportPackage:
    metadata_path = target / "metadata.json"
    video_path = target / "final.mp4"
    caption_path = target / "caption.txt"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExportPackageConflict(f"Export version already exists but is incomplete: {target}") from exc
    if metadata.get("export_input_sha256") != expected_input_digest:
        raise ExportPackageConflict(f"Export version already exists with different inputs: {target}")
    probe = probe_video(video_path, decode_all=True)
    caption_sha256 = _sha256(caption_path)
    if (
        probe.sha256 != metadata.get("video", {}).get("sha256")
        or caption_sha256 != metadata.get("files", {}).get("caption.txt", {}).get("sha256")
    ):
        raise ExportPackageConflict(f"Export version already exists but its bytes changed: {target}")
    return ExportPackage(
        directory=target,
        video_path=video_path,
        caption_path=caption_path,
        metadata_path=metadata_path,
        video_sha256=probe.sha256,
        caption_sha256=caption_sha256,
    )


def build_export_package(
    *,
    source_video: str | Path,
    exports_root: str | Path,
    project_id: str,
    version: str,
    product_identity: str,
    verified_claims: Sequence[str] = (),
    cta: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    expected: VideoExpectation | None = None,
) -> ExportPackage:
    """Create one immutable final.mp4/caption.txt/metadata.json package.

    The source is fully decoded before any export directory is created. Repeating
    the exact inputs is idempotent; changing inputs requires a new version.

    If *expected* is provided the video is validated against resolution, duration,
    codec, and frame-count constraints (fail-closed — ``InvalidVideoArtifact`` is
    raised and no export directory is created).  Callers at the product-video layer
    SHOULD supply an expectation matching the delivery spec (e.g. 1080×1920 / h264).
    Lower-level callers (tests, manual exports) may omit it to skip spec enforcement.
    """

    source = Path(source_video).resolve()
    if expected is not None:
        probe = validate_export(source, expected)
    else:
        probe = probe_video(source, decode_all=True)
    identity = _validate_identity(product_identity)
    safe_project = _safe_segment(project_id, field="project_id")
    safe_version = _safe_segment(version, field="version")
    root = Path(exports_root).resolve()
    target = root / f"export_{safe_project}_{safe_version}"
    provenance_data = dict(provenance or {})
    caption = _build_caption(identity, verified_claims, cta)
    input_digest = _canonical_digest(
        {
            "project_id": project_id,
            "version": version,
            "product_identity": identity,
            "verified_claims": list(verified_claims),
            "cta": cta,
            "provenance": provenance_data,
            "source_video_sha256": probe.sha256,
        }
    )

    if target.exists():
        return _existing_package(target, expected_input_digest=input_digest)

    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".export-stage-", dir=root))
    try:
        video_path = stage / "final.mp4"
        caption_path = stage / "caption.txt"
        metadata_path = stage / "metadata.json"
        keyframes_dir = stage / "keyframes"
        qc_dir = stage / "qc"
        scenes_dir = stage / "scenes"
        keyframes_dir.mkdir(parents=True, exist_ok=True)
        qc_dir.mkdir(parents=True, exist_ok=True)
        scenes_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(source, video_path)
        copied_probe = probe_video(video_path, decode_all=True)
        if copied_probe.sha256 != probe.sha256:
            raise ValueError("Export copy SHA-256 does not match the validated source")
        caption_path.write_text(caption, encoding="utf-8", newline="\n")
        caption_sha256 = _sha256(caption_path)
        video_metadata = asdict(copied_probe)
        video_metadata["path"] = str(target / "final.mp4")

        # Snapshot keyframe images if provided in provenance
        keyframes_snapshot: dict[str, Any] = {}
        for kf in provenance_data.get("keyframes", []) if isinstance(provenance_data.get("keyframes"), list) else []:
            if isinstance(kf, dict):
                kf_path = kf.get("image_path") or kf.get("file_path") or kf.get("path")
                kf_id = kf.get("frame_id") or kf.get("scene_id") or "keyframe"
                if kf_path and Path(kf_path).is_file():
                    dest = keyframes_dir / f"{kf_id}.png"
                    shutil.copy2(Path(kf_path), dest)
                    keyframes_snapshot[f"keyframes/{kf_id}.png"] = {
                        "sha256": _sha256(dest),
                        "size_bytes": dest.stat().st_size,
                    }

        # Snapshot QC reports if provided in provenance
        qc_snapshot: dict[str, Any] = {}
        qc_reports_raw = provenance_data.get("qc_reports") or provenance_data.get("qc") or {}
        if isinstance(qc_reports_raw, dict):
            for stage_name, report_data in qc_reports_raw.items():
                if report_data:
                    dest = qc_dir / f"{stage_name}-report.json"
                    dest.write_text(
                        json.dumps(report_data, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                        newline="\n",
                    )
                    qc_snapshot[f"qc/{stage_name}-report.json"] = {
                        "sha256": _sha256(dest),
                        "size_bytes": dest.stat().st_size,
                    }

        # Snapshot scene video clips if provided in provenance
        scenes_snapshot: dict[str, Any] = {}
        scenes_list = provenance_data.get("scenes") or []
        if isinstance(scenes_list, list):
            for scene_data in scenes_list:
                if isinstance(scene_data, dict):
                    scene_path = scene_data.get("video_path") or scene_data.get("file_path") or scene_data.get("path")
                    scene_id = scene_data.get("scene_id") or scene_data.get("id") or "scene"
                    if scene_path and Path(scene_path).is_file():
                        dest = scenes_dir / f"{scene_id}.mp4"
                        shutil.copy2(Path(scene_path), dest)
                        scenes_snapshot[f"scenes/{scene_id}.mp4"] = {
                            "sha256": _sha256(dest),
                            "size_bytes": dest.stat().st_size,
                        }

        files_map = {
            "final.mp4": {
                "sha256": copied_probe.sha256,
                "size_bytes": copied_probe.file_size,
            },
            "caption.txt": {
                "sha256": caption_sha256,
                "size_bytes": caption_path.stat().st_size,
            },
        }
        files_map.update(keyframes_snapshot)
        files_map.update(qc_snapshot)
        files_map.update(scenes_snapshot)

        metadata = {
            "schema_version": "product-video-export-v2",
            "project_id": project_id,
            "version": version,
            "product_identity": identity,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "export_input_sha256": input_digest,
            "video": video_metadata,
            "files": files_map,
            "provenance": provenance_data,
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        try:
            os.replace(stage, target)
        except FileExistsError:
            return _existing_package(target, expected_input_digest=input_digest)
    finally:
        if stage.exists():
            shutil.rmtree(stage)

    package = ExportPackage(
        directory=target,
        video_path=target / "final.mp4",
        caption_path=target / "caption.txt",
        metadata_path=target / "metadata.json",
        video_sha256=probe.sha256,
        caption_sha256=caption_sha256,
    )
    
    # Mirror to VideoAI Hub (fail-safe: never break core export on hub errors)
    try:
        from video_factory.storage.video_hub import mirror_export_to_hub
        
        brand = provenance_data.get("brand") or provenance_data.get("product_brand")
        hub_folder = mirror_export_to_hub(
            export_dir=target,
            product_identity=identity,
            brand=brand,
        )
        
        # Update metadata to include hub path
        metadata["video_ai_hub_path"] = str(hub_folder)
        package.metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
    except Exception as e:
        # Log but don't fail the export
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to mirror export to VideoAI hub: {e}")
    
    return package
