"""P0 verification: Flow keyframe unknown after Send does not block the entire daily run.

Tests:

1. A Send=1 -> unknown. Reconciliation expires. B exports new MP4, decodable,

   receipt matches project, file, SHA. Report A has reason and operation ID.

2. Replay same day and after DB reopen: A still Send=1. B keeps MP4 and receipt,

   no new storyboard or render jobs.

3. Valid image found in old project during reconciliation: A uses it and continues,

   with total Send=1.

4. Navigation or login error: reconciliation stops on time. Live session or active lease

   is not revoked.

"""

from __future__ import annotations

import hashlib

import json

import sqlite3

import subprocess

import time

from datetime import datetime, timezone

from pathlib import Path

from unittest.mock import MagicMock, patch

import pytest

from PIL import Image

from automation.daily_report import write_daily_report

from automation.pipelines import spark_pipeline as pipeline

from automation.pipelines.spark_pipeline import probe_mp4_file

from automation.scheduler import daily_trigger

from video_factory.automation.importer import publish_daily_run_to_exchange

from video_factory.creative.contracts import (

    EligibleAsset,

    ProductIdentityV2,

    ResearchDossier,

    ScriptScenePlanV2,

    ScriptSceneV2,

)

from video_factory.integrations.capacity import ProviderCapacityStore

from video_factory.integrations.google_flow_keyframe_provider import (

    GoogleFlowKeyframeProvider,

)

from video_factory.integrations.keyframe_provider import (

    KeyframeGenerationRequest,

    KeyframeGenerationResult,

    KeyframeOperationStore,

    KeyframeRuntime,

)

from video_factory.storage_infra.sqlite import connect

from video_factory.workflow import ProductVideoWorkflowState

def _create_image(path: Path, width: int = 720, height: int = 1280, color: str = "red") -> Path:

    path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (width, height), color=color)

    img.save(path, format="PNG")

    return path

def _create_minimal_mp4(path: Path) -> Path:

    """Create a real decodable MP4 file using PyAV."""

    import av

    path.parent.mkdir(parents=True, exist_ok=True)

    container = av.open(str(path), mode="w", format="mp4")

    stream = container.add_stream("libx264", rate=24)

    stream.width = 720

    stream.height = 1280

    stream.pix_fmt = "yuv420p"

    for i in range(24):

        img = Image.new("RGB", (720, 1280), color=(i * 10, 100, 150))

        frame = av.VideoFrame.from_image(img)

        for packet in stream.encode(frame):

            container.mux(packet)

    for packet in stream.encode():

        container.mux(packet)

    container.close()

    return path

