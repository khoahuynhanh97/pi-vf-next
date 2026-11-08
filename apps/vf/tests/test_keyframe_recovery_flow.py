"""Comprehensive tests for Google Flow keyframe recovery, checkpoint durability, and CLI commands.

Verifies:
1. Crash after checkpoint preserves project URL across real SQLite file reopen.
2. Unknown Google Flow operations retain their durable project and capacity until an operator can prove a terminal outcome.
3. Operator-confirmed image is reused without resending.
4. Rejected/missing image authorizes replacement for ONLY the exact missing frame.
5. Direct submit without a durable store or callback fails pre-Send with Send=0.
6. Real CLI commands parse positional workflow_id and authorization flags correctly.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from video_factory.application.operator_handler import (
    confirm_keyframe_submission,
    recover_unknown_keyframe_submission,
)
from video_factory.cli import main
from video_factory.creative.contracts import (
    EligibleAsset,
    ProductIdentityV2,
    ResearchDossier,
    ScriptScenePlanV2,
    ScriptSceneV2,
)
from video_factory.creative.storyboard import compile_keyframe_requests
from video_factory.integrations.google_flow_keyframe_provider import (
    GoogleFlowKeyframeProvider,
)
from video_factory.integrations.keyframe_provider import (
    KeyframeGenerationRequest,
    KeyframeGenerationResult,
    KeyframeOperationStore,
)
from video_factory.integrations.capacity import ProviderCapacityStore
from video_factory.storage_infra.sqlite import connect
from video_factory.workflow import ProductVideoWorkflowState
from video_factory.workflows.envelope import build_envelope
from video_factory.workflows.repository import WorkflowRepository


def _create_test_image(path: Path, width: int = 720, height: int = 1280) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (width, height), color=(200, 100, 50))
    im.save(path, format="PNG")
    return path


def _quarantine_storyboard_outbox(
    conn: sqlite3.Connection,
    workflow_id: str,
    error_msg: str,
    scene_plan: Any | None = None,
    project_id: str | None = None,
    owner_user_id: str | None = None,
) -> int:
    payload: dict[str, Any] = {
        "job_name": "product_video.storyboard",
        "workflow_id": workflow_id,
    }
    if scene_plan is not None:
        payload["args"] = [scene_plan.to_dict() if hasattr(scene_plan, "to_dict") else scene_plan]
    if project_id or owner_user_id:
        kwargs: dict[str, Any] = {}
        if project_id:
            kwargs["project_id"] = project_id
            payload["project_id"] = project_id
        if owner_user_id:
            kwargs["owner_user_id"] = owner_user_id
            payload["owner_user_id"] = owner_user_id
        payload["kwargs"] = kwargs

    msg_env = build_envelope(
        workflow_id=workflow_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_{workflow_id}",
        payload=payload,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        cur = conn.execute(
            """INSERT INTO workflow_outbox (workflow_id, message, state, last_error, created_at, updated_at)
            VALUES (?, ?, 'quarantined', ?, ?, ?)""",
            (workflow_id, msg_env, error_msg, now_iso, now_iso),
        )
        return cur.lastrowid


def _setup_workflow_in_db(
    conn: sqlite3.Connection,
    workflow_id: str = "wf_rec_test",
    owner_user_id: str = "owner_alice",
    project_id: str = "proj_rec_test",
    tmp_path: Path | None = None,
    num_scenes: int = 1,
) -> tuple[ScriptScenePlanV2, ResearchDossier, list[KeyframeGenerationRequest]]:
    ref_dir = tmp_path if tmp_path else Path("tests/fixtures")
    ref_img1 = ref_dir / f"ref1_{workflow_id}.png"
    ref_img2 = ref_dir / f"ref2_{workflow_id}.png"
    _create_test_image(ref_img1, 100, 100)
    _create_test_image(ref_img2, 100, 100)
    ref_sha1 = hashlib.sha256(ref_img1.read_bytes()).hexdigest()
    ref_sha2 = hashlib.sha256(ref_img2.read_bytes()).hexdigest()

    dossier = ResearchDossier.create(
        workflow_id=workflow_id,
        product_identity=ProductIdentityV2("BrandX", "SerumX", "BrandX SerumX"),
        verified_claims=(),
        eligible_assets=(
            EligibleAsset("asset-1", str(ref_img1), ref_sha1, "qc-1"),
            EligibleAsset("asset-2", str(ref_img2), ref_sha2, "qc-1"),
        ),
    )

    scenes = []
    for i in range(1, num_scenes + 1):
        scenes.append(
            ScriptSceneV2(
                scene_id=f"scene_{i}",
                order=i,
                story_role="demo",
                duration_seconds=6,
                narration=f"Show serum {i}",
                overlay_text=f"Hydration {i}",
                visual_objective=f"Show bottle {i}",
                environment="Studio",
                subject_action="Hold bottle",
                product_interaction="Hold serum bottle",
                start_state="Bottle on desk",
                end_state="Bottle held",
                visual_change="Pick up",
                camera_framing="Close up",
                camera_movement="Static",
                transition_out="Cut",
                claim_refs=(),
                reference_asset_ids=("asset-1",),
            )
        )

    script = ScriptScenePlanV2.create(
        target_duration_seconds=6 * num_scenes,
        brief_digest="sha256:" + "a" * 64,
        scenes=tuple(scenes),
    )
    scene_plan_dict = script.to_dict()
    scene_plan_dict["research_dossier"] = dossier.to_dict()

    repo = WorkflowRepository(conn)
    repo.commit_workflow(
        workflow_id=workflow_id,
        aggregate_json=json.dumps({
            "state": ProductVideoWorkflowState.CREATING_STORYBOARD.value,
            "owner_user_id": owner_user_id,
            "project_id": project_id,
            "scene_plan": scene_plan_dict,
            "latest_artifact": scene_plan_dict,
        }),
        events=[],
        expected_version=None,
    )
    requests = list(
        compile_keyframe_requests(
            workflow_id=workflow_id,
            project_id=project_id,
            dossier=dossier,
            script=script,
        )
    )
    return script, dossier, requests


def test_script_scene_plan_v2_round_trips_without_changing_digest(tmp_path: Path):
    conn = connect(tmp_path / "roundtrip.db")
    try:
        script, _dossier, _requests = _setup_workflow_in_db(conn, tmp_path=tmp_path)
        restored = ScriptScenePlanV2.from_dict(script.to_dict())
        assert restored.digest() == script.digest()
    finally:
        conn.close()


def test_crash_after_checkpoint_preserves_url_real_db_reopen(tmp_path: Path):
    """Case 1: Crash after checkpoint preserves project URL across real SQLite file reopen."""
    db_file = tmp_path / "pilot_case1.db"
    conn = connect(db_file)
    store = KeyframeOperationStore(conn)

    req = KeyframeGenerationRequest(
        operation_id="op_case1",
        workflow_id="wf_case1",
        project_id="proj_case1",
        frame_id="scene_1-start",
        prompt="A bottle of serum",
        product_description="Skincare serum",
        reference_image_paths=(),
    )

    # Claim first submission
    claimed = store.claim_first_submission(req, "google_flow")
    assert claimed is True

    # Record project checkpoint before Generate
    project_url = "https://flow.google.com/project/11111111-2222-3333-4444-555555555555"
    store.record_project_checkpoint(req, project_url, "google_flow")

    # Simulate process crash: close SQLite connection immediately
    conn.close()

    # Worker restarts: reopen connection to the real SQLite file
    conn2 = connect(db_file)
    store2 = KeyframeOperationStore(conn2)

    op_record = store2.get(req.operation_id)
    assert op_record is not None
    assert op_record["provider_operation_id"] == project_url
    assert op_record["status"] == "project_created"
    assert json.loads(op_record["provider_metadata_json"])["project_url"] == project_url

    conn2.close()


def test_output_download_reconcile_is_bounded_and_survives_restart(tmp_path: Path):
    db_file = tmp_path / "download_reconcile.db"
    conn = connect(db_file)
    store = KeyframeOperationStore(conn)
    req = KeyframeGenerationRequest(
        operation_id="op_download_bound", workflow_id="wf_download_bound",
        project_id="proj_download_bound", frame_id="scene_1-start",
        prompt="A bottle", product_description="Bottle",
        reference_image_paths=(),
    )
    project_url = "https://flow.google.com/project/11111111-2222-3333-4444-555555555555"
    assert store.claim_first_submission(req, "google_flow") is True
    store.record_project_checkpoint(req, project_url, "google_flow")
    store.save_result(req, KeyframeGenerationResult(
        operation_id=req.operation_id, status="unknown", provider_operation_id=project_url,
        error_code="RECONCILIATION_REQUIRED",
    ))

    for expected in (1, 2, 3):
        assert store.claim_output_reconciliation(req.operation_id, project_url, max_attempts=3) == expected
    assert store.claim_output_reconciliation(req.operation_id, project_url, max_attempts=3) is None
    conn.close()

    reopened = connect(db_file)
    assert KeyframeOperationStore(reopened).claim_output_reconciliation(
        req.operation_id, project_url, max_attempts=3,
    ) is None
    assert KeyframeOperationStore(reopened).get(req.operation_id)["status"] == "unknown"
    reopened.close()


def test_recover_unknown_keyframe_retries_download_without_resubmitting(tmp_path: Path, monkeypatch):
    conn = connect(tmp_path / "recover_download.db")
    workflow_id = "wf_download_reconcile"
    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=workflow_id, tmp_path=tmp_path,
    )
    request = requests[0]
    store = KeyframeOperationStore(conn)
    assert store.claim_first_submission(request, "google_flow") is True
    project_url = "https://flow.google.com/project/11111111-2222-3333-4444-555555555555"
    store.record_project_checkpoint(request, project_url, "google_flow")
    store.save_result(request, KeyframeGenerationResult(
        operation_id=request.operation_id, status="unknown", provider_operation_id=project_url,
        error_code="RECONCILIATION_REQUIRED",
    ))
    _quarantine_storyboard_outbox(conn, workflow_id, "RECONCILIATION_REQUIRED: output download timed out")
    image = _create_test_image(tmp_path / "recovered.png")

    class ExistingProjectProvider:
        provider_kind = "google_flow"
        calls = 0

        def bind_operation_store(self, bound_store):
            self.store = bound_store

        def submit(self, _request):
            raise AssertionError("download reconciliation must never submit generation")

        def reconcile_existing_output(self, target_request, url):
            assert target_request.operation_id == request.operation_id
            assert url == project_url
            self.calls += 1
            attempt = self.store.claim_output_reconciliation(
                target_request.operation_id, url, max_attempts=3,
            )
            if attempt is None:
                return KeyframeGenerationResult(
                    operation_id=target_request.operation_id, status="unknown",
                    provider_operation_id=url, error_code="OUTPUT_RECONCILIATION_EXHAUSTED",
                )
            if self.calls == 1:
                return KeyframeGenerationResult(
                    operation_id=target_request.operation_id, status="unknown",
                    provider_operation_id=url, error_code="GOOGLE_FLOW_OUTPUT_NOT_RETRIEVED",
                )
            return KeyframeGenerationResult(
                operation_id=target_request.operation_id, status="completed",
                provider_operation_id=url, image_path=str(image),
                metadata={"provider_kind": "google_flow", "download_attempt": attempt},
            )

    provider = ExistingProjectProvider()
    monkeypatch.setattr(
        "video_factory.integrations.keyframe_provider_factory.resolve_keyframe_provider",
        lambda *_args, **_kwargs: (provider, "google_flow"),
    )

    with pytest.raises(ValueError, match="unknown"):
        recover_unknown_keyframe_submission(
            conn, workflow_id=workflow_id, owner_user_id="owner_alice",
            operation_id=request.operation_id,
        )
    result = recover_unknown_keyframe_submission(
        conn, workflow_id=workflow_id, owner_user_id="owner_alice",
        operation_id=request.operation_id,
    )

    assert result["reconciled"] == 1
    assert result["resumed"] is True
    assert provider.calls == 2
    op = KeyframeOperationStore(conn).get(request.operation_id)
    assert op["status"] == "completed"
    assert op["output_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert json.loads(op["provider_metadata_json"])["download_attempt"] == 2
    event = conn.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='KeyframeOutputReconciled'",
        (workflow_id,),
    ).fetchone()
    assert json.loads(event["payload_json"])["output_sha256"] == op["output_sha256"]
    assert conn.execute("SELECT state FROM workflow_outbox WHERE workflow_id=?", (workflow_id,)).fetchone()[0] == "pending"
    conn.close()


def test_unknown_keyframe_status_keeps_operation_and_capacity_locked(tmp_path: Path):
    """An unresolved Flow project must not be replaced just because its lease expired."""
    db_file = tmp_path / "pilot_case2.db"
    conn = connect(db_file)
    wf_id = "wf_case2"
    owner_id = "owner_alice"
    proj_id = "proj_case2"
    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path
    )
    req = requests[0]

    # Create a quarantined outbox entry with RECONCILIATION_REQUIRED
    _quarantine_storyboard_outbox(
        conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain"
    )

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req, "google_flow")
    store.record_project_checkpoint(req, "https://flow.google.com/project/url-proj-created", "google_flow")
    capacity_store = ProviderCapacityStore(conn)
    assert capacity_store.try_acquire(
        provider_kind="google_flow", operation_id=req.operation_id,
        lease_owner="dead-worker", limit=1,
        profile_id="D:\\work\\video-factory\\data\\chrome_flow_profile",
    )
    capacity_store.bind_provider_operation(
        "google_flow", req.operation_id, "https://flow.google.com/project/url-proj-created",
    )

    # Close and reopen SQLite file
    conn.close()
    conn2 = connect(db_file)

    auth = {
        "workflow_id": wf_id,
        "run_id": "auth_run_case2",
        "scope": ["keyframe"],
        "expires_at": "2028-12-31T23:59:59Z",
        "max_cost_usd": 5.0,
    }

    with pytest.raises(ValueError, match="still unknown"):
        recover_unknown_keyframe_submission(
            conn2,
            workflow_id=wf_id,
            owner_user_id=owner_id,
            authorization=auth,
            operation_id=req.operation_id,
        )

    op_after = KeyframeOperationStore(conn2).get(req.operation_id)
    assert op_after["status"] == "project_created"
    capacity = conn2.execute(
        "SELECT provider_operation_id FROM video_factory_provider_capacity "
        "WHERE provider_kind='google_flow' AND operation_id=?",
        (req.operation_id,),
    ).fetchone()
    assert capacity["provider_operation_id"] == "https://flow.google.com/project/url-proj-created"
    assert conn2.execute("SELECT state FROM workflow_outbox WHERE workflow_id=?", (wf_id,)).fetchone()[0] == "quarantined"

    conn2.close()


@pytest.mark.parametrize(("outcome", "expected_status"), [
    ("failed", "failed"),
    ("cancelled", "cancelled"),
    ("canceled", "cancelled"),
])
def test_operator_terminal_reconciliation_requires_exact_project_and_terminal_state(
    tmp_path: Path, outcome: str, expected_status: str,
):
    from video_factory.application.operator_handler import record_terminal_keyframe_outcome

    conn = connect(tmp_path / "terminal-reconcile.db")
    workflow_id = "wf_terminal_reconcile"
    owner_id = "owner_alice"
    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=workflow_id, owner_user_id=owner_id,
        project_id="proj_terminal_reconcile", tmp_path=tmp_path,
    )
    request = requests[0]
    project_url = "https://flow.google.com/project/terminal-reconcile"
    _quarantine_storyboard_outbox(
        conn, workflow_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain",
    )
    store = KeyframeOperationStore(conn)
    store.claim_first_submission(request, "google_flow")
    store.record_project_checkpoint(request, project_url, "google_flow")
    capacity = ProviderCapacityStore(conn)
    assert capacity.try_acquire(
        provider_kind="google_flow", operation_id=request.operation_id,
        lease_owner="test-worker", limit=1, profile_id="flow-profile",
    )
    capacity.bind_provider_operation("google_flow", request.operation_id, project_url)

    with pytest.raises(ValueError, match="terminal outcome"):
        record_terminal_keyframe_outcome(
            conn, workflow_id=workflow_id, operation_id=request.operation_id,
            owner_user_id=owner_id, project_url=project_url, outcome="running",
            evidence_reference="manual Flow inspection", note="Still running",
        )
    with pytest.raises(ValueError, match="does not match"):
        record_terminal_keyframe_outcome(
            conn, workflow_id=workflow_id, operation_id=request.operation_id,
            owner_user_id=owner_id, project_url="https://flow.google.com/project/wrong",
            outcome=outcome, evidence_reference="manual Flow inspection",
            note="Flow shows terminal",
        )

    assert store.get(request.operation_id)["status"] == "project_created"
    assert capacity.active("google_flow")[0]["provider_operation_id"] == project_url

    result = record_terminal_keyframe_outcome(
        conn, workflow_id=workflow_id, operation_id=request.operation_id,
        owner_user_id=owner_id, project_url=project_url, outcome=outcome,
        evidence_reference="operator checked exact Flow project", note="Flow project is terminal",
    )
    assert result["status"] == expected_status
    assert result["capacity_released"] is True
    assert store.get(request.operation_id)["status"] == expected_status
    assert capacity.active("google_flow") == []
    assert conn.execute(
        "SELECT state FROM workflow_outbox WHERE workflow_id=?", (workflow_id,),
    ).fetchone()[0] == "quarantined"
    event = conn.execute(
        "SELECT payload_json FROM workflow_event WHERE workflow_id=? AND event_type='KeyframeOperationTerminalReconciled'",
        (workflow_id,),
    ).fetchone()
    assert json.loads(event["payload_json"])["provider_operation_id"] == project_url
    conn.close()


def test_confirmed_image_is_reused_without_resend(tmp_path: Path):
    """Case 3: Operator-confirmed image updates operation to completed and continues workflow without replacement."""
    db_file = tmp_path / "pilot_case3.db"
    conn = connect(db_file)
    wf_id = "wf_case3"
    owner_id = "owner_alice"
    proj_id = "proj_case3"
    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path
    )
    req = requests[0]
    op_id = req.operation_id

    # Enqueue and quarantine storyboard job
    _quarantine_storyboard_outbox(
        conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain"
    )

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req, "google_flow")
    project_url = "https://flow.google.com/project/url-case3"
    store.record_project_checkpoint(req, project_url, "google_flow")

    # Operator downloads verified image from Flow
    img_file = _create_test_image(tmp_path / "downloaded_keyframe.png", 720, 1280)

    # Operator confirms the keyframe image
    confirm_res = confirm_keyframe_submission(
        conn,
        workflow_id=wf_id,
        operation_id=op_id,
        image_path=img_file,
        owner_user_id=owner_id,
        project_url=project_url,
        resume_workflow=True,
    )
    assert confirm_res["status"] == "confirmed"
    assert confirm_res["resumed"] is True
    assert confirm_res["recovery_result"]["reconciled"] == 1
    assert confirm_res["recovery_result"]["replaced"] == 0

    # Close and reopen database connection
    conn.close()
    conn2 = connect(db_file)
    store2 = KeyframeOperationStore(conn2)
    record = store2.get(op_id)
    assert record["status"] == "completed"
    assert record["output_path"] == str(img_file.resolve())
    assert record["output_sha256"] == confirm_res["sha256"]

    # Quarantined row must be back to pending
    outbox_row = conn2.execute("SELECT state FROM workflow_outbox WHERE workflow_id = ?", (wf_id,)).fetchone()
    assert outbox_row[0] == "pending"

    conn2.close()


def test_unverified_rejected_image_keeps_exact_operation_locked(tmp_path: Path):
    """A note that output was rejected cannot replace an operation still reported unknown."""
    db_file = tmp_path / "pilot_case4.db"
    conn = connect(db_file)
    wf_id = "wf_case4"
    owner_id = "owner_alice"
    proj_id = "proj_case4"
    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path, num_scenes=2
    )
    req1 = requests[0]
    req2 = requests[1]

    store = KeyframeOperationStore(conn)
    # Scene 1: Already completed
    img1 = _create_test_image(tmp_path / "s1.png", 720, 1280)
    store.claim_first_submission(req1, "google_flow")
    store.save_result(req1, KeyframeGenerationResult(operation_id=req1.operation_id, status="completed", image_path=str(img1)))

    # Scene 2: Unknown / project_created (operator inspected and confirmed no usable output)
    store.claim_first_submission(req2, "google_flow")
    store.record_project_checkpoint(req2, "https://flow.google.com/project/url-rejected-s2", "google_flow")

    # Quarantine outbox
    _quarantine_storyboard_outbox(
        conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output rejected"
    )

    # Close and reopen database
    conn.close()
    conn2 = connect(db_file)

    auth = {
        "workflow_id": wf_id,
        "run_id": "auth_replace_s2",
        "scope": ["keyframe"],
        "expires_at": "2028-12-31T23:59:59Z",
        "max_cost_usd": 5.0,
    }

    with pytest.raises(ValueError, match="still unknown"):
        recover_unknown_keyframe_submission(
            conn2,
            workflow_id=wf_id,
            owner_user_id=owner_id,
            authorization=auth,
            operation_id=req2.operation_id,
        )
    updated_agg, _ver = WorkflowRepository(conn2).get_workflow(wf_id)
    assert "keyframe_recovery_plan" not in updated_agg
    assert KeyframeOperationStore(conn2).get(req2.operation_id)["status"] == "project_created"
    assert conn2.execute("SELECT state FROM workflow_outbox WHERE workflow_id=?", (wf_id,)).fetchone()[0] == "quarantined"

    conn2.close()


@patch.object(GoogleFlowKeyframeProvider, "_attach_image_references", lambda self, page, paths: None)
@patch.object(GoogleFlowKeyframeProvider, "_ensure_image_generation_settings", lambda self, page: None)
def test_direct_submit_without_store_does_not_click(tmp_path: Path):
    """Case 5: Direct submit without a durable store or callback fails pre-Send with Send=0."""
    ref_file = _create_test_image(tmp_path / "ref.png")
    out_dir = tmp_path / "out"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Provider with NO store and NO on_project_created callback
    provider = GoogleFlowKeyframeProvider(
        profile_path=profile_dir,
        output_dir=out_dir,
        ui_contract_verified=True,
        reference_upload_verified=True,
        cost_contract_verified=True,
    )

    req = KeyframeGenerationRequest(
        operation_id="op_direct_nostore",
        workflow_id="wf_direct",
        project_id="proj_direct",
        frame_id="scene_1-start",
        prompt="A bottle of serum",
        product_description="Skincare serum",
        reference_image_paths=(str(ref_file),),
    )

    generate_click_mock = MagicMock()
    mock_page = MagicMock()
    mock_page.url = "https://flow.google.com/project/url-nostore"
    mock_page.query_selector.side_effect = lambda sel: MagicMock() if "profile-button" in sel else None

    def mock_locator(sel):
        loc = MagicMock()
        if "new-project-button" in sel:
            loc.count.return_value = 1
        elif 'input[type="file"]' in sel:
            loc.count.return_value = 1
            loc.evaluate.return_value = 1
        elif "detail-add-to-prompt-btn" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif "ProseMirror" in sel:
            loc.count.return_value = 1
        elif "Bắt đầu tạo" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
            loc.click = generate_click_mock
        else:
            loc.count.return_value = 0
            loc.is_visible.return_value = False
        return loc

    mock_page.locator.side_effect = mock_locator
    mock_browser = MagicMock()
    mock_browser.pages = [mock_page]

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = mock_browser
        res = provider.submit(req)

    # Must fail pre-Send with DURABLE_CHECKPOINT_REQUIRED
    assert res.status == "failed"
    assert res.error_code == "DURABLE_CHECKPOINT_REQUIRED"
    assert res.metadata["send_attempted"] is False
    assert generate_click_mock.call_count == 0, "Generate button must NEVER be clicked if durable store is missing"


def test_cli_commands_receive_correct_positional_and_flag_argv(tmp_path: Path):
    """Case 6: Real CLI commands parse positional workflow_id and authorization flags correctly."""
    db_file = tmp_path / "cli_flow.db"
    conn = connect(db_file)
    owner_id = "owner_alice"

    # Part A: CLI confirm-keyframe
    wf_confirm = "wf_cli_confirm"
    proj_confirm = "proj_cli_confirm"
    _script1, _dossier1, reqs1 = _setup_workflow_in_db(
        conn, workflow_id=wf_confirm, owner_user_id=owner_id, project_id=proj_confirm, tmp_path=tmp_path
    )
    req_confirm = reqs1[0]
    op_confirm_id = req_confirm.operation_id

    _quarantine_storyboard_outbox(
        conn, wf_confirm, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain"
    )

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req_confirm, "google_flow")
    url_confirm = "https://flow.google.com/project/url-cli-confirm"
    store.record_project_checkpoint(req_confirm, url_confirm, "google_flow")

    # Part B: CLI recover-keyframe
    wf_recover = "wf_cli_recover"
    proj_recover = "proj_cli_recover"
    _script2, _dossier2, reqs2 = _setup_workflow_in_db(
        conn, workflow_id=wf_recover, owner_user_id=owner_id, project_id=proj_recover, tmp_path=tmp_path
    )
    req_recover = reqs2[0]

    _quarantine_storyboard_outbox(
        conn, wf_recover, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain"
    )

    store.claim_first_submission(req_recover, "google_flow")
    url_recover = "https://flow.google.com/project/url-cli-recover"
    store.record_project_checkpoint(req_recover, url_recover, "google_flow")
    conn.close()

    img_file = _create_test_image(tmp_path / "cli_keyframe.png", 720, 1280)

    # Test CLI 1: confirm-keyframe with positional workflow_id
    ret_confirm = main([
        "confirm-keyframe",
        wf_confirm,
        "--operation-id", op_confirm_id,
        "--image-path", str(img_file),
        "--owner-user-id", owner_id,
        "--project-url", url_confirm,
        "--db-path", str(db_file),
    ])
    assert ret_confirm == 0

    # Test CLI 2: recover-keyframe with positional workflow_id, required --operation-id, and authorization flags
    ret_recover = main([
        "recover-keyframe",
        wf_recover,
        "--operation-id", req_recover.operation_id,
        "--owner-user-id", owner_id,
        "--authorization-run-id", "auth_run_cli",
        "--authorization-scope", "keyframe",
        "--authorization-expires-at", "2028-12-31T23:59:59Z",
        "--authorization-max-cost-usd", "10.0",
        "--db-path", str(db_file),
    ])
    assert ret_recover == 1
    check_conn = connect(db_file)
    assert KeyframeOperationStore(check_conn).get(req_recover.operation_id)["status"] == "project_created"
    check_conn.close()


@patch.object(GoogleFlowKeyframeProvider, "_attach_image_references", lambda self, page, paths: None)
@patch.object(GoogleFlowKeyframeProvider, "_ensure_image_generation_settings", lambda self, page: None)
def test_fake_store_without_method_fails_pre_send_with_zero_clicks(tmp_path: Path):
    """Requirement 6.1: Store without durable persistence methods fails pre-Send with Send=0."""
    ref_file = _create_test_image(tmp_path / "ref.png")
    out_dir = tmp_path / "out"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    fake_store = object()

    provider = GoogleFlowKeyframeProvider(
        profile_path=profile_dir,
        output_dir=out_dir,
        store=fake_store,
        ui_contract_verified=True,
        reference_upload_verified=True,
        cost_contract_verified=True,
    )

    req = KeyframeGenerationRequest(
        operation_id="op_fake_store",
        workflow_id="wf_fake_store",
        project_id="proj_fake_store",
        frame_id="scene_1-start",
        prompt="A bottle of serum",
        product_description="Skincare serum",
        reference_image_paths=(str(ref_file),),
    )

    generate_click_mock = MagicMock()
    mock_page = MagicMock()
    mock_page.url = "https://flow.google.com/project/url-fake-store"
    mock_page.query_selector.side_effect = lambda sel: MagicMock() if "profile-button" in sel else None

    def mock_locator(sel):
        loc = MagicMock()
        if "new-project-button" in sel:
            loc.count.return_value = 1
        elif 'input[type="file"]' in sel:
            loc.count.return_value = 1
            loc.evaluate.return_value = 1
        elif "detail-add-to-prompt-btn" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
        elif "ProseMirror" in sel:
            loc.count.return_value = 1
        elif "Bắt đầu tạo" in sel:
            loc.count.return_value = 1
            loc.is_visible.return_value = True
            loc.click = generate_click_mock
        else:
            loc.count.return_value = 0
            loc.is_visible.return_value = False
        return loc

    mock_page.locator.side_effect = mock_locator
    mock_browser = MagicMock()
    mock_browser.pages = [mock_page]

    with patch("video_factory.integrations.google_flow_keyframe_provider.sync_playwright") as mock_pw:
        mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = mock_browser
        res = provider.submit(req)

    assert res.status == "failed"
    assert res.error_code == "DURABLE_CHECKPOINT_REQUIRED"
    assert res.metadata["send_attempted"] is False
    assert generate_click_mock.call_count == 0


def test_two_unknowns_confirm_one_returns_pending_accurately(tmp_path: Path):
    """Requirement 6.2: When 2 unknown operations exist, confirming one returns resumed=False with accurate pending list."""
    db_file = tmp_path / "two_unknowns_confirm.db"
    conn = connect(db_file)
    wf_id = "wf_two_confirm"
    owner_id = "owner_alice"
    proj_id = "proj_two_confirm"

    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path, num_scenes=2
    )
    req1, req2 = requests[0], requests[1]

    outbox_id = _quarantine_storyboard_outbox(
        conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain"
    )

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req1, "google_flow")
    url1 = "https://flow.google.com/project/url-two-1"
    store.record_project_checkpoint(req1, url1, "google_flow")

    store.claim_first_submission(req2, "google_flow")
    url2 = "https://flow.google.com/project/url-two-2"
    store.record_project_checkpoint(req2, url2, "google_flow")

    img1 = _create_test_image(tmp_path / "img1.png", 720, 1280)

    # Confirm only op 1
    confirm_res = confirm_keyframe_submission(
        conn,
        workflow_id=wf_id,
        operation_id=req1.operation_id,
        image_path=img1,
        owner_user_id=owner_id,
        project_url=url1,
        resume_workflow=True,
    )

    assert confirm_res["status"] == "confirmed"
    assert confirm_res["resumed"] is False
    assert confirm_res["pending_operation_ids"] == [req2.operation_id]
    assert confirm_res["recovery_result"]["resumed"] is False
    assert confirm_res["recovery_result"]["pending_operation_ids"] == [req2.operation_id]

    # Verify outbox remains quarantined because req2 is still pending reconciliation
    outbox_state = conn.execute("SELECT state FROM workflow_outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()[0]
    assert outbox_state == "quarantined"

    # Verify req1 is completed while req2 is still project_created
    op1_rec = store.get(req1.operation_id)
    assert op1_rec["status"] == "completed"
    assert op1_rec["output_path"] == str(img1.resolve())

    op2_rec = store.get(req2.operation_id)
    assert op2_rec["status"] == "project_created"

    conn.close()


def test_two_unknown_operations_remain_untouched_when_status_is_unknown(tmp_path: Path):
    """An unknown result for one operation must not release it or alter other uncertain work."""
    db_file = tmp_path / "two_unknowns_reject.db"
    conn = connect(db_file)
    wf_id = "wf_two_reject"
    owner_id = "owner_alice"
    proj_id = "proj_two_reject"

    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path, num_scenes=2
    )
    req1, req2 = requests[0], requests[1]

    outbox_id = _quarantine_storyboard_outbox(
        conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain"
    )

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req1, "google_flow")
    url1 = "https://flow.google.com/project/url-rej-1"
    store.record_project_checkpoint(req1, url1, "google_flow")

    store.claim_first_submission(req2, "google_flow")
    url2 = "https://flow.google.com/project/url-rej-2"
    store.record_project_checkpoint(req2, url2, "google_flow")

    auth = {
        "workflow_id": wf_id,
        "run_id": "auth_target_rej",
        "scope": ["keyframe"],
        "expires_at": "2028-12-31T23:59:59Z",
        "max_cost_usd": 5.0,
    }

    with pytest.raises(ValueError, match="still unknown"):
        recover_unknown_keyframe_submission(
            conn,
            workflow_id=wf_id,
            owner_user_id=owner_id,
            authorization=auth,
            operation_id=req1.operation_id,
        )
    outbox_state = conn.execute("SELECT state FROM workflow_outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()[0]
    assert outbox_state == "quarantined"
    op1_rec = store.get(req1.operation_id)
    assert op1_rec["status"] == "project_created"
    op2_rec = store.get(req2.operation_id)
    assert op2_rec["status"] == "project_created"
    agg, _ = WorkflowRepository(conn).get_workflow(wf_id)
    assert "keyframe_recovery_plan" not in agg

    conn.close()


def test_confirm_invalid_url_or_state_rejected(tmp_path: Path):
    """Requirement 6.4: Confirm with wrong URL, missing URL, wrong state, or invalid operation ID is rejected."""
    db_file = tmp_path / "confirm_validation.db"
    conn = connect(db_file)
    wf_id = "wf_conf_val"
    owner_id = "owner_alice"
    proj_id = "proj_conf_val"

    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path
    )
    req = requests[0]
    _quarantine_storyboard_outbox(conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain")

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req, "google_flow")
    correct_url = "https://flow.google.com/project/url-correct-1234"
    store.record_project_checkpoint(req, correct_url, "google_flow")

    img_file = _create_test_image(tmp_path / "valid.png", 720, 1280)

    # 1. Missing project_url -> rejected
    with pytest.raises(ValueError, match="Operator project_url is required"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id=req.operation_id,
            image_path=img_file,
            owner_user_id=owner_id,
            project_url=None,
        )

    # 2. Mismatched project_url -> rejected
    with pytest.raises(ValueError, match="Project URL mismatch"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id=req.operation_id,
            image_path=img_file,
            owner_user_id=owner_id,
            project_url="https://flow.google.com/project/url-wrong-9999",
        )

    # 3. Operation not belonging to Scene Plan -> rejected
    with pytest.raises(ValueError, match="does not belong to the approved Scene Plan"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id="op_alien_id",
            image_path=img_file,
            owner_user_id=owner_id,
            project_url=correct_url,
        )

    # 4. Wrong state: completed -> rejected
    conn.execute(
        "UPDATE video_factory_keyframe_operation SET status = 'completed' WHERE operation_id = ?",
        (req.operation_id,),
    )
    with pytest.raises(ValueError, match="not in a reconciliation state"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id=req.operation_id,
            image_path=img_file,
            owner_user_id=owner_id,
            project_url=correct_url,
        )

    # 5. Wrong state: failed -> rejected
    conn.execute(
        "UPDATE video_factory_keyframe_operation SET status = 'failed' WHERE operation_id = ?",
        (req.operation_id,),
    )
    with pytest.raises(ValueError, match="not in a reconciliation state"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id=req.operation_id,
            image_path=img_file,
            owner_user_id=owner_id,
            project_url=correct_url,
        )

    conn.close()


def test_after_final_confirm_offline_worker_runs_without_provider_resubmit(tmp_path: Path):
    """Requirement 6.5 & 5: After final confirm, running worker offline via outbox proves provider.submit is NOT called again."""
    from video_factory.workflows.worker import run_worker
    from video_factory.mcp_adapters import MCPVideoFactoryAdapter
    from video_factory.qc.evaluator import MultimodalEvaluator

    os.environ["HERMES_VIDEO_FACTORY_WORKSPACE"] = str(tmp_path)
    db_file = tmp_path / "offline_worker.db"
    conn = connect(db_file)
    wf_id = "wf_offline_worker"
    owner_id = "default-owner"
    proj_id = "proj_offline_worker"

    script, dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path
    )
    req = requests[0]

    scene_plan = script.to_dict()
    scene_plan["research_dossier"] = dossier.to_dict()

    repo = WorkflowRepository(conn)
    agg, ver = repo.get_workflow(wf_id)
    agg["qc_authorization"] = {
        "authorization_id": f"auth_{wf_id}",
        "workflow_id": wf_id,
        "scope": ["keyframe", "product_video"],
        "expires_at": "2099-01-01T00:00:00+00:00",
        "max_cost_usd": 10.0,
        "workflow_max_cost_usd": 500.0,
        "campaign_max_cost_usd": 1000.0,
    }
    agg["source_context"] = {"brand": "BrandX", "model": "SerumX", "display_name": "BrandX SerumX"}
    agg["product_metadata"] = {"brand": "BrandX", "model": "SerumX", "display_name": "BrandX SerumX"}
    agg["qc_max_cost_usd"] = 10.0
    repo.commit_workflow(
        workflow_id=wf_id,
        aggregate_json=json.dumps(agg),
        events=[],
        expected_version=ver,
    )

    outbox_id = _quarantine_storyboard_outbox(
        conn,
        wf_id,
        "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain",
        scene_plan=scene_plan,
        project_id=proj_id,
        owner_user_id=owner_id,
    )

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req, "google_flow")
    project_url = "https://flow.google.com/project/url-worker-test"
    store.record_project_checkpoint(req, project_url, "google_flow")

    img_file = _create_test_image(tmp_path / "confirmed_worker.png", 720, 1280)

    # Operator confirms keyframe
    res = confirm_keyframe_submission(
        conn,
        workflow_id=wf_id,
        operation_id=req.operation_id,
        image_path=img_file,
        owner_user_id=owner_id,
        project_url=project_url,
        resume_workflow=True,
    )
    assert res["resumed"] is True
    assert res["pending_operation_ids"] == []

    # Outbox is back to pending
    outbox_state = conn.execute("SELECT state FROM workflow_outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()[0]
    assert outbox_state == "pending"

    conn.close()

    mock_provider = MagicMock()
    mock_provider.provider_kind = "google_flow"
    mock_provider.submit = MagicMock()

    fake_vf_response = {
        "ok": True,
        "project_id": proj_id,
        "storyboard_id": f"sb_{proj_id}",
        "frames": [{"scene_id": "scene_1"}],
        "project": {
            "id": proj_id,
            "owner_user_id": owner_id,
            "scene_plan_approval": "approved",
            "scene_plan": {"scenes": [{"scene_id": "scene_1"}]},
            "storyboard": {
                "storyboard_id": f"sb_{proj_id}",
                "frames": [{"scene_id": "scene_1"}],
            },
        },
    }
    fake_qc_eval = {
        "identity_consistency": 45,
        "scene_adherence": 27,
        "visual_cleanliness": 18,
        "total_score": 90,
        "verdict": "pass",
        "hard_failure": False,
        "reason": "Passes all keyframe quality rubrics",
        "prompt_reference_consistent": True,
        "script_adherence": True,
        "important_details_preserved": True,
        "composition_usable": True,
        "animation_suitable": True,
        "recommended_action": "none",
        "source_branding_detected": False,
        "identity_mismatch": False,
    }

    with patch(
        "video_factory.integrations.keyframe_provider_factory.resolve_keyframe_provider",
        return_value=(mock_provider, "google_flow"),
    ), patch(
        "video_factory.integrations.nine_router.configured_qc_evaluator",
        return_value=MultimodalEvaluator(),
    ), patch.object(
        MCPVideoFactoryAdapter,
        "create_storyboard",
        return_value=fake_vf_response,
    ), patch.object(
        MultimodalEvaluator,
        "evaluate_multiple_images",
        return_value=fake_qc_eval,
    ):
        report = run_worker(db_file, once=True, provider_mode="configured")

    # The worker processed the pending outbox job
    assert report is not None
    assert report.claimed == 1
    assert report.succeeded == 1
    assert report.failed == 0
    assert report.quarantined == 0
    # CRITICAL PROOF: provider.submit was NOT called again (reused confirmed image)!
    assert mock_provider.submit.call_count == 0, "provider.submit must NEVER be called again for confirmed keyframe"

    # Close and reopen SQLite file to verify durable persisted state across connection cycles
    conn2 = connect(db_file)
    agg_after, version_after = WorkflowRepository(conn2).get_workflow(wf_id)
    assert agg_after["state"] == ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL.value
    # Outbox row was claimed and ACKed (deleted on success)
    acked_outbox = conn2.execute("SELECT * FROM workflow_outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
    assert acked_outbox is None, "Successfully ACKed outbox message must be deleted from workflow_outbox"
    conn2.close()


def test_intentional_rollback_on_recover_preserves_all_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Requirement 6 & 1: Forced error during recover commit rolls back operation, capacity, version, and outbox."""
    from video_factory.integrations.capacity import ProviderCapacityStore

    db_file = tmp_path / "rollback_recover.db"
    conn = connect(db_file)
    wf_id = "wf_rollback_rec"
    owner_id = "owner_alice"
    proj_id = "proj_rollback_rec"

    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path
    )
    req = requests[0]

    outbox_id = _quarantine_storyboard_outbox(
        conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain"
    )

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req, "google_flow")
    url = "https://flow.google.com/project/url-rollback-test"
    store.record_project_checkpoint(req, url, "google_flow")

    # Acquire capacity slot for this operation
    cap_store = ProviderCapacityStore(conn)
    acquired = cap_store.try_acquire(
        provider_kind="google_flow",
        operation_id=req.operation_id,
        lease_owner="test-lease-owner",
        limit=5,
    )
    assert acquired is True
    assert len(cap_store.active("google_flow")) == 1

    class TerminalFailureProvider:
        def get_status(self, provider_operation_id: str) -> KeyframeGenerationResult:
            return KeyframeGenerationResult(
                operation_id=req.operation_id,
                status="failed",
                provider_operation_id=provider_operation_id,
                error_code="FLOW_OPERATION_FAILED",
            )

    monkeypatch.setattr(
        "video_factory.integrations.keyframe_provider_factory.resolve_keyframe_provider",
        lambda *_args, **_kwargs: (TerminalFailureProvider(), "google_flow"),
    )

    auth = {
        "workflow_id": wf_id,
        "run_id": "auth_rollback_test",
        "scope": ["keyframe"],
        "expires_at": "2028-12-31T23:59:59Z",
        "max_cost_usd": 5.0,
    }

    # Verify initial state
    init_op = store.get(req.operation_id)
    assert init_op["status"] == "project_created"
    _agg_init, init_version = WorkflowRepository(conn).get_workflow(wf_id)
    assert init_version == 0 or init_version == 1

    # Inject forced failure during workflow commit inside the transaction
    with patch.object(
        WorkflowRepository,
        "commit_workflow_in_transaction",
        side_effect=RuntimeError("FORCED_WORKFLOW_COMMIT_FAILURE"),
    ):
        with pytest.raises(RuntimeError, match="FORCED_WORKFLOW_COMMIT_FAILURE"):
            recover_unknown_keyframe_submission(
                conn,
                workflow_id=wf_id,
                owner_user_id=owner_id,
                operation_id=req.operation_id,
                authorization=auth,
            )

    # Immediately close connection to prove real SQLite engine rollback
    conn.close()

    # Reopen database file and verify ABSOLUTE ZERO partial mutation
    conn2 = connect(db_file)
    store2 = KeyframeOperationStore(conn2)
    cap_store2 = ProviderCapacityStore(conn2)
    repo2 = WorkflowRepository(conn2)

    # 1. Operation record was rolled back: status is still project_created, NOT failed!
    op_after = store2.get(req.operation_id)
    assert op_after["status"] == "project_created"
    assert op_after["error_code"] != "REPLACED_AFTER_UNKNOWN_OUTCOME"

    # 2. Capacity slot was rolled back: NOT released!
    assert len(cap_store2.active("google_flow")) == 1

    # 3. Workflow version was rolled back: version unchanged!
    _agg2, v2 = repo2.get_workflow(wf_id)
    assert v2 == init_version

    # 4. Outbox state was rolled back: still quarantined!
    outbox_row = conn2.execute("SELECT state FROM workflow_outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
    assert outbox_row["state"] == "quarantined"

    # 5. No recovery events were committed
    event_count = conn2.execute(
        "SELECT COUNT(*) FROM workflow_event WHERE workflow_id = ? AND event_type = 'UnknownKeyframeReplacementAuthorized'",
        (wf_id,),
    ).fetchone()[0]
    assert event_count == 0

    conn2.close()


def test_intentional_rollback_on_confirm_preserves_all_state(tmp_path: Path):
    """Requirement 6 & 1: Forced error during confirm requeue rolls back operation and capacity."""
    from video_factory.integrations.capacity import ProviderCapacityStore
    from video_factory.workflows.outbox_repository import OutboxRepository

    db_file = tmp_path / "rollback_confirm.db"
    conn = connect(db_file)
    wf_id = "wf_rollback_conf"
    owner_id = "owner_alice"
    proj_id = "proj_rollback_conf"

    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path
    )
    req = requests[0]

    outbox_id = _quarantine_storyboard_outbox(
        conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain"
    )

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req, "google_flow")
    url = "https://flow.google.com/project/url-confirm-rollback"
    store.record_project_checkpoint(req, url, "google_flow")

    cap_store = ProviderCapacityStore(conn)
    cap_store.try_acquire(
        provider_kind="google_flow",
        operation_id=req.operation_id,
        lease_owner="test-lease-owner",
        limit=5,
    )
    assert len(cap_store.active("google_flow")) == 1

    img_file = _create_test_image(tmp_path / "conf_roll.png", 720, 1280)

    # Inject forced failure during outbox requeue inside the transaction
    with patch.object(
        OutboxRepository,
        "requeue_quarantined_in_transaction",
        side_effect=RuntimeError("FORCED_OUTBOX_REQUEUE_FAILURE"),
    ):
        with pytest.raises(RuntimeError, match="FORCED_OUTBOX_REQUEUE_FAILURE"):
            confirm_keyframe_submission(
                conn,
                workflow_id=wf_id,
                operation_id=req.operation_id,
                image_path=img_file,
                owner_user_id=owner_id,
                project_url=url,
                resume_workflow=True,
            )

    conn.close()

    # Reopen SQLite file and verify complete rollback
    conn2 = connect(db_file)
    store2 = KeyframeOperationStore(conn2)
    cap_store2 = ProviderCapacityStore(conn2)

    # 1. Operation status was rolled back: remains project_created, NOT completed!
    op_after = store2.get(req.operation_id)
    assert op_after["status"] == "project_created"
    assert op_after["output_path"] is None

    # 2. Capacity slot was rolled back: remains active!
    assert len(cap_store2.active("google_flow")) == 1

    # 3. Outbox remains quarantined: NOT pending!
    outbox_row = conn2.execute("SELECT state FROM workflow_outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
    assert outbox_row["state"] == "quarantined"

    conn2.close()


def test_confirm_preconditions_rejected_before_any_mutation(tmp_path: Path):
    """Requirement 3: confirm_keyframe_submission rejects before any mutation if preconditions are not met."""
    from video_factory.integrations.capacity import ProviderCapacityStore

    db_file = tmp_path / "preconditions.db"
    conn = connect(db_file)
    wf_id = "wf_precond"
    owner_id = "owner_alice"
    proj_id = "proj_precond"

    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path
    )
    req = requests[0]
    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req, "google_flow")
    url = "https://flow.google.com/project/url-precond"
    store.record_project_checkpoint(req, url, "google_flow")

    cap_store = ProviderCapacityStore(conn)
    cap_store.try_acquire(
        provider_kind="google_flow",
        operation_id=req.operation_id,
        lease_owner="test",
        limit=5,
    )

    img_file = _create_test_image(tmp_path / "precond.png", 720, 1280)

    # 1. Reject if no quarantined outbox exists
    with pytest.raises(ValueError, match="No quarantined storyboard job found"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id=req.operation_id,
            image_path=img_file,
            owner_user_id=owner_id,
            project_url=url,
        )

    # 2. Reject if quarantined job is NOT product_video.storyboard (e.g. product_video.render)
    env_wrong_job = build_envelope(
        workflow_id=wf_id,
        topic="job.dispatch",
        idempotency_key=f"idemp_{wf_id}_wrong",
        payload={"job_name": "product_video.render", "workflow_id": wf_id},
    )
    conn.execute(
        "INSERT INTO workflow_outbox (workflow_id, message, state, last_error, created_at, updated_at) "
        "VALUES (?, ?, 'quarantined', 'Job execution failed [RECONCILIATION_REQUIRED]', datetime('now'), datetime('now'))",
        (wf_id, env_wrong_job),
    )
    with pytest.raises(ValueError, match="expected 'product_video.storyboard'"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id=req.operation_id,
            image_path=img_file,
            owner_user_id=owner_id,
            project_url=url,
        )
    conn.execute("DELETE FROM workflow_outbox WHERE workflow_id = ?", (wf_id,))

    # 3. Reject if quarantined last_error is NOT an uncertain outcome (e.g. deterministic schema error)
    _quarantine_storyboard_outbox(conn, wf_id, "Job execution failed [INVALID_SCRIPT_DIGEST]: Invalid prompt")
    with pytest.raises(ValueError, match="indicates a deterministic error"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id=req.operation_id,
            image_path=img_file,
            owner_user_id=owner_id,
            project_url=url,
        )
    conn.execute("DELETE FROM workflow_outbox WHERE workflow_id = ?", (wf_id,))

    # 4. Reject if workflow is NOT in an active storyboard state (e.g. state = 'rendered')
    _quarantine_storyboard_outbox(conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Timeout")
    conn.execute("UPDATE workflow SET aggregate_json = json_set(aggregate_json, '$.state', 'rendered') WHERE workflow_id = ?", (wf_id,))
    with pytest.raises(ValueError, match="not in an active storyboard state"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id=req.operation_id,
            image_path=img_file,
            owner_user_id=owner_id,
            project_url=url,
        )

    # 5. --no-resume (resume_workflow=False) must NOT bypass quarantine checks
    # If outbox is deleted, even with resume_workflow=False, it must still reject!
    conn.execute("DELETE FROM workflow_outbox WHERE workflow_id = ?", (wf_id,))
    conn.execute("UPDATE workflow SET aggregate_json = json_set(aggregate_json, '$.state', 'creating_storyboard') WHERE workflow_id = ?", (wf_id,))
    with pytest.raises(ValueError, match="No quarantined storyboard job found"):
        confirm_keyframe_submission(
            conn,
            workflow_id=wf_id,
            operation_id=req.operation_id,
            image_path=img_file,
            owner_user_id=owner_id,
            project_url=url,
            resume_workflow=False,
        )

    # Verify that operation and capacity were NEVER mutated by any of the failed attempts!
    assert store.get(req.operation_id)["status"] == "project_created"
    assert len(cap_store.active("google_flow")) == 1

    conn.close()


def test_recovery_without_operation_id_rejected_at_interface(tmp_path: Path):
    """Khe hở 1: recover_unknown_keyframe_submission strictly requires operation_id at interface."""
    db_file = tmp_path / "interface_check.db"
    conn = connect(db_file)
    wf_id = "wf_iface"
    owner_id = "owner_alice"

    # 1. Missing operation_id keyword argument entirely -> TypeError
    with pytest.raises(TypeError):
        recover_unknown_keyframe_submission(  # type: ignore[call-arg]
            conn,
            workflow_id=wf_id,
            owner_user_id=owner_id,
        )

    # 2. Empty string -> ValueError
    with pytest.raises(ValueError, match="operation_id is required"):
        recover_unknown_keyframe_submission(
            conn,
            workflow_id=wf_id,
            owner_user_id=owner_id,
            operation_id="",
        )

    with pytest.raises(ValueError, match="operation_id is required"):
        recover_unknown_keyframe_submission(
            conn,
            workflow_id=wf_id,
            owner_user_id=owner_id,
            operation_id="   ",
        )

    conn.close()


@pytest.mark.parametrize(
    "failure_code",
    (
        "GOOGLE_FLOW_NEW_PROJECT_SELECTOR_NOT_UNIQUE",
        "GOOGLE_FLOW_AUTH_REQUIRED",
        "GOOGLE_FLOW_IMAGE_MODE_SELECTOR_NOT_UNIQUE",
        "GOOGLE_FLOW_REFERENCE_ASSET_NOT_UNIQUE",
    ),
)
def test_verified_presend_flow_failure_can_be_recovered_without_provider_status_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_code: str,
):
    conn = connect(tmp_path / "presend-flow-recovery.db")
    wf_id = "wf_presend_flow"
    owner_id = "owner_alice"
    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, tmp_path=tmp_path,
    )
    request = requests[0]
    outbox_id = _quarantine_storyboard_outbox(
        conn,
        wf_id,
        f"Job execution failed [GOOGLE_FLOW_KEYFRAME_FAILED]: {failure_code}",
    )
    store = KeyframeOperationStore(conn)
    assert store.claim_first_submission(request, "google_flow") is True
    store.save_result(
        request,
        KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="failed",
            error_code="GOOGLE_FLOW_KEYFRAME_FAILED",
            error_message=failure_code,
            metadata={"provider_kind": "google_flow", "send_attempted": False},
        ),
    )

    class NoStatusProvider:
        def get_status(self, _operation_id: str) -> KeyframeGenerationResult:
            raise AssertionError("a confirmed pre-Send failure must not be reconciled with the provider")

    monkeypatch.setattr(
        "video_factory.integrations.keyframe_provider_factory.resolve_keyframe_provider",
        lambda *_args, **_kwargs: (NoStatusProvider(), "google_flow"),
    )

    result = recover_unknown_keyframe_submission(
        conn,
        workflow_id=wf_id,
        owner_user_id=owner_id,
        operation_id=request.operation_id,
    )

    assert result["resumed"] is True
    assert result["recovery_attempt"] == 1
    aggregate, _ = WorkflowRepository(conn).get_workflow(wf_id)
    assert aggregate["state"] == ProductVideoWorkflowState.CREATING_STORYBOARD.value
    assert aggregate["keyframe_recovery_plan"]["frames"] == [{
        "frame_id": request.frame_id,
        "actions": ["regenerate_keyframe"],
        "feedback": f"Operator-authorized replacement after confirmed pre-Send failure for {request.operation_id}",
    }]
    assert conn.execute("SELECT state FROM workflow_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()[0] == "quarantined"
    assert conn.execute(
        "SELECT event_type FROM workflow_event WHERE workflow_id=? ORDER BY rowid DESC LIMIT 1", (wf_id,),
    ).fetchone()[0] == "PreSendKeyframeRetryAuthorized"
    conn.close()


def test_verified_square_flow_output_can_be_replaced_with_aspect_targeted_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    conn = connect(tmp_path / "square-flow-output-recovery.db")
    wf_id = "wf_square_flow_output"
    owner_id = "owner_alice"
    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, tmp_path=tmp_path,
    )
    request = requests[0]
    square_output = _create_test_image(tmp_path / "flow-square-output.png", 1024, 1024)
    outbox_id = _quarantine_storyboard_outbox(
        conn,
        wf_id,
        "Job execution failed [GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED]: expected native 9:16 within 1%, received 1024x1024",
    )
    store = KeyframeOperationStore(conn)
    assert store.claim_first_submission(request, "google_flow") is True
    store.save_result(
        request,
        KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="failed",
            provider_operation_id="https://flow.google.com/project/test-square-output",
            image_path=str(square_output),
            error_code="GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED",
            error_message="GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED: expected native 9:16 within 1%, received 1024x1024",
            metadata={
                "provider_kind": "google_flow",
                "send_attempted": True,
                "output_received": True,
                "output_dimensions": [1024, 1024],
            },
        ),
    )

    class NoStatusProvider:
        def get_status(self, _operation_id: str) -> KeyframeGenerationResult:
            raise AssertionError("known invalid output must not be treated as an unknown submission")

    monkeypatch.setattr(
        "video_factory.integrations.keyframe_provider_factory.resolve_keyframe_provider",
        lambda *_args, **_kwargs: (NoStatusProvider(), "google_flow"),
    )

    result = recover_unknown_keyframe_submission(
        conn,
        workflow_id=wf_id,
        owner_user_id=owner_id,
        operation_id=request.operation_id,
    )

    assert result["resumed"] is True
    assert result["recovery_attempt"] == 1
    aggregate, _ = WorkflowRepository(conn).get_workflow(wf_id)
    feedback = aggregate["keyframe_recovery_plan"]["frames"][0]["feedback"]
    assert "verified as 1024x1024" in feedback
    assert "native full-frame 9:16" in feedback
    operation = store.get(request.operation_id)
    assert operation["status"] == "failed"
    assert operation["error_code"] == "GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED"
    assert operation["output_path"] == str(square_output)
    assert conn.execute(
        "SELECT state FROM workflow_outbox WHERE outbox_id=?", (outbox_id,),
    ).fetchone()[0] == "quarantined"
    assert conn.execute(
        "SELECT 1 FROM workflow_event WHERE workflow_id=? AND event_type=?",
        (wf_id, "KnownInvalidKeyframeOutputReplacementAuthorized"),
    ).fetchone() is not None
    conn.close()


