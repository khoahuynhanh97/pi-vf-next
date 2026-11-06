from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from video_factory.integrations.domain import (
    VideoFactoryArtifact, RenderBatch,
    ABVariantSet, AssetReference, Claim, ClaimStatus, CreativeBrief,
    FinalApprovalStatus, FrameGenerationStatus, FramePrompt, GeneratedScene,
    HookVariant, ProjectStatus, RawIdea, ResourceIdentity, ResourcePack,
    Scene, ScenePlan, Storyboard, StoryboardApprovalStatus, StoryboardFrame,
    Timeline, TimelineClip, TimelineStatus, VideoFactoryProject,
    VideoGenerationStatus, VideoPrompt,
)
from video_factory.integrations.ports import VideoFactoryRepository


class VideoFactoryConflictError(ValueError):
    """A project snapshot was based on an older committed revision."""


def _json(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json(item) for item in value]
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    return value


def _dump(value: Any) -> str:
    return json.dumps(_json(asdict(value) if hasattr(value, "__dataclass_fields__") else value), ensure_ascii=True)


def _asset(value: dict) -> AssetReference:
    data = dict(value)
    data["metadata"] = data.get("metadata", {})
    return AssetReference(**data)


def _identity(value: dict | None) -> ResourceIdentity | None:
    if not value:
        return None
    data = dict(value)
    data["distinctive_features"] = tuple(data.get("distinctive_features", []))
    return ResourceIdentity(**data)


def _resource(value: dict, owner_user_id: str) -> ResourcePack | None:
    if not value:
        return None
    data = dict(value)
    data["owner_user_id"] = owner_user_id
    data["product_references"] = tuple(_asset(item) for item in data.get("product_references", []))
    data["character_references"] = tuple(_asset(item) for item in data.get("character_references", []))
    data["locked_product_identity"] = _identity(data.get("locked_product_identity"))
    data["locked_character_identity"] = _identity(data.get("locked_character_identity"))
    return ResourcePack(**data)


def _idea(value: dict) -> RawIdea | None:
    if not value:
        return None
    data = dict(value)
    data["required_elements"] = tuple(data.get("required_elements", []))
    return RawIdea(**data)


def _claim(value: dict) -> Claim:
    data = dict(value)
    data["status"] = ClaimStatus(data["status"])
    data["evidence_refs"] = tuple(data.get("evidence_refs", []))
    return Claim(**data)


def _brief(value: dict) -> CreativeBrief | None:
    if not value:
        return None
    data = dict(value)
    data["content_blocks"] = tuple(data.get("content_blocks", []))
    data["restrictions"] = tuple(data.get("restrictions", []))
    data["required_content"] = tuple(data.get("required_content", []))
    data["verified_selling_points"] = tuple(_claim(item) for item in data.get("verified_selling_points", []))
    return CreativeBrief(**data)


def _scene_plan(value: dict) -> ScenePlan | None:
    if not value or not value.get("scenes"):
        return None
    scenes = []
    for item in value["scenes"]:
        data = dict(item)
        data["required_resources"] = tuple(data.get("required_resources", []))
        scenes.append(Scene(**data))
    return ScenePlan(tuple(scenes))


def _frame_prompt(value: dict | None) -> FramePrompt | None:
    if not value:
        return None
    data = dict(value)
    data["reference_asset_ids"] = tuple(data.get("reference_asset_ids", []))
    data["provider_options"] = data.get("provider_options", {})
    return FramePrompt(**data)


def _storyboard_frame(value: dict) -> StoryboardFrame:
    data = dict(value)
    data["required_resource_ids"] = tuple(data.get("required_resource_ids", []))
    data["prompt"] = _frame_prompt(data.get("prompt"))
    data["generation_status"] = FrameGenerationStatus(data.get("generation_status", "planned"))
    return StoryboardFrame(**data)


def _storyboard(value: dict) -> Storyboard | None:
    if not value or not value.get("frames"):
        return None
    data = dict(value)
    data["frames"] = tuple(_storyboard_frame(item) for item in data["frames"])
    data["approval_status"] = StoryboardApprovalStatus(data.get("approval_status", "pending"))
    return Storyboard(**data)


def _video_prompt(value: dict) -> VideoPrompt:
    data = dict(value)
    data["reference_frame_ids"] = tuple(data.get("reference_frame_ids", []))
    data["provider_options"] = data.get("provider_options", {})
    return VideoPrompt(**data)