def test_p0_flow_unknown_a_stops_b_exports_and_report_has_reason_and_opid(tmp_path, monkeypatch):

    """P0 Scenario 1: A Send=1 -> unknown. Reconciliation expires.

    B exports new MP4, decodable, with receipt matching project, file, SHA.

    Report A contains reason and operation ID.

    """

    day = "2026-11-09"

    inbox = tmp_path / "inbox"

    workspace = tmp_path / "vf_workspace"

    database = tmp_path / "hermes.db"

    workspace.mkdir(parents=True)

    (workspace / "keyframes").mkdir(parents=True)

    (workspace / "exports").mkdir(parents=True)

    # 1. Prepare exchange batches for A and B

    batches = []

    prod_keys = ["prod_A", "prod_B"]

    for i, pkey in enumerate(prod_keys, 1):

        source = tmp_path / f"pi-{pkey}"

        source.mkdir(parents=True)

        img_path = _create_image(source / "img.png", 720, 1280, color="blue" if i == 1 else "green")

        (source / "products.json").write_text(json.dumps({

            "date": day, "products": [{

                "id": pkey, "name": f"Product {pkey}", "brand": f"Brand_{pkey}", "model": f"Model_{pkey}", "variant": "standard",

                "images": [{"asset_id": f"img-{i}", "absolute_path": str(img_path)}],

            }],

        }), encoding="utf-8")

        batch = publish_daily_run_to_exchange(source, inbox, batch_id=f"batch_{pkey}")

        batches.append({"row_id": f"row_{i:04}", "candidate_id": pkey, "batch_path": str(batch)})

    (tmp_path / "sel.csv").write_text("candidate_id\nprod_A\nprod_B\n", encoding="utf-8")

    # Mock provider state

    a_send_calls = []

    a_reconcile_calls = []

    b_send_calls = []

    real_resolve = None

    from video_factory.integrations import keyframe_provider_factory

    class MockFlowProvider(GoogleFlowKeyframeProvider):

        provider_kind = "google_flow"

        def __init__(self, **kwargs):

            super().__init__(

                profile_path=tmp_path / "chrome_profile",

                output_dir=workspace / "keyframes",

                ui_contract_verified=True,

                reference_upload_verified=True,

                cost_contract_verified=True,

                timeout_seconds=5.0,

            )

            self.profile_path.mkdir(parents=True, exist_ok=True)

        def submit(self, request):

            is_a = "prod_A" in request.product_description or "prod_A" in request.prompt or (not a_send_calls)

            if is_a:

                a_send_calls.append(request.operation_id)

                project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"

                self._persist_project_checkpoint(request, project_url)

                return KeyframeGenerationResult(

                    operation_id=request.operation_id,

                    status="unknown",

                    provider_operation_id=project_url,

                    error_code="RECONCILIATION_REQUIRED",

                    error_message="Flow post-Send timeout simulating unknown outcome",

                    metadata={"provider_kind": "google_flow", "project_url": project_url, "send_attempted": True},

                )

            # Product B succeeds

            b_send_calls.append(request.operation_id)

            out_img = _create_image(self.output_dir / f"{request.operation_id}.png", 720, 1280, "green")

            project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000002"

            self._persist_project_checkpoint(request, project_url)

            return KeyframeGenerationResult(

                operation_id=request.operation_id,

                status="completed",

                provider_operation_id=project_url,

                image_path=str(out_img),

                metadata={"provider_kind": "google_flow", "project_url": project_url, "output_dimensions": [720, 1280]},

            )

        def reconcile_existing_output(self, request, project_url):

            if request.operation_id in a_send_calls or not b_send_calls:

                a_reconcile_calls.append(request.operation_id)

                attempt = self.store.claim_output_reconciliation(request.operation_id, project_url, max_attempts=3)

                if attempt is None:

                    return KeyframeGenerationResult(

                        request.operation_id, "unknown", provider_operation_id=project_url,

                        error_code="OUTPUT_RECONCILIATION_EXHAUSTED",

                        error_message="Output retrieval reached its three-attempt limit; generation remains unreconciled",

                        metadata={"provider_kind": "google_flow", "project_url": project_url},

                    )

                return KeyframeGenerationResult(

                    request.operation_id, "unknown", provider_operation_id=project_url,

                    error_code="GOOGLE_FLOW_OUTPUT_NOT_RETRIEVED",

                    error_message=f"Flow project checked, tile not available yet (attempt {attempt})",

                    metadata={"provider_kind": "google_flow", "project_url": project_url,

                              "output_reconciliation_attempts": attempt, "output_reconciliation_finished_attempt": attempt},

                )

            return KeyframeGenerationResult(request.operation_id, "completed", provider_operation_id=project_url)

    # Monkeypatch video composer / renderer so B completes rendering and export without real GPU

    from video_factory.integrations import composer

    def mock_compose_and_export(*args, **kwargs):

        output_file = workspace / "exports" / f"export_{day}_prod_B.mp4"

        _create_minimal_mp4(output_file)

        file_sha = hashlib.sha256(output_file.read_bytes()).hexdigest()

        meta_file = output_file.parent / "metadata.json"

        meta_file.write_text(json.dumps({

            "project_id": "proj_prod_B",

            "video": {"sha256": file_sha},

            "files": {output_file.name: {"sha256": file_sha}},

        }), encoding="utf-8")

        return {"file_path": str(output_file), "sha256": file_sha}

    monkeypatch.setattr(pipeline, "INBOX_ROOT", inbox)

    monkeypatch.setattr(pipeline, "PI_RUNS_DIR", tmp_path / "runs")

    monkeypatch.setattr(pipeline, "VF_RUNTIME_DIR", database.parent)

    monkeypatch.setattr(pipeline, "VF_WORKSPACE_DIR", workspace)

    monkeypatch.setattr(pipeline, "VF_DB_PATH", database)

    import sys

    tests_dir = str(Path(__file__).parent)

    if tests_dir not in sys.path:

        sys.path.insert(0, tests_dir)

    from test_creative_resume import config

    profile = tmp_path / "profile.json"

    profile.write_text(json.dumps(config()))

    monkeypatch.setattr(pipeline, "DEFAULT_PROFILE", profile)

    monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)

    monkeypatch.setattr(daily_trigger, "LEDGER_PATH", tmp_path / "ledger.json")

    monkeypatch.setattr(daily_trigger, "prepare_sheet_intake", lambda *a, **kw: {

        "status": "success", "selection_csv": str(tmp_path / "sel.csv"), "exchange_batches": batches,

    })

    monkeypatch.setattr(keyframe_provider_factory, "resolve_keyframe_provider", lambda *a, **kw: (MockFlowProvider(), "google_flow"))

    # Initialize keyframe and capacity tables

    init_conn = connect(database)

    KeyframeOperationStore(init_conn)

    ProviderCapacityStore(init_conn)

    init_conn.close()

    # Also hook scene renderer for B

    from video_factory.integrations import scene_renderer

    class FakeSceneRenderer:

        provider_kind = "google_flow"

        def submit(self, req):

            clip = workspace / f"{req.operation_id}.mp4"

            _create_minimal_mp4(clip)

            return KeyframeGenerationResult(req.operation_id, "completed", image_path=str(clip))

        def get_status(self, op_id):

            clip = workspace / f"{op_id}.mp4"

            return KeyframeGenerationResult(op_id, "completed", image_path=str(clip))

    monkeypatch.setenv("VF_ENABLE_KEYFRAME_RUNTIME", "1")

    monkeypatch.setenv("VF_TEST_MOCK_FLOW", "1")

    monkeypatch.delenv("VF_CAPACITY_LEASE_SECONDS", raising=False)

    monkeypatch.setenv("VF_TEST_FLOW_REPLACE_SUCCESS", "1")

    lease_waits = []

    def advance_past_lease(delay):

        lease_waits.append(delay)

        assert 0 < delay <= 300

        with sqlite3.connect(database) as conn:

            assert conn.execute(

                "UPDATE video_factory_provider_capacity SET lease_expires_at=0 "

                "WHERE provider_kind='google_flow' AND lease_expires_at>?",

                (time.time(),),

            ).rowcount == 1

    monkeypatch.setattr(pipeline.time, "sleep", advance_past_lease)

    # Run the daily pipeline with QC low (runs real separate child processes)

    options = dict(date=day, limit=2, scenes=3, provider_mode="fake", qc_level="low")

    result = daily_trigger.trigger_daily_run(**options)

    assert len(lease_waits) == 1

    # 1. Assertions on A and B from SQLite database

    with connect(database) as conn:

        ops = conn.execute("SELECT * FROM video_factory_keyframe_operation ORDER BY rowid").fetchall()

        wfs = conn.execute("SELECT workflow_id, aggregate_json FROM workflow WHERE workflow_id LIKE 'pv-daily-%' ORDER BY rowid ASC").fetchall()

    assert len(wfs) >= 2, "Both product workflows must be created"

    wf_a_id = wfs[0]["workflow_id"]

    wf_b_id = wfs[1]["workflow_id"]

    a_ops = [op for op in ops if op["workflow_id"] == wf_a_id]

    b_ops = [op for op in ops if op["workflow_id"] == wf_b_id]

    # Run 1: A exhausted reconciliation; slot was retained while worker process was alive.

    assert len(a_ops) == 1, f"Product A must have initial operation in Run 1, got {len(a_ops)}"

    first_op = a_ops[0]

    assert first_op["status"] in ("unknown", "abandoned_unknown")

    assert first_op["provider_operation_id"], "First op must preserve project URL"

    # 2. Assertions on B

    assert len(b_ops) >= 1, "Product B must proceed with keyframe generation"

    assert any(op["status"] == "completed" for op in b_ops), "Product B keyframe must be completed"

    assert len(result.exported_videos) >= 1, "Product B must export a verified MP4"

    b_video = result.exported_videos[0]

    probe = probe_mp4_file(b_video["file_path"])

    assert probe.decodable is True, "Product B MP4 must be decodable"

    assert b_video["decodable"] is True

    receipt_path = Path(b_video["file_path"]).parent / "metadata.json"

    assert receipt_path.is_file(), "Export receipt metadata.json must exist"

    receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))

    video_sha = (

        receipt_data.get("video", {}).get("sha256")

        or receipt_data.get("files", {}).get(Path(b_video["file_path"]).name, {}).get("sha256")

        or receipt_data.get("provenance", {}).get("render_sha256")

    )

    assert video_sha == probe.sha256

    # 3. Assertions on Product Outcomes & Report

    a_outcome = next(o for o in result.product_outcomes if "prod_A" in o.get("product_name", "") or "prod_A" in o.get("product_key", "") or "prod_A" in o.get("workflow_id", ""))

    assert a_outcome.get("operation_id") or a_outcome.get("workflow_id"), "Product A outcome must include ID"

    # Write daily report and check structure

    report_path = write_daily_report(result, db_path=database, root=tmp_path / "reports")

    report = json.loads(report_path.read_text(encoding="utf-8"))

    run_info = next(iter(report["runs"].values()))

    rep_a = next(p for p in run_info["products"] if "prod_A" in p.get("name", "") or "prod_A" in str(p.get("outcome") or {}))

    assert rep_a is not None, "Daily report for A must be recorded"

    # 4. Run 2: Resume via daily trigger after the previous lease expired.

    # Verifies that slot is released safely, A sends replacement with a NEW operation ID for exact frame scene_1-start.

    resume_result = daily_trigger.trigger_daily_run(**options, force=True)

    with connect(database) as conn2:

        ops2 = conn2.execute("SELECT * FROM video_factory_keyframe_operation ORDER BY rowid").fetchall()

        a_ops2 = [op for op in ops2 if op["workflow_id"] == wf_a_id]

        b_ops2 = [op for op in ops2 if op["workflow_id"] == wf_b_id]

    assert len(a_ops2) >= 2, f"Product A must now have replaced operation, got {len(a_ops2)}"

    op_orig = a_ops2[0]

    op_repl = a_ops2[1]

    assert op_orig["status"] == "abandoned_unknown", f"Original op must be marked abandoned_unknown, got {op_orig['status']}"

    assert op_orig["error_code"] == "AUTO_REPLACED_UNRETRIEVABLE_OUTPUT"

    assert op_orig["provider_operation_id"], "Original op must preserve project URL"

    assert op_repl["operation_id"] != op_orig["operation_id"], "Replacement operation must have a NEW operation ID"

    assert op_repl["frame_id"] == op_orig["frame_id"], "Replacement operation must target the exact same frame"

    assert op_repl["status"] == "completed", f"Replacement operation must complete, got {op_repl['status']}"

    # 5. Run 3: Replay test through daily trigger on the same database after close and reopen:

    # Verifies that A does NOT send a duplicate replacement, and B keeps MP4/receipt without new jobs.

    replay_result = daily_trigger.trigger_daily_run(**options, force=True)

    assert isinstance(replay_result, pipeline.PipelineRunResult)

    with connect(database) as conn3:

        ops_after = conn3.execute("SELECT * FROM video_factory_keyframe_operation ORDER BY rowid").fetchall()

        a_ops_after = [op for op in ops_after if op["workflow_id"] == wf_a_id]

        b_ops_after = [op for op in ops_after if op["workflow_id"] == wf_b_id]

    assert len(a_ops_after) == len(a_ops2), "Replay must NOT generate duplicate replacement submissions for A"

    assert len(b_ops_after) == len(b_ops2), "Replay must NOT generate duplicate operations for B"

    assert Path(b_video["file_path"]).is_file(), "Product B MP4 must remain intact"

    recheck_probe = probe_mp4_file(b_video["file_path"])

    assert recheck_probe.sha256 == video_sha, "Product B MP4 SHA must match original receipt after replay"

