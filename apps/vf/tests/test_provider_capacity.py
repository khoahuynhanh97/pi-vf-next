from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from video_factory.integrations.capacity import ProviderCapacityStore
from video_factory.integrations.ports import (
    VideoGenerationRequest,
    VideoGenerationResult,
)
from video_factory.integrations.scene_renderer import (
    SceneRenderOperationStore,
    SceneRenderRuntime,
)


def test_capacity_is_shared_between_sqlite_workers(tmp_path: Path) -> None:
    database = tmp_path / "capacity.sqlite"
    ProviderCapacityStore(sqlite3.connect(database)).connection.close()
    barrier = threading.Barrier(2)
    results: list[tuple[str, bool]] = []

    def worker(operation: str) -> None:
        connection = sqlite3.connect(database)
        store = ProviderCapacityStore(connection)
        barrier.wait()
        won = store.try_acquire(
            provider_kind="vertex_ai", operation_id=operation,
            lease_owner=operation, limit=1,
        )
        results.append((operation, won))
        connection.close()

    workers = [threading.Thread(target=worker, args=(name,)) for name in ("op-a", "op-b")]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join(timeout=5)
        assert not worker_thread.is_alive()
    assert sorted(won for _operation, won in results) == [False, True]

    connection = sqlite3.connect(database)
    store = ProviderCapacityStore(connection)
    winner = next(operation for operation, won in results if won)
    loser = next(operation for operation, won in results if not won)
    assert len(store.active("vertex_ai")) == 1
    store.release_terminal("vertex_ai", winner)
    assert store.try_acquire(
        provider_kind="vertex_ai", operation_id=loser,
        lease_owner="retry-worker", limit=1,
    )
    connection.close()


def test_flow_profile_is_exclusive_and_ambiguous_expiry_stays_blocked(tmp_path: Path) -> None:
    database = tmp_path / "profile.sqlite"
    first_conn = sqlite3.connect(database)
    second_conn = sqlite3.connect(database)
    first = ProviderCapacityStore(first_conn)
    second = ProviderCapacityStore(second_conn)
    assert first.try_acquire(
        provider_kind="google_flow", operation_id="flow-a", lease_owner="worker-a",
        profile_id="profile-1", limit=2, lease_seconds=1,
    )
    assert not second.try_acquire(
        provider_kind="google_flow", operation_id="flow-b", lease_owner="worker-b",
        profile_id="profile-1", limit=2,
    )
    first_conn.execute(
        "UPDATE video_factory_provider_capacity SET lease_expires_at=0 WHERE operation_id='flow-a'"
    )
    first_conn.commit()
    assert not second.try_acquire(
        provider_kind="google_flow", operation_id="flow-a", lease_owner="worker-b",
        profile_id="profile-1", limit=2, reconciled_takeover=True,
    )
    first.bind_provider_operation("google_flow", "flow-a", "real-flow-operation")
    assert second.try_acquire(
        provider_kind="google_flow", operation_id="flow-a", lease_owner="worker-b",
        profile_id="profile-1", limit=2, reconciled_takeover=True,
    )
    assert not first.try_acquire(
        provider_kind="google_flow", operation_id="flow-b", lease_owner="worker-a",
        profile_id="profile-1", limit=2,
    )
    second.release_terminal("google_flow", "flow-a")
    assert first.try_acquire(
        provider_kind="google_flow", operation_id="flow-b", lease_owner="worker-a",
        profile_id="profile-1", limit=2,
    )
    first_conn.close()
    second_conn.close()


def test_capacity_rejects_invalid_limit(tmp_path: Path) -> None:
    store = ProviderCapacityStore(sqlite3.connect(tmp_path / "limit.sqlite"))
    with pytest.raises(ValueError, match="positive"):
        store.try_acquire(
            provider_kind="vertex_ai", operation_id="op", lease_owner="worker", limit=0,
        )


