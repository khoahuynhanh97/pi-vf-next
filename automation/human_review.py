"""Human verdicts live beside immutable exports and bind to the reviewed video bytes."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from statistics import median
from typing import NamedTuple

from automation.config import VF_DB_PATH, VF_EXPORTS_DIR, now_bangkok
from automation.daily_report import preproduction_summary
from automation.storage import atomic_json


VERDICT_CAUSES = ("script", "render", "product", "other")


class ComparisonRow(NamedTuple):
    export: str
    preproduction: str
    final: str
    final_score: int | str
    human: str
    cause: str
    reason: str
    preproduction_score: int | None = None


def _video_sha(export_dir):
    digest = hashlib.sha256()
    with (export_dir / "final.mp4").open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binding(metadata):
    return {"project_id": metadata["project_id"], "export_version": metadata["version"],
            "video_sha256": metadata["video"]["sha256"].removeprefix("sha256:")}


def save_human_verdict(export_dir, verdict, reason, *, cause):
    reason = " ".join(reason.split())
    if verdict not in ("approve", "reject") or not 1 <= len(reason) <= 240:
        raise ValueError("Provide approve/reject and a reason of 1–240 characters.")
    if cause not in VERDICT_CAUSES:
        raise ValueError("Provide a cause of script, render, product, or other.")
    metadata = json.loads((export_dir / "metadata.json").read_text(encoding="utf-8"))
    binding = _binding(metadata)
    if _video_sha(export_dir) != binding["video_sha256"]:
        raise ValueError("final.mp4 does not match the export metadata")
    path = export_dir / "human_verdict.json"
    atomic_json(path, {**binding, "verdict": verdict, "cause": cause, "reason": reason,
                       "reviewed_at": now_bangkok().isoformat()})
    return path


def _qc_comparison(conn, metadata, sha, tables):
    provenance = metadata.get("provenance") or {}
    creative = provenance.get("creative") or {}
    final = (creative.get("final_review") or {}).get("verdict") or "—"
    score, preproduction, preproduction_score = "—", "—", None
    report_id = (provenance.get("qc") or {}).get("video")
    if not report_id or "product_video_qc_reports" not in tables:
        return preproduction, final, score, preproduction_score
    row = conn.execute(
        "SELECT workflow_id, score, verdict, findings_json FROM product_video_qc_reports "
        "WHERE report_id=? AND artifact_digest IN (?, ?)",
        (report_id, sha, "sha256:" + sha),
    ).fetchone()
    if not row:
        return preproduction, final, score, preproduction_score
    findings = json.loads(row[3])
    if findings.get("technical_only_final"):
        final = f"{row[2]} (technical)"
    elif findings.get("content_evidence_source") == "creative_final_review":
        final = f"{row[2]} (content)"
    else:
        final, score = row[2], row[1]
    if "workflow" in tables:
        saved = conn.execute("SELECT aggregate_json FROM workflow WHERE workflow_id=?", (row[0],)).fetchone()
        scene = (json.loads(saved[0]).get("scene_plan") or {}) if saved else {}
        lineage = creative.get("creative_lineage") or {}
        script_digest = (lineage.get("script") or {}).get("script_digest")
        bundle_digest = (lineage.get("scene_prompts") or {}).get("digest")
        if (script_digest and script_digest == scene.get("script_digest") and bundle_digest
                and bundle_digest == (scene.get("scene_prompts") or {}).get("digest")):
            review = preproduction_summary(scene)
            if review:
                total = review.get("total")
                if review["verdict"] in ("PASS", "FAIL") and type(total) is int and 0 <= total <= 100:
                    preproduction_score = total
                shown = preproduction_score if preproduction_score is not None else "—"
                preproduction = f"{shown} ({review['verdict']})"
    return preproduction, final, score, preproduction_score


def comparison_rows(exports_root, db_path):
    if db_path.is_file():
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            yield from _export_rows(exports_root, conn, tables)
    else:
        yield from _export_rows(exports_root, None, set())


def _export_rows(exports_root, conn, tables):
    for path in sorted(exports_root.glob("*/metadata.json")):
        export = path.parent
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            binding = _binding(metadata)
            human, cause, reason = "pending", "unknown", ""
            verdict_file = export / "human_verdict.json"
            if verdict_file.is_file():
                saved = json.loads(verdict_file.read_text(encoding="utf-8"))
                if (any(saved.get(key) != value for key, value in binding.items())
                        or _video_sha(export) != binding["video_sha256"]):
                    yield ComparisonRow(export.name, "—", "—", "—", "stale", "unknown", "Export binding changed")
                    continue
                human, reason = saved["verdict"], saved["reason"]
                if saved.get("cause") in VERDICT_CAUSES:
                    cause = saved["cause"]
            preproduction, final, score, preproduction_score = _qc_comparison(
                conn, metadata, binding["video_sha256"], tables
            )
            yield ComparisonRow(export.name, preproduction, final, score, human, cause, reason, preproduction_score)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            yield ComparisonRow(export.name, "—", "—", "—", "unavailable", "unknown", type(exc).__name__)


def cmd_human_verdict(args):
    try:
        print(save_human_verdict(args.export_dir, args.verdict, args.reason, cause=args.cause))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Could not save verdict: {exc}", file=sys.stderr)
        return 1
    return 0


def _print_summary(rows, threshold):
    scores = [row.preproduction_score for row in rows if row.preproduction_score is not None]
    print(f"\nPreproduction QC /100 (threshold={threshold}, scored={len(scores)}/{len(rows)}): "
          f"min={min(scores) if scores else '—'}, median={median(scores) if scores else '—'}, "
          f"max={max(scores) if scores else '—'}, below={sum(score < threshold for score in scores)}")
    reviewed = [row for row in rows if row.human in ("approve", "reject")]
    for label, causes in (("script+product", ("script", "product")), ("render/other", ("render", "other"))):
        group = [row for row in reviewed if row.cause in causes]
        scored = [row for row in group if row.preproduction_score is not None]
        approved = sum(row.human == "approve" for row in group)
        print(f"{label}: reviewed={len(group)}, approve={approved}, reject={len(group) - approved}, scored={len(scored)}")
        if label == "script+product":
            above = sum(row.human == "reject" and row.preproduction_score >= threshold for row in scored)
            below = sum(row.human == "approve" and row.preproduction_score < threshold for row in scored)
            print(f"  reject at/above threshold={above}, approve below threshold={below}")
    print(f"Unknown cause (excluded from both groups): {sum(row.cause == 'unknown' for row in reviewed)}")


def cmd_compare_qc(args):
    try:
        rows = list(comparison_rows(args.exports_root, args.db_path))
    except sqlite3.Error as exc:
        print(f"Could not read QC database: {exc}", file=sys.stderr)
        return 1
    print("| Export | Preproduction /100 | Final QC | Final score | Human | Cause | Reason |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        cells = (" ".join(str(cell).split()).replace("|", "&#124;") for cell in row[:-1])
        print("| " + " | ".join(cells) + " |")
    _print_summary(rows, args.threshold)
    return 0