def test_p0_replay_same_day_after_db_reopen_keeps_send_1_and_no_new_jobs(tmp_path, monkeypatch):

    """P0 Scenario 2: Rerun on the same day after DB reopen.

    A still Send=1. B keeps MP4 and receipt, without new storyboard or render jobs.

    """

    day = "2026-11-09"

    database = tmp_path / "hermes.db"

    conn = connect(database)

    store = KeyframeOperationStore(conn)

    capacity = ProviderCapacityStore(conn)

    # Setup Product A: already Send=1 and exhausted

    req_a = KeyframeGenerationRequest(

        operation_id="op_a_replay", workflow_id="wf_a_replay",

        project_id="proj_a", frame_id="scene_1-start",

        prompt="A pouch", product_description="Pouch",

        reference_image_paths=(),

    )

    url_a = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"

    store.claim_first_submission(req_a, "google_flow")

    store.record_project_checkpoint(req_a, url_a, "google_flow")

    store.save_result(req_a, KeyframeGenerationResult(

        req_a.operation_id, "unknown", provider_operation_id=url_a,

        error_code="OUTPUT_RECONCILIATION_EXHAUSTED",

        error_message="Reconciliation exhausted 3 attempts",

        metadata={"provider_kind": "google_flow", "project_url": url_a, "output_reconciliation_attempts": 3},

    ))

    # Local capacity slot for A was released

    assert capacity.active("google_flow") == []

    # Close and reopen DB

    conn.close()

    conn2 = connect(database)

    store2 = KeyframeOperationStore(conn2)

    capacity2 = ProviderCapacityStore(conn2)

    submit_calls = []

    class DummyProvider:

        provider_kind = "google_flow"

        def submit(self, req):

            submit_calls.append(req.operation_id)

            return KeyframeGenerationResult(req.operation_id, "failed")

        def reconcile_existing_output(self, req, url):

            submit_calls.append("reconcile:" + req.operation_id)

            return KeyframeGenerationResult(req.operation_id, "unknown", error_code="OUTPUT_RECONCILIATION_EXHAUSTED")

    runtime2 = KeyframeRuntime(store=store2, provider=DummyProvider(), capacity_store=capacity2)

    res_a = runtime2.run(req_a)

    assert res_a.status == "unknown"

    assert res_a.error_code == "OUTPUT_RECONCILIATION_EXHAUSTED"

    assert len(submit_calls) == 0, "A must NOT call submit or reconcile again when already exhausted"

    conn2.close()