def _generated_scene(value: dict) -> GeneratedScene:
    data = dict(value)
    data["video_prompt"] = _video_prompt(data["video_prompt"])
    data["generation_status"] = VideoGenerationStatus(data.get("generation_status", "pending"))
    return GeneratedScene(**data)


def _timeline_clip(value: dict) -> TimelineClip:
    data = dict(value)
    data["audio_metadata"] = data.get("audio_metadata", {})
    return TimelineClip(**data)


def _timeline(value: dict) -> Timeline | None:
    if not value or not value.get("clips"):
        return None
    data = dict(value)
    data["clips"] = tuple(_timeline_clip(item) for item in data["clips"])
    data["status"] = TimelineStatus(data.get("status", "draft"))
    return Timeline(**data)


def _hook_variant(value: dict) -> HookVariant:
    data = dict(value)
    data["creative_brief"] = _brief(data.get("creative_brief"))
    data["scene_plan"] = _scene_plan(data.get("scene_plan"))
    data["storyboard"] = _storyboard(data.get("storyboard"))
    data["generated_scenes"] = tuple(_generated_scene(item) for item in data.get("generated_scenes", []))
    data["timeline"] = _timeline(data.get("timeline"))
    return HookVariant(**data)


def _ab_variant_set(value: dict | None) -> ABVariantSet | None:
    if not value or not value.get("variants"):
        return None
    data = dict(value)
    data["variants"] = tuple(_hook_variant(item) for item in data["variants"])
    return ABVariantSet(**data)



