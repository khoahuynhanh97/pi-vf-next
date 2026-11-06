"""Canonical workspace layout and artifact path resolver for Product Video.

Provides content-addressed storage for references, per-project directory hierarchies,
relative path encoding, and atomic file operations.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from video_factory.constants import get_hermes_home


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


import re

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def validate_identifier(value: Any, name: str = "identifier") -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {name}: must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"Invalid {name}: cannot be empty")
    if "/" in clean or "\\" in clean or ":" in clean or "\x00" in clean:
        raise ValueError(f"Invalid {name}: path separators or drive prefixes are forbidden: {value!r}")
    if clean in (".", "..") or not _IDENTIFIER_RE.match(clean):
        raise ValueError(f"Invalid {name}: must contain only alphanumeric, underscore, or hyphen: {value!r}")
    return clean


def validate_version_or_attempt(value: Any, name: str = "version") -> int:
    try:
        val = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {name}: must be an integer >= 1: {value!r}")
    if val < 1:
        raise ValueError(f"Invalid {name}: must be >= 1: {value!r}")
    return val


def _assert_descendant(child: Path, parent: Path, label: str = "path") -> Path:
    child_res = child.resolve()
    parent_res = parent.resolve()
    try:
        child_res.relative_to(parent_res)
        return child
    except ValueError:
        raise ValueError(f"Directory traversal detected for {label}: {child} escapes {parent}")


def get_trusted_exchange_roots() -> tuple[Path, ...]:
    """Read trusted external reference exchange roots from config.yaml and standard defaults."""
    roots: list[Path] = []
    try:
        from video_factory.automation.importer import (
            DEFAULT_EXCHANGE_INBOX,
            FALLBACK_EXCHANGE_INBOX,
            LEGACY_EXCHANGE_INBOX,
        )
        for def_root in (
            DEFAULT_EXCHANGE_INBOX,
            Path(r"Z:\work\hermes-data\product-intelligence-data\inbox"),
            FALLBACK_EXCHANGE_INBOX,
            Path(r"Z:\work\hermes-agent-data\product-intelligence\inbox"),
            LEGACY_EXCHANGE_INBOX,
            Path(r"Z:\work\Personal\hermes-product-video-exchange\inbox"),
        ):
            if def_root.exists():
                roots.append(def_root.resolve())
            roots.append(def_root)
        env_inbox = os.environ.get("HERMES_EXCHANGE_INBOX")
        if env_inbox and env_inbox.strip():
            roots.append(Path(env_inbox).expanduser().resolve())
        try:
            repo_inbox = Path(__file__).resolve().parents[5] / "runtime" / "exchange" / "inbox"
            if repo_inbox.is_dir():
                roots.append(repo_inbox.resolve())
        except (IndexError, ValueError):
            pass
    except Exception:
        pass

    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        pv = cfg.get("product_video", {})
        if isinstance(pv, dict):
            configured = (
                pv.get("allowed_exchange_roots")
                or pv.get("storage", {}).get("allowed_exchange_roots")
                or pv.get("allowed_source_roots")
                or []
            )
            if isinstance(configured, (list, tuple)):
                for r in configured:
                    if r and str(r).strip():
                        resolved = Path(r).expanduser().resolve()
                        if resolved not in roots:
                            roots.append(resolved)
    except Exception:
        pass
    return tuple(roots)


class ProductVideoWorkspaceLayout:
    """Canonical workspace resolver and storage layout manager."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = self._resolve_root(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _resolve_root(cls, override: str | Path | None = None) -> Path:
        if override is not None and str(override).strip():
            return Path(override).expanduser().resolve()

        # Internal environment variable bridge (tests & runtime overrides)
        env_ws = os.environ.get("HERMES_VIDEO_FACTORY_WORKSPACE", "").strip()
        if env_ws:
            return Path(env_ws).expanduser().resolve()

        # Check config.yaml if accessible
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            pv_storage = cfg.get("product_video", {}).get("storage", {})
            if isinstance(pv_storage, dict):
                ws_cfg = pv_storage.get("workspace_root") or pv_storage.get("workspace_dir")
                if ws_cfg and str(ws_cfg).strip():
                    return Path(ws_cfg).expanduser().resolve()
        except Exception:
            pass

        # Canonical default: <HERMES_HOME>/workspaces/video_factory
        return get_hermes_home() / "workspaces" / "video_factory"

    @property
    def root(self) -> Path:
        return self._root

    # ── Path Resolution & Relative Mapping ───────────────────────────────────

    def to_relative_path(self, path: str | Path) -> str:
        """Convert an absolute or relative path to a workspace-relative string.
        
        Raises ValueError if the path lies outside the workspace root.
        """
        s = str(path).strip()
        if s.startswith("file://"):
            s = s[7:]
            while s.startswith(("/", "\\")) and not (len(s) >= 2 and s[1] == ":" and s[0].isalpha()):
                s = s[1:]
        p = Path(s)
        if ".." in p.parts:
            raise ValueError(f"Path traversal detected in path: {path}")

        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (self._root / p).resolve()

        try:
            rel = resolved.relative_to(self._root.resolve())
            return rel.as_posix()
        except ValueError:
            raise ValueError(f"Path is outside workspace root: {path}")

    def resolve_path(self, rel_or_abs_path: str | Path, allowed_roots: tuple[Path, ...] = ()) -> Path:
        """Resolve a relative or absolute path safely within the workspace or allowed roots.
        
        Rejects paths outside the workspace unless explicitly permitted by allowed_roots.
        No fallback to unrestricted Path.resolve().
        """
        s = str(rel_or_abs_path).strip()
        if not s:
            raise ValueError("Path cannot be empty")
            
        if s.startswith("file://"):
            from urllib.parse import unquote, urlparse
            parsed = urlparse(s)
            path_part = unquote(parsed.path)
            netloc_part = unquote(parsed.netloc)
            if netloc_part and not path_part.startswith(("/", "\\")):
                combined = f"{netloc_part}/{path_part}"
            elif netloc_part:
                combined = f"{netloc_part}{path_part}"
            else:
                combined = path_part

            # Windows drive letter /D:/ -> D:/
            if len(combined) >= 3 and combined[0] in ("/", "\\") and combined[2] == ":" and combined[1].isalpha():
                combined = combined[1:]

            if len(combined) >= 2 and combined[1] == ":" and combined[0].isalpha():
                if len(combined) == 2 or combined[2] not in ("/", "\\"):
                    combined = f"{combined[:2]}/{combined[2:]}"

            p = Path(combined)
        else:
            if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
                if len(s) == 2 or s[2] not in ("/", "\\"):
                    s = f"{s[:2]}/{s[2:]}"
            p = Path(s)

        parts = p.parts
        if ".." in parts:
            raise ValueError(f"Path traversal detected: {rel_or_abs_path}")

        all_roots = (self._root.resolve(),) + tuple(r.resolve() for r in allowed_roots)

        if p.is_absolute():
            resolved = p.resolve()
            for r in all_roots:
                try:
                    resolved.relative_to(r)
                    return resolved
                except ValueError:
                    continue
            raise ValueError(f"CONTAINMENT_VIOLATION: Absolute path outside approved workspace/exchange roots: {rel_or_abs_path}")

        resolved = (self._root / p).resolve()
        for r in all_roots:
            try:
                resolved.relative_to(r)
                return resolved
            except ValueError:
                continue
        raise ValueError(f"CONTAINMENT_VIOLATION: Relative path escapes approved workspace roots: {rel_or_abs_path}")

    def validate_safe_path(self, path: str | Path, allowed_roots: tuple[Path, ...] = ()) -> Path:
        """Ensure path does not escape allowed workspace roots or exchange roots."""
        return self.resolve_path(path, allowed_roots=allowed_roots)

    # ── Per-Project Hierarchy ────────────────────────────────────────────────

    def get_project_dir(self, project_id: str) -> Path:
        clean_proj = validate_identifier(project_id, "project_id")
        base = self._root / "projects"
        p = base / clean_proj
        _assert_descendant(p, base, "project_dir")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_reference_asset_path(self, project_id: str, sha256_hex: str, ext: str = ".jpg") -> Path:
        proj_dir = self.get_project_dir(project_id)
        clean_sha = validate_identifier(sha256_hex, "sha256_hex")
        clean_ext = ext if ext.startswith(".") else f".{ext}"
        if not re.match(r"^\.[a-zA-Z0-9]+$", clean_ext):
            raise ValueError(f"Invalid extension: {ext}")
        base = proj_dir / "assets" / "references"
        p = base / f"{clean_sha}{clean_ext}"
        _assert_descendant(p, base, "reference_asset_path")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def get_cleaned_asset_path(self, project_id: str, sha256_hex: str) -> Path:
        proj_dir = self.get_project_dir(project_id)
        clean_sha = validate_identifier(sha256_hex, "sha256_hex")
        base = proj_dir / "assets" / "cleaned"
        p = base / f"{clean_sha}.png"
        _assert_descendant(p, base, "cleaned_asset_path")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def get_creative_dir(self, project_id: str, version: int = 1) -> Path:
        proj_dir = self.get_project_dir(project_id)
        ver = validate_version_or_attempt(version, "version")
        base = proj_dir / "creative"
        p = base / f"v{ver}"
        _assert_descendant(p, base, "creative_dir")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_storyboard_dir(self, project_id: str, version: int = 1) -> Path:
        proj_dir = self.get_project_dir(project_id)
        ver = validate_version_or_attempt(version, "version")
        base = proj_dir / "storyboard"
        p = base / f"v{ver}"
        _assert_descendant(p, base, "storyboard_dir")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_scene_dir(self, project_id: str, version: int, scene_id: str) -> Path:
        sb_dir = self.get_storyboard_dir(project_id, version)
        clean_scene = validate_identifier(scene_id, "scene_id")
        base = sb_dir / "scenes"
        p = base / clean_scene
        _assert_descendant(p, base, "scene_dir")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_keyframe_path(
        self,
        project_id: str,
        version: int,
        scene_id: str,
        attempt: int = 1,
        frame_role: str = "start",
    ) -> Path:
        scene_dir = self.get_scene_dir(project_id, version, scene_id)
        att = validate_version_or_attempt(attempt, "attempt")
        clean_role = validate_identifier(frame_role, "frame_role")
        if clean_role not in ("start", "end"):
            raise ValueError(f"Invalid frame_role: must be 'start' or 'end', got {frame_role!r}")
        base = scene_dir / "keyframes"
        p = base / f"attempt-{att:03d}" / f"{clean_role}.png"
        _assert_descendant(p, base, "keyframe_path")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def get_render_batch_dir(self, project_id: str, version: int = 1) -> Path:
        proj_dir = self.get_project_dir(project_id)
        ver = validate_version_or_attempt(version, "version")
        base = proj_dir / "renders"
        p = base / f"v{ver}"
        _assert_descendant(p, base, "render_batch_dir")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_rendered_clip_path(
        self,
        project_id: str,
        version: int,
        scene_id: str,
        attempt: int = 1,
    ) -> Path:
        batch_dir = self.get_render_batch_dir(project_id, version)
        clean_scene = validate_identifier(scene_id, "scene_id")
        att = validate_version_or_attempt(attempt, "attempt")
        base = batch_dir / "scenes"
        p = base / clean_scene / f"attempt-{att:03d}" / "clip.mp4"
        _assert_descendant(p, base, "rendered_clip_path")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def get_composite_dir(self, project_id: str, version: int = 1) -> Path:
        batch_dir = self.get_render_batch_dir(project_id, version)
        base = batch_dir / "composite"
        _assert_descendant(base, batch_dir, "composite_dir")
        base.mkdir(parents=True, exist_ok=True)
        return base

    def get_export_dir(self, project_id: str, version: str | int) -> Path:
        clean_proj = validate_identifier(project_id, "project_id")
        clean_ver = validate_identifier(str(version), "version")
        base = self._root / "exports"
        p = base / f"export_{clean_proj}_{clean_ver}"
        _assert_descendant(p, base, "export_dir")
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ── Reference Ingestion ──────────────────────────────────────────────────

    def ingest_reference(
        self,
        project_id: str,
        source_path: str | Path,
        *,
        is_cleaned: bool = False,
        allowed_source_roots: tuple[Path, ...] | None = None,
    ) -> tuple[Path, str, int]:
        """Ingest an external reference image into content-addressed project storage.
        
        Reads an explicitly supplied source file, copies it into content-addressed project storage,
        and returns the destination Path, sha256 hex, and file size.
        """
        clean_proj = validate_identifier(project_id, "project_id")
        src_raw = str(source_path).strip()
        if src_raw.startswith("file://"):
            src_raw = src_raw[7:]
            while src_raw.startswith(("/", "\\")) and not (len(src_raw) >= 2 and src_raw[1] == ":" and src_raw[0].isalpha()):
                src_raw = src_raw[1:]
        src = Path(src_raw).resolve()
        if not src.is_file() or src.stat().st_size == 0:
            raise FileNotFoundError(f"Source reference image not found or empty: {src}")

        # Determine approved containment roots
        # The workspace root itself is always approved. External roots must come from
        # allowed_source_roots (if explicitly passed) or config.yaml trusted roots.
        # Empty allowed_source_roots must NOT mean unrestricted access!
        if allowed_source_roots is not None:
            configured_roots = tuple(r.resolve() for r in allowed_source_roots)
        else:
            configured_roots = tuple(r.resolve() for r in get_trusted_exchange_roots())

        approved_roots = (self._root.resolve(),) + configured_roots
        is_allowed = any(src.is_relative_to(r) for r in approved_roots)
        if not is_allowed:
            s_str = str(src)
            alt_candidates = []
            if s_str.startswith("D:") or s_str.startswith("d:"):
                alt_candidates.append(Path("Z:" + s_str[2:]))
            elif s_str.startswith("Z:") or s_str.startswith("z:"):
                alt_candidates.append(Path("D:" + s_str[2:]))
            if "\\\\100.106.92.122\\Khoaha-D" in s_str:
                alt_candidates.append(Path(s_str.replace("\\\\100.106.92.122\\Khoaha-D", "Z:")))
                alt_candidates.append(Path(s_str.replace("\\\\100.106.92.122\\Khoaha-D", "D:")))
            for alt in alt_candidates:
                if any(alt.is_relative_to(r) for r in approved_roots):
                    is_allowed = True
                    break
        if not is_allowed:
            raise ValueError(f"UNTRUSTED_REFERENCE_SOURCE: Source reference path outside approved workspace/exchange roots: {src}")

        ext = src.suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            raise ValueError(f"INVALID_IMAGE_ARTIFACT: Unsupported image extension: {ext}")

        # Validate actual image decodability
        try:
            from PIL import Image
            with Image.open(src) as img:
                img.verify()
        except Exception as exc:
            raise ValueError(f"INVALID_IMAGE_ARTIFACT: Source image cannot be decoded: {exc}")

        sha256_hex = _compute_sha256(src)

        if is_cleaned:
            target = self.get_cleaned_asset_path(clean_proj, sha256_hex)
        else:
            target = self.get_reference_asset_path(clean_proj, sha256_hex, ext=ext)

        if not target.is_file() or target.stat().st_size == 0 or _compute_sha256(target) != sha256_hex:
            self.atomic_copy(src, target)

        copied_sha = _compute_sha256(target)
        if copied_sha != sha256_hex:
            raise ValueError(f"Ingested file SHA mismatch: expected {sha256_hex}, got {copied_sha}")

        return target, sha256_hex, target.stat().st_size

    # ── Atomic File Operations ───────────────────────────────────────────────

    @classmethod
    def atomic_write_bytes(cls, target_path: Path, data: bytes) -> None:
        target_path = Path(target_path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = target_path.parent / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_file = tempfile.mkstemp(dir=str(tmp_dir), prefix=".atomic_write_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, str(target_path))
        except Exception:
            if os.path.exists(tmp_file):
                os.unlink(tmp_file)
            raise

    @classmethod
    def atomic_write_text(cls, target_path: Path, text: str, encoding: str = "utf-8") -> None:
        cls.atomic_write_bytes(target_path, text.encode(encoding))

    @classmethod
    def atomic_copy(cls, source: Path, destination: Path) -> None:
        source = Path(source).resolve()
        destination = Path(destination).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = destination.parent / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_file = tempfile.mkstemp(dir=str(tmp_dir), prefix=".atomic_copy_")
        os.close(fd)
        try:
            shutil.copy2(str(source), tmp_file)
            os.replace(tmp_file, str(destination))
        except Exception:
            if os.path.exists(tmp_file):
                os.unlink(tmp_file)
            raise


def get_workspace_layout(root: str | Path | None = None) -> ProductVideoWorkspaceLayout:
    """Get or instantiate the canonical workspace layout."""
    return ProductVideoWorkspaceLayout(root)