def test_square_flow_output_recovery_rejects_changed_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = connect(tmp_path / "square-flow-output-changed.db")
    wf_id = "wf_square_flow_changed"
    owner_id = "owner_alice"
    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, tmp_path=tmp_path,
    )
    request = requests[0]
    square_output = _create_test_image(tmp_path / "flow-square-changed.png", 1024, 1024)
    outbox_id = _quarantine_storyboard_outbox(
        conn,
        wf_id,
        "Job execution failed [GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED]: expected native 9:16 within 1%, received 1024x1024",
    )
    store = KeyframeOperationStore(conn)
    assert store.claim_first_submission(request, "google_flow") is True
    store.save_result(
        request,
        KeyframeGenerationResult(
            operation_id=request.operation_id,
            status="failed",
            provider_operation_id="https://flow.google.com/project/test-square-changed",
            image_path=str(square_output),
            error_code="GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED",
            error_message="GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED: expected native 9:16 within 1%, received 1024x1024",
            metadata={"provider_kind": "google_flow", "send_attempted": True, "output_received": True, "output_dimensions": [1024, 1024]},
        ),
    )
    square_output.write_bytes(b"changed after provider output")

    class NoStatusProvider:
        def get_status(self, _operation_id: str) -> KeyframeGenerationResult:
            raise AssertionError("changed output must fail verification before provider lookup")

    monkeypatch.setattr(
        "video_factory.integrations.keyframe_provider_factory.resolve_keyframe_provider",
        lambda *_args, **_kwargs: (NoStatusProvider(), "google_flow"),
    )

    with pytest.raises(ValueError, match="output bytes do not match the recorded SHA-256"):
        recover_unknown_keyframe_submission(
            conn,
            workflow_id=wf_id,
            owner_user_id=owner_id,
            operation_id=request.operation_id,
        )
    assert conn.execute(
        "SELECT state FROM workflow_outbox WHERE outbox_id=?", (outbox_id,),
    ).fetchone()[0] == "quarantined"
    assert store.get(request.operation_id)["error_code"] == "GOOGLE_FLOW_OUTPUT_ASPECT_RATIO_UNSUPPORTED"
    conn.close()


