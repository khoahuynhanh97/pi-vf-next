from __future__ import annotations
from typing import Any, List, Optional, Dict
from pydantic import BaseModel, Field, ConfigDict
from enum import Enum

class QCVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    UNKNOWN = "unknown"

class QCScorecardSchema(BaseModel):
    report_id: str
    stage: str
    score: int = Field(ge=0, le=100)
    verdict: QCVerdict
    findings: Dict[str, Any] = Field(default_factory=dict)
    created_at: str

class ResearchDossierSchema(BaseModel):
    product_id: str
    brand: str
    model: str
    product_name: str
    verified_claims: List[str] = Field(default_factory=list)
    images: List[Dict[str, Any]] = Field(default_factory=list)

class CreativeBriefSchema(BaseModel):
    brief_id: str
    hook: str
    target_audience: str
    target_duration: int
    script: List[Dict[str, str]] = Field(default_factory=list)

class SceneSchema(BaseModel):
    scene_id: str
    main_action: str
    prompt: str
    duration: int

class KeyframeSchema(BaseModel):
    frame_id: str
    scene_id: str
    image_url: Optional[str] = None
    prompt: str
    status: str = "pending"

class VideoWorkflowAggregate(BaseModel):
    # extra="allow" keeps aggregate keys this model does not declare
    # (locked_pack, source_context, latest_artifact, ...) so validation
    # never silently drops pipeline state on re-serialization.
    model_config = ConfigDict(
        from_attributes=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="allow",
    )

    workflow_id: str
    project_id: Optional[str] = None
    state: str
    owner_user_id: str = "local-owner"
    created_at: Optional[str] = None
    
    research_dossier: Optional[ResearchDossierSchema] = None
    creative_brief: Optional[CreativeBriefSchema] = None
    scene_plan: Optional[List[SceneSchema]] = None
    storyboard: Optional[List[KeyframeSchema]] = None
    
    qc_reports: List[QCScorecardSchema] = Field(default_factory=list)
    render_result: Optional[Dict[str, Any]] = None
    export_result: Optional[Dict[str, Any]] = None
    
    qc_authorization: Optional[Dict[str, Any]] = None
    selected_render_provider: Optional[str] = "google_flow"
    
    pending_approval_id: Optional[str] = None
    pending_approval_gate: Optional[str] = None