def test_scene_poll_reuses_process_capacity_and_keeps_ambiguous_operation(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    class Provider:
        provider_kind = "google_flow"
        profile_id = "flow-profile"

        def is_available(self) -> bool:
            return True

        def submit(self, request: VideoGenerationRequest) -> VideoGenerationResult:
            return VideoGenerationResult(
                request_id=request.request_id, status="submitted",
                provider_operation_id="real-flow-id",
            )

        def get_status(self, operation_id: str) -> VideoGenerationResult:
            assert operation_id == "real-flow-id"
            return VideoGenerationResult(
                request_id="", status="failed",
                provider_operation_id=operation_id,
                error_code="RECONCILIATION_REQUIRED",
            )

    connection = sqlite3.connect(tmp_path / "scene.sqlite")
    store = SceneRenderOperationStore(connection)
    capacity = ProviderCapacityStore(connection)
    request = VideoGenerationRequest(
        request_id="scene-op", owner_user_id="owner", project_id="project",
        scene_id="scene-001", prompt="product rotating", duration_seconds=4,
    )
    first = SceneRenderRuntime(store=store, provider=Provider(), capacity_store=capacity)
    assert first.run(request).status == "submitted"
    with connection:
        connection.execute("UPDATE video_factory_scene_operation SET submitted_at=? WHERE operation_id=?",
                           ((datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(), request.request_id))
    second = SceneRenderRuntime(store=store, provider=Provider(), capacity_store=capacity)
    result = second.run(request)
    assert result.error_code == "RECONCILIATION_REQUIRED"
    assert store.get(request.request_id)["status"] == "submitted"
    assert len(capacity.active("google_flow")) == 1
    connection.close()


def test_admission_recovers_only_bound_completed_slots_after_restart(tmp_path):
    path = tmp_path / "terminal-capacity.db"
    connection = sqlite3.connect(path)
    capacity = ProviderCapacityStore(connection)
    assert capacity.try_acquire(provider_kind="google_flow", operation_id="old",
                                lease_owner="worker", limit=1)
    capacity.bind_provider_operation("google_flow", "old", "flow-old")
    with connection:
        connection.execute(
            "CREATE TABLE video_factory_keyframe_operation "
            "(operation_id TEXT, provider_kind TEXT, provider_operation_id TEXT, status TEXT, error_code TEXT)"
        )
        connection.execute("INSERT INTO video_factory_keyframe_operation VALUES "
                           "('old', 'vertex_keyframe', 'flow-old', 'completed', '')")
    connection.close()
    connection = sqlite3.connect(path)
    capacity = ProviderCapacityStore(connection)
    kwargs = dict(provider_kind="google_flow", operation_id="next", lease_owner="worker", limit=1)
    assert not capacity.try_acquire(**kwargs)
    with connection:
        connection.execute("UPDATE video_factory_keyframe_operation SET provider_kind='google_flow', "
                           "status='failed', error_code='KEYFRAME_TIMEOUT_EXPIRED'")
    assert not capacity.try_acquire(**kwargs)
    with connection:
        connection.execute("UPDATE video_factory_keyframe_operation SET status='completed', error_code=''")
    assert capacity.try_acquire(**kwargs)
    assert [slot["operation_id"] for slot in capacity.active("google_flow")] == ["next"]
    connection.close()


@pytest.mark.parametrize("kind", ["keyframe", "scene"])
def test_terminal_persistence_and_slot_release_roll_back_together(tmp_path, monkeypatch, kind):
    from video_factory.integrations.keyframe_provider import (
        KeyframeGenerationRequest, KeyframeGenerationResult, KeyframeOperationStore,
    )

    connection = sqlite3.connect(tmp_path / f"{kind}.sqlite")
    capacity = ProviderCapacityStore(connection)
    assert capacity.try_acquire(provider_kind="google_flow", operation_id="op",
                                lease_owner="worker", limit=1)
    if kind == "keyframe":
        store = KeyframeOperationStore(connection)
        request = KeyframeGenerationRequest(operation_id="op", workflow_id="wf", project_id="project",
                                            frame_id="frame", prompt="test", product_description="test",
                                            reference_image_paths=())
        store.claim_first_submission(request, "google_flow")
        result = KeyframeGenerationResult("op", "failed", error_code="PROVIDER_DENIED")
    else:
        store = SceneRenderOperationStore(connection)
        request = VideoGenerationRequest("op", "owner", "project", "scene", "test", 4)
        store.mark_planned(request)
        store.claim_provider_for_submission(request, "google_flow")
        result = VideoGenerationResult("op", "failed", error_code="PROVIDER_DENIED")

    def fail_release(*args):
        raise sqlite3.OperationalError("simulated release failure")

    with monkeypatch.context() as patch:
        patch.setattr(capacity, "release_terminal_in_transaction", fail_release)
        with pytest.raises(sqlite3.OperationalError, match="simulated release failure"):
            store.save_result(request, result, capacity=capacity)
    assert store.get("op")["status"] == "submitting"
    assert len(capacity.active("google_flow")) == 1
    store.save_result(request, result, capacity=capacity)
    assert store.get("op")["status"] == "failed"
    assert capacity.active("google_flow") == []
    connection.close()


def test_expired_ambiguous_slot_can_be_owned_for_lookup_only(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "reconcile.sqlite")
    store = ProviderCapacityStore(connection)
    assert store.try_acquire(
        provider_kind="google_flow", operation_id="scene-op", lease_owner="dead-worker",
        profile_id="flow-profile", limit=1,
    )
    connection.execute(
        "UPDATE video_factory_provider_capacity SET lease_expires_at=0 WHERE operation_id='scene-op'"
    )
    connection.commit()
    assert store.try_acquire(
        provider_kind="google_flow", operation_id="scene-op", lease_owner="recovery-worker",
        profile_id="flow-profile", limit=1, reconciliation_only=True,
    )
    assert not store.try_acquire(
        provider_kind="google_flow", operation_id="other-op", lease_owner="other-worker",
        profile_id="flow-profile", limit=1,
    )
    with pytest.raises(ValueError, match="profile binding conflict"):
        store.try_acquire(
            provider_kind="google_flow", operation_id="scene-op", lease_owner="recovery-worker",
            profile_id="another-profile", limit=1,
        )
    connection.close()


def test_legacy_profileless_operation_with_provider_id_can_only_be_reconciled(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "legacy-flow.sqlite")
    store = ProviderCapacityStore(connection)
    assert store.try_acquire(
        provider_kind="google_flow", operation_id="legacy-op", lease_owner="old-worker",
        profile_id="", limit=2,
    )
    provider_operation_id = "https://flow.google.com/project/00000000-0000-0000-0000-000000000001"
    store.bind_provider_operation("google_flow", "legacy-op", provider_operation_id)
    connection.execute(
        "UPDATE video_factory_provider_capacity SET lease_expires_at=0 WHERE operation_id='legacy-op'"
    )
    connection.commit()

    assert store.try_acquire(
        provider_kind="google_flow", operation_id="legacy-op", lease_owner="recovery-worker",
        profile_id="verified-profile", limit=2, reconciled_takeover=True,
    )
    legacy = store.active("google_flow")[0]
    assert legacy["profile_id"] == ""
    assert legacy["provider_operation_id"] == provider_operation_id
    assert legacy["lease_owner"] == "recovery-worker"
    assert not store.try_acquire(
        provider_kind="google_flow", operation_id="new-op", lease_owner="other-worker",
        profile_id="verified-profile", limit=2,
    )
    connection.close()