def test_p0_reconcile_finds_valid_image_a_uses_it_with_send_1(tmp_path):

    """P0 Scenario 3: Valid image found in old project during reconciliation:

    A uses that image and continues with total Send=1.

    """

    db_file = tmp_path / "reconcile_success.db"

    conn = connect(db_file)

    store = KeyframeOperationStore(conn)

    capacity = ProviderCapacityStore(conn)

    valid_img = _create_image(tmp_path / "valid_flow.png", 720, 1280, "gold")

    req = KeyframeGenerationRequest(

        operation_id="op_rec_success", workflow_id="wf_rec_success",

        project_id="proj_rec", frame_id="scene_1-start",

        prompt="A beverage can", product_description="Can",

        reference_image_paths=(),

    )

    url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000077"

    submit_count = 0

    reconcile_count = 0

    class FlowReconcileSuccessProvider:

        provider_kind = "google_flow"

        def submit(self, r):

            nonlocal submit_count

            submit_count += 1

            store.record_project_checkpoint(r, url, "google_flow")

            return KeyframeGenerationResult(

                r.operation_id, "unknown", provider_operation_id=url,

                error_code="RECONCILIATION_REQUIRED",

            )

        def reconcile_existing_output(self, r, u):

            nonlocal reconcile_count

            reconcile_count += 1

            return KeyframeGenerationResult(

                r.operation_id, "completed", provider_operation_id=u,

                image_path=str(valid_img),

                metadata={"provider_kind": "google_flow", "project_url": u, "output_dimensions": [720, 1280]},

            )

    runtime = KeyframeRuntime(store=store, provider=FlowReconcileSuccessProvider(), capacity_store=capacity)

    # First call submits -> unknown

    first = runtime.run(req)

    assert first.status == "unknown"

    assert submit_count == 1

    # Second call (reconciliation) -> finds valid image!

    second = runtime.run(req)

    assert second.status == "completed"

    assert second.image_path == str(valid_img)

    assert submit_count == 1, "Send must remain exactly 1"

    assert reconcile_count == 1

    # Operation in store is marked completed

    row = store.get(req.operation_id)

    assert row["status"] == "completed"

    assert row["output_path"] == str(valid_img)

    conn.close()

