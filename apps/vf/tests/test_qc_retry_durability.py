"""Tests for durable SQLite QC quota ledger, idempotency, and restart survival (T06)."""
import pytest
import sqlite3
from pathlib import Path

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from video_factory.qc.retry_policy import (
    clamp_max_retries,
    claim_qc_attempt,
    complete_qc_attempt,
    get_item_retry_count,
    get_item_remaining_recoveries,
    record_qc_retry,
    ensure_qc_retry_schema,
    QCQuotaExhaustedError,
)
from video_factory.qc.models import NormalizedProduct
from video_factory.qc.service import (
    ProductVideoQCService,
    _candidate_image_attempt_hash,
    _image_qc_context_hash,
    _initial_image_attempt_hash,
)
from video_factory.storage_infra.sqlite import connect


def _test_image_context_hash(evaluator, display_name: str = "Unknown Unknown") -> str:
    product = NormalizedProduct(brand="Unknown", model="Unknown", display_name=display_name)
    return _image_qc_context_hash(product, evaluator)


class TestClampMaxRetries:
    def test_default_is_three_recoveries(self):
        assert clamp_max_retries(None) == 3

    def test_valid_ranges_accepted(self):
        assert clamp_max_retries(0) == 0
        assert clamp_max_retries(1) == 1
        assert clamp_max_retries(2) == 2
        assert clamp_max_retries(3) == 3
        assert clamp_max_retries(4) == 3
        assert clamp_max_retries(5) == 3
        assert clamp_max_retries("2") == 2

    def test_out_of_bounds_rejected_with_value_error(self):
        with pytest.raises(ValueError, match="between 0 and 3"):
            clamp_max_retries(-1)

        with pytest.raises(ValueError, match="Must be an integer"):
            clamp_max_retries("invalid")


