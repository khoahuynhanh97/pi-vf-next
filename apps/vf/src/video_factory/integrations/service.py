from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from video_factory.integrations.domain import (
    CreativeBrief, FinalApprovalStatus, FrameGenerationStatus, GeneratedScene, ProjectStatus, RawIdea, ResourceIdentity, ResourcePack,
    ScenePlan, Storyboard, StoryboardApprovalStatus, Timeline,
    TimelineStatus, VideoFactoryProject, VideoGenerationStatus,
    new_id,
)
from video_factory.integrations.ports import VideoFactoryRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VideoFactoryService:
    """Validate and persist F1-F5 artifacts; Hermes remains the creative owner."""

    def __init__(self, repository: VideoFactoryRepository) -> None:
        self.repository = repository

    def create_project(self, owner_user_id: str, project_id: str = "") -> VideoFactoryProject:
        owner_user_id = self._required(owner_user_id, "owner_user_id")
        stamp = _now()
        project = VideoFactoryProject(
            id=project_id.strip() or new_id("vfp"),
            owner_user_id=owner_user_id,
            created_at=stamp,
            updated_at=stamp,
        )
        return self.repository.create(project)

    def get_project(self, owner_user_id: str, project_id: str) -> VideoFactoryProject:
        owner_user_id = self._required(owner_user_id, "owner_user_id")
        project = self.repository.get_owned(self._required(project_id, "project_id"), owner_user_id)
        if project is None:
            raise ValueError("PROJECT_NOT_FOUND")
        return project

    def save_resource_pack(self, owner_user_id: str, project_id: str, pack: ResourcePack) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if pack.owner_user_id != owner_user_id:
            raise ValueError("OWNER_MISMATCH")
        for asset in (*pack.product_references, *pack.character_references):
            self._validate_asset_uri(asset.uri)
        if project.resource_pack and project.resource_pack.locked_at:
            if (
                pack.locked_product_identity != project.resource_pack.locked_product_identity
                or pack.locked_character_identity != project.resource_pack.locked_character_identity
            ):
                raise ValueError("RESOURCE_IDENTITY_LOCKED")
        updated = replace(
            project,
            resource_pack=pack,
            resource_version=project.resource_version + 1,
            status=ProjectStatus.RESOURCE_READY,
            updated_at=_now(),
        )
        return self.repository.save(updated)

    def lock_resource_pack(
        self,
        owner_user_id: str,
        project_id: str,
        product_identity: ResourceIdentity,
        character_identity: ResourceIdentity | None = None,
    ) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if project.resource_pack is None:
            raise ValueError("RESOURCE_PACK_REQUIRED")
        locked_pack = replace(
            project.resource_pack,
            locked_product_identity=product_identity,
            locked_character_identity=character_identity,
            locked_at=_now(),
            version=project.resource_pack.version + 1,
        )
        updated = replace(
            project,
            resource_pack=locked_pack,
            resource_version=project.resource_version + 1,
            updated_at=_now(),
        )
        return self.repository.save(updated)

    def unlock_resource_pack(self, owner_user_id: str, project_id: str) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if project.resource_pack is None or not project.resource_pack.locked_at:
            raise ValueError("RESOURCE_PACK_NOT_LOCKED")
        unlocked = replace(
            project.resource_pack,
            locked_product_identity=None,
            locked_character_identity=None,
            locked_at=None,
            version=project.resource_pack.version + 1,
        )
        updated_sb = (
            replace(project.storyboard, approval_status=StoryboardApprovalStatus.PENDING, updated_at=_now())
            if project.storyboard
            else None
        )
        updated = replace(
            project,
            resource_pack=unlocked,
            resource_version=project.resource_version + 1,
            brief_approval="pending",
            scene_plan_approval="pending",
            storyboard=updated_sb,
            draft_video_asset_id=None,
            final_video_asset_id=None,
            final_approval=FinalApprovalStatus.PENDING,
            final_approval_notes="",
            status=ProjectStatus.RESOURCE_READY,
            updated_at=_now(),
        )
        return self.repository.save(updated)

    def save_raw_idea(self, owner_user_id: str, project_id: str, idea: RawIdea) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        updated = replace(
            project,
            raw_idea=idea,
            idea_version=project.idea_version + 1,
            updated_at=_now(),
        )
        return self.repository.save(updated)

    def save_creative_brief(
        self, owner_user_id: str, project_id: str, brief: CreativeBrief
    ) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if project.resource_pack is None or not project.resource_pack.locked_at:
            raise ValueError("RESOURCE_PACK_LOCKED_REQUIRED")
        updated_sb = (
            replace(project.storyboard, approval_status=StoryboardApprovalStatus.PENDING, updated_at=_now())
            if project.storyboard
            else None
        )
        updated = replace(
            project,
            creative_brief=brief,
            brief_version=project.brief_version + 1,
            brief_approval="pending",
            scene_plan_approval="pending",
            storyboard=updated_sb,
            draft_video_asset_id=None,
            final_video_asset_id=None,
            final_approval=FinalApprovalStatus.PENDING,
            final_approval_notes="",
            status=ProjectStatus.BRIEF_READY,
            updated_at=_now(),
        )
        return self.repository.save(updated)

    def approve_creative_brief(self, owner_user_id: str, project_id: str) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if project.creative_brief is None:
            raise ValueError("CREATIVE_BRIEF_REQUIRED")
        updated = replace(project, brief_approval="approved", updated_at=_now())
        return self.repository.save(updated)

    def save_scene_plan(self, owner_user_id: str, project_id: str, plan: ScenePlan) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if project.creative_brief is None or project.brief_approval != "approved":
            raise ValueError("BRIEF_APPROVAL_REQUIRED")
        updated_sb = (
            replace(project.storyboard, approval_status=StoryboardApprovalStatus.PENDING, updated_at=_now())
            if project.storyboard
            else None
        )
        updated = replace(
            project,
            scene_plan=plan,
            scene_version=project.scene_version + 1,
            scene_plan_approval="pending",
            storyboard=updated_sb,
            draft_video_asset_id=None,
            final_video_asset_id=None,
            final_approval=FinalApprovalStatus.PENDING,
            final_approval_notes="",
            status=ProjectStatus.SCENE_PLAN_READY,
            updated_at=_now(),
        )
        return self.repository.save(updated)

    def approve_scene_plan(self, owner_user_id: str, project_id: str) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if project.scene_plan is None:
            raise ValueError("SCENE_PLAN_REQUIRED")
        updated = replace(
            project,
            scene_plan_approval="approved",
            status=ProjectStatus.READY_FOR_STORYBOARD,
            updated_at=_now(),
        )
        return self.repository.save(updated)

    # F2: Storyboard operations

    def save_storyboard(self, owner_user_id: str, project_id: str, storyboard: Storyboard) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if project.scene_plan is None or project.scene_plan_approval != "approved":
            raise ValueError("SCENE_PLAN_APPROVAL_REQUIRED")
        if (
            project.storyboard
            and storyboard.source_scene_version > 0
            and project.storyboard.source_scene_version == storyboard.source_scene_version
            and storyboard.source_scene_version == project.scene_version
        ):
            def intent(frame: Any) -> tuple[Any, ...]:
                return (
                    frame.frame_id, frame.scene_id, frame.order, frame.label,
                    frame.purpose, frame.visual_state, frame.subject_action,
                    frame.product_state, frame.character_state, frame.context,
                    frame.camera_intention, frame.required_resource_ids, frame.prompt,
                )

            if tuple(map(intent, project.storyboard.frames)) == tuple(map(intent, storyboard.frames)):
                return project
            raise ValueError("STORYBOARD_CONFLICT: approved scene plan already has a different storyboard")
        sb_pending = replace(
            storyboard,
            approval_status=StoryboardApprovalStatus.PENDING,
            updated_at=_now(),
        )
        updated = replace(
            project,
            storyboard=sb_pending,
            storyboard_version=project.storyboard_version + 1,
            draft_video_asset_id=None,
            final_video_asset_id=None,
            final_approval=FinalApprovalStatus.PENDING,
            final_approval_notes="",
            status=ProjectStatus.STORYBOARD_READY,
            updated_at=_now(),
        )
        return self.repository.save(updated)

    def update_frame_generation_status(
        self,
        owner_user_id: str,
        project_id: str,
        frame_id: str,
        status: FrameGenerationStatus,
        asset_id: str | None = None,
        job_id: str | None = None,
    ) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if not project.storyboard:
            raise ValueError("STORYBOARD_REQUIRED")

        updated_frames = []
        for frame in project.storyboard.frames:
            if frame.frame_id == frame_id:
                updated_frame = replace(
                    frame,
                    generation_status=status,
                    generated_asset_id=asset_id or frame.generated_asset_id,
                    generation_job_id=job_id or frame.generation_job_id,
                    version=frame.version + 1,
                )
                updated_frames.append(updated_frame)
            else:
                updated_frames.append(frame)

        updated_sb = replace(
            project.storyboard,
            frames=tuple(updated_frames),
            version=project.storyboard.version + 1,
            updated_at=_now(),
        )
        return self.repository.save(
            replace(
                project,
                storyboard=updated_sb,
                storyboard_version=project.storyboard_version + 1,
                updated_at=_now(),
            )
        )

    def approve_storyboard(self, owner_user_id: str, project_id: str, notes: str = "") -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if not project.storyboard:
            raise ValueError("STORYBOARD_REQUIRED")

        updated_sb = replace(
            project.storyboard,
            approval_status=StoryboardApprovalStatus.APPROVED,
            approval_notes=notes,
            updated_at=_now(),
        )
        return self.repository.save(
            replace(
                project,
                storyboard=updated_sb,
                status=ProjectStatus.STORYBOARD_APPROVED,
                updated_at=_now(),
            )
        )

    def reject_storyboard_frame(
        self, owner_user_id: str, project_id: str, frame_id: str, notes: str
    ) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if not project.storyboard:
            raise ValueError("STORYBOARD_REQUIRED")

        updated_frames = []
        for frame in project.storyboard.frames:
            if frame.frame_id == frame_id:
                updated_frame = replace(
                    frame,
                    generation_status=FrameGenerationStatus.REJECTED,
                    review_notes=notes,
                    version=frame.version + 1,
                )
                updated_frames.append(updated_frame)
            else:
                updated_frames.append(frame)

        updated_sb = replace(
            project.storyboard,
            frames=tuple(updated_frames),
            approval_status=StoryboardApprovalStatus.REVISION_REQUIRED,
            version=project.storyboard.version + 1,
            updated_at=_now(),
        )
        return self.repository.save(
            replace(
                project,
                storyboard=updated_sb,
                storyboard_version=project.storyboard_version + 1,
                updated_at=_now(),
            )
        )

    def require_storyboard_approved(self, owner_user_id: str, project_id: str) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if not project.storyboard or project.storyboard.approval_status != StoryboardApprovalStatus.APPROVED:
            raise ValueError("STORYBOARD_APPROVAL_REQUIRED")
        return project

    # F3: Video generation operations

    def save_generated_scene(
        self, owner_user_id: str, project_id: str, scene: GeneratedScene
    ) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if project.storyboard and project.storyboard.approval_status != StoryboardApprovalStatus.APPROVED:
            raise ValueError("STORYBOARD_APPROVAL_REQUIRED")

        updated_scenes = list(project.generated_scenes)
        found = False
        for i, existing in enumerate(updated_scenes):
            if existing.scene_id == scene.scene_id:
                updated_scenes[i] = scene
                found = True
                break
        if not found:
            updated_scenes.append(scene)

        return self.repository.save(
            replace(
                project,
                generated_scenes=tuple(updated_scenes),
                video_generation_version=project.video_generation_version + 1,
                status=ProjectStatus.SCENES_GENERATED,
                updated_at=_now(),
            )
        )

    def update_scene_generation_status(
        self,
        owner_user_id: str,
        project_id: str,
        scene_id: str,
        status: VideoGenerationStatus,
        asset_id: str | None = None,
        job_id: str | None = None,
        provider_operation_id: str | None = None,
    ) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)

        updated_scenes = []
        for scene in project.generated_scenes:
            if scene.scene_id == scene_id:
                updated_scene = replace(
                    scene,
                    generation_status=status,
                    generated_asset_id=asset_id or scene.generated_asset_id,
                    generation_job_id=job_id or scene.generation_job_id,
                    provider_operation_id=provider_operation_id or scene.provider_operation_id,
                    version=scene.version + 1,
                    updated_at=_now(),
                )
                updated_scenes.append(updated_scene)
            else:
                updated_scenes.append(scene)

        return self.repository.save(
            replace(
                project,
                generated_scenes=tuple(updated_scenes),
                video_generation_version=project.video_generation_version + 1,
                updated_at=_now(),
            )
        )

    # F4: Timeline operations

    def save_timeline(self, owner_user_id: str, project_id: str, timeline: Timeline) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if not project.generated_scenes:
            raise ValueError("GENERATED_SCENES_REQUIRED")
        scenes_by_id = {scene.scene_id: scene for scene in project.generated_scenes}
        for clip in timeline.clips:
            scene = scenes_by_id.get(clip.source_asset_id)
            if scene is None:
                scene = next(
                    (s for s in project.generated_scenes if s.generated_asset_id == clip.source_asset_id),
                    None,
                )
            if scene is None:
                raise ValueError(f"TIMELINE_CLIP_SOURCE_NOT_GENERATED: {clip.source_asset_id}")
            if scene.generation_status != VideoGenerationStatus.COMPLETED or not scene.generated_asset_id:
                raise ValueError(
                    f"GENERATED_SCENE_ASSET_REQUIRED: scene {scene.scene_id} has no generated video asset"
                )
        return self.repository.save(
            replace(
                project,
                timeline=timeline,
                timeline_version=project.timeline_version + 1,
                draft_video_asset_id=None,
                final_video_asset_id=None,
                final_approval=FinalApprovalStatus.PENDING,
                final_approval_notes="",
                status=ProjectStatus.TIMELINE_READY,
                updated_at=_now(),
            )
        )

    def update_timeline_status(
        self, owner_user_id: str, project_id: str, status: TimelineStatus
    ) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if not project.timeline:
            raise ValueError("TIMELINE_REQUIRED")

        updated_timeline = replace(project.timeline, status=status, updated_at=_now())
        return self.repository.save(
            replace(
                project,
                timeline=updated_timeline,
                draft_video_asset_id=None if status != TimelineStatus.COMPLETED else project.draft_video_asset_id,
                final_video_asset_id=None if status != TimelineStatus.COMPLETED else project.final_video_asset_id,
                final_approval=FinalApprovalStatus.PENDING if status != TimelineStatus.COMPLETED else project.final_approval,
                final_approval_notes="" if status != TimelineStatus.COMPLETED else project.final_approval_notes,
                updated_at=_now(),
            )
        )

    def save_draft_video(self, owner_user_id: str, project_id: str, asset_id: str) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if not project.timeline:
            raise ValueError("TIMELINE_REQUIRED")
        if project.timeline.status != TimelineStatus.COMPLETED:
            raise ValueError("TIMELINE_NOT_COMPLETED")

        return self.repository.save(
            replace(
                project,
                draft_video_asset_id=asset_id,
                final_video_asset_id=None,
                final_approval=FinalApprovalStatus.PENDING,
                final_approval_notes="",
                status=ProjectStatus.DRAFT_VIDEO_READY,
                updated_at=_now(),
            )
        )

    # F5: Final review and export

    def approve_final_video(self, owner_user_id: str, project_id: str, notes: str = "") -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if not project.draft_video_asset_id:
            raise ValueError("DRAFT_VIDEO_REQUIRED")

        return self.repository.save(
            replace(
                project,
                final_approval=FinalApprovalStatus.APPROVED,
                final_approval_notes=notes,
                updated_at=_now(),
            )
        )

    def save_final_export(self, owner_user_id: str, project_id: str, asset_id: str) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if project.final_approval != FinalApprovalStatus.APPROVED:
            raise ValueError("FINAL_APPROVAL_REQUIRED")

        return self.repository.save(
            replace(
                project,
                final_video_asset_id=asset_id,
                status=ProjectStatus.READY_TO_PUBLISH,
                updated_at=_now(),
            )
        )

    def request_final_revision(self, owner_user_id: str, project_id: str, notes: str) -> VideoFactoryProject:
        project = self.get_project(owner_user_id, project_id)
        if not project.draft_video_asset_id:
            raise ValueError("DRAFT_VIDEO_REQUIRED")

        return self.repository.save(
            replace(
                project,
                final_approval=FinalApprovalStatus.REVISION_REQUIRED,
                final_approval_notes=notes,
                updated_at=_now(),
            )
        )

    @staticmethod
    def _required(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
        return value.strip()

    @staticmethod
    def _validate_asset_uri(uri: str) -> None:
        value = uri.strip()
        if value.startswith(("http://", "https://", "s3://", "asset://")):
            return
        configured = os.environ.get("HERMES_VIDEO_FACTORY_WORKSPACE", "").strip()
        root = Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()
        candidate = (Path(value).expanduser() if Path(value).is_absolute() else root / value).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            # Allow workspace or project directory relatives
            pass
