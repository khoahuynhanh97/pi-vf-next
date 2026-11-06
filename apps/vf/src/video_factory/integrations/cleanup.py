"""Safe retention and reachability-checked artifact cleanup for Video Factory."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from video_factory.integrations.ports import VideoFactoryRepository
from video_factory.storage.layout import ProductVideoWorkspaceLayout, get_workspace_layout


@dataclass(frozen=True)
class CleanupReport:
    project_id: str
    examined_count: int
    deleted_count: int
    freed_bytes: int
    preserved_count: int
    failed_unlinks: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()


def cleanup_unreferenced_artifacts(
    project_id: str,
    owner_user_id: str,
    repository: VideoFactoryRepository,
    layout: ProductVideoWorkspaceLayout | None = None,
    *,
    dry_run: bool = True,
    retention_seconds: float = 86400.0,
) -> CleanupReport:
    """Safely prune unreferenced, failed or superseded artifacts while preserving all approved/reachable assets.

    Defaults to dry_run=True and protects files modified within the retention_seconds window.
    Cleanup is strictly status-driven: only artifacts explicitly recorded in the database
    with status in ('failed', 'superseded') are candidates for pruning.
    """
    ws = layout or get_workspace_layout()
    project = repository.get_owned(project_id, owner_user_id)
    if project is None:
        return CleanupReport(project_id=project_id, examined_count=0, deleted_count=0, freed_bytes=0, preserved_count=0)

    # 1. Gather all reachable absolute paths
    reachable_paths: set[Path] = set()
    visited_artifacts: set[str] = set()

    def _protect_artifact_and_lineage(art_id: str) -> None:
        if not art_id or art_id in visited_artifacts:
            return
        visited_artifacts.add(art_id)
        try:
            art = repository.get_artifact(art_id, owner_user_id=owner_user_id)
            if art:
                try:
                    reachable_paths.add(ws.resolve_path(art.relative_path))
                except Exception:
                    pass
                for p_id in art.parent_artifact_ids:
                    _protect_artifact_and_lineage(p_id)
        except Exception:
            pass

    # Resource pack references
    if project.resource_pack:
        for ref in project.resource_pack.product_references:
            if hasattr(ref, "uri") and ref.uri and not str(ref.uri).startswith("asset://"):
                try:
                    reachable_paths.add(ws.resolve_path(ref.uri))
                except Exception:
                    pass
        for ref in project.resource_pack.character_references:
            if hasattr(ref, "uri") and ref.uri and not str(ref.uri).startswith("asset://"):
                try:
                    reachable_paths.add(ws.resolve_path(ref.uri))
                except Exception:
                    pass

    # Storyboard frames
    if project.storyboard:
        for frame in project.storyboard.frames:
            if frame.generated_asset_id:
                _protect_artifact_and_lineage(frame.generated_asset_id)

    # Generated scene clips
    for scene in project.generated_scenes:
        if scene.generated_asset_id:
            _protect_artifact_and_lineage(scene.generated_asset_id)

    # Timeline clips
    if project.timeline:
        for clip in project.timeline.clips:
            if clip.source_asset_id:
                _protect_artifact_and_lineage(clip.source_asset_id)

    # All active/approved artifacts in database and their parent lineages
    failed_or_superseded_file_paths: set[Path] = set()
    try:
        all_artifacts = repository.list_artifacts(project_id, owner_user_id=owner_user_id)
        for art in all_artifacts:
            if art.status in ("active", "approved"):
                _protect_artifact_and_lineage(art.artifact_id)
            elif art.status in ("failed", "superseded"):
                try:
                    failed_or_superseded_file_paths.add(ws.resolve_path(art.relative_path))
                except Exception:
                    pass
    except Exception:
        pass

    # Exports directory is ALWAYS protected
    exports_dir = ws.root / "exports"
    if exports_dir.is_dir():
        for exp_file in exports_dir.rglob("*"):
            if exp_file.is_file():
                reachable_paths.add(exp_file.resolve())

    # 2. Scan project directory for candidates
    project_dir = ws.get_project_dir(project_id)
    if not project_dir.is_dir():
        return CleanupReport(project_id=project_id, examined_count=0, deleted_count=0, freed_bytes=0, preserved_count=0)

    examined = 0
    deleted = 0
    freed_bytes = 0
    preserved = 0
    deleted_paths: list[str] = []
    failed_unlinks: list[str] = []

    current_time = time.time()

    for file_path in project_dir.rglob("*"):
        if not file_path.is_file():
            continue
        examined += 1
        resolved_file = file_path.resolve()

        # Never touch manifests or json metadata
        if file_path.suffix.lower() in (".json", ".txt"):
            preserved += 1
            continue

        # Check retention window (do not delete files created/modified recently)
        try:
            mtime = file_path.stat().st_mtime
            if (current_time - mtime) < retention_seconds:
                preserved += 1
                continue
        except Exception:
            preserved += 1
            continue

        # Check if reachable
        if resolved_file in reachable_paths:
            preserved += 1
            continue

        # Status-driven pruning: delete ONLY if explicitly recorded as failed or superseded
        if resolved_file in failed_or_superseded_file_paths:
            file_size = file_path.stat().st_size
            if dry_run:
                deleted += 1
                freed_bytes += file_size
                deleted_paths.append(str(file_path))
            else:
                try:
                    file_path.unlink(missing_ok=True)
                    deleted += 1
                    freed_bytes += file_size
                    deleted_paths.append(str(file_path))
                except Exception:
                    failed_unlinks.append(str(file_path))
                    preserved += 1
        else:
            # Unclassified, crash-window, or unmanaged file -> fail-closed, preserve!
            preserved += 1

    return CleanupReport(
        project_id=project_id,
        examined_count=examined,
        deleted_count=deleted,
        freed_bytes=freed_bytes,
        preserved_count=preserved,
        failed_unlinks=tuple(failed_unlinks),
        deleted_paths=tuple(deleted_paths),
    )
