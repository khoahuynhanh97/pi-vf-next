"""Video Factory canonical edge capability.

This package owns the canonical Video Factory domain, service, SQLite persistence,
Vertex provider, and FastMCP server entry point.
"""
from __future__ import annotations

from .domain import (
    AssetReference, Claim, ClaimStatus, CreativeBrief, FinalApprovalStatus,
    FrameGenerationStatus, FramePrompt, GeneratedScene, ProjectStatus, RawIdea,
    ResourceIdentity, ResourcePack, Scene, ScenePlan, Storyboard,
    StoryboardApprovalStatus, StoryboardFrame, Timeline, TimelineClip,
    TimelineStatus, VideoFactoryProject, VideoGenerationStatus, VideoPrompt,
)
from .service import VideoFactoryService
from .repository import SQLiteVideoFactoryRepository
from .server import mcp, create_brief, create_scene_plan, create_storyboard, render, get_job_status, export

def create_video_factory_mcp():
    """Return the canonical Video Factory FastMCP server."""
    return mcp

def main():
    """Run the canonical Video Factory MCP server over stdio transport."""
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()

__all__ = [
    "VideoFactoryProject",
    "CreativeBrief",
    "ScenePlan",
    "Storyboard",
    "ResourcePack",
    "VideoFactoryService",
    "SQLiteVideoFactoryRepository",
    "create_video_factory_mcp",
    "mcp",
    "main",
    "create_brief",
    "create_scene_plan",
    "create_storyboard",
    "render",
    "get_job_status",
    "export", "AssetReference", "Claim", "ClaimStatus", "FinalApprovalStatus", "FrameGenerationStatus", "FramePrompt", "GeneratedScene", "ProjectStatus", "RawIdea", "ResourceIdentity", "Scene", "StoryboardApprovalStatus", "StoryboardFrame", "Timeline", "TimelineClip", "TimelineStatus", "VideoGenerationStatus", "VideoPrompt",
]
