"""Unified validation for QC reports across all gate boundaries."""
from __future__ import annotations

import sqlite3
from typing import Any
from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.repository import QCReportRepository
from video_factory.qc.normalization import resource_pack_fingerprint
from video_factory.qc.evaluator import is_eligible_image_evidence
from video_factory.qc.fingerprint import (
    IMAGE_POLICY_VERSION, IMAGE_RUBRIC_VERSION,
    KEYFRAME_POLICY_VERSION, KEYFRAME_RUBRIC_VERSION,
    VIDEO_POLICY_VERSION, VIDEO_RUBRIC_VERSION,
    minimum_pass_score,
)


class QCValidationError(ValueError):
    """Raised when an artifact fails QC pass verification at any execution gate."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason


def require_current_report(
    repository: QCReportRepository | Any,
    *, workflow_id: str, stage: QCStage | str, artifact: dict[str, Any],
    owner_user_id: str | None = None, project_id: str | None = None,
    provider_mode: str = "configured", allow_missing: bool = False,
) -> QCReport | None:
    """Fetch current source evidence without promoting its verdict to PASS."""
    if isinstance(stage, str):
        stage = QCStage(stage.lower())
    qc_repo = repository if isinstance(repository, QCReportRepository) else QCReportRepository(
        repository.conn if hasattr(repository, "conn") else repository
    )
    if stage == QCStage.IMAGE:
        expected = resource_pack_fingerprint(artifact)
    elif stage == QCStage.KEYFRAME:
        expected = artifact.get("package_digest") or artifact.get("digest")
    else:
        expected = (artifact.get("sha256") or artifact.get("digest")
                    or artifact.get("manifest_digest") or artifact.get("output_digest"))
    report = qc_repo.get_latest_report(
        workflow_id, stage, expected_artifact_digest=expected,
        provider_mode=provider_mode, owner_user_id=owner_user_id, project_id=project_id,
    )
    if report is None and not allow_missing:
        raise QCValidationError("QC_REPORT_MISSING", f"No current {stage.value} QC record matches the artifact")
    if report is not None and provider_mode == "configured" and (
        not report.owner_user_id or not report.project_id or not report.evaluator_name
        or not report.evaluator_version or not report.rubric_version
    ):
        raise QCValidationError("QC_BINDING_MISSING", f"Current {stage.value} QC record has incomplete bindings")
    return report


def require_current_pass_report(
    repository: QCReportRepository | Any,
    *,
    workflow_id: str,
    stage: QCStage | str,
    artifact: dict[str, Any],
    owner_user_id: str | None = None,
    project_id: str | None = None,
    provider_mode: str = "configured",
) -> QCReport:
    """Strictly validate and return current verified PASS QCReport for an artifact.

    In configured mode per SPEC S2-S3:
    1. Report must exist in repository for the workflow and stage.
    2. Stage must match.
    3. Artifact digest must match current artifact digest.
    4. Verdict must be strictly PASS.
    5. Generated media scores meet their thresholds (Keyframe: 60; Video: 80).
    6. hard_failure must be False.
    7. Owner & project must match if supplied.
    8. Artifact ID & version must match if present.
    9. Provider mode must be configured.
    10. Evaluator name and rubric must be present.
    11. Coverage must be valid:
        - Image: at least 1 eligible image with evidence; selected references match reference_digests.
        - Keyframe: no failed frames; per-frame items must all be PASS >= 60.
        - Video: no scene/technical failures.
    """
    if isinstance(stage, str):
        stage = QCStage(stage.lower())

    if hasattr(repository, "conn"):
        qc_repo = QCReportRepository(repository.conn)
    elif isinstance(repository, sqlite3.Connection):
        qc_repo = QCReportRepository(repository)
    elif isinstance(repository, QCReportRepository):
        qc_repo = repository
    else:
        qc_repo = QCReportRepository(repository)

    if provider_mode != "configured":
        # In non-configured (e.g. fake/test) mode, if report exists, return it; otherwise construct a dummy pass
        report = qc_repo.get_latest_report(workflow_id=workflow_id, stage=stage)
        if report:
            return report

    # Resolve expected artifact digest
    expected_digest = None
    if stage == QCStage.IMAGE:
        expected_digest = resource_pack_fingerprint(artifact)
    elif stage == QCStage.KEYFRAME:
        expected_digest = artifact.get("package_digest") or artifact.get("digest")
    elif stage == QCStage.VIDEO:
        expected_digest = (
            artifact.get("sha256")
            or artifact.get("digest")
            or artifact.get("manifest_digest")
            or artifact.get("output_digest")
        )

    clean_digest = expected_digest.removeprefix("sha256:") if expected_digest else ""
    minimum_score = minimum_pass_score(stage)

    # Query latest report for this workflow and stage
    report = qc_repo.get_latest_report(
        workflow_id=workflow_id,
        stage=stage,
        expected_artifact_digest=expected_digest,
    )

    if not report:
        # Check if there is ANY report for this workflow and stage to provide precise error code
        c = qc_repo.conn.cursor()
        any_row = c.execute(
            "SELECT report_id, score, verdict, artifact_digest FROM product_video_qc_reports "
            "WHERE workflow_id = ? AND stage = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (workflow_id, stage.value),
        ).fetchone()
        if any_row:
            rep_id, rep_score, rep_verdict, rep_digest = any_row
            clean_rep_digest = str(rep_digest).removeprefix("sha256:")
            if clean_digest and clean_rep_digest != clean_digest:
                raise QCValidationError(
                    "QC_STALE_REPORT",
                    f"QC report {rep_id} artifact digest '{clean_rep_digest}' does not match current artifact digest '{clean_digest}'",
                )
            if str(rep_verdict).lower() != "pass":
                raise QCValidationError(
                    "QC_VERDICT_NOT_PASS",
                    f"QC report {rep_id} verdict is '{rep_verdict}', must be strictly PASS (score={rep_score})",
                )
            if int(rep_score or 0) < minimum_score:
                raise QCValidationError(
                    "QC_SCORE_BELOW_THRESHOLD",
                    f"QC report {rep_id} score {rep_score} is below threshold {minimum_score}",
                )
        raise QCValidationError(
            "QC_REPORT_MISSING",
            f"No verified QC report found for workflow '{workflow_id}' at stage '{stage.value}'",
        )

    # Validate report attributes strictly
    clean_rep_digest = report.artifact_digest.removeprefix("sha256:")
    if clean_digest and clean_rep_digest != clean_digest:
        raise QCValidationError(
            "QC_STALE_REPORT",
            f"QC report artifact digest '{clean_rep_digest}' does not match current artifact digest '{clean_digest}'",
        )

    if report.verdict != QCVerdict.PASS and str(report.verdict).lower() != "pass":
        raise QCValidationError(
            "QC_VERDICT_NOT_PASS",
            f"QC report {report.report_id} verdict is '{report.verdict}', must be strictly PASS",
        )

    reused_final_review = (
        stage == QCStage.VIDEO
        and report.findings.get("content_evidence_source") == "creative_final_review"
    )
    technical_only_final = (
        stage == QCStage.VIDEO and report.findings.get("technical_only_final") is True
    )
    if reused_final_review and int(report.score) != 0:
        raise QCValidationError(
            "QC_SCORE_NOT_APPLICABLE",
            "Reused final review must keep score at 0; its PASS verdict comes from the bound reviewer evidence",
        )
    if int(report.score) < minimum_score and not (reused_final_review or technical_only_final):
        raise QCValidationError(
            "QC_SCORE_BELOW_THRESHOLD",
            f"QC report {report.report_id} score {report.score} is below threshold {minimum_score}",
        )

    findings = report.findings or {}
    if findings.get("hard_failure"):
        raise QCValidationError(
            "QC_HARD_FAILURE",
            f"QC report {report.report_id} contains hard failure: {findings.get('reason', 'unspecified')}",
        )

    # Mandatory bindings check
    if provider_mode == "configured":
        if report.provider_mode != "configured":
            raise QCValidationError(
                "QC_PROVIDER_MODE_MISMATCH",
                f"QC report provider mode '{report.provider_mode}' is not configured",
            )
        if not report.owner_user_id or not str(report.owner_user_id).strip():
            raise QCValidationError(
                "QC_OWNER_MISMATCH",
                f"Configured QC report {report.report_id} missing mandatory owner_user_id binding",
            )
        if owner_user_id and report.owner_user_id != owner_user_id:
            raise QCValidationError(
                "QC_OWNER_MISMATCH",
                f"QC report owner '{report.owner_user_id}' does not match expected '{owner_user_id}'",
            )
        if not report.project_id or not str(report.project_id).strip():
            raise QCValidationError(
                "QC_PROJECT_MISMATCH",
                f"Configured QC report {report.report_id} missing mandatory project_id binding",
            )
        if project_id and report.project_id != project_id:
            raise QCValidationError(
                "QC_PROJECT_MISMATCH",
                f"QC report project '{report.project_id}' does not match expected '{project_id}'",
            )
        if not report.evaluator_name or not str(report.evaluator_name).strip():
            raise QCValidationError(
                "QC_EVALUATOR_MISSING",
                f"Configured QC report {report.report_id} missing mandatory evaluator_name",
            )
        if not report.evaluator_version or not str(report.evaluator_version).strip():
            raise QCValidationError(
                "QC_EVALUATOR_VERSION_MISSING",
                f"Configured QC report {report.report_id} missing mandatory evaluator_version",
            )
        if not report.rubric_version or not str(report.rubric_version).strip():
            raise QCValidationError(
                "QC_RUBRIC_VERSION_MISSING",
                f"Configured QC report {report.report_id} missing mandatory rubric_version",
            )
        if not report.policy_version or not str(report.policy_version).strip():
            raise QCValidationError(
                "QC_POLICY_VERSION_MISSING",
                f"Configured QC report {report.report_id} missing mandatory policy_version",
            )
        expected_versions = {
            QCStage.IMAGE: (IMAGE_RUBRIC_VERSION, IMAGE_POLICY_VERSION),
            QCStage.KEYFRAME: (KEYFRAME_RUBRIC_VERSION, KEYFRAME_POLICY_VERSION),
            QCStage.VIDEO: (VIDEO_RUBRIC_VERSION, VIDEO_POLICY_VERSION),
        }
        expected_rubric, expected_policy = expected_versions[stage]
        if report.rubric_version != expected_rubric:
            raise QCValidationError(
                "QC_RUBRIC_VERSION_MISMATCH",
                f"QC report rubric '{report.rubric_version}' is not current '{expected_rubric}'",
            )
        if report.policy_version != expected_policy:
            raise QCValidationError(
                "QC_POLICY_VERSION_MISMATCH",
                f"QC report policy '{report.policy_version}' is not current '{expected_policy}'",
            )
        if stage == QCStage.IMAGE and not str(report.model_identifier or "").strip():
            raise QCValidationError(
                "QC_EVALUATOR_MISSING",
                f"Configured Image QC report {report.report_id} missing model_identifier",
            )
        if stage == QCStage.IMAGE and (
            report.evaluator_name == "exchange_prevalidated_qc"
            or report.model_identifier == "exchange-manifest-v1"
        ):
            raise QCValidationError(
                "QC_VISUAL_REVIEW_REQUIRED",
                f"Legacy Exchange manifest report {report.report_id} is not visual source QC; "
                "evaluate the saved product images before preparation or production",
            )
        if not report.artifact_id or not str(report.artifact_id).strip():
            raise QCValidationError(
                "QC_ARTIFACT_ID_MISMATCH",
                f"Configured QC report {report.report_id} missing mandatory artifact_id",
            )
        exp_art_id = str(
            artifact.get("resource_pack_id")
            or artifact.get("artifact_id")
            or artifact.get("storyboard_id")
            or artifact.get("render_id")
            or artifact.get("export_id")
            or artifact.get("id")
            or ""
        ).strip()
        if exp_art_id and report.artifact_id != exp_art_id:
            raise QCValidationError(
                "QC_ARTIFACT_ID_MISMATCH",
                f"QC report artifact ID '{report.artifact_id}' does not match expected '{exp_art_id}'",
            )
        if not report.artifact_version or report.artifact_version <= 0:
            raise QCValidationError(
                "QC_ARTIFACT_VERSION_MISMATCH",
                f"Configured QC report {report.report_id} missing or invalid artifact_version ({report.artifact_version})",
            )
        exp_art_ver = artifact.get("version") or artifact.get("resource_pack_version")
        if exp_art_ver is not None:
            try:
                ver_int = int(exp_art_ver)
                if ver_int > 0 and report.artifact_version != ver_int:
                    raise QCValidationError(
                        "QC_ARTIFACT_VERSION_MISMATCH",
                        f"QC report artifact version '{report.artifact_version}' does not match expected '{ver_int}'",
                    )
            except (ValueError, TypeError):
                pass
    elif provider_mode in ("manual", "legacy"):
        has_item_ev = bool(
            findings.get("items")
            and len([it for it in findings["items"] if (is_eligible_image_evidence(it) if stage == QCStage.IMAGE else (str(it.get("verdict")).lower() == "pass" and int(it.get("score") or 0) >= minimum_pass_score(stage)))]) >= (1 if stage == QCStage.IMAGE else 2)
        )
        has_manual_approval = bool(
            findings.get("manual_approval")
            or findings.get("operator_approved")
            or findings.get("approved_by")
        )
        if not has_item_ev and not has_manual_approval:
            raise QCValidationError(
                "QC_LEGACY_EVIDENCE_MISSING",
                f"Legacy/manual report {report.report_id} lacks equivalent item or operator approval evidence; migration required",
            )
    else:
        if owner_user_id and report.owner_user_id and report.owner_user_id != owner_user_id:
            raise QCValidationError("QC_OWNER_MISMATCH", f"QC report owner '{report.owner_user_id}' does not match expected '{owner_user_id}'")
        if project_id and report.project_id and report.project_id != project_id:
            raise QCValidationError("QC_PROJECT_MISMATCH", f"QC report project '{report.project_id}' does not match expected '{project_id}'")

    # Validate coverage per stage
    if stage == QCStage.IMAGE:
        from pathlib import Path
        from video_factory.qc.image_qc import compute_file_sha256
        from video_factory.qc.normalization import _is_decodable_image

        pack_refs = (
            artifact.get("assets")
            or artifact.get("assets_mapping")
            or artifact.get("media_assets")
            or artifact.get("product_references")
            or []
        )
        selected_manifest = findings.get("eligible_reference_manifest")
        if isinstance(selected_manifest, list):
            refs = [item for item in selected_manifest if isinstance(item, dict) and item.get("eligible") is True]
        else:
            refs = pack_refs
        if len(refs) < 1:
            raise QCValidationError(
                "QC_INSUFFICIENT_COVERAGE",
                f"Resource pack has no usable selected reference (found {len(refs)})",
            )

        ref_shas = []
        pack_sha_by_id: dict[str, set[str]] = {}
        for pack_ref in pack_refs:
            pack_id = str(pack_ref.get("asset_id") or pack_ref.get("id") or "").strip()
            pack_loc = pack_ref.get("local_path") or pack_ref.get("path") or pack_ref.get("absolute_path")
            if not pack_id or not pack_loc or not Path(pack_loc).is_file():
                continue
            pack_sha_by_id.setdefault(pack_id, set()).add(compute_file_sha256(Path(pack_loc)).lower())

        for ref in refs[:1]:
            ref_id = str(ref.get("asset_id") or ref.get("id") or "").strip()
            r_sha = str(ref.get("sha256") or ref.get("digest") or "").removeprefix("sha256:").lower()
            matching_pack_ref = next(
                (
                    item for item in pack_refs
                    if str(item.get("asset_id") or item.get("id") or "").strip() == ref_id
                    and r_sha in pack_sha_by_id.get(ref_id, set())
                ),
                None,
            )
            if provider_mode == "configured" and selected_manifest is not None and matching_pack_ref is None:
                raise QCValidationError(
                    "QC_REFERENCE_DIGEST_MISMATCH",
                    f"Selected report reference '{ref_id or r_sha}' is not present in the current resource pack",
                )
            resolved_ref = matching_pack_ref or ref
            loc = resolved_ref.get("local_path") or resolved_ref.get("path") or resolved_ref.get("absolute_path")
            if provider_mode == "configured" and (not loc or not Path(loc).is_file() or len(r_sha) != 64):
                raise QCValidationError(
                    "QC_REFERENCE_DIGEST_MISMATCH",
                    f"Selected reference '{ref_id}' lacks readable bytes or a SHA-256 digest",
                )
            if loc:
                p = Path(loc)
                if p.is_file():
                    if provider_mode == "configured" and not _is_decodable_image(p):
                        raise QCValidationError("QC_REFERENCE_DIGEST_MISMATCH", f"Selected reference '{ref_id}' is not a decodable image")
                    disk_sha = compute_file_sha256(p).lower()
                    if r_sha and len(r_sha) == 64 and disk_sha != r_sha:
                        raise QCValidationError(
                            "QC_REFERENCE_DIGEST_MISMATCH",
                            f"Selected reference '{ref_id}' file sha256 '{disk_sha}' does not match declared '{r_sha}'",
                        )
                    r_sha = disk_sha
            ref_shas.append(r_sha)

        if len(ref_shas) > 1 and ref_shas[0] and ref_shas[1] and ref_shas[0] == ref_shas[1]:
            raise QCValidationError(
                "QC_INSUFFICIENT_COVERAGE",
                "Selected references must have distinct image contents (found duplicate SHA-256)",
            )

        # A report may evaluate more than one pack image. Its explicit eligible
        # manifest selects approved references; all report digests still
        # must belong to the current pack and include those selected bytes.
        report_ref_digests = {
            str(d).removeprefix("sha256:").lower() for d in (report.reference_digests or [])
        }
        if provider_mode == "configured" and not set(ref_shas).issubset(report_ref_digests):
            raise QCValidationError("QC_REFERENCE_DIGEST_MISMATCH", "Image report does not bind every selected image SHA-256")
        if provider_mode == "configured" and report_ref_digests:
            current_pack_digests = {
                digest
                for values in pack_sha_by_id.values()
                for digest in values
            }
            if not report_ref_digests.issubset(current_pack_digests):
                raise QCValidationError("QC_REFERENCE_DIGEST_MISMATCH", "Image report contains a reference SHA-256 absent from the current resource pack")
        if report.reference_digests:
            clean_rep_ref_digests = {
                str(d).removeprefix("sha256:").lower() for d in report.reference_digests if d
            }
            for idx, r_sha in enumerate(ref_shas):
                if r_sha and len(r_sha) == 64 and any(len(d) == 64 for d in clean_rep_ref_digests):
                    if r_sha not in clean_rep_ref_digests:
                        raise QCValidationError(
                            "QC_REFERENCE_DIGEST_MISMATCH",
                            f"Selected reference digest '{r_sha}' not found in report reference_digests",
                        )

        # In configured mode, report must prove PASS evidence for the selected reference
        # Do NOT infer coverage solely from count of reference_digests, passed_items, or eligible_count
        items = findings.get("items") or []
        if not isinstance(items, list) or len(items) < 1:
            raise QCValidationError(
                "QC_INSUFFICIENT_COVERAGE",
                f"Image QC report {report.report_id} lacks verified item evidence list (found {len(items) if isinstance(items, list) else 0} items; minimum 1 required)",
            )

        matched_item_indices = set()
        for idx, ref in enumerate(refs[:1]):
            ref_id = str(ref.get("asset_id") or "").strip()
            r_sha = ref_shas[idx]
            match_found = False
            for item_idx, it in enumerate(items):
                if item_idx in matched_item_indices:
                    continue
                it_asset_id = str(it.get("asset_id") or "").strip()
                it_sha = str(it.get("sha256") or it.get("digest") or "").removeprefix("sha256:").lower()

                id_matches = False
                if ref_id and it_asset_id and ref_id == it_asset_id:
                    id_matches = True
                elif r_sha and it_sha and r_sha == it_sha:
                    id_matches = True

                if id_matches:
                    if provider_mode == "configured" and (not it_sha or it_sha != r_sha):
                        raise QCValidationError(
                            "QC_REFERENCE_DIGEST_MISMATCH",
                            f"Item evidence for '{ref_id}' does not match current image bytes",
                        )
                    it_verdict = str(it.get("verdict") or ("pass" if it.get("eligible") else "fail")).lower()
                    it_score = int(it.get("score") or 0)
                    it_hard_fail = bool(it.get("hard_failure", False))
                    if is_eligible_image_evidence(it):
                        match_found = True
                        matched_item_indices.add(item_idx)
                        break
                    else:
                        raise QCValidationError(
                            "QC_INSUFFICIENT_COVERAGE",
                            f"Item evidence for selected reference '{ref_id or r_sha}' lacks explicit clean PASS evidence (verdict={it_verdict}, score={it_score})",
                        )

            if not match_found:
                raise QCValidationError(
                    "QC_INSUFFICIENT_COVERAGE",
                    f"Image report lacks verified PASS item evidence for selected reference '{ref_id or r_sha}'",
                )

        if len(matched_item_indices) < 1:
            raise QCValidationError(
                "QC_INSUFFICIENT_COVERAGE",
                f"Image report failed to prove a selected PASS item (matched only {len(matched_item_indices)})",
            )

    elif stage == QCStage.KEYFRAME:
        failed_frame_ids = findings.get("failed_frame_ids") or []
        if failed_frame_ids:
            raise QCValidationError(
                "QC_FAILED_FRAMES",
                f"Keyframe QC report {report.report_id} has failed frame(s): {failed_frame_ids}",
            )
        items = findings.get("items") or []
        for it in items:
            it_verdict = str(it.get("verdict", "")).lower()
            it_score = int(it.get("score") or 0)
            if it_verdict != "pass" or it_score < minimum_pass_score(QCStage.KEYFRAME) or bool(it.get("hard_failure")):
                raise QCValidationError(
                    "QC_FRAME_EVIDENCE_FAIL",
                    f"Keyframe item '{it.get('frame_id')}' failed verification (verdict={it_verdict}, score={it_score})",
                )

    elif stage == QCStage.VIDEO:
        if findings.get("technical_only_final") is True:
            if not (findings.get("technical_specs") or {}).get("technical_pass"):
                raise QCValidationError(
                    "QC_VIDEO_TECHNICAL_FAIL", "Technical-only final QC does not contain a current technical PASS"
                )
            video_path = artifact.get("file_path") or artifact.get("video_url") or artifact.get("output_url")
            if not video_path:
                raise QCValidationError("QC_VIDEO_PATH_MISSING", "Technical-only final QC requires the current MP4 path")
            try:
                from video_factory.creative.agents.video_review import require_final_pass
                require_final_pass({**artifact, "file_path":str(video_path),
                    "sha256":artifact.get("sha256") or report.artifact_digest.removeprefix("sha256:")})
            except (KeyError, ValueError, OSError) as exc:
                raise QCValidationError("QC_VIDEO_TECHNICAL_FAIL", str(exc)) from exc
            return report
        if reused_final_review:
            from pathlib import Path
            from video_factory.creative.agents.video_review import require_final_pass

            evidence = findings.get("creative_final_review")
            video_path = (
                artifact.get("file_path")
                or artifact.get("video_url")
                or artifact.get("final_video_url")
                or artifact.get("output_url")
            )
            if not isinstance(evidence, dict) or not video_path:
                raise QCValidationError(
                    "QC_CREATIVE_REVIEW_MISSING",
                    "Reused final content review lacks the current media evidence or local video path",
                )
            try:
                require_final_pass({**evidence, "file_path": str(video_path)})
            except (KeyError, ValueError, OSError) as exc:
                raise QCValidationError("QC_CREATIVE_REVIEW_INVALID", str(exc)) from exc
            if str(evidence.get("sha256") or "").removeprefix("sha256:") != clean_digest:
                raise QCValidationError(
                    "QC_STALE_REPORT", "Reused final content review does not match the report artifact SHA-256"
                )
            review_scene_ids = findings.get("sample_coverage", {}).get("boundaries", [])
            visual_scenes = findings.get("visual_scenes") or []
            expected_scene_ids = evidence.get("review_context", {}).get("scene_ids", [])
            if (
                findings.get("creative_final_review_input_digest")
                != evidence.get("final_review", {}).get("input_digest")
                or not findings.get("sample_coverage", {}).get("coverage_valid")
                or len(visual_scenes) != len(review_scene_ids)
                or [scene.get("scene_id") for scene in visual_scenes if isinstance(scene, dict)] != expected_scene_ids
                or any(
                    scene.get("verdict") != "pass"
                    or scene.get("content_evidence_source") != "shared_final_review"
                    or int(scene.get("samples_extracted", 0)) != 5
                    or not set(scene.get("sample_timestamps", [])).issubset(
                        evidence.get("review_context", {}).get("required_timestamps", [])
                    )
                    for scene in visual_scenes if isinstance(scene, dict)
                )
            ):
                raise QCValidationError(
                    "QC_INSUFFICIENT_COVERAGE", "Reused final review does not bind every current scene sample"
                )
            return report
        if findings.get("failed_scenes") or findings.get("technical_failures"):
            raise QCValidationError(
                "QC_VIDEO_SCENE_FAIL",
                f"Video QC report {report.report_id} contains scene/technical failures",
            )

    return report