def test_presend_failure_from_existing_recovery_attempt_can_be_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    conn = connect(tmp_path / "recovery-attempt-presend.db")
    wf_id = "wf_recovery_attempt_presend"
    owner_id = "owner_alice"
    script, dossier, initial_requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, tmp_path=tmp_path,
    )
    first = initial_requests[0]
    feedback = f"Operator-authorized replacement after confirmed pre-Send failure for {first.operation_id}"
    recovery_plan = {"frames": [{
        "frame_id": first.frame_id,
        "actions": ["regenerate_keyframe"],
        "feedback": feedback,
    }]}
    aggregate, version = WorkflowRepository(conn).get_workflow(wf_id)
    aggregate["keyframe_recovery_plan"] = recovery_plan
    aggregate["keyframe_recovery_attempt"] = 1
    with conn:
        conn.execute(
            "UPDATE workflow SET aggregate_json = ?, version = ? WHERE workflow_id = ?",
            (json.dumps(aggregate), version, wf_id),
        )
    recovery_request = next(
        request for request in compile_keyframe_requests(
            workflow_id=wf_id,
            project_id=aggregate["project_id"],
            dossier=dossier,
            script=script,
            recovery_plan=recovery_plan,
            recovery_attempt=1,
        )
        if request.frame_id == first.frame_id
    )
    outbox_id = _quarantine_storyboard_outbox(
        conn,
        wf_id,
        "Job execution failed [GOOGLE_FLOW_KEYFRAME_FAILED]: GOOGLE_FLOW_AUTH_REQUIRED",
    )
    store = KeyframeOperationStore(conn)
    assert store.claim_first_submission(recovery_request, "google_flow") is True
    store.save_result(
        recovery_request,
        KeyframeGenerationResult(
            operation_id=recovery_request.operation_id,
            status="failed",
            error_code="GOOGLE_FLOW_KEYFRAME_FAILED",
            error_message="GOOGLE_FLOW_AUTH_REQUIRED",
            metadata={"provider_kind": "google_flow", "send_attempted": False},
        ),
    )

    class NoStatusProvider:
        def get_status(self, _operation_id: str) -> KeyframeGenerationResult:
            raise AssertionError("a confirmed pre-Send failure must not call provider status")

    monkeypatch.setattr(
        "video_factory.integrations.keyframe_provider_factory.resolve_keyframe_provider",
        lambda *_args, **_kwargs: (NoStatusProvider(), "google_flow"),
    )

    result = recover_unknown_keyframe_submission(
        conn,
        workflow_id=wf_id,
        owner_user_id=owner_id,
        operation_id=recovery_request.operation_id,
    )

    assert result["resumed"] is True
    assert result["recovery_attempt"] == 2
    assert conn.execute(
        "SELECT state FROM workflow_outbox WHERE outbox_id = ?", (outbox_id,),
    ).fetchone()[0] == "quarantined"
    assert conn.execute(
        "SELECT 1 FROM workflow_outbox WHERE workflow_id = ? AND state = 'pending'",
        (wf_id,),
    ).fetchone() is not None
    conn.close()


