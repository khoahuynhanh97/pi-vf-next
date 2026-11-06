"""Actual SQLite admission must exclude maintenance in either direction."""

import os
import sqlite3
import subprocess
import sys
import threading

import pytest

from video_factory.integrations.capacity import ProviderCapacityStore
from video_factory.maintenance.ledger import RepairLedger


def setup_stores(tmp_path):
    database = tmp_path / "worker.sqlite"
    maintenance_connection = sqlite3.connect(database)
    provider_connection = sqlite3.connect(database)
    ledger = RepairLedger(maintenance_connection)
    capacity = ProviderCapacityStore(provider_connection)
    incident = ledger.incident(workflow_id="wf", operation_id="repair", profile_id="profile",
                               error_code="GOOGLE_FLOW_UI_UNVERIFIED", fingerprint="layout")
    return database, ledger, capacity, incident["incident_id"]


def acquire(capacity, provider="google_flow", operation="operation"):
    return capacity.try_acquire(provider_kind=provider, operation_id=operation,
                                lease_owner="provider-worker", limit=2, profile_id="profile")


def test_active_maintenance_lease_blocks_provider_on_another_connection(tmp_path):
    _, ledger, capacity, incident = setup_stores(tmp_path)
    assert ledger.acquire(incident, "maintenance-worker", 60)
    assert not acquire(capacity)
    assert capacity.active("google_flow") == []
    ledger.release(incident, "maintenance-worker")
    assert acquire(capacity)


def test_expired_maintenance_lease_still_blocks_live_pid_without_signaling_process(tmp_path):
    _, ledger, capacity, incident = setup_stores(tmp_path)
    assert ledger.acquire(incident, f"{os.getpid()}:maintenance", 60)
    with ledger.connection:
        ledger.connection.execute("UPDATE flow_ui_lease SET expires_at=0")
    assert not acquire(capacity)


def test_expired_dead_pid_does_not_keep_provider_blocked(tmp_path):
    _, ledger, capacity, incident = setup_stores(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    assert ledger.acquire(incident, f"{child.pid}:finished", 60)
    with ledger.connection:
        ledger.connection.execute("UPDATE flow_ui_lease SET expires_at=0")
    assert acquire(capacity)


def test_maintenance_lease_does_not_change_other_provider_admission(tmp_path):
    _, ledger, capacity, incident = setup_stores(tmp_path)
    assert ledger.acquire(incident, "maintenance-worker", 60)
    assert acquire(capacity, "vertex_ai")


def test_existing_provider_renewal_cannot_bypass_active_maintenance(tmp_path):
    _, ledger, capacity, incident = setup_stores(tmp_path)
    assert acquire(capacity)
    with ledger.connection:
        ledger.connection.execute("INSERT INTO flow_ui_lease VALUES(?,?,?,?)",
                                  ("profile", incident, "repair-worker", 99999999999))
    assert not acquire(capacity)


def test_capacity_still_works_when_maintenance_tables_have_never_existed(tmp_path):
    capacity = ProviderCapacityStore(sqlite3.connect(tmp_path / "legacy.sqlite"))
    assert acquire(capacity)


def test_maintenance_and_provider_race_has_only_one_winner(tmp_path):
    database, ledger, capacity, incident = setup_stores(tmp_path)
    ledger.connection.close()
    capacity.connection.close()
    barrier = threading.Barrier(2)
    results, errors = [], []

    def maintenance():
        connection = sqlite3.connect(database, timeout=10)
        try:
            store = RepairLedger(connection)
            barrier.wait(timeout=5)
            results.append(store.acquire(incident, "repair-worker", 60))
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    def provider():
        connection = sqlite3.connect(database, timeout=10)
        try:
            store = ProviderCapacityStore(connection)
            barrier.wait(timeout=5)
            results.append(acquire(store))
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=maintenance), threading.Thread(target=provider)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert errors == []
    assert sorted(results) == [False, True]


def test_pid_probe_handles_self_dead_and_invalid_owners_without_mutation():
    from video_factory.integrations import capacity
    probe = getattr(capacity, "process_lease_owner_alive", None)
    assert probe is not None, "safe shared PID probe is missing"
    assert probe(f"{os.getpid()}:owner")
    assert not probe("not-a-pid")
    assert not probe("-1:owner")


@pytest.mark.parametrize("profile", ["", "other-profile"])
def test_maintenance_exclusion_cannot_be_bypassed_by_different_profile_spelling(tmp_path, profile):
    _, ledger, capacity, incident = setup_stores(tmp_path)
    assert ledger.acquire(incident, "repair-worker", 60)
    assert not capacity.try_acquire(provider_kind="google_flow", operation_id="other", lease_owner="worker",
                                    limit=2, profile_id=profile)
