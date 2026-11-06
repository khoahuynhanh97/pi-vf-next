"""Canonical SQLite persistence for Product Video QC reports."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from video_factory.qc.models import QCReport, QCStage, QCVerdict
from video_factory.qc.fingerprint import minimum_pass_score

_QC_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS product_video_qc_reports (
    report_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_version INTEGER NOT NULL,
    artifact_digest TEXT NOT NULL,
    reference_digests TEXT NOT NULL,
    evaluator_name TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    score INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    sample_timestamps_json TEXT,
    fingerprint TEXT NOT NULL DEFAULT '',
    provider_mode TEXT NOT NULL DEFAULT 'configured',
    model_identifier TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    owner_user_id TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS product_video_qc_claims (
    workflow_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed')),
    report_id TEXT,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY(workflow_id, stage, fingerprint)
);
"""


class QCReportRepository:
    """Stores and queries QC reports in canonical SQLite database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.executescript(_QC_TABLE_SQL)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(product_video_qc_reports)").fetchall()}
        migrations = {
            "fingerprint": "ALTER TABLE product_video_qc_reports ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''",
            "provider_mode": "ALTER TABLE product_video_qc_reports ADD COLUMN provider_mode TEXT NOT NULL DEFAULT 'configured'",
            "model_identifier": "ALTER TABLE product_video_qc_reports ADD COLUMN model_identifier TEXT NOT NULL DEFAULT ''",
            "policy_version": "ALTER TABLE product_video_qc_reports ADD COLUMN policy_version TEXT NOT NULL DEFAULT ''",
            "owner_user_id": "ALTER TABLE product_video_qc_reports ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''",
            "project_id": "ALTER TABLE product_video_qc_reports ADD COLUMN project_id TEXT NOT NULL DEFAULT ''",
        }
        for name, statement in migrations.items():
            if name not in columns:
                self.conn.execute(statement)
        # Legacy databases predate fingerprint. Create its index only after
        # the additive ALTER TABLE migration has succeeded.
        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_qc_wf_stage ON product_video_qc_reports(workflow_id, stage);
            CREATE INDEX IF NOT EXISTS idx_qc_artifact ON product_video_qc_reports(artifact_id, artifact_digest);
            CREATE INDEX IF NOT EXISTS idx_qc_fingerprint ON product_video_qc_reports(workflow_id, stage, fingerprint);
            CREATE INDEX IF NOT EXISTS idx_qc_owner_proj ON product_video_qc_reports(owner_user_id, project_id);
            """
        )
        if not self.conn.in_transaction:
            self.conn.commit()

    def save_report(self, report: QCReport) -> None:
        sql = """
        INSERT OR REPLACE INTO product_video_qc_reports (
            report_id, workflow_id, stage, artifact_id, artifact_version,
            artifact_digest, reference_digests, evaluator_name, evaluator_version,
            rubric_version, created_at, score, verdict, findings_json,
            sample_timestamps_json, fingerprint, provider_mode, model_identifier,
            policy_version, owner_user_id, project_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self.conn:
            self.conn.execute(
                sql,
                (
                    report.report_id,
                    report.workflow_id,
                    report.stage.value,
                    report.artifact_id,
                    report.artifact_version,
                    report.artifact_digest,
                    json.dumps(report.reference_digests),
                    report.evaluator_name,
                    report.evaluator_version,
                    report.rubric_version,
                    report.created_at,
                    report.score,
                    report.verdict.value,
                    json.dumps(report.findings),
                    json.dumps(report.sample_timestamps) if report.sample_timestamps is not None else None,
                    report.fingerprint,
                    report.provider_mode,
                    report.model_identifier,
                    report.policy_version,
                    report.owner_user_id,
                    report.project_id,
                ),
            )

    def claim(self, workflow_id: str, stage: QCStage, fingerprint: str) -> QCReport | None:
        """Claim one evaluation durably, or return its completed report."""
        with self.conn:
            row = self.conn.execute(
                "SELECT status, report_id FROM product_video_qc_claims "
                "WHERE workflow_id = ? AND stage = ? AND fingerprint = ?",
                (workflow_id, stage.value, fingerprint),
            ).fetchone()
            if row:
                if row[0] == "completed" and row[1]:
                    return self.get_report(row[1])
                raise RuntimeError("QC evaluation is already claimed for this workflow input")
            self.conn.execute(
                "INSERT INTO product_video_qc_claims "
                "(workflow_id, stage, fingerprint, status, claimed_at) VALUES (?, ?, ?, 'running', datetime('now'))",
                (workflow_id, stage.value, fingerprint),
            )
        return None

    def complete_claim(self, workflow_id: str, stage: QCStage, fingerprint: str, report_id: str) -> None:
        with self.conn:
            updated = self.conn.execute(
                "UPDATE product_video_qc_claims SET status = 'completed', report_id = ? "
                "WHERE workflow_id = ? AND stage = ? AND fingerprint = ? AND status = 'running'",
                (report_id, workflow_id, stage.value, fingerprint),
            ).rowcount
            if updated != 1:
                raise RuntimeError("QC claim is missing or was already completed")

    def release_claim(self, workflow_id: str, stage: QCStage, fingerprint: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM product_video_qc_claims WHERE workflow_id = ? AND stage = ? "
                "AND fingerprint = ? AND status = 'running'",
                (workflow_id, stage.value, fingerprint),
            )

    def get_report(self, report_id: str) -> QCReport | None:
        c = self.conn.cursor()
        row = c.execute(
            "SELECT report_id, workflow_id, stage, artifact_id, artifact_version, "
            "artifact_digest, reference_digests, evaluator_name, evaluator_version, "
            "rubric_version, created_at, score, verdict, findings_json, sample_timestamps_json, "
            "fingerprint, provider_mode, model_identifier, policy_version, owner_user_id, project_id "
            "FROM product_video_qc_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_report(row)

    def get_latest_report(
        self,
        workflow_id: str,
        stage: QCStage,
        expected_artifact_digest: str | None = None,
        expected_fingerprint: str | None = None,
        provider_mode: str | None = None,
        model_identifier: str | None = None,
        policy_version: str | None = None,
        owner_user_id: str | None = None,
        project_id: str | None = None,
        artifact_id: str | None = None,
        artifact_version: int | None = None,
        verdict: QCVerdict | None = None,
    ) -> QCReport | None:
        """Fetch most recent report for workflow and stage. Invalidate if digest differs."""
        c = self.conn.cursor()
        query = (
            "SELECT report_id, workflow_id, stage, artifact_id, artifact_version, "
            "artifact_digest, reference_digests, evaluator_name, evaluator_version, "
            "rubric_version, created_at, score, verdict, findings_json, sample_timestamps_json, "
            "fingerprint, provider_mode, model_identifier, policy_version, owner_user_id, project_id "
            "FROM product_video_qc_reports WHERE workflow_id = ? AND stage = ?"
        )
        params: list[Any] = [workflow_id, stage.value]
        if expected_fingerprint:
            query += " AND fingerprint = ?"
            params.append(expected_fingerprint)
        if expected_artifact_digest:
            query += " AND (artifact_digest = ? OR artifact_digest = ?)"
            clean = expected_artifact_digest.removeprefix("sha256:")
            params.extend([clean, f"sha256:{clean}"])
        if owner_user_id:
            query += " AND owner_user_id = ?"
            params.append(owner_user_id)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if artifact_id:
            query += " AND artifact_id = ?"
            params.append(artifact_id)
        if artifact_version is not None:
            query += " AND artifact_version = ?"
            params.append(artifact_version)
        if verdict is not None:
            query += " AND UPPER(verdict) = ?"
            params.append(verdict.value.upper())
        query += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        row = c.execute(query, params).fetchone()
        if not row:
            return None
        report = self._row_to_report(row)
        if expected_artifact_digest and report.artifact_digest.removeprefix("sha256:") != expected_artifact_digest.removeprefix("sha256:"):
            # Digest mismatch means artifact was modified after this QC report was generated (stale!)
            return None
        if expected_fingerprint and report.fingerprint != expected_fingerprint:
            return None
        if provider_mode and report.provider_mode != provider_mode:
            return None
        if model_identifier and report.model_identifier != model_identifier:
            return None
        if policy_version and report.policy_version != policy_version:
            return None
        if owner_user_id and report.owner_user_id != owner_user_id:
            return None
        if project_id and report.project_id != project_id:
            return None
        if artifact_id and report.artifact_id != artifact_id:
            return None
        if artifact_version is not None and report.artifact_version != artifact_version:
            return None
        if verdict is not None and report.verdict != verdict:
            return None
        return report

    def get_verified_pass_report(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        workflow_id: str,
        artifact_id: str | None = None,
        artifact_version: int | None = None,
        stage: QCStage,
        expected_sha256: str,
        artifact_path: str | None = None,
    ) -> QCReport | None:
        """Strictly fetch and verify a PASS QC report matching all provenance attributes and exact SHA-256."""
        c = self.conn.cursor()
        clean_sha = expected_sha256.removeprefix("sha256:")
        query = (
            "SELECT report_id, workflow_id, stage, artifact_id, artifact_version, "
            "artifact_digest, reference_digests, evaluator_name, evaluator_version, "
            "rubric_version, created_at, score, verdict, findings_json, sample_timestamps_json, "
            "fingerprint, provider_mode, model_identifier, policy_version, owner_user_id, project_id "
            "FROM product_video_qc_reports "
            "WHERE owner_user_id = ? AND project_id = ? AND workflow_id = ? "
            "AND stage = ? "
            "AND (artifact_digest = ? OR artifact_digest = ?)"
        )
        params: list[Any] = [
            owner_user_id,
            project_id,
            workflow_id,
            stage.value,
            clean_sha,
            f"sha256:{clean_sha}",
        ]
        if artifact_id:
            query += " AND artifact_id = ?"
            params.append(artifact_id)
        if artifact_version is not None:
            query += " AND artifact_version = ?"
            params.append(artifact_version)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        row = c.execute(query, params).fetchone()
        if not row:
            return None
        report = self._row_to_report(row)
        if report.artifact_digest.removeprefix("sha256:") != clean_sha:
            return None
        if report.verdict != QCVerdict.PASS:
            return None
        findings = report.findings or {}
        reused_final_review = findings.get("content_evidence_source") == "creative_final_review"
        technical_only_final = findings.get("technical_only_final") is True
        if reused_final_review:
            evidence = findings.get("creative_final_review")
            review_receipt = (
                (evidence.get("final_review") or {}).get("evaluator_receipt")
                if isinstance(evidence, dict) else {}
            )
            reviewer_provider = str((review_receipt or {}).get("provider") or "").casefold()
            reviewer_model = str((review_receipt or {}).get("model") or "").strip()
            if (
                stage != QCStage.VIDEO
                or report.score != 0
                or report.provider_mode != "configured"
                or not isinstance(evidence, dict)
                or not artifact_path
                or not reviewer_provider
                or not reviewer_model
                or reviewer_provider in {"fake", "mock", "offline", "test"}
                or str(evidence.get("sha256") or "").removeprefix("sha256:") != clean_sha
            ):
                return None
            try:
                from video_factory.creative.agents.video_review import require_final_pass
                require_final_pass({**evidence, "file_path": artifact_path})
            except (KeyError, ValueError, OSError):
                return None
        elif technical_only_final:
            if (stage != QCStage.VIDEO or report.score != 0 or report.provider_mode != "configured"
                    or not (findings.get("technical_specs") or {}).get("technical_pass") or not artifact_path):
                return None
            try:
                from video_factory.creative.agents.video_review import require_final_pass
                require_final_pass({'file_path':artifact_path,'sha256':clean_sha})
            except (KeyError, ValueError, OSError):
                return None
        elif report.score < minimum_pass_score(stage):
            return None
        return report

    @staticmethod
    def _row_to_report(row: Any) -> QCReport:
        ref_digests = json.loads(row[6]) if row[6] else []
        findings = json.loads(row[13]) if row[13] else {}
        sample_ts = json.loads(row[14]) if row[14] else None
        return QCReport(
            report_id=row[0],
            workflow_id=row[1],
            stage=QCStage(row[2]),
            artifact_id=row[3],
            artifact_version=row[4],
            artifact_digest=row[5],
            reference_digests=ref_digests,
            evaluator_name=row[7],
            evaluator_version=row[8],
            rubric_version=row[9],
            created_at=row[10],
            score=row[11],
            verdict=QCVerdict(row[12]),
            findings=findings,
            sample_timestamps=sample_ts,
            fingerprint=row[15] if len(row) > 15 else "",
            provider_mode=row[16] if len(row) > 16 else "configured",
            model_identifier=row[17] if len(row) > 17 else "",
            policy_version=row[18] if len(row) > 18 else "",
            owner_user_id=row[19] if len(row) > 19 else "",
            project_id=row[20] if len(row) > 20 else "",
        )