def test_presend_recovery_reuses_completed_start_frame_from_operation_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    conn = connect(tmp_path / "recovery-preserves-completed-frame.db")
    wf_id = "wf_recovery_preserves_completed_frame"
    owner_id = "owner_alice"
    script, dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, tmp_path=tmp_path, num_scenes=2,
    )
    start_req, failed_req = requests[0], requests[1]
    start_path = _create_test_image(tmp_path / "completed-start.png")
    store = KeyframeOperationStore(conn)
    assert store.claim_first_submission(start_req, "google_flow") is True
    store.save_result(
        start_req,
        KeyframeGenerationResult(
            operation_id=start_req.operation_id,
            status="completed",
            image_path=str(start_path),
            metadata={"provider_kind": "google_flow", "send_attempted": True},
        ),
    )
    assert store.claim_first_submission(failed_req, "google_flow") is True
    store.save_result(
        failed_req,
        KeyframeGenerationResult(
            operation_id=failed_req.operation_id,
            status="failed",
            error_code="GOOGLE_FLOW_KEYFRAME_FAILED",
            error_message="GOOGLE_FLOW_AUTH_UNVERIFIED",
            metadata={"provider_kind": "google_flow", "send_attempted": False},
        ),
    )
    _quarantine_storyboard_outbox(
        conn,
        wf_id,
        "Job execution failed [GOOGLE_FLOW_KEYFRAME_FAILED]: GOOGLE_FLOW_AUTH_UNVERIFIED",
    )

    class NoStatusProvider:
        def get_status(self, _operation_id: str) -> KeyframeGenerationResult:
            raise AssertionError("proven pre-Send failures do not need status lookup")

    monkeypatch.setattr(
        "video_factory.integrations.keyframe_provider_factory.resolve_keyframe_provider",
        lambda *_args, **_kwargs: (NoStatusProvider(), "google_flow"),
    )

    result = recover_unknown_keyframe_submission(
        conn,
        workflow_id=wf_id,
        owner_user_id=owner_id,
        operation_id=failed_req.operation_id,
    )

    assert result["resumed"] is True
    pending = conn.execute(
        "SELECT message FROM workflow_outbox WHERE workflow_id = ? AND state = 'pending'",
        (wf_id,),
    ).fetchone()
    payload = json.loads(pending[0])["payload"]["kwargs"]
    assert [item["frame_id"] for item in payload["previous_storyboard"]["generated_keyframes"]] == [
        start_req.frame_id,
    ]
    assert payload["recovery_plan"]["frames"] == [{
        "frame_id": failed_req.frame_id,
        "actions": ["regenerate_keyframe"],
        "feedback": f"Operator-authorized replacement after confirmed pre-Send failure for {failed_req.operation_id}",
    }]
    conn.close()