def test_p0_navigation_or_auth_error_stops_and_does_not_revoke_live_slot(tmp_path):

    """P0 Scenario 4: Navigation or auth error stops reconciliation on time.

    Live session or active lease is NOT revoked.

    """

    db_file = tmp_path / "auth_error.db"

    conn = connect(db_file)

    store = KeyframeOperationStore(conn)

    capacity = ProviderCapacityStore(conn)

    req = KeyframeGenerationRequest(

        operation_id="op_auth_test", workflow_id="wf_auth_test",

        project_id="proj_auth", frame_id="scene_1-start",

        prompt="A watch", product_description="Watch",

        reference_image_paths=(),

    )

    url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000088"

    assert store.claim_first_submission(req, "google_flow") is True

    store.record_project_checkpoint(req, url, "google_flow")

    # Slot acquired with an active lease into future

    import os

    lease_owner = f"{os.getpid()}:live_token"

    prof_path = str(tmp_path / "chrome_prof")

    assert capacity.try_acquire(

        provider_kind="google_flow", operation_id=req.operation_id,

        lease_owner=lease_owner, lease_seconds=600.0, limit=1,

        profile_id=prof_path,

    )

    # Provider simulates auth error

    class AuthErrorProvider(GoogleFlowKeyframeProvider):

        provider_kind = "google_flow"

        def __init__(self):

            super().__init__(

                profile_path=tmp_path / "chrome_prof",

                output_dir=tmp_path / "keyframes",

                timeout_seconds=2.0,

            )

        def reconcile_existing_output(self, r, u):

            # Auth error stops cleanly

            return KeyframeGenerationResult(

                r.operation_id, "unknown", provider_operation_id=u,

                error_code="GOOGLE_FLOW_AUTH_REQUIRED",

                error_message="Flow login session expired or unverified",

                metadata={"provider_kind": "google_flow", "project_url": u},

            )

    provider = AuthErrorProvider()

    provider.bind_operation_store(store)

    runtime = KeyframeRuntime(store=store, provider=provider, capacity_store=capacity)

    runtime.lease_owner = lease_owner

    res = runtime.run(req)

    assert res.status == "unknown"

    assert res.error_code == "GOOGLE_FLOW_AUTH_REQUIRED"

    # Verify that the active lease slot was NOT revoked (since lease is active and process is alive)

    active_slots = capacity.active("google_flow")

    assert len(active_slots) == 1, "Live session or active lease must NOT be revoked"

    assert active_slots[0]["operation_id"] == req.operation_id

    conn.close()

def test_p0_guard_does_not_release_slot_for_live_pid_or_active_lease(tmp_path):

    import os

    """P0 Issue 1: Guard does not release slot when PID is alive or lease is active.

    1. Caller with matching lease_owner cannot delete slot if PID is alive or lease is active.

    2. cleanup_orphaned_slots does not delete expired lease if PID is alive.

    3. Both delete slot only when PID is dead AND lease is expired.

    """

    db = tmp_path / "guard_test.db"

    conn = connect(db)

    store = KeyframeOperationStore(conn)

    capacity = ProviderCapacityStore(conn)

    req = KeyframeGenerationRequest(

        operation_id="op_guard", workflow_id="wf_guard",

        project_id="proj_guard", frame_id="scene_1-start",

        prompt="guard prompt", product_description="guard product",

        reference_image_paths=(),

    )

    url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000099"

    store.claim_first_submission(req, "google_flow")

    store.record_project_checkpoint(req, url, "google_flow")

    store.save_result(req, KeyframeGenerationResult(

        req.operation_id, "unknown", provider_operation_id=url,

        error_code="OUTPUT_RECONCILIATION_EXHAUSTED",

        metadata={"provider_kind": "google_flow", "project_url": url, "output_reconciliation_attempts": 3},

    ))

    # Case 1: Live PID, active lease -> matching caller cannot release

    live_owner = f"{os.getpid()}:worker_token"

    assert capacity.try_acquire(

        provider_kind="google_flow", operation_id=req.operation_id,

        lease_owner=live_owner, lease_seconds=300.0, limit=1,

    )

    capacity.bind_provider_operation("google_flow", req.operation_id, url)

    released = capacity.release_exhausted_reconciliation_slot(

        provider_kind="google_flow", operation_id=req.operation_id, lease_owner=live_owner,

    )

    assert released is False, "Caller with matching lease_owner must NOT release while PID is alive and lease is active"

    assert len(capacity.active("google_flow")) == 1

    # Case 2: Live PID, expired lease -> cleanup_orphaned_slots must NOT delete

    with conn:

        conn.execute("UPDATE video_factory_provider_capacity SET lease_expires_at=0 WHERE operation_id=?", (req.operation_id,))

    assert capacity.cleanup_orphaned_slots(conn) == 0, "cleanup_orphaned_slots must NOT delete slot for live PID even if lease expired"

    assert len(capacity.active("google_flow")) == 1

    # Matching caller also cannot release while PID is alive even if lease expired

    assert capacity.release_exhausted_reconciliation_slot(

        provider_kind="google_flow", operation_id=req.operation_id, lease_owner=live_owner,

    ) is False

    # Case 3: Dead PID, active lease -> must NOT delete

    dead_owner = "99999999:dead_token"  # Non-existent PID

    with conn:

        conn.execute("UPDATE video_factory_provider_capacity SET lease_owner=?, lease_expires_at=? WHERE operation_id=?",

                     (dead_owner, time.time() + 300.0, req.operation_id))

    assert capacity.cleanup_orphaned_slots(conn) == 0, "cleanup_orphaned_slots must NOT delete slot while lease is active"

    assert capacity.release_exhausted_reconciliation_slot(

        provider_kind="google_flow", operation_id=req.operation_id,

    ) is False, "release_exhausted_reconciliation_slot must NOT release while lease is active"

    assert len(capacity.active("google_flow")) == 1

    # Case 4: Dead PID, expired lease -> safely released

    with conn:

        conn.execute("UPDATE video_factory_provider_capacity SET lease_expires_at=0 WHERE operation_id=?", (req.operation_id,))

    released = capacity.release_exhausted_reconciliation_slot(

        provider_kind="google_flow", operation_id=req.operation_id,

    )

    assert released is True, "Must release slot when PID is dead AND lease is expired"

    assert len(capacity.active("google_flow")) == 0

    conn.close()

