"""Google Cloud Vertex AI video generation provider (Veo REST API).

Ports the proven Vertex Veo submit/poll contract from the live test baseline.
Reuses agent.vertex_adapter for profile-scoped ADC / token / project resolution.
Supports injected credential and HTTP post callables for offline testing.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Callable

import requests

from video_factory.integrations.ports import (
    VideoGenerationPort,
    VideoGenerationRequest,
    VideoGenerationResult,
)

logger = logging.getLogger(__name__)

SUPPORTED_ASPECT_RATIOS = frozenset({"9:16", "16:9"})
SUPPORTED_RESOLUTIONS = frozenset({"720p", "1080p"})
ALLOWED_DURATIONS = frozenset({4, 6, 8})
DEFAULT_MODEL = "veo-3.1-generate-001"
DEFAULT_LOCATION = "us-central1"


def mime_for(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"


def vertex_model_endpoint(project: str, location: str, model: str, action: str) -> str:
    """Build Vertex publisher model endpoint for predictLongRunning / fetchPredictOperation."""
    location = (location or DEFAULT_LOCATION).strip()
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{location}/publishers/google/models/{model}:{action}"
    )


class VertexVideoProvider(VideoGenerationPort):
    """Google Cloud Vertex Veo video generation provider.

    Implements VideoGenerationPort using the predictLongRunning and
    fetchPredictOperation REST endpoints.
    """

    provider_kind: str = "vertex_ai"
    supports_end_keyframe: bool = True

    def __init__(
        self,
        project_id: str | None = None,
        location: str | None = None,
        model: str | None = None,
        output_dir: Path | str | None = None,
        timeout: float = 60.0,
        credential_resolver: Callable[[], tuple[str | None, str | None]] | None = None,
        post_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.provider_kind = "vertex_ai"
        self._explicit_project = project_id
        self.location = (location or "").strip()
        self.model = (model or DEFAULT_MODEL).strip()
        self.timeout = timeout
        self._credential_resolver = credential_resolver
        self._post_fn = post_fn or requests.post

        configured_dir = output_dir or ""
        self.output_dir = Path(configured_dir).expanduser().resolve() if configured_dir else (Path.cwd() / "workspace")

    def is_available(self) -> bool:
        token, proj, _ = self._resolve_auth()
        return bool(token and proj)

    def _resolve_auth(self) -> tuple[str | None, str | None, str | None]:
        """Resolve (token, project_id, error_message).

        Never raises; fails closed before network calls.
        """
        if self._credential_resolver is not None:
            try:
                token, proj = self._credential_resolver()
                if not token:
                    return None, None, "MISSING_CREDENTIALS: Credential resolver returned no access token"
                effective_project = self._explicit_project or proj
                if not effective_project:
                    return None, None, "MISSING_PROJECT_ID: Credential resolver returned no project ID"
                return token, effective_project, None
            except Exception as exc:
                return None, None, f"CREDENTIAL_ERROR: {exc}"

        try:
            try:
                from agent.vertex_adapter import (
                    _resolve_project_override,
                    _resolve_region,
                    get_vertex_credentials,
                )
            except ImportError:
                import sys
                for extra in (Path(r"D:\work\hermes-agent"), Path(r"D:\work\hermes-upstream-rearchitecture")):
                    if extra.is_dir() and str(extra) not in sys.path:
                        sys.path.insert(0, str(extra))
                from agent.vertex_adapter import (
                    _resolve_project_override,
                    _resolve_region,
                    get_vertex_credentials,
                )

            token, proj = get_vertex_credentials()
            if not token:
                return None, None, "MISSING_CREDENTIALS: No Google Cloud credentials resolved via vertex_adapter"
            effective_project = self._explicit_project or _resolve_project_override() or proj
            if not effective_project:
                return None, None, "MISSING_PROJECT_ID: Google Cloud project ID is not set"
            # Override location if configured in vertex_adapter
            resolved_region = _resolve_region(self.location)
            self.location = resolved_region if (resolved_region and resolved_region != "global") else DEFAULT_LOCATION
            return token, effective_project, None
        except ImportError:
            return None, None, "DEPENDENCY_MISSING: google-auth is not installed for Vertex AI"
        except Exception as exc:
            return None, None, f"CREDENTIAL_ERROR: {exc}"

    def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Submit an async video generation request to Vertex AI."""
        # 1. Validate request parameters
        val_err = self._validate_request(request)
        if val_err:
            return VideoGenerationResult(
                request_id=request.request_id,
                status="failed",
                error_code="INVALID_INPUT",
                error_message=val_err,
                metadata={"provider": "google_vertex"},
            )

        # 2. Resolve credentials
        token, project_id, auth_err = self._resolve_auth()
        if auth_err:
            return VideoGenerationResult(
                request_id=request.request_id,
                status="failed",
                error_code="AUTH_FAILED",
                error_message=auth_err,
                metadata={"provider": "google_vertex"},
            )

        # 3. Construct body
        try:
            body = self._build_submit_body(request)
        except Exception as exc:
            return VideoGenerationResult(
                request_id=request.request_id,
                status="failed",
                error_code="INVALID_INPUT",
                error_message=f"Failed to encode reference image: {exc}",
                metadata={"provider": "google_vertex"},
            )

        # 4. Dispatch HTTP POST
        endpoint = vertex_model_endpoint(project_id, self.location, self.model, "predictLongRunning")
        try:
            response = self._post_fn(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=self.timeout,
            )
        except Exception as exc:
            return VideoGenerationResult(
                request_id=request.request_id,
                status="unknown",
                error_code="NETWORK_UNKNOWN_OUTCOME",
                error_message=str(exc),
                metadata={"provider": "google_vertex", "reconcile_required": True},
            )

        if response.status_code != 200:
            return VideoGenerationResult(
                request_id=request.request_id,
                status="failed",
                error_code=f"HTTP_{response.status_code}",
                error_message=self._normalize_error(response),
                metadata={"provider": "google_vertex", "http_status": response.status_code},
            )

        try:
            res_json = response.json()
        except Exception:
            return VideoGenerationResult(
                request_id=request.request_id,
                status="unknown",
                error_code="RECONCILIATION_REQUIRED",
                error_message="predictLongRunning response was not parseable; reconcile before retrying",
                metadata={"provider": "google_vertex", "http_status": response.status_code, "reconcile_required": True},
            )

        operation_name = res_json.get("name")
        if not operation_name:
            return VideoGenerationResult(
                request_id=request.request_id,
                status="unknown",
                error_code="RECONCILIATION_REQUIRED",
                error_message="predictLongRunning response contained no operation name; reconcile before retrying",
                metadata={"provider": "google_vertex", "reconcile_required": True},
            )

        return VideoGenerationResult(
            request_id=request.request_id,
            status="submitted",
            provider_operation_id=operation_name,
            metadata={"provider": "google_vertex", "model": self.model, "status": "submitted"},
        )

    async def generate_video(
        self,
        prompt: str,
        reference_image: Path | None = None,
        duration: int = 6,
        aspect_ratio: str = "9:16",
    ) -> Path:
        """Generate video via Vertex Veo with optional reference image."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        request_body: dict[str, Any] = {
            "prompt": prompt,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
        }

        if reference_image and Path(reference_image).exists():
            ref_path = Path(reference_image)
            img_bytes = ref_path.read_bytes()
            img_b64 = base64.b64encode(img_bytes).decode("ascii")
            request_body["reference_images"] = [
                {
                    "image": {"bytesBase64Encoded": img_b64},
                    "config": {"reference_type": "SUBJECT"},
                }
            ]
            logger.info("Added reference image to Veo request: %s", ref_path.name)

        op_id = hashlib.sha256(f"{prompt}_{reference_image}_{duration}".encode()).hexdigest()[:16]
        req = VideoGenerationRequest(
            request_id=f"veo-{op_id}",
            prompt=prompt,
            duration_seconds=duration,
            aspect_ratio=aspect_ratio,
            reference_image_paths=(str(reference_image),) if reference_image else (),
        )
        res = self.submit(req)
        if res.status == "completed" and res.video_path:
            return Path(res.video_path)
        raise RuntimeError(
            f"Vertex video generation did not produce a local output: "
            f"{res.status}/{res.error_code or 'NO_OUTPUT'} {res.error_message}"
        )

    def get_status(self, operation_id: str) -> VideoGenerationResult:
        """Poll the status of an ongoing Vertex operation."""
        if not operation_id or not operation_id.strip():
            return VideoGenerationResult(
                request_id="",
                status="failed",
                error_code="INVALID_OPERATION_ID",
                error_message="operation_id cannot be empty",
                metadata={"provider": "google_vertex"},
            )

        token, project_id, auth_err = self._resolve_auth()
        if auth_err:
            return VideoGenerationResult(
                request_id=operation_id,
                status="failed",
                error_code="AUTH_FAILED",
                error_message=auth_err,
                metadata={"provider": "google_vertex"},
            )

        endpoint = vertex_model_endpoint(project_id, self.location, self.model, "fetchPredictOperation")
        try:
            response = self._post_fn(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json={"operationName": operation_id},
                timeout=self.timeout,
            )
        except Exception as exc:
            return VideoGenerationResult(
                request_id=operation_id,
                status="unknown",
                provider_operation_id=operation_id,
                error_code="NETWORK_UNKNOWN_OUTCOME",
                error_message=str(exc),
                metadata={"provider": "google_vertex", "reconcile_required": True},
            )

        if response.status_code != 200:
            return VideoGenerationResult(
                request_id=operation_id,
                status="failed",
                provider_operation_id=operation_id,
                error_code=f"HTTP_{response.status_code}",
                error_message=self._normalize_error(response),
                metadata={"provider": "google_vertex", "http_status": response.status_code},
            )

        try:
            body = response.json()
        except Exception:
            return VideoGenerationResult(
                request_id=operation_id,
                status="unknown",
                provider_operation_id=operation_id,
                error_code="RECONCILIATION_REQUIRED",
                error_message="fetchPredictOperation response was not parseable; reconcile before retrying",
                metadata={"provider": "google_vertex", "reconcile_required": True},
            )

        error = body.get("error")
        if error:
            err_msg = error.get("message", "") or f"vertex operation error: {error.get('status', '')}"
            return VideoGenerationResult(
                request_id=operation_id,
                status="failed",
                provider_operation_id=operation_id,
                error_code="PROVIDER_ERROR",
                error_message=err_msg,
                metadata={"provider": "google_vertex"},
            )

        if not body.get("done"):
            return VideoGenerationResult(
                request_id=operation_id,
                status="running",
                provider_operation_id=operation_id,
                metadata={"provider": "google_vertex", "status": "running"},
            )

        return self._decode_and_save_result(operation_id, body.get("response") or {})

    def _validate_request(self, request: VideoGenerationRequest) -> str | None:
        if not request.prompt or not request.prompt.strip():
            return "prompt cannot be empty"
        if request.duration_seconds not in ALLOWED_DURATIONS:
            return f"duration_seconds must be one of {sorted(ALLOWED_DURATIONS)}, got {request.duration_seconds}"
        if request.aspect_ratio and request.aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
            return f"aspect_ratio must be one of {sorted(SUPPORTED_ASPECT_RATIOS)}, got {request.aspect_ratio}"
        if request.reference_image_paths:
            for p in request.reference_image_paths:
                path_obj = Path(p)
                if not path_obj.is_file():
                    return f"Reference image does not exist or is not a readable file: {p}"
        if request.end_keyframe is not None:
            if not request.reference_image_paths:
                return "end_keyframe requires a start reference image"
            if not Path(request.end_keyframe.image_path).is_file():
                return f"End keyframe does not exist or is not a readable file: {request.end_keyframe.image_path}"
        return None

    def _build_submit_body(self, request: VideoGenerationRequest) -> dict[str, Any]:
        instance: dict[str, Any] = {"prompt": request.prompt}
        if request.reference_image_paths:
            first = Path(request.reference_image_paths[0])
            instance["image"] = {
                "bytesBase64Encoded": base64.b64encode(first.read_bytes()).decode("ascii"),
                "mimeType": mime_for(first),
            }
        if request.reference_image_paths and not request.provider_options.get("approved_keyframe"):
            # Visual grounding reference_images array for Veo
            ref_list = []
            for ref_p in request.reference_image_paths:
                p = Path(ref_p)
                if p.is_file():
                    ref_list.append({
                        "image": {"bytesBase64Encoded": base64.b64encode(p.read_bytes()).decode("ascii")},
                        "config": {"reference_type": "SUBJECT"},
                    })
            if ref_list:
                instance["reference_images"] = ref_list
        if request.end_keyframe is not None:
            last = Path(request.end_keyframe.image_path)
            instance["lastFrame"] = {
                "bytesBase64Encoded": base64.b64encode(last.read_bytes()).decode("ascii"),
                "mimeType": mime_for(last),
            }

        parameters: dict[str, Any] = {
            "durationSeconds": int(request.duration_seconds),
        }
        if self.model.startswith("veo-3"):
            # Veo 3 requires an explicit audio choice. The approved scene plan
            # requests voiceover only when it has narration.
            parameters["generateAudio"] = request.request_audio
        if request.aspect_ratio in SUPPORTED_ASPECT_RATIOS:
            parameters["aspectRatio"] = request.aspect_ratio

        provider_options = request.provider_options or {}
        for key in ("resolution", "sampleCount", "seed", "enhancePrompt", "generateAudio", "personGeneration", "negativePrompt"):
            if key in provider_options:
                val = provider_options[key]
                if key == "resolution" and val not in SUPPORTED_RESOLUTIONS:
                    continue
                parameters[key] = val

        return {"instances": [instance], "parameters": parameters}

    def _decode_and_save_result(self, operation_id: str, response: dict[str, Any]) -> VideoGenerationResult:
        videos = response.get("videos") or []
        if videos:
            video = videos[0]
            data = video.get("bytesBase64Encoded")
            if data:
                segment = operation_id.rsplit("/", 1)[-1]
                segment = "".join(c for c in segment if c.isalnum() or c in "-_.") or "video"
                self.output_dir.mkdir(parents=True, exist_ok=True)
                output_path = (self.output_dir / f"{segment[:40]}.mp4").resolve()

                # Verify workspace containment
                try:
                    output_path.relative_to(self.output_dir.resolve())
                except ValueError:
                    return VideoGenerationResult(
                        request_id=operation_id,
                        status="failed",
                        provider_operation_id=operation_id,
                        error_code="PATH_TRAVERSAL",
                        error_message="Computed output path escapes configured workspace directory",
                        metadata={"provider": "google_vertex"},
                    )

                output_path.write_bytes(base64.b64decode(data))
                return VideoGenerationResult(
                    request_id=operation_id,
                    status="completed",
                    video_path=str(output_path),
                    provider_operation_id=operation_id,
                    metadata={"provider": "google_vertex", "status": "completed"},
                )

        gcs_uri = response.get("gcsUri") or (videos[0].get("video", {}).get("gcsUri") if videos else None)
        if gcs_uri:
            return VideoGenerationResult(
                request_id=operation_id,
                status="failed",
                error_code="GCS_OUTPUT_UNSUPPORTED",
                error_message=f"provider returned gcsUri but inline decoding was expected: {gcs_uri}",
                provider_operation_id=operation_id,
                metadata={"provider": "google_vertex", "gcs_uri": gcs_uri},
            )

        return VideoGenerationResult(
            request_id=operation_id,
            status="failed",
            error_code="NO_VIDEO_BYTES",
            error_message="vertex response contained no video bytes",
            provider_operation_id=operation_id,
            metadata={"provider": "google_vertex"},
        )

    @staticmethod
    def _normalize_error(response: requests.Response) -> str:
        try:
            body = response.json()
            message = body.get("error", {}).get("message", "")
            return f"vertex http {response.status_code}: {message}" if message else f"vertex http {response.status_code}"
        except Exception:
            return f"vertex http {response.status_code}: non-JSON response (endpoint/model may not exist)"

    def lookup_by_request_id(self, request_id: str) -> VideoGenerationResult | None:
        """Lookup an operation by client request ID.

        Vertex Veo REST API does not provide a client request-id reverse lookup endpoint;
        returns None so caller fails closed to controlled operator reconciliation.
        """
        return None