def _artifact_from_row(row: Any) -> VideoFactoryArtifact:
    parent_ids = json.loads(row["parent_artifact_ids_json"]) if row["parent_artifact_ids_json"] else []
    prov_meta = json.loads(row["provider_metadata_json"]) if row["provider_metadata_json"] else {}
    return VideoFactoryArtifact(
        artifact_id=row["artifact_id"],
        owner_user_id=row["owner_user_id"],
        workflow_id=row["workflow_id"],
        project_id=row["project_id"],
        kind=row["kind"],
        scene_id=row["scene_id"],
        frame_role=row["frame_role"],
        version=row["version"],
        attempt=row["attempt"],
        relative_path=row["relative_path"],
        sha256=row["sha256"],
        size=row["size"],
        mime_type=row["mime_type"],
        status=row["status"],
        parent_artifact_ids=tuple(parent_ids),
        provider_metadata=prov_meta,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _batch_from_row(row: Any) -> RenderBatch:
    return RenderBatch(
        render_batch_id=row["render_batch_id"],
        owner_user_id=row["owner_user_id"],
        workflow_id=row["workflow_id"],
        project_id=row["project_id"],
        version=row["version"],
        status=row["status"],
        scene_count=row["scene_count"],
        completed_scene_count=row["completed_scene_count"],
        timeline_artifact_id=row["timeline_artifact_id"],
        composite_artifact_id=row["composite_artifact_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SQLiteVideoFactoryRepository(VideoFactoryRepository):
    SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS video_factory_projects (
        id TEXT PRIMARY KEY,
        owner_user_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        resource_pack_json TEXT NOT NULL DEFAULT '{}',
        raw_idea_json TEXT NOT NULL DEFAULT '{}',
        creative_brief_json TEXT NOT NULL DEFAULT '{}',
        scene_plan_json TEXT NOT NULL DEFAULT '{}',
        storyboard_json TEXT NOT NULL DEFAULT '{}',
        generated_scenes_json TEXT NOT NULL DEFAULT '[]',
        timeline_json TEXT NOT NULL DEFAULT '{}',
        draft_video_asset_id TEXT,
        final_video_asset_id TEXT,
        brief_approval TEXT NOT NULL DEFAULT 'pending',
        scene_plan_approval TEXT NOT NULL DEFAULT 'pending',
        final_approval TEXT NOT NULL DEFAULT 'pending',
        final_approval_notes TEXT NOT NULL DEFAULT '',
        ab_variants_json TEXT NOT NULL DEFAULT '{}',
        resource_version INTEGER NOT NULL DEFAULT 0,
        idea_version INTEGER NOT NULL DEFAULT 0,
        brief_version INTEGER NOT NULL DEFAULT 0,
        scene_version INTEGER NOT NULL DEFAULT 0,
        storyboard_version INTEGER NOT NULL DEFAULT 0,
        video_generation_version INTEGER NOT NULL DEFAULT 0,
        timeline_version INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(owner_user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_video_factory_owner_updated
    ON video_factory_projects(owner_user_id, updated_at DESC);

    CREATE TABLE IF NOT EXISTS video_factory_artifacts (
        artifact_id TEXT PRIMARY KEY,
        owner_user_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        scene_id TEXT NOT NULL DEFAULT '',
        frame_role TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        attempt INTEGER NOT NULL DEFAULT 1,
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        mime_type TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        parent_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
        provider_metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_vf_artifacts_proj ON video_factory_artifacts(project_id, kind);
    CREATE INDEX IF NOT EXISTS idx_vf_artifacts_sha ON video_factory_artifacts(sha256);

    CREATE TABLE IF NOT EXISTS video_factory_render_batches (
        render_batch_id TEXT PRIMARY KEY,
        owner_user_id TEXT NOT NULL,
        workflow_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending',
        scene_count INTEGER NOT NULL DEFAULT 0,
        completed_scene_count INTEGER NOT NULL DEFAULT 0,
        timeline_artifact_id TEXT,
        composite_artifact_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_vf_batches_proj ON video_factory_render_batches(project_id, version);
    """

    def __init__(self, database: Any) -> None:
        self._database = database
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        if hasattr(self._database, "connect"):
            return self._database.connect()
        db_path = str(self._database)
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def initialize(self) -> None:
        if hasattr(self._database, "initialize"):
            try:
                self._database.initialize()
            except Exception:
                pass
        conn = self._connect()
        try:
            conn.executescript(self.SCHEMA_DDL)
            cur = conn.execute("PRAGMA table_info(video_factory_projects)")
            cols = {r[1] for r in cur.fetchall()}
            if "ab_variants_json" not in cols:
                conn.execute("ALTER TABLE video_factory_projects ADD COLUMN ab_variants_json TEXT NOT NULL DEFAULT '{}'")
            if "revision" not in cols:
                conn.execute("ALTER TABLE video_factory_projects ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        finally:
            conn.close()

    @contextmanager
    def _transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        if hasattr(self._database, "transaction"):
            with self._database.transaction(immediate=immediate) as conn:
                yield conn
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, project: VideoFactoryProject) -> VideoFactoryProject:
        with self._transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO video_factory_projects(id, owner_user_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (project.id, project.owner_user_id, project.created_at, project.updated_at),
            )
        return project

    def get_owned(self, project_id: str, owner_user_id: str) -> VideoFactoryProject | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM video_factory_projects WHERE id = ? AND owner_user_id = ?",
                (project_id, owner_user_id),
            ).fetchone()
        return self._from_row(row) if row else None

    def list_owned(self, owner_user_id: str) -> list[VideoFactoryProject]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM video_factory_projects WHERE owner_user_id = ? ORDER BY updated_at DESC",
                (owner_user_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def save(self, project: VideoFactoryProject) -> VideoFactoryProject:
        with self._transaction(immediate=True) as conn:
            scenes_data = [_json(asdict(scene)) for scene in project.generated_scenes]
            ab_variants_json = _dump(project.ab_variants) if project.ab_variants else "{}"

            cursor = conn.execute(
                """UPDATE video_factory_projects SET 
                   status=?, resource_pack_json=?, raw_idea_json=?,
                   creative_brief_json=?, scene_plan_json=?, 
                   storyboard_json=?, generated_scenes_json=?, timeline_json=?,
                   draft_video_asset_id=?, final_video_asset_id=?,
                   brief_approval=?, scene_plan_approval=?, final_approval=?, final_approval_notes=?,
                   ab_variants_json=?,
                   resource_version=?, idea_version=?, brief_version=?, scene_version=?,
                   storyboard_version=?, video_generation_version=?, timeline_version=?,
                   updated_at=?, revision=revision+1
                   WHERE id=? AND owner_user_id=? AND revision=?""",
                (
                    project.status.value,
                    _dump(project.resource_pack) if project.resource_pack else "{}",
                    _dump(project.raw_idea) if project.raw_idea else "{}",
                    _dump(project.creative_brief) if project.creative_brief else "{}",
                    _dump(project.scene_plan) if project.scene_plan else "{}",
                    _dump(project.storyboard) if project.storyboard else "{}",
                    json.dumps(scenes_data, ensure_ascii=True),
                    _dump(project.timeline) if project.timeline else "{}",
                    project.draft_video_asset_id,
                    project.final_video_asset_id,
                    project.brief_approval,
                    project.scene_plan_approval,
                    project.final_approval.value,
                    project.final_approval_notes,
                    ab_variants_json,
                    project.resource_version,
                    project.idea_version,
                    project.brief_version,
                    project.scene_version,
                    project.storyboard_version,
                    project.video_generation_version,
                    project.timeline_version,
                    project.updated_at,
                    project.id,
                    project.owner_user_id,
                    project.revision,
                ),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT * FROM video_factory_projects WHERE id=? AND owner_user_id=?",
                    (project.id, project.owner_user_id),
                ).fetchone()
                if row is None:
                    raise ValueError("Video Factory project is missing or owned by another user")
                current = self._from_row(row)
                if replace(project, revision=current.revision, updated_at=current.updated_at) == current:
                    return current
                raise VideoFactoryConflictError(
                    f"VIDEO_FACTORY_CONFLICT: project {project.id} changed since revision {project.revision}"
                )
        return replace(project, revision=project.revision + 1)

    def _from_row(self, row: Any) -> VideoFactoryProject:
        try:
            keys = set(row.keys()) if hasattr(row, "keys") else set()
            generated_scenes_json = row["generated_scenes_json"] if "generated_scenes_json" in keys else "[]"
            storyboard_json = row["storyboard_json"] if "storyboard_json" in keys else "{}"
            timeline_json = row["timeline_json"] if "timeline_json" in keys else "{}"
            draft_video_asset_id = row["draft_video_asset_id"] if "draft_video_asset_id" in keys else None
            final_video_asset_id = row["final_video_asset_id"] if "final_video_asset_id" in keys else None
            final_approval = row["final_approval"] if "final_approval" in keys else "pending"
            final_approval_notes = row["final_approval_notes"] if "final_approval_notes" in keys else ""
            storyboard_version = row["storyboard_version"] if "storyboard_version" in keys else 0
            video_generation_version = row["video_generation_version"] if "video_generation_version" in keys else 0
            timeline_version = row["timeline_version"] if "timeline_version" in keys else 0
            ab_variants_json = row["ab_variants_json"] if "ab_variants_json" in keys else "{}"
        except (KeyError, IndexError):
            generated_scenes_json = "[]"
            storyboard_json = "{}"
            timeline_json = "{}"
            draft_video_asset_id = None
            final_video_asset_id = None
            final_approval = "pending"
            final_approval_notes = ""
            storyboard_version = 0
            video_generation_version = 0
            timeline_version = 0
            ab_variants_json = "{}"

        generated_scenes_data = json.loads(generated_scenes_json)
        return VideoFactoryProject(
            id=row["id"],
            owner_user_id=row["owner_user_id"],
            status=ProjectStatus(row["status"]),
            resource_pack=_resource(json.loads(row["resource_pack_json"]), row["owner_user_id"]),
            raw_idea=_idea(json.loads(row["raw_idea_json"])),
            creative_brief=_brief(json.loads(row["creative_brief_json"])),
            brief_approval=row["brief_approval"],
            scene_plan=_scene_plan(json.loads(row["scene_plan_json"])),
            scene_plan_approval=row["scene_plan_approval"],
            storyboard=_storyboard(json.loads(storyboard_json)),
            generated_scenes=tuple(_generated_scene(item) for item in generated_scenes_data),
            timeline=_timeline(json.loads(timeline_json)),
            draft_video_asset_id=draft_video_asset_id,
            final_video_asset_id=final_video_asset_id,
            final_approval=FinalApprovalStatus(final_approval),
            final_approval_notes=final_approval_notes,
            ab_variants=_ab_variant_set(json.loads(ab_variants_json)),
            resource_version=row["resource_version"],
            idea_version=row["idea_version"],
            brief_version=row["brief_version"],
            scene_version=row["scene_version"],
            storyboard_version=storyboard_version,
            video_generation_version=video_generation_version,
            timeline_version=timeline_version,
            revision=row["revision"] if "revision" in row.keys() else 0,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_artifact(self, artifact: VideoFactoryArtifact) -> VideoFactoryArtifact:
        # Enforce workspace-relative path only
        p = Path(artifact.relative_path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Artifact relative_path must be workspace-relative and without traversal: {artifact.relative_path}")

        parents_json = json.dumps(list(artifact.parent_artifact_ids), ensure_ascii=True)
        meta_json = json.dumps(artifact.provider_metadata, ensure_ascii=True)
        with self._transaction(immediate=True) as conn:
            # Check collision across different owner or project
            existing = conn.execute(
                "SELECT owner_user_id, project_id FROM video_factory_artifacts WHERE artifact_id = ?",
                (artifact.artifact_id,),
            ).fetchone()
            if existing and (existing["owner_user_id"] != artifact.owner_user_id or existing["project_id"] != artifact.project_id):
                raise ValueError(
                    f"Artifact ID collision: artifact {artifact.artifact_id} already exists for owner {existing['owner_user_id']} / project {existing['project_id']}"
                )

            conn.execute(
                """
                INSERT INTO video_factory_artifacts (
                    artifact_id, owner_user_id, workflow_id, project_id, kind,
                    scene_id, frame_role, version, attempt, relative_path,
                    sha256, size, mime_type, status, parent_artifact_ids_json,
                    provider_metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    workflow_id=excluded.workflow_id,
                    kind=excluded.kind,
                    scene_id=excluded.scene_id,
                    frame_role=excluded.frame_role,
                    version=excluded.version,
                    attempt=excluded.attempt,
                    relative_path=excluded.relative_path,
                    sha256=excluded.sha256,
                    size=excluded.size,
                    mime_type=excluded.mime_type,
                    status=excluded.status,
                    parent_artifact_ids_json=excluded.parent_artifact_ids_json,
                    provider_metadata_json=excluded.provider_metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    artifact.artifact_id, artifact.owner_user_id, artifact.workflow_id,
                    artifact.project_id, artifact.kind, artifact.scene_id,
                    artifact.frame_role, artifact.version, artifact.attempt,
                    artifact.relative_path, artifact.sha256, artifact.size,
                    artifact.mime_type, artifact.status, parents_json,
                    meta_json, artifact.created_at, artifact.updated_at
                ),
            )
        return artifact

    def get_artifact(self, artifact_id: str, owner_user_id: str) -> VideoFactoryArtifact | None:
        if not owner_user_id:
            raise ValueError("owner_user_id is required to fetch an artifact")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM video_factory_artifacts WHERE artifact_id = ? AND owner_user_id = ?",
                (artifact_id, owner_user_id),
            ).fetchone()
        return _artifact_from_row(row) if row else None

    def list_artifacts(self, project_id: str, owner_user_id: str, kind: str | None = None) -> list[VideoFactoryArtifact]:
        if not owner_user_id:
            raise ValueError("owner_user_id is required to list artifacts")
        with self._transaction() as conn:
            query = "SELECT * FROM video_factory_artifacts WHERE project_id = ? AND owner_user_id = ?"
            params: list[Any] = [project_id, owner_user_id]
            if kind:
                query += " AND kind = ?"
                params.append(kind)
            query += " ORDER BY created_at ASC"
            rows = conn.execute(query, tuple(params)).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def save_render_batch(self, batch: RenderBatch) -> RenderBatch:
        with self._transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO video_factory_render_batches (
                    render_batch_id, owner_user_id, workflow_id, project_id,
                    version, status, scene_count, completed_scene_count,
                    timeline_artifact_id, composite_artifact_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(render_batch_id) DO UPDATE SET
                    status=excluded.status,
                    scene_count=excluded.scene_count,
                    completed_scene_count=excluded.completed_scene_count,
                    timeline_artifact_id=excluded.timeline_artifact_id,
                    composite_artifact_id=excluded.composite_artifact_id,
                    updated_at=excluded.updated_at
                """,
                (
                    batch.render_batch_id, batch.owner_user_id, batch.workflow_id,
                    batch.project_id, batch.version, batch.status,
                    batch.scene_count, batch.completed_scene_count,
                    batch.timeline_artifact_id, batch.composite_artifact_id,
                    batch.created_at, batch.updated_at
                ),
            )
        return batch

    def get_render_batch(self, render_batch_id: str) -> RenderBatch | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM video_factory_render_batches WHERE render_batch_id = ?",
                (render_batch_id,),
            ).fetchone()
        return _batch_from_row(row) if row else None

    def list_render_batches(self, project_id: str) -> list[RenderBatch]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM video_factory_render_batches WHERE project_id = ? ORDER BY created_at ASC",
                (project_id,),
            ).fetchall()
        return [_batch_from_row(row) for row in rows]