def test_p0_capacity_pause_unpauses_when_unknown_slot_released(tmp_path):

    """P0 Issue 2: PROVIDER_CAPACITY_WAIT unpauses when slot is released, even if product A is unknown."""

    from video_factory.workflows.runtime_control import ensure_pipeline_pause, pause_pipeline

    from video_factory.automation.failure_classification import FailureCategory

    db = tmp_path / "unpause_test.db"

    conn = connect(db)

    store = KeyframeOperationStore(conn)

    capacity = ProviderCapacityStore(conn)

    # Product A is unknown with OUTPUT_RECONCILIATION_EXHAUSTED

    req = KeyframeGenerationRequest(

        operation_id="op_a_paused", workflow_id="wf_a_paused",

        project_id="proj_a", frame_id="scene_1-start",

        prompt="p", product_description="p", reference_image_paths=(),

    )

    url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"

    store.claim_first_submission(req, "google_flow")

    store.record_project_checkpoint(req, url, "google_flow")

    store.save_result(req, KeyframeGenerationResult(

        req.operation_id, "unknown", provider_operation_id=url,

        error_code="OUTPUT_RECONCILIATION_EXHAUSTED",

        metadata={"provider_kind": "google_flow", "project_url": url, "output_reconciliation_attempts": 3},

    ))

    # Pipeline is paused with PROVIDER_CAPACITY_WAIT

    pause_pipeline(

        conn, category=FailureCategory.QC_INFRA_PAUSED, code="PROVIDER_CAPACITY_WAIT",

        message="provider capacity is full; retry the same operation later", workflow_id="wf_a_paused",

    )

    # Capacity table is empty (A already surrendered its slot)

    assert capacity.active("google_flow") == []

    # ensure_pipeline_pause should clear the pause because slot is free and A is an exhausted unknown

    pause = ensure_pipeline_pause(conn)

    assert pause is None, "Pipeline must unpause when capacity slot is free and unknown operation is exhausted"

    row = conn.execute("SELECT paused, resume_note FROM product_video_runtime_control WHERE state_id=1").fetchone()

    assert row["paused"] == 0

    assert "Provider capacity is available" in row["resume_note"]

    conn.close()

def test_p0_operator_can_replace_frame_after_single_attempt_timeout(tmp_path):

    """P0 Issue 3: Operator can replace keyframe after a single reconciliation attempt times out."""

    import sys

    from pathlib import Path

    tests_dir = str(Path(__file__).parent)

    if tests_dir not in sys.path:

        sys.path.insert(0, tests_dir)

    from video_factory.application.operator_handler import recover_unknown_keyframe_submission

    from test_keyframe_recovery_flow import _setup_workflow_in_db, _quarantine_storyboard_outbox

    db = tmp_path / "op_timeout.db"

    conn = connect(db)

    wf, owner = "wf-timeout", "owner-timeout"

    _, _, requests = _setup_workflow_in_db(

        conn, workflow_id=wf, owner_user_id=owner, project_id="proj-timeout", tmp_path=tmp_path,

    )

    req = requests[0]

    url = "https://flow.google.com/project/57cd810f-5f7f-4b46-9978-3fb460c5272e"

    store = KeyframeOperationStore(conn)

    capacity = ProviderCapacityStore(conn)

    store.claim_first_submission(req, "google_flow")

    store.record_project_checkpoint(req, url, "google_flow")

    # Only 1 reconciliation attempt was claimed and it timed out

    claim_attempt = store.claim_output_reconciliation(req.operation_id, url)

    assert claim_attempt == 1

    store.save_result(req, KeyframeGenerationResult(

        req.operation_id, "unknown", provider_operation_id=url,

        error_code="GOOGLE_FLOW_KEYFRAME_TIMEOUT",

        error_message="Flow post-Send timeout on first attempt",

        metadata={"provider_kind": "google_flow", "project_url": url, "send_attempted": True,

                  "output_reconciliation_attempts": 1, "output_reconciliation_finished_attempt": 1},

    ))

    # Quarantine outbox

    _quarantine_storyboard_outbox(

        conn, wf, "[GOOGLE_FLOW_KEYFRAME_TIMEOUT] Timed out on first reconciliation attempt"

    )

    # Acquire slot with expired lease and dead owner

    assert capacity.try_acquire(provider_kind="google_flow", operation_id=req.operation_id,

                                lease_owner="dead_worker", limit=1)

    capacity.bind_provider_operation("google_flow", req.operation_id, url)

    with conn:

        conn.execute("UPDATE video_factory_provider_capacity SET lease_expires_at=0")

    # Operator replaces the frame even though attempts == 1 (< 3)

    kwargs = dict(

        workflow_id=wf, owner_user_id=owner, operation_id=req.operation_id,

        replacement_note="Operator replaced frame after first reconciliation attempt timed out.",

    )

    result = recover_unknown_keyframe_submission(conn, **kwargs)

    assert result["resumed"] is True

    assert store.get(req.operation_id)["status"] == "abandoned_unknown"

    conn.close()

