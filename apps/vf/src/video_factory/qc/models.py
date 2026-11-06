"""Domain models for Product Video Grounded Content and Multimodal QC."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class QCStage(str, Enum):
    IMAGE = "image"
    KEYFRAME = "keyframe"
    VIDEO = "video"


class QCVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NormalizedProduct:
    brand: str
    model: str
    variant: str = ""
    display_name: str = ""
    category: str = "Thiết bị tiện ích"
    verified_claims: list[str] = field(default_factory=list)
    approved_media: list[dict[str, Any]] = field(default_factory=list)
    source_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand": self.brand,
            "model": self.model,
            "variant": self.variant,
            "display_name": self.display_name,
            "category": self.category,
            "verified_claims": list(self.verified_claims),
            "approved_media": list(self.approved_media),
            "source_context": dict(self.source_context),
        }


@dataclass
class ImageQCItem:
    asset_id: str
    uri: str
    digest: str
    score: int
    identity_score: int
    clarity_score: int
    usability_score: int
    verdict: QCVerdict
    eligible: bool
    hard_failure: bool
    reason: str
    findings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "uri": self.uri,
            "digest": self.digest,
            "score": self.score,
            "identity_score": self.identity_score,
            "clarity_score": self.clarity_score,
            "usability_score": self.usability_score,
            "verdict": self.verdict.value,
            "eligible": self.eligible,
            "hard_failure": self.hard_failure,
            "reason": self.reason,
            "findings": self.findings,
        }


@dataclass
class QCReport:
    report_id: str
    workflow_id: str
    stage: QCStage
    artifact_id: str
    artifact_version: int
    artifact_digest: str
    reference_digests: list[str]
    evaluator_name: str
    evaluator_version: str
    rubric_version: str
    created_at: str
    score: int
    verdict: QCVerdict
    findings: dict[str, Any]
    sample_timestamps: list[float] | None = None
    fingerprint: str = ""
    provider_mode: str = "configured"
    model_identifier: str = ""
    policy_version: str = ""
    owner_user_id: str = ""
    project_id: str = ""
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "workflow_id": self.workflow_id,
            "stage": self.stage.value,
            "artifact_id": self.artifact_id,
            "artifact_version": self.artifact_version,
            "artifact_digest": self.artifact_digest,
            "reference_digests": list(self.reference_digests),
            "evaluator_name": self.evaluator_name,
            "evaluator_version": self.evaluator_version,
            "rubric_version": self.rubric_version,
            "created_at": self.created_at,
            "score": self.score,
            "verdict": self.verdict.value,
            "findings": self.findings,
            "sample_timestamps": list(self.sample_timestamps) if self.sample_timestamps is not None else None,
            "fingerprint": self.fingerprint,
            "provider_mode": self.provider_mode,
            "model_identifier": self.model_identifier,
            "policy_version": self.policy_version,
            "owner_user_id": self.owner_user_id,
            "project_id": self.project_id,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QCReport:
        artifact_id = str(data.get("artifact_id") or data.get("report_id") or "")
        return cls(
            report_id=str(data["report_id"]),
            workflow_id=str(data["workflow_id"]),
            stage=QCStage(data["stage"]),
            artifact_id=artifact_id,
            artifact_version=int(data.get("artifact_version", 1)),
            artifact_digest=str(data["artifact_digest"]),
            reference_digests=list(data.get("reference_digests", [])),
            evaluator_name=str(data.get("evaluator_name", "unknown")),
            evaluator_version=str(data.get("evaluator_version", "1.0")),
            rubric_version=str(data.get("rubric_version", "1.0")),
            created_at=str(data["created_at"]),
            score=int(data["score"]),
            verdict=QCVerdict(data["verdict"]),
            findings=dict(data.get("findings", {})),
            sample_timestamps=data.get("sample_timestamps"),
            fingerprint=str(data.get("fingerprint", "")),
            provider_mode=str(data.get("provider_mode", "configured")),
            model_identifier=str(data.get("model_identifier", "")),
            policy_version=str(data.get("policy_version", "")),
            owner_user_id=str(data.get("owner_user_id", "")),
            project_id=str(data.get("project_id", "")),
            cost_usd=(float(data["cost_usd"]) if data.get("cost_usd") is not None else None),
        )
