import hashlib
import json

from PIL import Image

from video_factory.creative.storyboard import select_best_keyframe_candidate
from video_factory.qc.retry_policy import claim_qc_attempt, complete_qc_attempt
from video_factory.storage_infra.sqlite import connect


def _candidate(path, *, score, verdict="pass", identity_mismatch=False, attempt=1):
    Image.new("RGB", (64, 96), (attempt * 40, 20, 90)).save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "frame_id": "scene-1-start", "image_path": str(path), "sha256": digest,
        "qc_verdict": verdict, "qc_score": score, "qc_hard_failure": verdict != "pass",
        "qc_findings": {"identity_mismatch": identity_mismatch},
        "qc_receipt": {"report_id": f"qc-{attempt}", "artifact_digest": f"board-{attempt}"},
    }


def test_keyframe_candidate_selection_stops_at_one_eligible_initial_and_ranks_passes(tmp_path):
    first = _candidate(tmp_path / "first.png", score=72)
    assert select_best_keyframe_candidate([first]) == first
    better = _candidate(tmp_path / "better.png", score=88, attempt=2)
    failed = _candidate(tmp_path / "failed.png", score=99, verdict="fail", attempt=3)
    assert select_best_keyframe_candidate([first, better, failed]) == better
    assert select_best_keyframe_candidate([first, failed]) == first


def test_high_score_wrong_identity_candidate_is_never_selected(tmp_path):
    valid = _candidate(tmp_path / "valid.png", score=40)
    wrong = _candidate(tmp_path / "wrong.png", score=100, identity_mismatch=True, attempt=2)
    assert select_best_keyframe_candidate([valid, wrong]) == valid
    assert select_best_keyframe_candidate([wrong]) is None


def test_candidate_receipt_and_quota_survive_restart(tmp_path):
    db_path = tmp_path / "candidate-resume.db"
    conn = connect(db_path)
    candidates = [
        _candidate(tmp_path / "first.png", score=80, attempt=1),
        _candidate(tmp_path / "better.png", score=91, attempt=2),
    ]
    first = claim_qc_attempt(conn, workflow_id="wf-kf", retry_type="four_agent_image_send",
        item_id="scene-1-start", operation_id="op-1", input_hash="prompt-v1", max_recoveries=2)
    complete_qc_attempt(conn, attempt_id=first["attempt_id"], status="completed", verdict="generated")
    second = claim_qc_attempt(conn, workflow_id="wf-kf", retry_type="four_agent_image_send",
        item_id="scene-1-start", operation_id="op-2", input_hash="prompt-v2", max_recoveries=2)
    complete_qc_attempt(conn, attempt_id=second["attempt_id"], status="completed", verdict="generated")
    conn.close()

    persisted_candidates = json.loads(json.dumps(candidates))
    conn = connect(db_path)
    replay = claim_qc_attempt(conn, workflow_id="wf-kf", retry_type="four_agent_image_send",
        item_id="scene-1-start", operation_id="op-1", input_hash="prompt-v1", max_recoveries=2)
    assert replay["reused"] is True and replay["attempt_number"] == 1
    assert conn.execute("SELECT COUNT(*) FROM workflow_qc_retries WHERE workflow_id='wf-kf'").fetchone()[0] == 2
    selected = select_best_keyframe_candidate(persisted_candidates)
    assert selected["sha256"] == hashlib.sha256((tmp_path / "better.png").read_bytes()).hexdigest()
    assert selected["qc_receipt"] == {"report_id": "qc-2", "artifact_digest": "board-2"}
    conn.close()