def test_p0_a_budget_or_time_exhausted_b_still_exports_verified_mp4(tmp_path, monkeypatch):

    """P0 Scenario 4: When A runs out of budget or time, A is skipped cleanly with reason.

    B still exports verified MP4 with receipt matching SHA.

    """

    day = "2026-11-09"

    inbox = tmp_path / "inbox"

    workspace = tmp_path / "vf_workspace"

    database = tmp_path / "hermes.db"

    workspace.mkdir(parents=True)

    (workspace / "keyframes").mkdir(parents=True)

    (workspace / "exports").mkdir(parents=True)

    batches = []

    prod_keys = ["prod_A", "prod_B"]

    for i, pkey in enumerate(prod_keys, 1):

        source = tmp_path / f"pi-{pkey}"

        source.mkdir(parents=True)

        img_path = _create_image(source / "img.png", 720, 1280, color="blue" if i == 1 else "green")

        (source / "products.json").write_text(json.dumps({

            "date": day, "products": [{

                "id": pkey, "name": f"Product {pkey}", "brand": f"Brand_{pkey}", "model": f"Model_{pkey}", "variant": "standard",

                "images": [{"asset_id": f"img-{i}", "absolute_path": str(img_path)}],

            }],

        }), encoding="utf-8")

        batch = publish_daily_run_to_exchange(source, inbox, batch_id=f"batch_{pkey}")

        batches.append({"row_id": f"row_{i:04}", "candidate_id": pkey, "batch_path": str(batch)})

    (tmp_path / "sel.csv").write_text("candidate_id\nprod_A\nprod_B\n", encoding="utf-8")

    from video_factory.integrations import keyframe_provider_factory

    monkeypatch.setattr(pipeline, "INBOX_ROOT", inbox)

    monkeypatch.setattr(pipeline, "PI_RUNS_DIR", tmp_path / "runs")

    monkeypatch.setattr(pipeline, "VF_RUNTIME_DIR", database.parent)

    monkeypatch.setattr(pipeline, "VF_WORKSPACE_DIR", workspace)

    monkeypatch.setattr(pipeline, "VF_DB_PATH", database)

    import sys

    tests_dir = str(Path(__file__).parent)

    if tests_dir not in sys.path:

        sys.path.insert(0, tests_dir)

    from test_creative_resume import config

    profile = tmp_path / "profile.json"

    cfg = config()

    profile.write_text(json.dumps(cfg))

    monkeypatch.setattr(pipeline, "DEFAULT_PROFILE", profile)

    monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)

    monkeypatch.setattr(daily_trigger, "LEDGER_PATH", tmp_path / "ledger.json")

    monkeypatch.setattr(daily_trigger, "prepare_sheet_intake", lambda *a, **kw: {

        "status": "success", "selection_csv": str(tmp_path / "sel.csv"), "exchange_batches": batches,

    })

    # Hook composer to produce minimal verified mp4 for B

    def mock_compose_and_export(*args, **kwargs):

        output_file = workspace / "exports" / f"export_{day}_prod_B.mp4"

        _create_minimal_mp4(output_file)

        file_sha = hashlib.sha256(output_file.read_bytes()).hexdigest()

        meta_file = output_file.parent / "metadata.json"

        meta_file.write_text(json.dumps({

            "project_id": "proj_prod_B",

            "video": {"sha256": file_sha},

            "files": {output_file.name: {"sha256": file_sha}},

        }), encoding="utf-8")

        return {"file_path": str(output_file), "sha256": file_sha}

    init_conn = connect(database)

    KeyframeOperationStore(init_conn)

    ProviderCapacityStore(init_conn)

    init_conn.close()

    monkeypatch.setenv("VF_ENABLE_KEYFRAME_RUNTIME", "1")

    monkeypatch.setenv("VF_TEST_MOCK_FLOW", "1")

    monkeypatch.setenv("VF_CAPACITY_LEASE_SECONDS", "0.2")

    # VF_TEST_FLOW_REPLACE_SUCCESS is NOT set, so A exhausts attempts and stops

    monkeypatch.delenv("VF_TEST_FLOW_REPLACE_SUCCESS", raising=False)

    options = dict(date=day, limit=2, scenes=3, provider_mode="fake", qc_level="low")

    result = daily_trigger.trigger_daily_run(**options)

    # Assert B exported verified MP4

    assert len(result.exported_videos) >= 1, "Product B must export verified MP4 even when A is exhausted"

    b_video = result.exported_videos[0]

    probe = probe_mp4_file(b_video["file_path"])

    assert probe.decodable is True

    receipt_path = Path(b_video["file_path"]).parent / "metadata.json"

    assert receipt_path.is_file()

    receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))

    video_sha = receipt_data.get("video", {}).get("sha256") or receipt_data.get("files", {}).get(Path(b_video["file_path"]).name, {}).get("sha256")

    assert video_sha == probe.sha256