def test_confirm_final_operation_requeue_false_rolls_back_all_state(tmp_path: Path):
    """Khe hở 2: When requeue_quarantined_in_transaction returns False on final confirm, rollback operation, capacity, and outbox."""
    from video_factory.integrations.capacity import ProviderCapacityStore
    from video_factory.workflows.outbox_repository import OutboxRepository

    db_file = tmp_path / "rollback_requeue_false.db"
    conn = connect(db_file)
    wf_id = "wf_requeue_false"
    owner_id = "owner_alice"
    proj_id = "proj_requeue_false"

    _script, _dossier, requests = _setup_workflow_in_db(
        conn, workflow_id=wf_id, owner_user_id=owner_id, project_id=proj_id, tmp_path=tmp_path
    )
    req = requests[0]

    outbox_id = _quarantine_storyboard_outbox(
        conn, wf_id, "Job execution failed [RECONCILIATION_REQUIRED]: Flow output uncertain"
    )

    store = KeyframeOperationStore(conn)
    store.claim_first_submission(req, "google_flow")
    url = "https://flow.google.com/project/url-requeue-false"
    store.record_project_checkpoint(req, url, "google_flow")

    cap_store = ProviderCapacityStore(conn)
    cap_store.try_acquire(
        provider_kind="google_flow",
        operation_id=req.operation_id,
        lease_owner="test-lease-owner",
        limit=5,
    )
    assert len(cap_store.active("google_flow")) == 1

    img_file = _create_test_image(tmp_path / "conf_requeue_false.png", 720, 1280)

    # Mock requeue_quarantined_in_transaction to return False
    with patch.object(
        OutboxRepository,
        "requeue_quarantined_in_transaction",
        return_value=False,
    ):
        with pytest.raises(RuntimeError, match="Failed to requeue quarantined storyboard job"):
            confirm_keyframe_submission(
                conn,
                workflow_id=wf_id,
                operation_id=req.operation_id,
                image_path=img_file,
                owner_user_id=owner_id,
                project_url=url,
                resume_workflow=True,
            )

    conn.close()

    # Reopen SQLite file and verify absolute rollback across the engine
    conn2 = connect(db_file)
    store2 = KeyframeOperationStore(conn2)
    cap_store2 = ProviderCapacityStore(conn2)

    # 1. Operation status was rolled back: remains project_created, NOT completed!
    op_after = store2.get(req.operation_id)
    assert op_after["status"] == "project_created"
    assert op_after["output_path"] is None

    # 2. Capacity slot was rolled back: remains active!
    assert len(cap_store2.active("google_flow")) == 1

    # 3. Outbox remains quarantined: NOT pending!
    outbox_row = conn2.execute("SELECT state FROM workflow_outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
    assert outbox_row["state"] == "quarantined"

    conn2.close()

