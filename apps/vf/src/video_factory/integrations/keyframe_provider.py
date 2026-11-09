"""Vertex Imagen product-keyframe provider with durable no-resubmit checkpoints."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from .ports import (
    KeyframeGenerationPort,
    KeyframeGenerationRequest,
    KeyframeGenerationResult,
)
from .capacity import ProviderCapacityStore

DEFAULT_MODEL = "gemini-2.5-flash-image"
DEFAULT_LOCATION = "us-east4"
MAX_OUTPUT_RECONCILIATION_ATTEMPTS = 3
SUPPORTED_ASPECT_RATIOS = frozenset({"1:1", "3:4", "4:3", "9:16", "16:9"})


def _stable_request_digest(request: KeyframeGenerationRequest) -> str:
    references = []
    for raw_path in request.reference_image_paths:
        path = Path(raw_path).resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        references.append({"path": str(path), "sha256": digest})
    payload = {
        "operation_id": request.operation_id,
        "workflow_id": request.workflow_id,
        "project_id": request.project_id,
        "frame_id": request.frame_id,
        "prompt": request.prompt,
        "product_description": request.product_description,
        "references": references,
        "aspect_ratio": request.aspect_ratio,
        "negative_prompt": request.negative_prompt,
        "provider_options": request.provider_options,
    }
    if request.provider_options.get('creative_mode')=='four_agent':
        payload['references']=[{'sha256':ref['sha256']} for ref in references]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class VertexKeyframeProvider(KeyframeGenerationPort):
    provider_kind = "vertex_keyframe"
    def __init__(
        self,
        *,
        project_id: str | None = None,
        location: str | None = None,
        model: str = DEFAULT_MODEL,
        output_dir: Path | str,
        timeout: float = 120.0,
        credential_resolver: Callable[[], tuple[str | None, str | None]] | None = None,
        post_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.project_id = project_id
        self.location = location
        self.model = model
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.timeout = timeout
        self.credential_resolver = credential_resolver
        self.post_fn = post_fn or requests.post

    def _auth(self) -> tuple[str | None, str | None, str | None]:
        if self.credential_resolver:
            try:
                token, resolved_project = self.credential_resolver()
            except Exception as exc:
                return None, None, f"CREDENTIAL_ERROR: {exc}"
        else:
            try:
                from agent.vertex_adapter import (
                    _resolve_project_override,
                    _resolve_region,
                    get_vertex_credentials,
                )

                token, resolved_project = get_vertex_credentials()
                resolved_region = _resolve_region(self.location)
                self.location = resolved_region if (resolved_region and resolved_region != "global") else DEFAULT_LOCATION
                resolved_project = _resolve_project_override() or resolved_project
            except Exception as exc:
                return None, None, f"CREDENTIAL_ERROR: {exc}"
        project = self.project_id or resolved_project
        if not token:
            return None, None, "MISSING_CREDENTIALS"
        if not project:
            return None, None, "MISSING_PROJECT_ID"
        return token, project, None

    def _output_path(self, operation_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", operation_id).strip("._") or "keyframe"
        output = (self.output_dir / f"{safe[:120]}.png").resolve()
        output.relative_to(self.output_dir)
        return output

    def submit(self, request: KeyframeGenerationRequest) -> KeyframeGenerationResult:
        if not request.prompt.strip() or not request.product_description.strip():
            return KeyframeGenerationResult(
                request.operation_id, "failed", error_code="INVALID_INPUT",
                error_message="prompt and product_description are required",
            )
        if request.aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
            return KeyframeGenerationResult(
                request.operation_id, "failed", error_code="INVALID_INPUT",
                error_message=f"unsupported aspect ratio: {request.aspect_ratio}",
            )
        reference_paths = [Path(value).resolve() for value in request.reference_image_paths]
        if not reference_paths or any(
            not path.is_file() or path.stat().st_size == 0 for path in reference_paths
        ):
            return KeyframeGenerationResult(
                request.operation_id, "failed", error_code="REFERENCE_IMAGE_NOT_FOUND",
                error_message="at least one readable product reference is required",
            )

        token, project, auth_error = self._auth()
        if auth_error:
            return KeyframeGenerationResult(
                request.operation_id, "failed", error_code="AUTH_FAILED",
                error_message=auth_error,
            )
        endpoint = (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{project}"
            f"/locations/{self.location}/publishers/google/models/{self.model}:generateContent"
        )
        empty_start = "outside the image at this START moment" in request.prompt
        subject_instruction = (
            "Generate only the empty scene before the named product enters. "
            "The attached product images are identity references for later frames; the product must not appear in this START image. "
            if empty_start else "Generate only the named product and the described scene. "
        )
        opt = request.provider_options or {}
        anatomy_text = opt.get("anatomy_injection", "")
        full_prompt = f"{request.prompt}\n\n{anatomy_text}".strip() if anatomy_text else request.prompt

        neg = request.negative_prompt or ""
        anatomy_neg = opt.get("anatomy_negative", "")
        full_neg = f"{neg}, {anatomy_neg}".strip(", ") if anatomy_neg else neg

        parts: list[dict[str, Any]] = [{
            "text": (
                f"{full_prompt}\n\n"
                + subject_instruction
                + "Do not add any seller, shop, store, marketplace, watermark, logo, "
                "URL, username, price, or unrelated brand name."
                + (f" Avoid: {full_neg}." if full_neg else "")
            )
        }]
        for path in reference_paths[:4]:
            parts.append({
                "inlineData": {
                    "mimeType": mimetypes.guess_type(path.name)[0] or "image/png",
                    "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
            })
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": request.aspect_ratio},
            },
        }
        ALLOWED_GEN_CONFIG_KEYS = frozenset({"aspectRatio", "temperature", "topP", "topK", "candidateCount"})
        filtered_options = {
            k: v for k, v in request.provider_options.items()
            if k in ALLOWED_GEN_CONFIG_KEYS
        }
        body["generationConfig"].update(filtered_options)
        response = None
        for attempt in range(8):
            try:
                response = self.post_fn(
                    endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                    timeout=self.timeout,
                )
            except Exception as exc:
                return KeyframeGenerationResult(
                    request.operation_id,
                    "unknown",
                    provider_operation_id=request.operation_id,
                    error_code="NETWORK_UNKNOWN_OUTCOME",
                    error_message=str(exc),
                )
            if response is not None and response.status_code == 429 and attempt < 7:
                import time
                time.sleep(10.0 * (attempt + 1))
                continue
            break

        if response is None or response.status_code != 200:
            try:
                message = response.json().get("error", {}).get("message", "") if response is not None else "no response"
            except Exception:
                message = "non-JSON provider response"
            status_code = response.status_code if response is not None else 500
            return KeyframeGenerationResult(
                request.operation_id,
                "failed",
                error_code=f"HTTP_{status_code}",
                error_message=message,
            )
        try:
            candidate = response.json()["candidates"][0]
            image_part = next(
                part for part in candidate["content"]["parts"] if "inlineData" in part
            )
            image_data = image_part["inlineData"]
            image_bytes = base64.b64decode(image_data["data"])
            if not image_bytes:
                raise ValueError("empty generated image")
            output = self._output_path(request.operation_id)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(image_bytes)
        except Exception as exc:
            return KeyframeGenerationResult(
                request.operation_id,
                "failed",
                error_code="INVALID_RESPONSE",
                error_message=str(exc),
            )
        return KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="completed",
            provider_operation_id=request.operation_id,
            image_path=str(output),
            metadata={
                "provider": "google_vertex",
                "model": self.model,
                "mime_type": image_data.get("mimeType") or mimetypes.guess_type(output)[0],
            },
        )

    def get_status(self, operation_id: str) -> KeyframeGenerationResult:
        output = self._output_path(operation_id)
        if output.is_file() and output.stat().st_size > 0:
            return KeyframeGenerationResult(
                operation_id=operation_id,
                status="completed",
                provider_operation_id=operation_id,
                image_path=str(output),
                metadata={"provider": "google_vertex", "model": self.model},
            )
        return KeyframeGenerationResult(
            operation_id=operation_id,
            status="unknown",
            provider_operation_id=operation_id,
            error_code="RECONCILIATION_REQUIRED",
            error_message="synchronous provider outcome is not available locally",
        )


class KeyframeOperationStore:
    DDL = """
    CREATE TABLE IF NOT EXISTS video_factory_keyframe_operation (
        operation_id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        frame_id TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        provider_operation_id TEXT,
        provider_kind TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        output_path TEXT,
        output_sha256 TEXT NOT NULL DEFAULT '',
        provider_metadata_json TEXT NOT NULL DEFAULT '{}',
        error_code TEXT NOT NULL DEFAULT '',
        error_message TEXT NOT NULL DEFAULT '',
        submitted_at TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(self.DDL)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(video_factory_keyframe_operation)")}
        if "provider_metadata_json" not in columns:
            self.connection.execute(
                "ALTER TABLE video_factory_keyframe_operation ADD COLUMN provider_metadata_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "submitted_at" not in columns:
            self.connection.execute(
                "ALTER TABLE video_factory_keyframe_operation ADD COLUMN submitted_at TEXT"
            )
        if "provider_kind" not in columns:
            self.connection.execute(
                "ALTER TABLE video_factory_keyframe_operation ADD COLUMN provider_kind TEXT NOT NULL DEFAULT ''"
            )

    def get(self, operation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM video_factory_keyframe_operation WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return dict(row) if row else None

    def claim_output_reconciliation(
        self, operation_id: str, provider_operation_id: str, *, max_attempts: int = MAX_OUTPUT_RECONCILIATION_ATTEMPTS,
    ) -> int | None:
        """Persist a bounded read/download attempt without changing generation state."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT status, provider_operation_id, provider_metadata_json "
                "FROM video_factory_keyframe_operation WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if (row is None or row["status"] not in {"unknown", "submitting", "project_created"}
                    or str(row["provider_operation_id"] or "") != provider_operation_id):
                self.connection.commit()
                return None
            try:
                metadata = json.loads(row["provider_metadata_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            attempt = int(metadata.get("output_reconciliation_attempts") or 0)
            if attempt >= max_attempts:
                self.connection.commit()
                return None
            attempt += 1
            metadata["output_reconciliation_attempts"] = attempt
            metadata["output_reconciliation_last_attempt_at"] = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                "UPDATE video_factory_keyframe_operation SET provider_metadata_json=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE operation_id=? AND status IN ('unknown','submitting','project_created')",
                (json.dumps(metadata, sort_keys=True), operation_id),
            )
            self.connection.commit()
            return attempt
        except Exception:
            self.connection.rollback()
            raise

    def claim_first_submission(
        self, request: KeyframeGenerationRequest, provider_kind: str = ""
    ) -> bool:
        """Atomically reserve the only first paid submit for this operation."""
        digest = _stable_request_digest(request)
        kind = str(provider_kind or "").strip()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT request_digest, provider_kind FROM video_factory_keyframe_operation WHERE operation_id = ?",
                (request.operation_id,),
            ).fetchone()
            if existing:
                if existing["request_digest"] != digest:
                    raise ValueError("keyframe operation idempotency conflict")
                existing_kind = str(existing["provider_kind"] or "").strip()
                if kind and existing_kind and kind != existing_kind:
                    raise ValueError("keyframe provider_kind mismatch")
                self.connection.commit()
                return False
            submitted_at = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                """INSERT INTO video_factory_keyframe_operation
                (operation_id, workflow_id, project_id, frame_id, request_digest,
                 provider_kind, status, error_code, submitted_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'submitting', 'SUBMISSION_NOT_RECONCILED', ?, CURRENT_TIMESTAMP)""",
                (request.operation_id, request.workflow_id, request.project_id,
                 request.frame_id, digest, kind, submitted_at),
            )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def record_project_checkpoint(
        self,
        request: KeyframeGenerationRequest,
        project_url: str,
        provider_kind: str = "google_flow",
    ) -> None:
        """Atomically persist project URL, operation ID, and request digest before generation Send."""
        digest = _stable_request_digest(request)
        with self.connection:
            row = self.connection.execute(
                "SELECT request_digest, provider_metadata_json, provider_operation_id FROM video_factory_keyframe_operation WHERE operation_id = ?",
                (request.operation_id,),
            ).fetchone()
            if row is None:
                submitted_at = datetime.now(timezone.utc).isoformat()
                meta = {"project_url": project_url}
                self.connection.execute(
                    """INSERT INTO video_factory_keyframe_operation
                    (operation_id, workflow_id, project_id, frame_id, request_digest,
                     provider_operation_id, provider_kind, status, provider_metadata_json,
                     error_code, submitted_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'project_created', ?, 'PROJECT_CREATED_BEFORE_SEND', ?, CURRENT_TIMESTAMP)""",
                    (
                        request.operation_id,
                        request.workflow_id,
                        request.project_id,
                        request.frame_id,
                        digest,
                        project_url,
                        provider_kind,
                        json.dumps(meta, sort_keys=True),
                        submitted_at,
                    ),
                )
            else:
                if row["request_digest"] != digest:
                    raise ValueError("keyframe operation idempotency conflict")
                meta = {}
                if row["provider_metadata_json"]:
                    try:
                        meta = json.loads(row["provider_metadata_json"])
                    except Exception:
                        meta = {}
                meta["project_url"] = project_url
                self.connection.execute(
                    """UPDATE video_factory_keyframe_operation
                    SET provider_operation_id = ?,
                        provider_kind = CASE WHEN provider_kind = '' THEN ? ELSE provider_kind END,
                        provider_metadata_json = ?,
                        status = 'project_created',
                        error_code = 'PROJECT_CREATED_BEFORE_SEND',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE operation_id = ? AND request_digest = ?""",
                    (project_url, provider_kind, json.dumps(meta, sort_keys=True), request.operation_id, digest),
                )

    def save_result(
        self,
        request: KeyframeGenerationRequest,
        result: KeyframeGenerationResult,
        *,
        capacity: ProviderCapacityStore | None = None,
    ) -> None:
        digest = _stable_request_digest(request)
        existing = self.get(request.operation_id)
        if existing and existing["request_digest"] != digest:
            raise ValueError("keyframe operation idempotency conflict")
        if existing and result.status == "unknown" and result.error_code == "OUTPUT_RECONCILIATION_EXHAUSTED":
            # No poll ran. Preserve the last settled outcome for operator recovery.
            return
        existing_kind = str((existing or {}).get("provider_kind") or "").strip()
        result_kind = str((result.metadata or {}).get("provider_kind") or "").strip()
        if existing_kind and result_kind and existing_kind != result_kind:
            raise ValueError("keyframe provider_kind mismatch")
        provider_kind = existing_kind or result_kind
        output_sha = ""
        if result.image_path:
            path = Path(result.image_path)
            if path.is_file() and path.stat().st_size > 0:
                output_sha = hashlib.sha256(path.read_bytes()).hexdigest()

        existing_meta = {}
        if existing and existing.get("provider_metadata_json"):
            try:
                existing_meta = json.loads(existing["provider_metadata_json"])
            except Exception:
                existing_meta = {}
        merged_meta = {**existing_meta, **(result.metadata or {})}
        provider_op_id = result.provider_operation_id or (existing.get("provider_operation_id") if existing else None)
        if provider_op_id and "project_url" not in merged_meta:
            merged_meta["project_url"] = provider_op_id

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO video_factory_keyframe_operation (
                    operation_id, workflow_id, project_id, frame_id, request_digest,
                    provider_operation_id, provider_kind, status, output_path, output_sha256,
                    provider_metadata_json, error_code, error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(operation_id) DO UPDATE SET
                    provider_operation_id=COALESCE(excluded.provider_operation_id, video_factory_keyframe_operation.provider_operation_id),
                    provider_kind=CASE WHEN video_factory_keyframe_operation.provider_kind = '' THEN excluded.provider_kind ELSE video_factory_keyframe_operation.provider_kind END,
                    status=excluded.status,
                    output_path=excluded.output_path,
                    output_sha256=excluded.output_sha256,
                    provider_metadata_json=excluded.provider_metadata_json,
                    error_code=excluded.error_code,
                    error_message=excluded.error_message,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    request.operation_id,
                    request.workflow_id,
                    request.project_id,
                    request.frame_id,
                    digest,
                    provider_op_id,
                    provider_kind,
                    result.status,
                    result.image_path,
                    output_sha,
                    json.dumps(merged_meta, sort_keys=True),
                    result.error_code,
                    result.error_message,
                ),
            )
            if capacity is not None and capacity.connection is self.connection:
                capacity.release_terminal_in_transaction(provider_kind, request.operation_id)
        if capacity is not None and capacity.connection is not self.connection:
            capacity.release_terminal(provider_kind, request.operation_id)


class KeyframeRuntime:
    def __init__(
        self, *, store: KeyframeOperationStore, provider: KeyframeGenerationPort,
        capacity_store: ProviderCapacityStore | None = None, capacity_limit: int = 1,
    ) -> None:
        self.store = store
        self.provider = provider
        self.capacity_store = capacity_store
        self.capacity_limit = capacity_limit
        self.lease_owner = uuid.uuid4().hex
        self.provider_kind = str(getattr(provider, "provider_kind", "vertex_keyframe"))
        if hasattr(provider, "bind_operation_store"):
            provider.bind_operation_store(store)
        elif hasattr(provider, "store") and getattr(provider, "store", None) is None:
            provider.store = store

    def run(self, request: KeyframeGenerationRequest) -> KeyframeGenerationResult:
        if self.capacity_store and self.store.get(request.operation_id) is None:
            if not self.capacity_store.try_acquire(
                provider_kind=self.provider_kind,
                operation_id=request.operation_id,
                lease_owner=self.lease_owner,
                limit=self.capacity_limit,
                profile_id=str(getattr(self.provider, "profile_id", "") or ""),
            ):
                return KeyframeGenerationResult(
                    request.operation_id, "unknown", error_code="PROVIDER_CAPACITY_WAIT",
                    error_message="provider capacity is full; retry the same operation later",
                )
        claimed = self.store.claim_first_submission(request, self.provider_kind)
        existing = self.store.get(request.operation_id)
        if existing and not claimed:
            if existing["request_digest"] != _stable_request_digest(request):
                raise ValueError("keyframe operation idempotency conflict")
            if existing["status"] == "completed":
                path = Path(existing["output_path"] or "")
                if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == existing["output_sha256"]:
                    return KeyframeGenerationResult(
                        operation_id=request.operation_id,
                        status="completed",
                        provider_operation_id=existing["provider_operation_id"],
                        image_path=str(path),
                    )
            provider_operation_id = existing["provider_operation_id"]
            if existing["status"] == "abandoned_unknown":
                return KeyframeGenerationResult(
                    request.operation_id, "failed", provider_operation_id=provider_operation_id,
                    error_code=existing["error_code"], error_message=existing["error_message"],
                )
            if existing["status"] == "failed":
                if str(existing.get("error_code") or "").upper() not in {
                    "HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504",
                    "PROVIDER_CAPACITY_WAIT", "NETWORK_UNKNOWN_OUTCOME", "RECONCILIATION_REQUIRED"
                }:
                    return KeyframeGenerationResult(
                        operation_id=request.operation_id,
                        status="failed",
                        provider_operation_id=provider_operation_id,
                        error_code=existing["error_code"],
                        error_message=existing["error_message"],
                    )
            if provider_operation_id:
                if self.capacity_store and not self.capacity_store.try_acquire(
                    provider_kind=self.provider_kind,
                    operation_id=request.operation_id,
                    lease_owner=self.lease_owner,
                    limit=self.capacity_limit,
                    profile_id=str(getattr(self.provider, "profile_id", "") or ""),
                    reconciliation_only=True,
                ):
                    return KeyframeGenerationResult(
                        request.operation_id, "unknown", error_code="PROVIDER_CAPACITY_WAIT",
                        error_message="provider capacity is full; retry the same operation later",
                    )
                reconcile_output = getattr(self.provider, "reconcile_existing_output", None)
                result = (
                    reconcile_output(request, provider_operation_id)
                    if callable(reconcile_output)
                    else self.provider.get_status(provider_operation_id)
                )
                if result.operation_id != request.operation_id:
                    result = KeyframeGenerationResult(
                        operation_id=request.operation_id,
                        status=result.status,
                        provider_operation_id=result.provider_operation_id or provider_operation_id,
                        image_path=result.image_path,
                        metadata={**(result.metadata or {}), "project_url": provider_operation_id},
                        error_code=result.error_code,
                        error_message=result.error_message,
                    )
                self._save_result(request, result)
                return result
            return KeyframeGenerationResult(
                request.operation_id,
                "unknown",
                error_code="RECONCILIATION_REQUIRED",
                error_message="checkpoint exists without provider operation id; refusing resubmit",
            )

        try:
            result = self.provider.submit(request)
        except Exception as exc:
            return KeyframeGenerationResult(
                request.operation_id, "unknown", error_code="RECONCILIATION_REQUIRED",
                error_message=str(exc),
            )
        self._save_result(request, result)
        return result

    def _save_result(self, request, result):
        terminal = result.status in {"completed", "failed"} and result.error_code != "RECONCILIATION_REQUIRED"
        self.store.save_result(request, result, capacity=self.capacity_store if terminal else None)
        if not terminal and self.capacity_store and result.provider_operation_id:
            self.capacity_store.bind_provider_operation(
                self.provider_kind, request.operation_id, result.provider_operation_id,
            )
