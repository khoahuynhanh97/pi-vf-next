"""Grounded, resumable video-scene compilation and paid-operation checkpoints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from PIL import Image

from video_factory.job_contracts import TerminalJobError
from .ports import (
    EndKeyframeInput,
    OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS,
    VideoGenerationPort,
    VideoGenerationRequest,
    VideoGenerationResult,
    validate_product_video_render_provider,
)
from .provider_selection import (
    ProviderSelectionError,
    SelectedVideoProvider,
    VideoProviderSelector,
    validate_reconciled_result,
)
from .capacity import ProviderCapacityStore, configured_capacity, process_lease_owner, process_lease_owner_alive
from .scene_validator import validate_scene_output


SUPPORTED_SCENE_DURATIONS = frozenset({4, 6, 8})
MANDATORY_VIDEO_NEGATIVE_PROMPT = (
    "promotional text, banner text, overlay typography, sale badge, floating letters, advertising graphics, "
    "readable text, captions, subtitles, watermark, seller name, shop name, store logo, "
    "marketplace UI, price tag, rating badge, social handle, shipping label, unrelated brand "
    "mark, source-store branding, malformed product, changed logo, changed product "
    "shape, changed port placement, changed display shape, wrong variant, duplicate product, "
    "duplicated product, extra product, malformed hands, malformed cables, cable entering screen, "
    "cable entering forbidden region, unrelated accessories, black bars, letterboxing, square composition, "
    "static pan, static zoom, slideshow"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _merge_negative_prompt(value: Any) -> str:
    chunks = [str(value or ""), MANDATORY_VIDEO_NEGATIVE_PROMPT]
    seen: set[str] = set()
    result: list[str] = []
    for item in ",".join(chunks).split(","):
        normalized = item.strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return ", ".join(result)


def _validate_keyframe_receipt(
    *,
    keyframe: dict[str, Any],
    receipt: dict[str, Any],
    package_digest: str,
    qc_level: str,
) -> None:
    verdict = receipt.get("verdict")
    ungraded_low_qc = qc_level == "low" and verdict in {"unknown", "not_performed"}
    if receipt.get("stage") != "keyframe" or (verdict != "pass" and not ungraded_low_qc):
        raise ValueError("generated keyframe requires a Keyframe QC PASS receipt")
    findings = receipt.get("findings") if isinstance(receipt.get("findings"), dict) else {}
    if receipt.get("hard_failure") or findings.get("hard_failure"):
        raise ValueError("generated keyframe QC receipt contains a hard failure")
    if not package_digest or str(receipt.get("artifact_digest") or "") != package_digest:
        raise ValueError("keyframe QC artifact digest does not match the approved package digest")

    items = findings.get("items") if isinstance(findings.get("items"), list) else []
    frame_id = str(keyframe.get("frame_id") or "")
    item = next(
        (
            value
            for value in items
            if isinstance(value, dict) and str(value.get("frame_id") or "") == frame_id
        ),
        None,
    )
    if ungraded_low_qc:
        if item and (item.get("verdict") == "fail" or item.get("hard_failure")):
            raise ValueError(f"keyframe {frame_id!r} failed QC")
        try:
            with Image.open(Path(str(keyframe.get("image_path") or ""))) as image:
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ValueError(f"cannot decode generated keyframe {frame_id!r}") from exc
        return
    if item is None:
        raise ValueError(f"keyframe QC receipt has no binding for frame {frame_id!r}")
    if (
        item.get("verdict") != "pass"
        or bool(item.get("hard_failure"))
    ):
        raise ValueError(f"keyframe {frame_id!r} did not pass QC or contains a hard failure")

    expected_sha = str(keyframe.get("sha256") or "").removeprefix("sha256:")
    item_sha = str(item.get("sha256") or "").removeprefix("sha256:")
    if not expected_sha or item_sha != expected_sha:
        raise ValueError(f"keyframe QC SHA-256 binding mismatch for frame {frame_id!r}")
    references = sorted(
        str(value).removeprefix("sha256:")
        for value in keyframe.get("reference_sha256s", [])
        if value
    )
    # END frames may use the approved START frame as an additional conditioning
    # image. Older storyboard packages record that digest separately from the
    # product-reference digests; Keyframe QC evaluates both images.
    conditioning_start_sha = str(keyframe.get("conditioning_start_sha256") or "").removeprefix("sha256:")
    if conditioning_start_sha and conditioning_start_sha not in references:
        references.append(conditioning_start_sha)
        references.sort()
    qc_references = sorted(
        str(value).removeprefix("sha256:")
        for value in item.get("reference_digests", [])
        if value
    )
    if not references or qc_references != references:
        raise ValueError(f"keyframe QC reference binding mismatch for frame {frame_id!r}")


def compile_grounded_scene_request(
    *,
    owner_user_id: str,
    workflow_id: str,
    project_id: str,
    scene: dict[str, Any],
    keyframe: dict[str, Any],
    end_keyframe: dict[str, Any] | None = None,
    keyframe_qc_receipt: dict[str, Any],
    package_digest: str,
    product_identity: str,
    provider_model: str,
    aspect_ratio: str = "9:16",
    provider_options: dict[str, Any] | None = None,
    scene_prompt: dict[str, Any] | None = None,
    qc_level: str = "middle",
) -> VideoGenerationRequest:
    """Compile one approved scene without leaking overlay copy to the video model."""
    scene_id = str(scene.get("scene_id") or "").strip()
    if not scene_id:
        raise ValueError("scene_id is required")
    duration = int(scene.get("duration_seconds") or 0)
    if duration not in SUPPORTED_SCENE_DURATIONS:
        raise ValueError(
            f"{scene_id} requests {duration}s; allowed values are 4, 6, 8"
        )
    required = (
        "subject_action",
        "product_interaction",
        "start_state",
        "end_state",
        "visual_change",
        "environment",
    )
    missing = [name for name in required if not str(scene.get(name) or "").strip()]
    if missing:
        raise ValueError(f"{scene_id} is missing grounded scene fields: {', '.join(missing)}")

    subject_action = str(scene["subject_action"]).strip()
    camera_only = subject_action.casefold().strip(" .") in {
        "zoom in",
        "zoom out",
        "pan left",
        "pan right",
        "rotate product",
        "show product",
        "demonstrate product",
    }
    if camera_only:
        raise ValueError(f"{scene_id} subject_action must describe a physical action")

    if str(keyframe.get("scene_id") or "") != scene_id:
        raise ValueError("generated keyframe scene binding mismatch")
    if str(keyframe.get("frame_role") or "start").lower() != "start":
        raise ValueError("scene START keyframe has the wrong frame role")
    _validate_keyframe_receipt(
        keyframe=keyframe,
        receipt=keyframe_qc_receipt,
        package_digest=package_digest,
        qc_level=qc_level,
    )
    keyframe_path = Path(str(keyframe.get("image_path") or "")).resolve()
    expected_sha = str(keyframe.get("sha256") or "").removeprefix("sha256:")
    if (
        not keyframe_path.is_file()
        or keyframe_path.stat().st_size == 0
        or _sha256(keyframe_path) != expected_sha
    ):
        raise ValueError("generated keyframe is missing, empty, or changed after QC")

    end_input = None
    policy = str(scene.get("end_keyframe_policy") or "omitted")
    if policy in {"generated", "required"} and end_keyframe is None:
        raise ValueError("required END keyframe is missing")
    if end_keyframe is not None:
        if policy not in {"generated", "required"}:
            raise ValueError("END keyframe is not permitted by the scene policy")
        if str(end_keyframe.get("scene_id") or "") != scene_id:
            raise ValueError("generated END keyframe scene binding mismatch")
        if str(end_keyframe.get("frame_role") or "").lower() != "end":
            raise ValueError("generated END keyframe has the wrong frame role")
        _validate_keyframe_receipt(
            keyframe=end_keyframe,
            receipt=keyframe_qc_receipt,
            package_digest=package_digest,
            qc_level=qc_level,
        )
        end_path = Path(str(end_keyframe.get("image_path") or "")).resolve()
        end_sha = str(end_keyframe.get("sha256") or "").removeprefix("sha256:")
        if not end_path.is_file() or end_path.stat().st_size == 0 or _sha256(end_path) != end_sha:
            raise ValueError("generated END keyframe is missing, empty, or changed after QC")
        end_input = EndKeyframeInput(
            frame_id=str(end_keyframe.get("frame_id") or ""),
            image_path=str(end_path),
            sha256=end_sha,
            package_digest=package_digest,
            qc_report_id=str(keyframe_qc_receipt.get("report_id") or ""),
        )

    if not workflow_id.strip():
        raise ValueError("workflow_id is required for deterministic scene operation identity")
    operation_material = f"{workflow_id}|{scene_id}|{package_digest}|{provider_model}|{duration}"
    operation_id = "video-scene-" + hashlib.sha256(
        operation_material.encode()
    ).hexdigest()[:24]
    interaction_pieces = []
    if scene.get("interaction_actor"):
        interaction_pieces.append(f"Interaction actor: {str(scene['interaction_actor']).strip()}")
    if scene.get("interaction_target"):
        interaction_pieces.append(f"Interaction target: {str(scene['interaction_target']).strip()}")
    if scene.get("contact_landmark"):
        interaction_pieces.append(f"Contact landmark: {str(scene['contact_landmark']).strip()}")
    if scene.get("approach_vector"):
        interaction_pieces.append(f"Approach vector: {str(scene['approach_vector']).strip()}")
    if scene.get("object_orientation"):
        interaction_pieces.append(f"Object orientation: {str(scene['object_orientation']).strip()}")
    forbidden = scene.get("forbidden_contact_regions", [])
    if forbidden:
        interaction_pieces.append(f"FORBIDDEN contact regions (do NOT touch or insert into): {', '.join(str(f) for f in forbidden)}")
    interaction_block = (". ".join(interaction_pieces) + ".") if interaction_pieces else ""

    prompt_parts = [
        f"Create a product-grounded vertical video scene for {product_identity}.",
        f"Subject action: {subject_action}.",
        f"Product interaction: {str(scene['product_interaction']).strip()}.",
    ]
    if interaction_block:
        prompt_parts.append(interaction_block)
    prompt_parts.extend([
        f"Exact start state: {str(scene['start_state']).strip()}.",
        f"Exact end state: {str(scene['end_state']).strip()}.",
        f"Required visible change: {str(scene['visual_change']).strip()}.",
        f"Environment: {str(scene['environment']).strip()}.",
        f"Camera framing: {str(scene.get('camera_framing') or 'product-focused medium close-up').strip()}.",
        f"Camera movement: {str(scene.get('camera_movement') or 'subtle action-following movement').strip()}.",
        f"Product placement: {str(scene.get('product_placement') or 'keep the approved product fully visible and unobstructed').strip()}.",
        f"Lighting: {str(scene.get('lighting') or 'preserve true product color and material').strip()}.",
        f"Continuity from prior scene: {str(scene.get('continuity_from_previous') or 'preserve the same approved product unit').strip()}.",
        f"Continuity into next scene: {str(scene.get('continuity_to_next') or 'keep identity, placement and orientation stable').strip()}.",
        "Required identity cues: "
        + ", ".join(str(value) for value in scene.get("product_identity_cues", []))
        + ".",
        "Required visible details: "
        + ", ".join(str(value) for value in scene.get("required_visible_details", []))
        + ".",
        "Preserve the approved product identity, exact model and variant, proportions, color, material, controls, logo placement and distinctive details.",
        "Animate the physical action and state transition; do not create a static pan, zoom, rotation or slideshow.",
        "Use the approved keyframe as the exact visual start state; do not redesign, replace, multiply or morph the product.",
        "Do not render any readable text, captions, watermark, marketplace UI, seller branding or unrelated logo.",
    ])
    narration = str(scene.get("narration") or "").strip()
    if narration:
        prompt_parts.append(
            f'If audio is supported, speak this exact Vietnamese narration as an off-screen voiceover: "{narration}". '
            "Do not add subtitles, extra claims, or other spoken words."
        )
    prompt = " ".join(part for part in prompt_parts if part)
    if scene_prompt is not None:
        if (scene_prompt.get('scene_id') != scene_id or
                subject_action not in str(scene_prompt.get('video_prompt') or '') or
                list(scene_prompt.get('reference_asset_ids', [])) != list(scene.get('reference_asset_ids', []))):
            raise ValueError('Director action/reference binding mismatch')
        from video_factory.creative.contracts import canonical_digest
        prompt = str(scene_prompt['video_prompt'])
        operation_id = 'video-scene-' + canonical_digest({'workflow':workflow_id, 'scene':scene,
            'prompt':scene_prompt, 'start_sha':expected_sha, 'end_sha':end_input.sha256 if end_input else '',
            'provider':provider_model,'profile':(provider_options or {}).get('creative_profile_digest'),
            'product_truth':(provider_options or {}).get('product_truth_digest')}).removeprefix('sha256:')[:24]
    options = dict(provider_options or {})
    options["negativePrompt"] = _merge_negative_prompt(options.get("negativePrompt"))
    if scene_prompt is not None:
        options['negativePrompt'] = str(scene_prompt.get('negative_prompt') or '')
        options['creative_mode'] = 'four_agent'
    options["approved_keyframe"] = {
        "frame_id": keyframe.get("frame_id"),
        "sha256": expected_sha,
        "package_digest": package_digest,
        "qc_report_id": keyframe_qc_receipt.get("report_id"),
    }
    options["end_keyframe_policy"] = policy
    if policy == "omitted":
        options["end_keyframe_omission_reason"] = str(
            scene.get("end_keyframe_omission_reason") or ""
        )
    return VideoGenerationRequest(
        request_id=operation_id,
        owner_user_id=owner_user_id,
        project_id=project_id,
        scene_id=scene_id,
        prompt=prompt,
        duration_seconds=duration,
        reference_image_paths=(str(keyframe_path),),
        aspect_ratio=aspect_ratio,
        provider_options=options,
        end_keyframe=end_input,
        request_audio=False if scene_prompt is not None else bool(narration),
    )


def stable_scene_request_digest(request: VideoGenerationRequest) -> str:
    references = []
    for raw_path in request.reference_image_paths:
        path = Path(raw_path).resolve()
        references.append(
            {
                "path": str(path),
                "sha256": _sha256(path) if path.is_file() else "",
            }
        )
    payload = {
        "request_id": request.request_id,
        "owner_user_id": request.owner_user_id,
        "project_id": request.project_id,
        "scene_id": request.scene_id,
        "prompt": request.prompt,
        "duration_seconds": request.duration_seconds,
        "reference_images": references,
        "aspect_ratio": request.aspect_ratio,
        "provider_options": request.provider_options,
        "end_keyframe": (
            {
                "frame_id": request.end_keyframe.frame_id,
                "image_path": request.end_keyframe.image_path,
                "sha256": request.end_keyframe.sha256,
                "package_digest": request.end_keyframe.package_digest,
                "qc_report_id": request.end_keyframe.qc_report_id,
            }
            if request.end_keyframe else None
        ),
    }
    if request.request_audio:
        payload["request_audio"] = True
    if request.provider_options.get('creative_mode')=='four_agent':
        # QC/batch lineage is checked at the board gate; it is not a new provider intent.
        options=dict(payload['provider_options'])
        options.pop('creative_package',None)
        if isinstance(options.get('approved_keyframe'),dict):
            options['approved_keyframe']={k:v for k,v in options['approved_keyframe'].items()
                                         if k not in {'package_digest','qc_report_id'}}
        if isinstance(options.get('selected_keyframe'),dict):
            options['selected_keyframe']={k:v for k,v in options['selected_keyframe'].items() if k!='package_digest'}
        payload['provider_options']=options
        payload['reference_images']=[{'sha256':ref['sha256']} for ref in payload['reference_images']]
        if payload['end_keyframe']:
            payload['end_keyframe']={k:v for k,v in payload['end_keyframe'].items()
                                      if k not in {'package_digest','qc_report_id','image_path'}}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ProviderClaimResult:
    status: Literal[
        "claimed",
        "already_claimed_same_provider",
        "provider_conflict",
        "invalid_state",
        "digest_conflict",
        "send_limit_exceeded",
    ]
    operation: dict[str, Any] | None
    message: str

    @property
    def can_submit(self) -> bool:
        return self.status == "claimed"


class SceneRenderOperationStore:
    DDL = """
    CREATE TABLE IF NOT EXISTS video_factory_scene_operation (
        operation_id TEXT PRIMARY KEY,
        owner_user_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        scene_id TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        provider_operation_id TEXT,
        status TEXT NOT NULL,
        output_path TEXT,
        output_sha256 TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        error_message TEXT NOT NULL DEFAULT '',
        render_batch_id TEXT NOT NULL DEFAULT '',
        scene_order INTEGER NOT NULL DEFAULT 0,
        attempt INTEGER NOT NULL DEFAULT 1,
        artifact_id TEXT NOT NULL DEFAULT '',
        expected_duration_seconds REAL NOT NULL DEFAULT 0,
        expected_aspect_ratio TEXT NOT NULL DEFAULT '9:16',
        provider_kind TEXT NOT NULL DEFAULT '',
        request_json TEXT NOT NULL DEFAULT '{}',
        submitted_at TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """

    def __init__(self, connection: sqlite3.Connection | str | Path) -> None:
        if isinstance(connection, (str, Path)):
            self.connection = sqlite3.connect(str(connection))
            self._owns_connection = True
        else:
            self.connection = connection
            self._owns_connection = False
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(self.DDL)
        self._ensure_migrations()

    def _ensure_migrations(self) -> None:
        cur = self.connection.execute("PRAGMA table_info(video_factory_scene_operation)")
        cols = {r[1] for r in cur.fetchall()}
        migrations = {
            "render_batch_id": "ALTER TABLE video_factory_scene_operation ADD COLUMN render_batch_id TEXT NOT NULL DEFAULT ''",
            "scene_order": "ALTER TABLE video_factory_scene_operation ADD COLUMN scene_order INTEGER NOT NULL DEFAULT 0",
            "attempt": "ALTER TABLE video_factory_scene_operation ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
            "artifact_id": "ALTER TABLE video_factory_scene_operation ADD COLUMN artifact_id TEXT NOT NULL DEFAULT ''",
            "expected_duration_seconds": "ALTER TABLE video_factory_scene_operation ADD COLUMN expected_duration_seconds REAL NOT NULL DEFAULT 0",
            "expected_aspect_ratio": "ALTER TABLE video_factory_scene_operation ADD COLUMN expected_aspect_ratio TEXT NOT NULL DEFAULT '9:16'",
            "provider_kind": "ALTER TABLE video_factory_scene_operation ADD COLUMN provider_kind TEXT NOT NULL DEFAULT ''",
            "request_json": "ALTER TABLE video_factory_scene_operation ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}'",
            "submitted_at": "ALTER TABLE video_factory_scene_operation ADD COLUMN submitted_at TEXT",
        }
        for col_name, stmt in migrations.items():
            if col_name not in cols:
                self.connection.execute(stmt)
        self.connection.executescript("CREATE INDEX IF NOT EXISTS idx_vf_scene_op_batch ON video_factory_scene_operation(render_batch_id, scene_order)")

    def get(self, operation_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM video_factory_scene_operation WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def request_from_record(record: dict[str, Any]) -> VideoGenerationRequest:
        raw = json.loads(record.get("request_json") or "{}")
        if not isinstance(raw, dict) or not raw:
            raise ValueError("planned scene has no durable request payload")
        end = raw.get("end_keyframe")
        if isinstance(end, dict):
            raw["end_keyframe"] = EndKeyframeInput(**end)
        raw["reference_image_paths"] = tuple(raw.get("reference_image_paths") or ())
        request = VideoGenerationRequest(**raw)
        if stable_scene_request_digest(request) != record["request_digest"]:
            raise ValueError("planned scene request digest changed")
        return request

    def retry_reconciled_flow_scene_on_vertex(
        self, operation_id: str, *, reconciliation_evidence: str
    ) -> dict[str, Any]:
        return self._retry_reconciled_flow_scene(
            operation_id, reconciliation_evidence=reconciliation_evidence, provider_kind="vertex_ai",
        )

    def fallback_rejected_flow_scene(self, operation_id: str) -> dict[str, Any]:
        return self._retry_reconciled_flow_scene(
            operation_id, reconciliation_evidence="", provider_kind="vertex_ai",
            confirmed_rejection=True,
        )

    def is_rejected_flow_fallback(self, operation: dict[str, Any]) -> bool:
        attempt = int(operation["attempt"])
        if attempt < 2 or operation["provider_kind"] != "vertex_ai":
            return False
        original_id, suffix, retry_attempt = operation["operation_id"].rpartition("-attempt-")
        if not suffix or retry_attempt != str(attempt):
            return False
        original = self.get(original_id)
        if not original or not self.is_flow_activity_rejection(original):
            return False
        request = replace(self.request_from_record(original), request_id=operation["operation_id"])
        return (
            original["attempt"] == attempt - 1
            and all(original[key] == operation[key] for key in (
                "render_batch_id", "owner_user_id", "project_id", "scene_id", "scene_order",
                "expected_duration_seconds", "expected_aspect_ratio",
            ))
            and operation["request_digest"] == stable_scene_request_digest(request)
        )

    @staticmethod
    def is_flow_activity_rejection(operation: dict[str, Any] | None) -> bool:
        return (
            operation is not None
            and operation["status"] == "failed"
            and operation["provider_kind"] == "google_flow"
            and operation["error_code"] == "PUBLIC_ERROR_UNUSUAL_ACTIVITY"
            and bool(operation["provider_operation_id"])
            and bool(operation["render_batch_id"])
            and not operation["output_path"]
            and not operation["output_sha256"]
        )

    def retry_reconciled_flow_scene(
        self, operation_id: str, *, reconciliation_evidence: str
    ) -> dict[str, Any]:
        return self._retry_reconciled_flow_scene(
            operation_id, reconciliation_evidence=reconciliation_evidence, provider_kind="google_flow",
        )

    def replace_inaccessible_flow_scene(
        self, operation_id: str, *, project_url: str, reconciliation_evidence: str,
    ) -> dict[str, Any]:
        """Retire an inaccessible project locally and start a new attempt on the current account."""
        if not project_url.strip():
            raise ValueError("The inaccessible Flow project URL is required")
        return self._retry_reconciled_flow_scene(
            operation_id, reconciliation_evidence=reconciliation_evidence,
            provider_kind="google_flow", inaccessible_project_url=project_url,
        )

    def _retry_reconciled_flow_scene(
        self, operation_id: str, *, reconciliation_evidence: str, provider_kind: str,
        confirmed_rejection: bool = False, inaccessible_project_url: str | None = None,
    ) -> dict[str, Any]:
        """Create a new attempt after confirmed rejection or operator reconciliation."""
        if not confirmed_rejection and not reconciliation_evidence.strip():
            raise ValueError("Flow reconciliation evidence is required")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM video_factory_scene_operation WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("scene operation not found")
            old = dict(row)
            if confirmed_rejection and not self.is_flow_activity_rejection(old):
                raise ValueError("Flow operation has no confirmed unusual activity rejection")
            if old["provider_kind"] != "google_flow" or not old["provider_operation_id"]:
                raise ValueError("only a pinned Flow operation can use this recovery")
            if old["status"] not in {"submitted", "running", "failed"}:
                raise ValueError("Flow operation is not eligible for reconciled recovery")
            if old["output_path"] or old["output_sha256"]:
                raise ValueError("Flow operation already has output")
            replacement_code = "OPERATOR_RECONCILED_FAILED"
            if inaccessible_project_url is not None:
                replacement_code = "OPERATOR_FLOW_PROJECT_INACCESSIBLE"
                if old["provider_operation_id"] != inaccessible_project_url or (
                    old["status"] == "failed" and old["error_code"] != replacement_code
                ):
                    raise ValueError("Only the exact inaccessible, unresolved project may be replaced")
                lease = self.connection.execute(
                    "SELECT lease_expires_at, lease_owner FROM video_factory_provider_capacity "
                    "WHERE provider_kind='google_flow' AND operation_id=?",
                    (operation_id,),
                ).fetchone()
                if lease and (float(lease[0]) > datetime.now(timezone.utc).timestamp()
                              or process_lease_owner_alive(lease[1])):
                    raise ValueError("An active Flow lease cannot be replaced")
            elif provider_kind == "google_flow" and (
                old["status"] != "failed" or old["attempt"] != 1
                or old["error_code"] not in {"SCENE_TIMEOUT_EXPIRED", "OPERATOR_RECONCILED_FAILED"}
            ):
                raise ValueError("Flow can only retry a reconciled initial scene timeout")
            request = self.request_from_record(old)
            next_attempt = int(old["attempt"]) + 1
            retry = replace(request, request_id=f"{operation_id}-attempt-{next_attempt}")
            retry_digest = stable_scene_request_digest(retry)
            existing = self.connection.execute(
                "SELECT * FROM video_factory_scene_operation WHERE operation_id = ?",
                (retry.request_id,),
            ).fetchone()
            if existing:
                if ((not confirmed_rejection and old["error_code"] != replacement_code)
                        or dict(existing)["request_digest"] != retry_digest
                        or dict(existing)["provider_kind"] != provider_kind):
                    raise ValueError("scene recovery conflicts with an existing attempt")
                self.connection.commit()
                return dict(existing)
            if (not confirmed_rejection and old["status"] == "failed" and old["error_code"] != "OPERATOR_RECONCILED_FAILED"
                    and provider_kind != "google_flow"):
                raise ValueError("Flow failure is not operator-reconciled")
            active = self.connection.execute(
                "SELECT COUNT(*) FROM video_factory_scene_operation "
                "WHERE render_batch_id = ? AND scene_id = ? AND attempt > ?",
                (old["render_batch_id"], old["scene_id"], old["attempt"]),
            ).fetchone()[0]
            if active:
                raise ValueError("a newer scene attempt already exists")
            if not confirmed_rejection:
                self.connection.execute(
                    "UPDATE video_factory_scene_operation SET status = 'failed', "
                    "error_code = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE operation_id = ? AND status IN ('submitted', 'running', 'failed')",
                    (replacement_code, reconciliation_evidence, operation_id),
                )
                if inaccessible_project_url is not None:
                    self.connection.execute(
                        "DELETE FROM video_factory_provider_capacity WHERE provider_kind='google_flow' AND operation_id=?",
                        (operation_id,),
                    )
            self.connection.execute(
                """
                INSERT INTO video_factory_scene_operation (
                    operation_id, owner_user_id, project_id, scene_id, request_digest,
                    provider_operation_id, status, output_path, output_sha256,
                    error_code, error_message, render_batch_id, scene_order, attempt,
                    expected_duration_seconds, expected_aspect_ratio, provider_kind,
                    request_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 'planned', NULL, '', '', '', ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    retry.request_id, old["owner_user_id"], old["project_id"],
                    old["scene_id"], retry_digest, old["render_batch_id"],
                    old["scene_order"], next_attempt, old["expected_duration_seconds"],
                    old["expected_aspect_ratio"], provider_kind, json.dumps(asdict(retry), sort_keys=True),
                ),
            )
            result = self.get(retry.request_id)
            self.connection.commit()
            return dict(result)
        except Exception:
            self.connection.rollback()
            raise

    def retry_qc_failed_scene(
        self,
        operation_id: str,
        *,
        qc_report_id: str,
        provider_kind: str | None = None,
        feedback: str = "",
    ) -> dict[str, Any]:
        """Plan a new scene attempt after video QC failure, with bounded feedback."""
        if not qc_report_id.strip():
            raise ValueError("failed video QC report ID is required")
        clean_feedback = " ".join(str(feedback or "").split())[:500]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM video_factory_scene_operation WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("scene operation not found")
            old = dict(row)
            if old["status"] != "completed" or not old["output_sha256"]:
                raise ValueError("QC retry requires a completed scene clip")
            retry_provider_kind = str(provider_kind or old["provider_kind"] or "").strip()
            if retry_provider_kind not in OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS:
                raise ValueError("QC retry requires a configured official video provider")
            request = self.request_from_record(old)
            next_attempt = int(old["attempt"]) + 1
            prompt = request.prompt
            if clean_feedback:
                prompt = f"{prompt}\n\nVideo QC recovery guidance: {clean_feedback}"
            retry = replace(
                request,
                request_id=f"{operation_id}-attempt-{next_attempt}",
                prompt=prompt,
            )
            retry_digest = stable_scene_request_digest(retry)
            existing = self.connection.execute(
                "SELECT * FROM video_factory_scene_operation WHERE operation_id = ?",
                (retry.request_id,),
            ).fetchone()
            if existing:
                if (
                    dict(existing)["request_digest"] != retry_digest
                    or dict(existing)["provider_kind"] != retry_provider_kind
                ):
                    raise ValueError("scene retry conflicts with an existing attempt")
                self.connection.commit()
                return dict(existing)
            if self.connection.execute(
                "SELECT COUNT(*) FROM video_factory_scene_operation "
                "WHERE render_batch_id = ? AND scene_id = ? AND attempt > ?",
                (old["render_batch_id"], old["scene_id"], old["attempt"]),
            ).fetchone()[0]:
                raise ValueError("a newer scene attempt already exists")
            self.connection.execute(
                """
                INSERT INTO video_factory_scene_operation (
                    operation_id, owner_user_id, project_id, scene_id, request_digest,
                    provider_operation_id, status, output_path, output_sha256,
                    error_code, error_message, render_batch_id, scene_order, attempt,
                    expected_duration_seconds, expected_aspect_ratio, provider_kind,
                    request_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 'planned', NULL, '', '', ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    retry.request_id, old["owner_user_id"], old["project_id"],
                    old["scene_id"], retry_digest, f"Video QC retry: {qc_report_id}",
                    old["render_batch_id"], old["scene_order"], next_attempt,
                    old["expected_duration_seconds"], old["expected_aspect_ratio"],
                    retry_provider_kind, json.dumps(asdict(retry), sort_keys=True),
                ),
            )
            result = self.get(retry.request_id)
            self.connection.commit()
            return dict(result)
        except Exception:
            self.connection.rollback()
            raise

    def mark_planned(self, request: VideoGenerationRequest, render_batch_id: str = "", scene_order: int = 0) -> None:
        """Insert-only or compare-and-set planning record; never regresses submitting/running/completed states."""
        digest = stable_scene_request_digest(request)
        existing = self.get(request.request_id)
        if existing:
            if existing["request_digest"] != digest:
                saved = self.request_from_record(existing)
                binding = saved.provider_options.get("continuity_binding") or {}
                if binding.get("base_request_digest") != digest:
                    raise ValueError("video scene operation idempotency conflict")
                return
            # If already in submitting, submitted, running, completed, or failed, never regress to planned
            if existing["status"] in ("submitting", "submitted", "running", "completed", "failed"):
                return
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO video_factory_scene_operation (
                    operation_id, owner_user_id, project_id, scene_id,
                    request_digest, provider_operation_id, status, output_path,
                    output_sha256, error_code, error_message, render_batch_id, scene_order,
                    expected_duration_seconds, expected_aspect_ratio, provider_kind,
                    request_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 'planned', NULL, '', '', '', ?, ?, ?, ?, '', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(operation_id) DO UPDATE SET
                    render_batch_id = CASE WHEN video_factory_scene_operation.render_batch_id = '' THEN excluded.render_batch_id ELSE video_factory_scene_operation.render_batch_id END,
                    scene_order = CASE WHEN video_factory_scene_operation.scene_order = 0 THEN excluded.scene_order ELSE video_factory_scene_operation.scene_order END,
                    expected_duration_seconds = CASE WHEN video_factory_scene_operation.expected_duration_seconds = 0 THEN excluded.expected_duration_seconds ELSE video_factory_scene_operation.expected_duration_seconds END,
                    expected_aspect_ratio = CASE WHEN video_factory_scene_operation.expected_aspect_ratio = '9:16' AND video_factory_scene_operation.status = 'planned' THEN excluded.expected_aspect_ratio ELSE video_factory_scene_operation.expected_aspect_ratio END,
                    request_json = CASE WHEN video_factory_scene_operation.request_json = '{}' THEN excluded.request_json ELSE video_factory_scene_operation.request_json END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE video_factory_scene_operation.status = 'planned'
                """,
                (
                    request.request_id,
                    request.owner_user_id,
                    request.project_id,
                    request.scene_id,
                    digest,
                    render_batch_id,
                    scene_order,
                    float(request.duration_seconds),
                    request.aspect_ratio,
                    json.dumps(asdict(request), sort_keys=True),
                ),
            )

    def bind_continuity_start(self, request: VideoGenerationRequest, *, expected_digest: str) -> dict[str, Any]:
        """Bind checked predecessor pixels only while the provider intent is unclaimed."""
        current = self.get(request.request_id)
        if current is None:
            raise ValueError("continuity requires an existing planned operation")
        digest = stable_scene_request_digest(request)
        if current["request_digest"] == digest:
            return current
        if current["status"] != "planned" or current.get("provider_operation_id"):
            raise ValueError("cannot rebind an accepted or claimed scene")
        original = self.request_from_record(current)
        options = dict(request.provider_options)
        binding = options.pop("continuity_binding", None)
        if (not isinstance(binding, dict) or binding.get("base_request_digest") != expected_digest
                or replace(request, reference_image_paths=original.reference_image_paths,
                           provider_options=options) != original):
            raise ValueError("continuity binding changed the approved render intent")
        if (len(request.reference_image_paths) != 1
                or str(Path(request.reference_image_paths[0]).resolve()) != str(Path(binding.get("frame_path", "")).resolve())
                or _sha256(Path(request.reference_image_paths[0])) != binding.get("frame_sha256")):
            raise ValueError("continuity frame is missing or changed")
        with self.connection:
            updated = self.connection.execute(
                """UPDATE video_factory_scene_operation SET request_json=?, request_digest=?, updated_at=CURRENT_TIMESTAMP
                   WHERE operation_id=? AND status='planned' AND request_digest=?
                     AND provider_operation_id IS NULL""",
                (json.dumps(asdict(request), sort_keys=True), digest, request.request_id, expected_digest),
            )
        result = self.get(request.request_id)
        if updated.rowcount != 1 and (result is None or result["request_digest"] != digest):
            raise ValueError("continuity admission changed concurrently; reconcile the claimed request")
        return result

    def mark_submitting(self, request: VideoGenerationRequest) -> bool:
        """Atomic compare-and-set transition to submitting."""
        digest = stable_scene_request_digest(request)
        existing = self.get(request.request_id)
        if existing and existing["request_digest"] != digest:
            raise ValueError("video scene operation idempotency conflict")
        with self.connection:
            cur = self.connection.execute(
                """
                INSERT INTO video_factory_scene_operation (
                    operation_id, owner_user_id, project_id, scene_id,
                    request_digest, provider_operation_id, status, output_path,
                    output_sha256, error_code, error_message, render_batch_id, scene_order,
                    expected_duration_seconds, expected_aspect_ratio, provider_kind, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 'submitting', NULL, '', 'SUBMISSION_NOT_RECONCILED', 'provider submission started without a durable provider operation id', '', 0, ?, ?, '', CURRENT_TIMESTAMP)
                ON CONFLICT(operation_id) DO UPDATE SET
                    status = 'submitting',
                    error_code = 'SUBMISSION_NOT_RECONCILED',
                    error_message = 'provider submission started without a durable provider operation id',
                    expected_duration_seconds = CASE WHEN video_factory_scene_operation.expected_duration_seconds = 0 THEN excluded.expected_duration_seconds ELSE video_factory_scene_operation.expected_duration_seconds END,
                    expected_aspect_ratio = CASE WHEN video_factory_scene_operation.expected_aspect_ratio = '9:16' AND video_factory_scene_operation.status = 'planned' THEN excluded.expected_aspect_ratio ELSE video_factory_scene_operation.expected_aspect_ratio END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE video_factory_scene_operation.status = 'planned'
                """,
                (
                    request.request_id,
                    request.owner_user_id,
                    request.project_id,
                    request.scene_id,
                    digest,
                    float(request.duration_seconds),
                    request.aspect_ratio,
                ),
            )
            return cur.rowcount > 0

    def claim_provider_for_submission(
        self,
        request: VideoGenerationRequest,
        provider_kind: str,
    ) -> "ProviderClaimResult":
        """Atomically pin a provider and claim the only right to submit.

        This is the only initial-submission transition used by
        ``SceneRenderRuntime``.  ``BEGIN IMMEDIATE`` serializes competing
        SQLite writers, while the conditional update makes the claim itself a
        compare-and-set over digest, state, and provider pin.
        """
        kind = str(provider_kind or "").strip()
        if not kind:
            raise ValueError("provider_kind is required")
        if kind not in OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS:
            raise ValueError(f"unsupported Product Video provider_kind: {kind}")
        digest = stable_scene_request_digest(request)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM video_factory_scene_operation WHERE operation_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is None:
                result = ProviderClaimResult(
                    status="invalid_state",
                    operation=None,
                    message="scene operation does not exist",
                )
            else:
                existing = dict(row)
                current_kind = str(existing.get("provider_kind") or "").strip()
                if existing["request_digest"] != digest:
                    result = ProviderClaimResult(
                        status="digest_conflict",
                        operation=existing,
                        message="video scene operation idempotency conflict",
                    )
                elif existing["status"] != "planned":
                    if (
                        existing["status"] == "submitting"
                        and current_kind == kind
                    ):
                        result = ProviderClaimResult(
                            status="already_claimed_same_provider",
                            operation=existing,
                            message="scene submission is already claimed by this provider",
                        )
                    elif current_kind and current_kind != kind:
                        result = ProviderClaimResult(
                            status="provider_conflict",
                            operation=existing,
                            message=(
                                f"scene operation is pinned to {current_kind}, "
                                f"not {kind}"
                            ),
                        )
                    else:
                        result = ProviderClaimResult(
                            status="invalid_state",
                            operation=existing,
                            message=(
                                f"scene operation is in status {existing['status']!r}, "
                                "not planned"
                            ),
                        )
                elif current_kind and current_kind != kind:
                    result = ProviderClaimResult(
                        status="provider_conflict",
                        operation=existing,
                        message=(
                            f"scene operation is pinned to {current_kind}, not {kind}"
                        ),
                    )
                else:
                    if request.provider_options.get('creative_mode')=='four_agent':
                        limit=min(12,int(request.provider_options.get('creative_video_call_limit',12)))
                        sent_total=int(self.connection.execute(
                            'SELECT COUNT(*) FROM video_factory_scene_operation WHERE owner_user_id=? AND project_id=? AND submitted_at IS NOT NULL',
                            (request.owner_user_id,request.project_id)).fetchone()[0])
                        if int(existing.get('scene_order') or 0) > 0:
                            scene_total=int(self.connection.execute(
                                'SELECT COUNT(*) FROM video_factory_scene_operation WHERE owner_user_id=? AND project_id=? AND scene_order=? AND submitted_at IS NOT NULL',
                                (request.owner_user_id,request.project_id,existing['scene_order'])).fetchone()[0])
                        else:
                            scene_total=int(self.connection.execute(
                                'SELECT COUNT(*) FROM video_factory_scene_operation WHERE owner_user_id=? AND project_id=? AND scene_id=? AND submitted_at IS NOT NULL',
                                (request.owner_user_id,request.project_id,request.scene_id)).fetchone()[0])
                        if sent_total>=limit or scene_total>=3:
                            self.connection.rollback()
                            return ProviderClaimResult(status='send_limit_exceeded',operation=existing,
                                message='Creative pilot total or logical-scene generation cap reached')
                    if kind == "vertex_ai":
                        sent_count = int(self.connection.execute(
                            "SELECT COUNT(*) FROM video_factory_scene_operation "
                            "WHERE provider_kind='vertex_ai' AND submitted_at IS NOT NULL"
                        ).fetchone()[0])
                        if sent_count >= 5:
                            result = ProviderClaimResult(
                                status="send_limit_exceeded",
                                operation=existing,
                                message="The pilot limit of five Vertex video submissions has been reached.",
                            )
                            self.connection.commit()
                            return result
                    submitted_at = datetime.now(timezone.utc).isoformat()
                    cursor = self.connection.execute(
                        """
                        UPDATE video_factory_scene_operation
                        SET provider_kind = ?,
                            status = 'submitting',
                            error_code = 'SUBMISSION_NOT_RECONCILED',
                            error_message = 'provider submission started without a durable provider operation id',
                            submitted_at = COALESCE(submitted_at, ?),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE operation_id = ?
                          AND request_digest = ?
                          AND status = 'planned'
                          AND (provider_kind = '' OR provider_kind = ?)
                        """,
                        (kind, submitted_at, request.request_id, digest, kind),
                    )
                    updated = self.get(request.request_id)
                    if cursor.rowcount == 1 and updated is not None:
                        result = ProviderClaimResult(
                            status="claimed",
                            operation=updated,
                            message="provider submission claim acquired",
                        )
                    elif updated is None:
                        result = ProviderClaimResult(
                            status="invalid_state",
                            operation=None,
                            message="scene operation disappeared during provider claim",
                        )
                    elif updated.get("request_digest") != digest:
                        result = ProviderClaimResult(
                            status="digest_conflict",
                            operation=updated,
                            message="video scene operation idempotency conflict",
                        )
                    elif (
                        updated.get("status") == "submitting"
                        and str(updated.get("provider_kind") or "") == kind
                    ):
                        result = ProviderClaimResult(
                            status="already_claimed_same_provider",
                            operation=updated,
                            message="scene submission was claimed by another worker",
                        )
                    else:
                        result = ProviderClaimResult(
                            status="invalid_state",
                            operation=updated,
                            message="scene operation changed during provider claim",
                        )
            self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise

    def bind_provider_kind(self, operation_id: str, provider_kind: str) -> dict[str, Any]:
        """Pin a provider for operator/migration use, with CAS verification."""
        kind = str(provider_kind or "").strip()
        if not kind:
            raise ValueError("provider_kind is required")
        if kind not in OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS:
            raise ValueError(f"unsupported Product Video provider_kind: {kind}")
        existing = self.get(operation_id)
        if not existing:
            raise ValueError(f"Operation {operation_id} not found")
        current = str(existing.get("provider_kind") or "").strip()
        if current and current != kind:
            raise ValueError("provider_kind mismatch")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE video_factory_scene_operation
                SET provider_kind = ?, updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ?
                  AND (
                      (provider_kind = '' AND status IN ('planned', 'submitting'))
                      OR provider_kind = ?
                  )
                """,
                (kind, operation_id, kind),
            )
        stored = self.get(operation_id)
        if stored is None:
            raise ValueError(f"Operation {operation_id} disappeared while binding provider")
        stored_kind = str(stored.get("provider_kind") or "").strip()
        if stored_kind and stored_kind != kind:
            raise ValueError("provider_kind mismatch")
        if not stored_kind and cursor.rowcount == 0:
            raise ValueError(
                f"cannot bind provider_kind while operation is in status {stored['status']!r}"
            )
        if stored_kind != kind:
            raise ValueError("provider_kind was not durably stored")
        return stored

    def bind_provider_operation_id(
        self, operation_id: str, provider_operation_id: str
    ) -> dict[str, Any]:
        """CAS-bind an operation ID discovered during reconciliation."""
        provider_id = str(provider_operation_id or "").strip()
        if not provider_id:
            raise ValueError("provider_operation_id is required")
        existing = self.get(operation_id)
        if not existing:
            raise ValueError(f"Operation {operation_id} not found")
        current = str(existing.get("provider_operation_id") or "").strip()
        if current:
            if current != provider_id:
                raise ValueError("provider_operation_id mismatch")
            return existing
        if existing.get("status") != "submitting":
            raise ValueError(
                f"cannot bind provider operation ID while operation is in status {existing['status']!r}"
            )
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE video_factory_scene_operation
                SET provider_operation_id = ?,
                    error_code = '',
                    error_message = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ?
                  AND status = 'submitting'
                  AND provider_operation_id IS NULL
                """,
                (provider_id, operation_id),
            )
        stored = self.get(operation_id)
        if stored is None:
            raise ValueError(f"Operation {operation_id} disappeared while binding operation ID")
        stored_id = str(stored.get("provider_operation_id") or "").strip()
        if stored_id != provider_id:
            raise ValueError("provider_operation_id was changed by another worker")
        if cursor.rowcount == 0 and stored_id != provider_id:
            raise ValueError("provider operation ID claim was lost")
        return stored

    def finalize_provider_result(
        self,
        operation_id: str,
        result: VideoGenerationResult,
        *,
        capacity: ProviderCapacityStore | None = None,
    ) -> dict[str, Any]:
        """Validate a provider result and CAS the durable operation state.
        
        Returns the final operation state after validation and transition.
        """
        existing = self.get(operation_id)
        if not existing:
            raise ValueError(f"Operation {operation_id} not found")
        if existing.get("error_code") == "OPERATOR_QUARANTINED_UNCERTAIN":
            return dict(existing)
        
        # Verify provider operation ID consistency
        if existing["provider_operation_id"]:
            if result.provider_operation_id and result.provider_operation_id != existing["provider_operation_id"]:
                raise ValueError("provider_operation_id mismatch")
        
        # A failed terminal operation stays failed. A completed operation is only
        # terminal when it already carries the digest produced by validation.
        if existing["status"] == "failed":
            return dict(existing)
        if existing["status"] == "completed":
            if existing.get("output_sha256"):
                return dict(existing)
            return self.mark_integrity_failed(
                operation_id,
                error_code="SCENE_DIGEST_MISSING",
                error_message="Completed scene operation is missing its trusted output digest",
            )
        
        # Validation checks
        expected_duration = float(existing.get("expected_duration_seconds") or 0)
        expected_aspect = str(existing.get("expected_aspect_ratio") or "")
        
        # 1. Reject missing validation contract
        terminal = result.status in {"completed", "failed", "cancelled", "canceled"}
        local_capacity = capacity if capacity is not None and capacity.connection is self.connection else None
        if expected_duration <= 0 or not expected_aspect:
            with self.connection:
                updated = self.connection.execute(
                    """
                    UPDATE video_factory_scene_operation
                    SET status = 'failed',
                        output_sha256 = '',
                        error_code = 'VALIDATION_CONTRACT_MISSING',
                        error_message = 'Operation missing expected_duration_seconds or expected_aspect_ratio',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE operation_id = ? AND status NOT IN ('completed', 'failed')
                    """,
                    (operation_id,),
                )
                if updated.rowcount and terminal and local_capacity:
                    local_capacity.release_terminal_in_transaction(existing["provider_kind"], operation_id)
            return dict(self.get(operation_id) or existing)
        
        final_status = result.status
        error_code = result.error_code or ""
        error_message = result.error_message or ""
        output_sha = ""
        
        if result.status == "completed":
            # 2. Reject completed result without video_path
            if not result.video_path:
                final_status = "failed"
                error_code = "VALIDATION_FAILED"
                error_message = "Completed result missing video_path"
            else:
                p_out = Path(result.video_path)
                # 3. Reject missing or empty file
                if not p_out.is_file() or p_out.stat().st_size == 0:
                    final_status = "failed"
                    error_code = "VALIDATION_FAILED"
                    error_message = f"Output file missing or empty: {result.video_path}"
                else:
                    # 4. Full decode and contract validation
                    try:
                        validation = validate_scene_output(
                            output_path=result.video_path,
                            expected_duration=expected_duration,
                            expected_aspect_ratio=expected_aspect,
                            duration_tolerance=0.5,
                        )
                        output_sha = validation.sha256
                        # Ensure status remains completed if validation passes
                        final_status = "completed"
                    except ValueError as exc:
                        # 5. Reject wrong duration, aspect ratio, or undecodable/zero-frame output
                        final_status = "failed"
                        error_code = "VALIDATION_FAILED"
                        error_message = str(exc)
                        output_sha = ""
                    except Exception as exc:
                        final_status = "failed"
                        error_code = "VALIDATION_FAILED"
                        error_message = f"Validation failed: {exc}"
                        output_sha = ""
        
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE video_factory_scene_operation
                SET status = ?,
                    provider_operation_id = COALESCE(?, video_factory_scene_operation.provider_operation_id),
                    output_path = COALESCE(?, video_factory_scene_operation.output_path),
                    output_sha256 = ?,
                    error_code = ?,
                    error_message = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ?
                  AND status NOT IN ('completed', 'failed')
                  AND COALESCE(error_code, '') != 'OPERATOR_QUARANTINED_UNCERTAIN'
                """,
                (
                    final_status,
                    result.provider_operation_id,
                    result.video_path,
                    output_sha,
                    error_code,
                    error_message,
                    operation_id,
                ),
            )
            if updated.rowcount and terminal and local_capacity:
                local_capacity.release_terminal_in_transaction(existing["provider_kind"], operation_id)
        
        return dict(self.get(operation_id) or existing)

    def mark_integrity_failed(
        self,
        operation_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        """Persist an integrity failure, including corruption of a terminal row."""
        existing = self.get(operation_id)
        if not existing:
            raise ValueError(f"Operation {operation_id} not found")
        with self.connection:
            self.connection.execute(
                """
                UPDATE video_factory_scene_operation
                SET status = 'failed',
                    output_sha256 = '',
                    error_code = ?,
                    error_message = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ?
                """,
                (error_code, error_message, operation_id),
            )
        return dict(self.get(operation_id) or existing)

    def save_result(
        self, request: VideoGenerationRequest, result: VideoGenerationResult,
        *, capacity: ProviderCapacityStore | None = None,
    ) -> None:
        """Atomically persist provider response, enforcing monotonic state transitions."""
        digest = stable_scene_request_digest(request)
        existing = self.get(request.request_id)
        if existing:
            if existing["request_digest"] != digest:
                raise ValueError("video scene operation idempotency conflict")
        else:
            self.mark_planned(request)
        self.finalize_provider_result(request.request_id, result, capacity=capacity)

    def quarantine_inactive_submission(
        self, operation_id: str, *, evidence: str, confirmed_inactive: bool
    ) -> dict[str, Any]:
        """Operator-only abandonment of inactive work with an unknown paid outcome.

        Keep the provider status, ID and Send history. Independent provider-page
        inspection must establish inactivity; an expired local lease alone is
        insufficient. The runtime guard prevents this operation being reused.
        """
        if confirmed_inactive is not True or not str(evidence).strip():
            raise ValueError("independent inactivity evidence is required")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE video_factory_scene_operation SET error_code='OPERATOR_QUARANTINED_UNCERTAIN', "
                "error_message=?, updated_at=CURRENT_TIMESTAMP WHERE operation_id=? "
                "AND status IN ('submitting','submitted','running') AND provider_kind='google_flow' "
                "AND COALESCE(provider_operation_id,'') != ''",
                (evidence.strip(), operation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("only a bound, unresolved Flow submission can be quarantined")
            lease = self.connection.execute(
                "SELECT lease_expires_at,lease_owner FROM video_factory_provider_capacity WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if lease and (float(lease[0]) > datetime.now(timezone.utc).timestamp()
                          or process_lease_owner_alive(lease[1])):
                raise ValueError("active worker capacity lease must not be quarantined")
            self.connection.execute(
                "DELETE FROM video_factory_provider_capacity WHERE operation_id=?", (operation_id,)
            )
        return dict(self.get(operation_id))

    def reconcile_submitting_operation(
        self,
        operation_id: str,
        *,
        action: Literal["bind", "fail"],
        provider_operation_id: str | None = None,
        error_message: str = "",
    ) -> dict[str, Any]:
        """Operator reconciliation for an operation stuck in 'submitting' after crash."""
        existing = self.get(operation_id)
        if not existing:
            raise ValueError(f"Operation {operation_id} not found")
        if existing["status"] != "submitting":
            raise ValueError(f"Operation {operation_id} is in status {existing['status']}, not 'submitting'")

        with self.connection:
            if action == "bind":
                if not provider_operation_id or not provider_operation_id.strip():
                    raise ValueError("provider_operation_id is required to bind a submitting operation")
                self.connection.execute(
                    """
                    UPDATE video_factory_scene_operation
                    SET status = 'running',
                        provider_operation_id = ?,
                        error_code = '',
                        error_message = '',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE operation_id = ? AND status = 'submitting'
                    """,
                    (provider_operation_id.strip(), operation_id),
                )
            elif action == "fail":
                self.connection.execute(
                    """
                    UPDATE video_factory_scene_operation
                    SET status = 'failed',
                        error_code = 'OPERATOR_RECONCILED_FAILED',
                        error_message = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE operation_id = ? AND status = 'submitting'
                    """,
                    (error_message or "Submission reconciled as failed by operator", operation_id),
                )
            else:
                raise ValueError(f"Unknown reconciliation action: {action}")
        return dict(self.get(operation_id))

    def clear_quarantine(self, operation_id: str) -> dict[str, Any]:
        """Reset OPERATOR_QUARANTINED_UNCERTAIN to allow resubmission."""
        existing = self.get(operation_id)
        if not existing:
            raise ValueError(f"Operation {operation_id} not found")
        if existing.get("error_code") != "OPERATOR_QUARANTINED_UNCERTAIN":
            raise ValueError(f"Operation {operation_id} is not quarantined")
        with self.connection:
            self.connection.execute(
                "UPDATE video_factory_scene_operation SET status='planned', error_code='', "
                "error_message='', provider_operation_id='', provider_kind='', updated_at=CURRENT_TIMESTAMP "
                "WHERE operation_id=?",
                (operation_id,),
            )
        return dict(self.get(operation_id))


class SceneRenderRuntime:
    def __init__(
        self,
        *,
        store: SceneRenderOperationStore,
        provider: VideoGenerationPort | Mapping[str, VideoGenerationPort],
        provider_kind: str | None = None,
        preferred_provider_kinds: tuple[str, ...] | None = None,
        preferred_provider_kind: str | None = None,
        fallback_provider_kind: str | None = None,
        allow_pre_submit_fallback: bool | None = None,
        capacity_store: ProviderCapacityStore | None = None,
        capacity_limit: int | None = 1,
    ) -> None:
        """Run one durable scene operation through an official AI provider.

        Selection is permitted only before the durable submission claim. Once
        pinned, polling and recovery must use that exact provider. No local
        image-animation fallback participates in this boundary.
        """
        self.store = store
        self.capacity_store = capacity_store
        self.capacity_limit = capacity_limit
        self.lease_owner = process_lease_owner()
        if isinstance(provider, Mapping):
            providers = dict(provider)
        else:
            validated = validate_product_video_render_provider(provider)
            providers = {validated.provider_kind.strip(): validated}

        explicit_kind = str(provider_kind or preferred_provider_kind or "").strip()
        if explicit_kind and preferred_provider_kinds:
            raise ValueError(
                "provide provider_kind/preferred_provider_kind or preferred_provider_kinds, not both"
            )
        preferred = preferred_provider_kinds
        if explicit_kind:
            preferred = (explicit_kind,)

        self.selector = VideoProviderSelector(
            providers,
            preferred_kinds=preferred,
            fallback_provider_kind=fallback_provider_kind,
            allow_pre_submit_fallback=(
                True if allow_pre_submit_fallback is None else allow_pre_submit_fallback
            ),
        )
        self.providers = self.selector.providers
        self.provider_kind = (
            explicit_kind
            or (preferred[0] if preferred else "")
            or (next(iter(self.providers)) if len(self.providers) == 1 else "")
        )
        self.provider = (
            self.providers[self.provider_kind]
            if self.provider_kind in self.providers
            else None
        )

    @staticmethod
    def _provider_is_available(provider: VideoGenerationPort) -> bool:
        availability = getattr(provider, "is_available", None)
        return bool(availability()) if callable(availability) else availability is not False

    def _resolve_existing_provider(
        self, existing: dict[str, Any], request: VideoGenerationRequest
    ) -> SelectedVideoProvider:
        durable_kind = str(existing.get("provider_kind") or "").strip()
        if not durable_kind:
            if existing.get("provider_operation_id"):
                raise ProviderSelectionError(
                    "RECONCILIATION_REQUIRED: operation has an external ID but no durable provider kind"
                )
            if existing.get("status") != "planned":
                raise ProviderSelectionError(
                    "RECONCILIATION_REQUIRED: operation has no durable provider pin"
                )
            return self._select_initial_for_request(request)
        return self.selector.resolve_pinned(durable_kind)

    def _select_initial_for_request(self, request: VideoGenerationRequest) -> SelectedVideoProvider:
        fallback_order = self.selector.fallback_order if self.selector.allow_pre_submit_fallback else ()
        for kind in (*self.selector.primary_order, *fallback_order):
            provider = self.providers[kind]
            if request.end_keyframe is not None and getattr(provider, "supports_end_keyframe", False) is not True:
                continue
            if not self.selector.is_available(provider):
                continue
            can_submit = getattr(provider, "can_submit", None)
            try:
                if callable(can_submit) and not can_submit(request):
                    continue
            except Exception:
                continue
            return SelectedVideoProvider(kind=kind, provider=provider)
        if request.end_keyframe is not None:
            raise ProviderSelectionError("no available provider supports the approved END keyframe")
        raise ProviderSelectionError("no configured video provider is ready with sufficient credits before submission")

    @staticmethod
    def _with_provider_kind(
        result: VideoGenerationResult, provider_kind: str
    ) -> VideoGenerationResult:
        metadata = dict(result.metadata or {})
        declared_kind = str(metadata.get("provider_kind") or "").strip()
        if declared_kind and declared_kind != provider_kind:
            raise ProviderSelectionError(
                f"provider result kind {declared_kind} does not match durable provider {provider_kind}"
            )
        metadata["provider_kind"] = provider_kind
        return replace(result, metadata=metadata)

    @staticmethod
    def _selection_failure(
        request: VideoGenerationRequest, exc: Exception
    ) -> VideoGenerationResult:
        return VideoGenerationResult(
            request_id=request.request_id,
            status="failed",
            error_code="PROVIDER_SELECTION_FAILED",
            error_message=str(exc),
        )

    @staticmethod
    def _reconciliation_required(
        request: VideoGenerationRequest, message: str
    ) -> VideoGenerationResult:
        return VideoGenerationResult(
            request_id=request.request_id,
            status="failed",
            error_code="RECONCILIATION_REQUIRED",
            error_message=message,
        )

    def _reconcile_submitting(
        self,
        request: VideoGenerationRequest,
        selected: SelectedVideoProvider,
    ) -> VideoGenerationResult | None:
        """Reconcile a crash window through the one canonical optional hook."""
        if not self._admit_capacity(request, selected, reconciliation_only=True):
            return self._capacity_wait(request)
        provider = selected.provider
        lookup = getattr(provider, "lookup_by_request_id", None)
        if not callable(lookup):
            return None
        try:
            reconciled = lookup(request.request_id)
        except Exception:
            return None
        try:
            reconciled = validate_reconciled_result(
                expected_request_id=request.request_id,
                durable_provider_kind=selected.kind,
                result=reconciled,
            )
        except ProviderSelectionError:
            return None
        self.store.bind_provider_operation_id(
            request.request_id, reconciled.provider_operation_id
        )
        if self.capacity_store:
            self.capacity_store.bind_provider_operation(
                selected.kind, request.request_id, reconciled.provider_operation_id
            )
        final_state = self.store.finalize_provider_result(
            request.request_id, reconciled, capacity=self.capacity_store,
        )
        if (self.capacity_store and self.capacity_store.connection is not self.store.connection
                and reconciled.status in {"completed", "failed", "cancelled", "canceled"}):
            self.capacity_store.release_terminal(selected.kind, request.request_id)
        if final_state.get("status") == "failed":
            return replace(
                reconciled,
                status="failed",
                error_code=final_state.get("error_code") or "SCENE_FAILED",
                error_message=final_state.get("error_message") or "scene reconciliation failed",
            )
        return replace(reconciled, status=final_state.get("status") or reconciled.status)

    def run(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        if request.end_keyframe is not None and not any(
            getattr(provider, "supports_end_keyframe", False) is True
            for provider in self.providers.values()
        ):
            return VideoGenerationResult(
                request_id=request.request_id,
                status="failed",
                error_code="PROVIDER_UNAVAILABLE",
                error_message="selected provider cannot condition on an END keyframe",
            )
        existing = self.store.get(request.request_id)
        if existing:
            if existing["request_digest"] != stable_scene_request_digest(request):
                raise ValueError("video scene operation idempotency conflict")
            if existing.get("error_code") == "OPERATOR_QUARANTINED_UNCERTAIN":
                return self._reconciliation_required(
                    request, "inactive submission was quarantined with an unknown provider outcome; no redispatch"
                )
            if existing["status"] == "completed":
                output = Path(existing["output_path"] or "")
                if (
                    output.is_file()
                    and existing["output_sha256"]
                    and _sha256(output) == existing["output_sha256"]
                ):
                    return self._with_provider_kind(VideoGenerationResult(
                        request_id=request.request_id,
                        status="completed",
                        provider_operation_id=existing["provider_operation_id"],
                        video_path=str(output),
                    ), str(existing.get("provider_kind") or ""))
                raise ValueError("completed video scene artifact is missing or changed")
            try:
                selected = self._resolve_existing_provider(existing, request)
            except ProviderSelectionError as exc:
                if existing["status"] in ("submitting", "submitted", "running"):
                    return self._reconciliation_required(request, str(exc))
                return self._selection_failure(request, exc)
            provider_operation_id = existing["provider_operation_id"]
            if provider_operation_id:
                if not self._admit_capacity(request, selected, reconciled_takeover=True):
                    return self._capacity_wait(request)
                result = selected.provider.get_status(provider_operation_id)
                result = replace(result, request_id=request.request_id)
                result = self._with_provider_kind(result, selected.kind)
                if (result.status == "failed" and result.error_code == "RECONCILIATION_REQUIRED") or result.status == "unknown":
                    return result
                self._save_result(request, result, selected.kind)
                return result
            if existing["status"] == "submitting":
                reconciled = self._reconcile_submitting(request, selected)
                if reconciled is not None:
                    return reconciled
                return self._reconciliation_required(
                    request,
                    "scene submission checkpoint has no provider operation id; "
                    "provider reconciliation is unavailable or ambiguous",
                )
            if existing["status"] == "planned":
                if not self._admit_capacity(request, selected):
                    return self._capacity_wait(request)
                claim = self.store.claim_provider_for_submission(
                    request, selected.kind
                )
                if not claim.can_submit:
                    if claim.status == "provider_conflict":
                        return self._selection_failure(
                            request, ProviderSelectionError(claim.message)
                        )
                    if claim.status == "send_limit_exceeded":
                        return VideoGenerationResult(
                            request_id=request.request_id,
                            status="failed",
                            error_code='CREATIVE_VIDEO_CALL_CAP' if request.provider_options.get('creative_mode')=='four_agent' else 'VERTEX_SEND_LIMIT_EXCEEDED',
                            error_message=claim.message,
                        )
                    return self.run(request)
            else:
                return self._reconciliation_required(
                    request,
                    f"scene operation is in status {existing['status']!r} without a "
                    "durable provider operation id; refusing a duplicate submission",
                )
        else:
            # Ensure planned first before submitting
            self.store.mark_planned(request)
            try:
                selected = self._select_initial_for_request(request)
            except ProviderSelectionError as exc:
                return self._selection_failure(
                    request,
                    exc,
                )
            if not self._admit_capacity(request, selected):
                return self._capacity_wait(request)
            claim = self.store.claim_provider_for_submission(request, selected.kind)
            if not claim.can_submit:
                if claim.status == "provider_conflict":
                    return self._selection_failure(
                        request, ProviderSelectionError(claim.message)
                    )
                if claim.status == "send_limit_exceeded":
                    return VideoGenerationResult(
                        request_id=request.request_id,
                        status="failed",
                        error_code='CREATIVE_VIDEO_CALL_CAP' if request.provider_options.get('creative_mode')=='four_agent' else 'VERTEX_SEND_LIMIT_EXCEEDED',
                        error_message=claim.message,
                    )
                return self.run(request)

        try:
            result = selected.provider.submit(request)
        except TerminalJobError as exc:
            if getattr(exc, "provider_operation_id", None):
                self.store.bind_provider_operation_id(request.request_id, exc.provider_operation_id)
                if self.capacity_store:
                    self.capacity_store.bind_provider_operation(
                        selected.kind, request.request_id, exc.provider_operation_id,
                    )
            raise
        except Exception:
            raise

        result = replace(result, request_id=request.request_id)
        result = self._with_provider_kind(result, selected.kind)
        if (result.status == "failed" and result.error_code == "RECONCILIATION_REQUIRED") or result.status == "unknown":
            if result.provider_operation_id:
                self.store.bind_provider_operation_id(request.request_id, result.provider_operation_id)
                if self.capacity_store:
                    self.capacity_store.bind_provider_operation(
                        selected.kind, request.request_id, result.provider_operation_id,
                    )
            return result

        self._save_result(request, result, selected.kind)
        return result

    def _save_result(self, request, result, provider_kind):
        terminal = result.status in {"completed", "failed", "cancelled", "canceled"}
        self.store.save_result(request, result, capacity=self.capacity_store)
        if self.capacity_store is None:
            return
        if terminal:
            if self.capacity_store.connection is not self.store.connection:
                self.capacity_store.release_terminal(provider_kind, request.request_id)
        elif result.provider_operation_id:
            self.capacity_store.bind_provider_operation(provider_kind, request.request_id, result.provider_operation_id)

    def _admit_capacity(
        self,
        request: VideoGenerationRequest,
        selected: SelectedVideoProvider,
        *,
        reconciled_takeover: bool = False,
        reconciliation_only: bool = False,
    ) -> bool:
        if self.capacity_store is None:
            return True
        return self.capacity_store.try_acquire(
            provider_kind=selected.kind,
            operation_id=request.request_id,
            lease_owner=self.lease_owner,
            limit=(self.capacity_limit if self.capacity_limit is not None else configured_capacity(selected.kind)),
            profile_id=str(getattr(selected.provider, "profile_id", "") or ""),
            reconciled_takeover=reconciled_takeover,
            reconciliation_only=reconciliation_only,
        )

    @staticmethod
    def _capacity_wait(request: VideoGenerationRequest) -> VideoGenerationResult:
        return VideoGenerationResult(
            request_id=request.request_id, status="failed",
            error_code="PROVIDER_CAPACITY_WAIT",
            error_message="provider capacity is full; retry the same operation later",
        )