class TestDurableQCQuotaLedger:
    @pytest.fixture
    def memory_db(self):
        conn = connect(":memory:")
        ensure_qc_retry_schema(conn)
        yield conn
        conn.close()

    def test_source_image_quota_bounds_to_one_plus_n(self, memory_db):
        """For N=3, product gets 1 initial + 3 recoveries = 4 total attempts. 5th fails."""
        wf_id = "wf_prod_quota_1"
        prod_key = "brand_model_var"

        # Attempt 1 (Initial)
        att1 = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            operation_id="op_init_1",
            max_recoveries=3,
        )
        assert att1["attempt_number"] == 1
        assert att1["reused"] is False
        assert get_item_remaining_recoveries(memory_db, workflow_id=wf_id, retry_type="source_image_qc", item_id=prod_key, max_recoveries=3) == 3

        # Attempt 2 (Recovery 1)
        att2 = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            operation_id="op_rec_1",
            max_recoveries=3,
        )
        assert att2["attempt_number"] == 2
        assert get_item_remaining_recoveries(memory_db, workflow_id=wf_id, retry_type="source_image_qc", item_id=prod_key, max_recoveries=3) == 2

        # Attempt 3 (Recovery 2)
        att3 = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            operation_id="op_rec_2",
            max_recoveries=3,
        )
        assert att3["attempt_number"] == 3
        assert get_item_remaining_recoveries(memory_db, workflow_id=wf_id, retry_type="source_image_qc", item_id=prod_key, max_recoveries=3) == 1

        # Attempt 4 (Recovery 3)
        att4 = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            operation_id="op_rec_3",
            max_recoveries=3,
        )
        assert att4["attempt_number"] == 4
        assert get_item_remaining_recoveries(memory_db, workflow_id=wf_id, retry_type="source_image_qc", item_id=prod_key, max_recoveries=3) == 0

        # Attempt 5: Exceeded! Must raise QCQuotaExhaustedError
        with pytest.raises(QCQuotaExhaustedError) as exc:
            claim_qc_attempt(
                memory_db,
                workflow_id=wf_id,
                retry_type="source_image_qc",
                item_id=prod_key,
                operation_id="op_rec_4",
                max_recoveries=3,
            )
        assert exc.value.attempts_used == 4
        assert exc.value.max_allowed == 4

    def test_legacy_n5_configuration_is_capped_at_four_total_attempts(self, memory_db):
        """Old settings cannot exceed the new one-evaluation plus three-retry cap."""
        wf_id = "wf_prod_quota_n5"
        prod_key = "prod_5"

        for i in range(1, 5):
            att = claim_qc_attempt(
                memory_db,
                workflow_id=wf_id,
                retry_type="source_image_qc",
                item_id=prod_key,
                operation_id=f"op_n5_{i}",
                max_recoveries=5,
            )
            assert att["attempt_number"] == i

        # 5th attempt must fail even when a legacy caller asks for five retries.
        with pytest.raises(QCQuotaExhaustedError):
            claim_qc_attempt(
                memory_db,
                workflow_id=wf_id,
                retry_type="source_image_qc",
                item_id=prod_key,
                operation_id="op_n5_5",
                max_recoveries=5,
            )

    def test_keyframe_quota_is_independent_per_logical_frame(self, memory_db):
        """Each logical keyframe gets its own 1 + N allowance."""
        wf_id = "wf_kf_quota"
        frame_start = "scene_1_start"
        frame_end = "scene_1_end"

        # Claim 4 attempts on start frame
        for i in range(1, 5):
            att = claim_qc_attempt(
                memory_db,
                workflow_id=wf_id,
                retry_type="keyframe_retry",
                item_id=frame_start,
                operation_id=f"op_start_{i}",
                max_recoveries=3,
            )
            assert att["attempt_number"] == i

        # Start frame is now exhausted
        with pytest.raises(QCQuotaExhaustedError):
            claim_qc_attempt(
                memory_db,
                workflow_id=wf_id,
                retry_type="keyframe_retry",
                item_id=frame_start,
                operation_id="op_start_5",
                max_recoveries=3,
            )

        # End frame is untouched and can claim its initial attempt
        end_att = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="keyframe_retry",
            item_id=frame_end,
            operation_id="op_end_1",
            max_recoveries=3,
        )
        assert end_att["attempt_number"] == 1
        assert end_att["reused"] is False

    def test_idempotent_replay_with_same_operation_id_does_not_advance_quota(self, memory_db):
        """Replaying with an existing operation_id returns the existing attempt."""
        wf_id = "wf_idemp_1"
        prod_key = "prod_idemp"

        att1 = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            operation_id="op_unique_100",
            max_recoveries=3,
        )
        assert att1["attempt_number"] == 1
        assert att1["reused"] is False

        # Replay attempt with same operation_id
        att1_replay = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            operation_id="op_unique_100",
            max_recoveries=3,
        )
        assert att1_replay["attempt_id"] == att1["attempt_id"]
        assert att1_replay["attempt_number"] == 1
        assert att1_replay["reused"] is True
        assert get_item_retry_count(memory_db, wf_id, "source_image_qc", prod_key) == 1

    def test_changing_candidate_asset_or_hash_does_not_reset_product_quota(self, memory_db):
        """Changing input hash or asset ID continues product attempt count."""
        wf_id = "wf_prod_subst"
        prod_key = "product_steady"

        att1 = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            input_hash="hash_candidate_1",
            operation_id="op_cand_1",
            max_recoveries=3,
        )
        assert att1["attempt_number"] == 1

        # Candidate 2 with different hash
        att2 = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            input_hash="hash_candidate_2",
            operation_id="op_cand_2",
            max_recoveries=3,
        )
        assert att2["attempt_number"] == 2

    def test_complete_qc_attempt_records_result(self, memory_db):
        wf_id = "wf_comp_test"
        prod_key = "prod_comp"

        att = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            operation_id="op_comp_1",
            max_recoveries=3,
        )
        attempt_id = att["attempt_id"]

        complete_qc_attempt(
            memory_db,
            attempt_id=attempt_id,
            status="completed",
            verdict="pass",
            qc_report_id="rep_test_99",
            output_hash="sha256:out99",
            budget_reference="res_99",
            cost_usd=0.05,
        )

        row = memory_db.execute(
            "SELECT status, verdict, qc_report_id, output_hash, budget_reference, cost_usd FROM workflow_qc_retries WHERE id = ?",
            (attempt_id,),
        ).fetchone()
        assert row["status"] == "completed"
        assert row["verdict"] == "pass"
        assert row["qc_report_id"] == "rep_test_99"
        assert row["output_hash"] == "sha256:out99"
        assert row["budget_reference"] == "res_99"
        assert abs(row["cost_usd"] - 0.05) < 1e-6

    def test_record_qc_retry_deduplicates_existing_claimed_attempt(self, memory_db):
        """record_qc_retry updates the existing claimed attempt instead of creating a duplicate."""
        wf_id = "wf_dedup"
        prod_key = "prod_dedup"

        att = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            operation_id="op_dedup_1",
            max_recoveries=3,
        )
        assert att["attempt_number"] == 1

        record_qc_retry(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=prod_key,
            attempt_number=1,
            verdict="fail",
            qc_report_id="rep_fail_1",
        )

        count = memory_db.execute(
            "SELECT COUNT(*) FROM workflow_qc_retries WHERE workflow_id = ? AND item_id = ?",
            (wf_id, prod_key),
        ).fetchone()[0]
        assert count == 1
        assert get_item_retry_count(memory_db, wf_id, "source_image_qc", prod_key) == 1

    def test_quota_survives_sqlite_close_and_reopen(self, tmp_path):
        """T06: Close SQLite connection, reopen from disk, and assert quota is preserved."""
        db_path = tmp_path / "durable_quota.db"
        conn1 = connect(db_path)
        ensure_qc_retry_schema(conn1)

        wf_id = "wf_restart_test"
        prod_key = "prod_restart"

        # Claim 4 attempts to exhaust quota
        for i in range(1, 5):
            claim_qc_attempt(
                conn1,
                workflow_id=wf_id,
                retry_type="source_image_qc",
                item_id=prod_key,
                operation_id=f"op_restart_{i}",
                max_recoveries=3,
            )
        conn1.close()

        # Reopen database in a completely fresh connection
        conn2 = connect(db_path)
        ensure_qc_retry_schema(conn2)

        assert get_item_retry_count(conn2, wf_id, "source_image_qc", prod_key) == 4
        assert get_item_remaining_recoveries(conn2, workflow_id=wf_id, retry_type="source_image_qc", item_id=prod_key, max_recoveries=3) == 0

        # Must still be exhausted after restart
        with pytest.raises(QCQuotaExhaustedError):
            claim_qc_attempt(
                conn2,
                workflow_id=wf_id,
                retry_type="source_image_qc",
                item_id=prod_key,
                operation_id="op_restart_after_close",
                max_recoveries=3,
            )
        conn2.close()

    def test_same_operation_id_with_changed_input_hash_rejected_with_value_error(self, memory_db):
        """SPEC S8: Replaying same operation_id with changed input_hash must be rejected."""
        wf_id = "wf_op_hash_check"
        item_id = "item_1"
        op_id = "op_hash_1"

        claim1 = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=item_id,
            input_hash="hash_initial_bytes",
            operation_id=op_id,
            max_recoveries=3,
        )
        assert claim1["reused"] is False

        # Same op_id with SAME input_hash succeeds
        claim_replay = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=item_id,
            input_hash="hash_initial_bytes",
            operation_id=op_id,
            max_recoveries=3,
        )
        assert claim_replay["reused"] is True

        # Same op_id with CHANGED input_hash must raise ValueError
        with pytest.raises(ValueError, match="input_hash changed"):
            claim_qc_attempt(
                memory_db,
                workflow_id=wf_id,
                retry_type="source_image_qc",
                item_id=item_id,
                input_hash="hash_modified_bytes",
                operation_id=op_id,
                max_recoveries=3,
            )

    def test_configured_image_qc_ignores_legacy_money_authorization_fields(self, memory_db, monkeypatch):
        """Legacy QC money fields do not gate evaluation; provider absence remains fail-closed."""
        svc = ProductVideoQCService(memory_db)
        monkeypatch.setattr("video_factory.qc.evaluator.get_vertex_config", lambda: (None, None))
        monkeypatch.setattr("video_factory.integrations.nine_router.load_config", lambda: {})
        wf_id = "wf_auth_guard"
        pack = {"resource_pack_id": "rp_auth", "version": 1, "assets": []}
        src_ctx = {"display_name": "Test Prod"}

        # 1. Authorization is None
        res_none = svc.select_image_references(
            workflow_id=wf_id,
            resource_pack=pack,
            source_context=src_ctx,
            provider_mode="configured",
            authorization=None,
        )
        assert res_none["code"] == "QC_PROVIDER_UNAVAILABLE"

        # 2. Budget is 0
        res_zero = svc.select_image_references(
            workflow_id=wf_id,
            resource_pack=pack,
            source_context=src_ctx,
            provider_mode="configured",
            authorization={
                "authorization_id": "auth_1",
                "workflow_id": wf_id,
                "max_cost_usd": 0.0,
                "scope": ["image"],
                "workflow_max_cost_usd": 0.0,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            },
        )
        assert res_zero["code"] == "QC_PROVIDER_UNAVAILABLE"

        # 3. Authorization is expired
        res_exp = svc.select_image_references(
            workflow_id=wf_id,
            resource_pack=pack,
            source_context=src_ctx,
            provider_mode="configured",
            authorization={
                "authorization_id": "auth_1",
                "workflow_id": wf_id,
                "max_cost_usd": 10.0,
                "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            },
        )
        assert res_exp["code"] == "QC_PROVIDER_UNAVAILABLE"

        res_missing_scope = svc.select_image_references(
            workflow_id=wf_id,
            resource_pack=pack,
            source_context=src_ctx,
            provider_mode="configured",
            authorization={"workflow_id": wf_id, "authorization_id": "auth_1", "max_cost_usd": 10.0},
        )
        assert res_missing_scope["code"] == "QC_PROVIDER_UNAVAILABLE"

        for invalid_auth in (
            {"workflow_id": wf_id, "scope": ["image"], "max_cost_usd": 10.0,
             "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
            {"workflow_id": wf_id, "authorization_id": "auth_1", "scope": ["image"],
             "max_cost_usd": 10.0},
        ):
            denied = svc.select_image_references(
                workflow_id=wf_id, resource_pack=pack, source_context=src_ctx,
                provider_mode="configured", authorization=invalid_auth,
            )
            assert denied["code"] == "QC_PROVIDER_UNAVAILABLE"

        assert memory_db.execute("SELECT COUNT(*) FROM budget_reservation WHERE scope_id = ?", (wf_id,)).fetchone()[0] == 0

    def test_legacy_qc_cost_flags_do_not_create_or_enforce_budget_reservations(self, memory_db, monkeypatch):
        monkeypatch.setattr("video_factory.qc.evaluator.get_vertex_config", lambda: (None, None))
        monkeypatch.setattr("video_factory.integrations.nine_router.load_config", lambda: {})
        svc = ProductVideoQCService(memory_db)
        result = svc.select_image_references(
            workflow_id="wf_small_cap",
            resource_pack={"resource_pack_id": "rp_small_cap", "version": 1, "assets": []},
            provider_mode="configured",
            authorization={"workflow_id": "wf_small_cap", "authorization_id": "auth_small", "workflow_max_cost_usd": 1.0, "scope": ["image"], "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
            max_cost_usd=2.0,
        )
        assert result["code"] == "QC_PROVIDER_UNAVAILABLE"
        assert memory_db.execute("SELECT COUNT(*) FROM budget_reservation WHERE scope_id='wf_small_cap'").fetchone()[0] == 0

    def test_missing_configured_evaluator_stops_before_budget_and_claim(self, memory_db, monkeypatch):
        monkeypatch.setattr("video_factory.qc.evaluator.get_vertex_config", lambda: (None, None))
        monkeypatch.setattr("video_factory.integrations.nine_router.load_config", lambda: {})
        workflow_id = "wf_no_qc_provider"
        result = ProductVideoQCService(memory_db).select_image_references(
            workflow_id=workflow_id,
            resource_pack={"resource_pack_id": "rp_no_provider", "version": 1, "assets": []},
            provider_mode="configured",
            authorization={
                "workflow_id": workflow_id, "authorization_id": "auth_no_provider",
                "scope": ["image"], "max_cost_usd": 1.0,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            },
        )
        assert result["code"] == "QC_PROVIDER_UNAVAILABLE"
        assert memory_db.execute("SELECT COUNT(*) FROM budget_reservation WHERE scope_id=?", (workflow_id,)).fetchone()[0] == 0
        assert memory_db.execute("SELECT COUNT(*) FROM workflow_qc_retries WHERE workflow_id=?", (workflow_id,)).fetchone()[0] == 0

    def test_existing_initial_claim_reconciles_without_provider_or_new_budget(self, memory_db, monkeypatch):
        from video_factory.qc.evaluator import MultimodalEvaluator

        workflow_id = "wf_reconcile_offline"
        pack_id = "rp_reconcile_offline"
        evaluator = MultimodalEvaluator()
        input_hash = _initial_image_attempt_hash(
            [], _test_image_context_hash(evaluator, "Test Prod")
        )
        claim_qc_attempt(
            memory_db, workflow_id=workflow_id, retry_type="source_image_qc",
            item_id=pack_id, input_hash=input_hash,
            operation_id=f"op_init_img_{workflow_id}_v1", max_recoveries=3,
        )
        monkeypatch.setattr(evaluator, "configured_preflight", lambda: pytest.fail("replay contacted provider"))

        result = ProductVideoQCService(memory_db, evaluator=evaluator).select_image_references(
            workflow_id=workflow_id,
            resource_pack={"resource_pack_id": pack_id, "version": 1, "assets": []},
            source_context={"display_name": "Test Prod"},
            provider_mode="configured",
            authorization={
                "workflow_id": workflow_id, "authorization_id": "auth_reconcile",
                "scope": ["image"], "max_cost_usd": 1.0,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            },
        )
        assert result["status"] == "pending"
        assert result["code"] == "QC_OUTCOME_UNCERTAIN"
        assert memory_db.execute("SELECT COUNT(*) FROM budget_reservation WHERE scope_id=?", (workflow_id,)).fetchone()[0] == 0

    def test_claimed_initial_attempt_does_not_evaluate_again(self, memory_db, tmp_path):
        from PIL import Image
        from video_factory.qc.image_qc import compute_file_sha256

        image_path = tmp_path / "candidate.png"
        Image.new("RGB", (16, 16), "red").save(image_path)
        wf_id = "wf_claimed_initial"
        evaluator = MagicMock()
        input_hash = _initial_image_attempt_hash(
            [["img_1", str(image_path), compute_file_sha256(image_path)]],
            _test_image_context_hash(evaluator),
        )
        claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id="rp_claimed_initial",
            input_hash=input_hash,
            operation_id=f"op_init_img_{wf_id}_v1",
            max_recoveries=3,
        )
        svc = ProductVideoQCService(memory_db, evaluator=evaluator)
        result = svc.select_image_references(
            workflow_id=wf_id,
            resource_pack={"resource_pack_id": "rp_claimed_initial", "version": 1,
                           "assets": [{"asset_id": "img_1", "local_path": str(image_path)}]},
            provider_mode="configured",
            authorization={"workflow_id": wf_id, "authorization_id": "auth_claimed", "max_cost_usd": 1.0, "scope": ["image"], "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        )
        assert result["status"] == "pending"
        evaluator.evaluate_image.assert_not_called()

    def test_claimed_candidate_attempt_does_not_evaluate_again(self, memory_db, tmp_path):
        from PIL import Image
        from video_factory.qc.image_qc import compute_file_sha256

        image_path = tmp_path / "candidate.png"
        Image.new("RGB", (16, 16), "blue").save(image_path)
        wf_id = "wf_claimed_candidate"
        evaluator = MagicMock()
        claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id="rp_claimed_candidate",
            input_hash=_candidate_image_attempt_hash(
                compute_file_sha256(image_path), _test_image_context_hash(evaluator)
            ),
            operation_id=f"op_rec_{wf_id}_cand_1",
            max_recoveries=3,
        )
        svc = ProductVideoQCService(memory_db, evaluator=evaluator)
        result = svc.select_image_references(
            workflow_id=wf_id,
            resource_pack={"resource_pack_id": "rp_claimed_candidate", "version": 1, "assets": []},
            source_context={"images": [{"asset_id": "cand_1", "local_path": str(image_path)}]},
            provider_mode="configured",
            authorization={"workflow_id": wf_id, "authorization_id": "auth_claimed_cand", "max_cost_usd": 1.0, "scope": ["image"], "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        )
        assert result["status"] == "pending"
        assert result["code"] == "QC_OUTCOME_UNCERTAIN"
        evaluator.evaluate_image.assert_not_called()

    def test_same_image_bytes_with_changed_product_prompt_rejects_replay(self, memory_db, tmp_path):
        from PIL import Image
        from video_factory.qc.image_qc import compute_file_sha256

        image_path = tmp_path / "same_bytes.png"
        Image.new("RGB", (16, 16), "green").save(image_path)
        evaluator = MagicMock()
        workflow_id = "wf_changed_product_prompt"
        old_hash = _initial_image_attempt_hash(
            [["img_1", str(image_path), compute_file_sha256(image_path)]],
            _test_image_context_hash(evaluator, "Original Model"),
        )
        claim_qc_attempt(
            memory_db, workflow_id=workflow_id, retry_type="source_image_qc",
            item_id="rp_changed_prompt", input_hash=old_hash,
            operation_id=f"op_init_img_{workflow_id}_v1", max_recoveries=3,
        )
        result = ProductVideoQCService(memory_db, evaluator=evaluator).select_image_references(
            workflow_id=workflow_id,
            resource_pack={"resource_pack_id": "rp_changed_prompt", "version": 1,
                           "assets": [{"asset_id": "img_1", "local_path": str(image_path)}]},
            source_context={"display_name": "Different Model"},
            provider_mode="configured",
            authorization={"workflow_id": workflow_id, "authorization_id": "auth_prompt",
                           "max_cost_usd": 1.0, "scope": ["image"],
                           "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        )
        assert result["code"] == "QC_OUTCOME_UNCERTAIN"
        evaluator.evaluate_image.assert_not_called()

    def test_candidate_timeout_stops_before_next_paid_candidate(self, memory_db, tmp_path):
        from PIL import Image

        paths = [tmp_path / f"image_{index}.png" for index in range(3)]
        for index, path in enumerate(paths):
            Image.new("RGB", (32, 32), (index * 70, 30, 150)).save(path)
        passed = {
            "verdict": "pass", "total_score": 90, "identity_score": 55,
            "clarity_score": 23, "usability_score": 12, "hard_failure": False,
            "source_branding_detected": False, "identity_mismatch": False,
            "eligible": True, "reason": "clear", "view_angle": "front",
            "shot_type": "product", "visible_details": [],
            "scene_suitability": [], "risk_flags": [], "text_coverage_percent": 0,
        }
        evaluator = MagicMock()
        failed = {**passed, "verdict": "fail", "eligible": False, "hard_failure": True,
                  "reason": "Clearly different product", "identity_mismatch": True}
        evaluator.evaluate_image.side_effect = [failed, TimeoutError("unknown paid result"), passed]
        workflow_id = "wf_candidate_timeout"
        assets = [{"asset_id": f"image_{index}", "local_path": str(path)} for index, path in enumerate(paths)]
        svc = ProductVideoQCService(memory_db, evaluator=evaluator)
        result = svc.select_image_references(
            workflow_id=workflow_id,
            resource_pack={"resource_pack_id": "rp_candidate_timeout", "version": 1, "assets": assets[:1]},
            source_context={"images": assets},
            provider_mode="configured",
            authorization={"workflow_id": workflow_id, "authorization_id": "auth_timeout",
                           "max_cost_usd": 1.0, "scope": ["image"],
                           "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        )
        assert result["status"] == "pending"
        assert result["code"] == "QC_OUTCOME_UNCERTAIN"
        assert evaluator.evaluate_image.call_count == 2
        assert memory_db.execute(
            "SELECT status FROM workflow_qc_retries WHERE operation_id=?",
            (f"op_rec_{workflow_id}_image_1",),
        ).fetchone()[0] == "technical_unknown"

    def test_candidate_substitution_reused_attempt_without_item_evidence_fails_closed(self, memory_db, tmp_path):
        """SPEC S12-S15: Reused candidate attempt lacking verified persisted item evidence fails closed without re-evaluating."""
        from video_factory.qc.image_qc import compute_file_sha256
        from PIL import Image
        cand_file = tmp_path / "cand1.png"
        Image.new("RGB", (16, 16), "red").save(cand_file)
        cand_sha = compute_file_sha256(cand_file)

        eval_mock = MagicMock()
        svc = ProductVideoQCService(memory_db, evaluator=eval_mock)
        wf_id = "wf_reused_no_ev"
        pack_id = "rp_no_ev"

        # Pre-seed an attempt claimed and completed with status='completed', but NO item report in QCReportRepository
        op_id = f"op_rec_{wf_id}_cand_1"
        claim = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=pack_id,
            input_hash=_candidate_image_attempt_hash(
                cand_sha, _test_image_context_hash(eval_mock, "Test Prod")
            ),
            operation_id=op_id,
            max_recoveries=3,
        )
        complete_qc_attempt(
            memory_db,
            attempt_id=claim["attempt_id"],
            status="completed",
            verdict="pass",
            qc_report_id="rep_non_existent",
            output_hash=cand_sha,
        )

        pack = {
            "resource_pack_id": pack_id,
            "version": 1,
            "assets": [],
        }
        src_ctx = {
            "display_name": "Test Prod",
            "images": [{"asset_id": "cand_1", "local_path": str(cand_file), "sha256": cand_sha}],
        }

        auth = {
            "authorization_id": "auth_1",
            "workflow_id": wf_id,
            "max_cost_usd": 10.0,
            "scope": ["image"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }

        res = svc.select_image_references(
            workflow_id=wf_id,
            resource_pack=pack,
            source_context=src_ctx,
            provider_mode="configured",
            authorization=auth,
        )

        assert res["status"] == "failed"
        assert res["code"] == "IMAGE_QC_FAILED"
        assert "lacks verified persisted PASS item evidence" in res["reason"]
        eval_mock.evaluate_image.assert_not_called()

    def test_candidate_substitution_reused_attempt_with_changed_input_hash_fails_closed(self, memory_db, tmp_path):
        """SPEC S8/S12-S15: If candidate file bytes change under same operation ID, reuse is rejected with error."""
        from video_factory.qc.image_qc import compute_file_sha256
        from PIL import Image
        cand_file = tmp_path / "cand2.png"
        Image.new("RGB", (16, 16), "red").save(cand_file)
        initial_sha = compute_file_sha256(cand_file)

        eval_mock = MagicMock()
        svc = ProductVideoQCService(memory_db, evaluator=eval_mock)
        wf_id = "wf_reused_changed_hash"
        pack_id = "rp_changed_hash"

        # Pre-seed attempt with initial_sha
        op_id = f"op_rec_{wf_id}_cand_1"
        claim = claim_qc_attempt(
            memory_db,
            workflow_id=wf_id,
            retry_type="source_image_qc",
            item_id=pack_id,
            input_hash=_candidate_image_attempt_hash(
                initial_sha, _test_image_context_hash(eval_mock, "Test Prod")
            ),
            operation_id=op_id,
            max_recoveries=3,
        )

        # Mutate the file on disk so actual bytes hash changes
        Image.new("RGB", (16, 16), "blue").save(cand_file)
        new_sha = compute_file_sha256(cand_file)

        pack = {
            "resource_pack_id": pack_id,
            "version": 1,
            "assets": [],
        }
        src_ctx = {
            "display_name": "Test Prod",
            "images": [{"asset_id": "cand_1", "local_path": str(cand_file), "sha256": new_sha}],
        }

        auth = {
            "authorization_id": "auth_1",
            "workflow_id": wf_id,
            "max_cost_usd": 10.0,
            "scope": ["image"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }

        # A changed paid operation is rejected as an operator wait, never re-evaluated.
        result = svc.select_image_references(
            workflow_id=wf_id,
            resource_pack=pack,
            source_context=src_ctx,
            provider_mode="configured",
            authorization=auth,
        )
        assert result["status"] == "pending"
        assert result["code"] == "QC_OUTCOME_UNCERTAIN"
