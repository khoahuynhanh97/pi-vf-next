"""Generic challenged web acquisition contracts.

HTTP-first acquisition with bounded browser fallback. A challenged or blocked
source is preserved as an observation and must not fail higher-level research.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from media.models import ImageCandidate


class AcquisitionStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CHALLENGED = "challenged"
    FAILED = "failed"


class ChallengeSignal(StrEnum):
    CAPTCHA_VISIBLE = "captcha_visible"
    SECURITY_CHECK_TEXT = "security_check_text"
    ROBOT_VERIFICATION = "robot_verification"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
    SLIDER_CHALLENGE = "slider_challenge"
    ACCESS_VERIFICATION = "access_verification"
    LOGIN_WALL = "login_wall"
    VERIFY_TRAFFIC = "verify_traffic"


class HttpAcquisitionRequest(BaseModel):
    url: str
    timeout_seconds: float = 20.0
    max_response_bytes: int = 2 * 1024 * 1024


class BrowserAcquisitionRequest(BaseModel):
    url: str
    timeout_seconds: float = 30.0
    wait_strategy: Literal["domcontentloaded", "networkidle", "load"] = "domcontentloaded"
    capture_html: bool = False
    capture_text: bool = True
    discover_images: bool = True


class SourceObservation(BaseModel):
    requested_url: str
    method: Literal["http", "browser"]
    status: AcquisitionStatus
    final_url: str | None = None
    domain: str | None = None
    http_status: int | None = None
    page_title: str | None = None
    signals: list[ChallengeSignal] = Field(default_factory=list)
    content_summary: dict = Field(default_factory=dict)
    reason: str | None = None
    observed_at: str = ""


class ExtractedPageContent(BaseModel):
    title: str | None = None
    meta_description: str | None = None
    canonical_url: str | None = None
    og: dict = Field(default_factory=dict)
    json_ld: list[dict] = Field(default_factory=list)
    visible_text: str | None = None
    image_urls: list[str] = Field(default_factory=list)


class HttpAcquisitionResult(BaseModel):
    observation: SourceObservation
    content: ExtractedPageContent | None = None


class BrowserAcquisitionResult(BaseModel):
    observation: SourceObservation
    content: ExtractedPageContent | None = None


class WebAcquisitionResult(BaseModel):
    """Combined result preserving every observation made."""

    requested_url: str
    status: AcquisitionStatus
    observations: list[SourceObservation] = Field(default_factory=list)
    content: ExtractedPageContent | None = None
    image_candidates: list[ImageCandidate] = Field(default_factory=list)
    reason: str | None = None