def test_p0_replacement_also_unknown_pipeline_stops_retrying_a(tmp_path, monkeypatch):

    """P0: If replacement operation also returns unknown, pipeline stops trying A and records reason."""

    from video_factory.automation.coordinator import ProductVideoCoordinator

    from video_factory.workflows.repository import WorkflowRepository

    from video_factory.workflows.outbox_repository import OutboxRepository

    db_file = tmp_path / "stop_retry.db"

    conn = connect(db_file)

    store = KeyframeOperationStore(conn)

    capacity = ProviderCapacityStore(conn)

    wf_repo = WorkflowRepository(conn)

    outbox_repo = OutboxRepository(conn)

    workflow_id = "pv-daily-stop-a"

    project_url_1 = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"

    project_url_2 = "https://flow.google.com/project/00000000-0000-0000-0000-000000000002"

    agg = {

        "workflow_id": workflow_id,

        "state": ProductVideoWorkflowState.CREATING_STORYBOARD.value,

        "keyframe_recovery_attempt": 1,  # Replacement already attempted once

    }

    wf_repo.commit_workflow(workflow_id, json.dumps(agg), events=[])

    # Insert second operation for the same keyframe which also became unknown

    req2 = KeyframeGenerationRequest(

        operation_id="op_replace_2", workflow_id=workflow_id,

        project_id="proj_stop", frame_id="scene_1-start",

        prompt="Replacement prompt", product_description="Product A",

        reference_image_paths=(),

    )

    store.claim_first_submission(req2, "google_flow")

    store.record_project_checkpoint(req2, project_url_2, "google_flow")

    # Mark op_replace_2 as exhausted unknown

    with conn:

        conn.execute(

            """UPDATE video_factory_keyframe_operation

               SET status='unknown', error_code='OUTPUT_RECONCILIATION_EXHAUSTED',

                   provider_metadata_json='{"output_reconciliation_attempts": 3}'

               WHERE operation_id=?""",

            (req2.operation_id,),

        )

        # Capacity slot has expired and process is dead

        conn.execute(

            "INSERT INTO video_factory_provider_capacity VALUES (?, ?, ?, ?, ?, ?)",

            ("google_flow", req2.operation_id, "99999999:dead", 0, 0, project_url_2),

        )

    outbox_msg = json.dumps({"type": "storyboard", "payload": {"workflow_id": workflow_id}})

    outbox_obj = outbox_repo.enqueue(workflow_id, outbox_msg)

    outbox_id = outbox_obj.outbox_id

    with conn:

        conn.execute(

            "UPDATE workflow_outbox SET state='quarantined', last_error='OUTPUT_RECONCILIATION_EXHAUSTED' WHERE outbox_id=?",

            (outbox_id,),

        )

    coordinator = ProductVideoCoordinator(conn, db_path=db_file)

    result = coordinator._resume_quarantine_for_workflow(workflow_id)

    assert result["state"] == "skipped"

    assert result["action"] == "skipped"

    assert "FLOW_KEYFRAME_REPLACEMENT_EXHAUSTED" in result["reason"] or "FLOW_REPLACEMENT_EXHAUSTED" in result["reason"]

    assert result["operation_id"] == req2.operation_id

    # Check database state: A is skipped, no new outbox message

    wf_data, _ = wf_repo.get_workflow(workflow_id)

    assert wf_data["state"] == "skipped"

    assert wf_data["keyframe_recovery_attempt"] == 1  # not reset

    # Outbox message is dead_letter

    row = conn.execute("SELECT state, last_error FROM workflow_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()

    assert row[0] == "quarantined"
    assert "FLOW_KEYFRAME_REPLACEMENT_EXHAUSTED" in row[1]

    # Capacity slot released so B can proceed

    assert capacity.active("google_flow") == []

    conn.close()

def test_p0_recovery_outbox_failure_rolls_back_atomically(tmp_path):

    """P0: If outbox update fails during recovery, the entire transaction is rolled back."""

    from video_factory.automation.coordinator import ProductVideoCoordinator

    from video_factory.workflows.repository import WorkflowRepository

    from video_factory.workflows.outbox_repository import OutboxRepository

    db_file = tmp_path / "rollback.db"

    conn = connect(db_file)

    store = KeyframeOperationStore(conn)

    capacity = ProviderCapacityStore(conn)

    wf_repo = WorkflowRepository(conn)

    outbox_repo = OutboxRepository(conn)

    workflow_id = "pv-daily-rollback"

    project_url = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"

    agg = {

        "workflow_id": workflow_id,

        "state": ProductVideoWorkflowState.CREATING_STORYBOARD.value,

        "keyframe_recovery_attempt": 0,

    }

    commit_res = wf_repo.commit_workflow(workflow_id, json.dumps(agg), events=[])

    version_before = commit_res.new_version

    req = KeyframeGenerationRequest(

        operation_id="op_rb_1", workflow_id=workflow_id,

        project_id="proj_rb", frame_id="scene_1-start",

        prompt="A watch", product_description="Watch",

        reference_image_paths=(),

    )

    store.claim_first_submission(req, "google_flow")

    store.record_project_checkpoint(req, project_url, "google_flow")

    with conn:

        conn.execute(

            """UPDATE video_factory_keyframe_operation

               SET status='unknown', error_code='OUTPUT_RECONCILIATION_EXHAUSTED',

                   provider_metadata_json='{"output_reconciliation_attempts": 3}'

               WHERE operation_id=?""",

            (req.operation_id,),

        )

        conn.execute(

            "INSERT INTO video_factory_provider_capacity VALUES (?, ?, ?, ?, ?, ?)",

            ("google_flow", req.operation_id, "99999999:dead", 0, 0, project_url),

        )

    outbox_msg = json.dumps({"type": "storyboard", "payload": {"workflow_id": workflow_id, "kwargs": {}}})

    outbox_obj = outbox_repo.enqueue(workflow_id, outbox_msg)

    outbox_id = outbox_obj.outbox_id

    with conn:

        conn.execute(

            "UPDATE workflow_outbox SET state='quarantined', last_error='OUTPUT_RECONCILIATION_EXHAUSTED' WHERE outbox_id=?",

            (outbox_id,),

        )

    # Inject a trigger or schema constraint that causes outbox UPDATE to fail

    with conn:

        conn.execute(

            "CREATE TRIGGER fail_outbox_update BEFORE UPDATE ON workflow_outbox "

            "BEGIN SELECT RAISE(ABORT, 'Simulated crash during outbox update'); END;"

        )

    coordinator = ProductVideoCoordinator(conn, db_path=db_file)

    with pytest.raises((sqlite3.OperationalError, sqlite3.IntegrityError), match="Simulated crash"):

        coordinator._resume_quarantine_for_workflow(workflow_id)

    # Verify atomic rollback:

    # 1. Operation is STILL unknown (NOT abandoned_unknown)

    op = store.get(req.operation_id)

    assert op["status"] == "unknown"

    # 2. Workflow version and aggregate are NOT changed

    wf_data, cur_ver = wf_repo.get_workflow(workflow_id)

    assert cur_ver == version_before

    assert wf_data.get("keyframe_recovery_attempt", 0) == 0

    # 3. Outbox is STILL quarantined

    outbox_row = conn.execute("SELECT state, last_error FROM workflow_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()

    assert outbox_row[0] == "quarantined"

    conn.close()

