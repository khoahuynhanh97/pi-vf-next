"""Keep the ordinary test suite from ever reaching the local paid-model router."""

from __future__ import annotations

import socket

import pytest


def _is_live_nine_router_address(address: object) -> bool:
    return (
        isinstance(address, tuple)
        and len(address) >= 2
        and str(address[0]).lower().strip("[]") in {"127.0.0.1", "localhost", "::1"}
        and str(address[1]) == "20128"
    )


@pytest.fixture(autouse=True)
def block_live_nine_router_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block and fail tests that attempt the real local 9Router HTTP endpoint."""
    attempted: list[object] = []
    original_create_connection = socket.create_connection
    original_socket_connect = socket.socket.connect

    def guarded_create_connection(address: object, *args: object, **kwargs: object) -> socket.socket:
        if _is_live_nine_router_address(address):
            attempted.append(address)
            raise AssertionError("Offline tests must not connect to the live 9Router endpoint")
        return original_create_connection(address, *args, **kwargs)  # type: ignore[arg-type]

    def guarded_socket_connect(sock: socket.socket, address: object) -> None:
        if _is_live_nine_router_address(address):
            attempted.append(address)
            raise AssertionError("Offline tests must not connect to the live 9Router endpoint")
        original_socket_connect(sock, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", guarded_socket_connect)
    yield
    assert not attempted, f"Test attempted a live 9Router connection: {attempted!r}"


@pytest.fixture(autouse=True)
def block_network_for_four_agent_contract_tests(request, monkeypatch):
    """New four-agent tests must use injected transports, including acquisition/QC."""
    files={'test_agent_contracts.py','test_creative_playbooks.py','test_agent_media_inputs.py',
        'test_four_agent_creative.py','test_creative_preview.py','test_preproduction_review.py',
        'test_storyboard_sequence_qc.py','test_agent_repair_policy.py','test_creative_auto_export.py',
        'test_silent_final_contract.py','test_muse_adapter.py','test_creative_resume.py',
        'test_creative_acquisition_e2e.py','test_export_integrity.py',
        'test_crash_recovery_boundaries.py','test_scene_render_runtime.py',
        'test_storyboard_compiler.py','test_keyframe_pipeline.py','test_keyframe_targeted_recovery.py',
        'test_video_qc_recovery.py','test_restart_recovery.py','test_video_qc.py',
        'test_export_package.py','test_auto_qc_recovery.py','test_creative_handler_wiring.py'}
    if request.node.path.name not in files:
        return
    def denied(*args,**kwargs):
        raise AssertionError('Four-agent offline tests cannot open network connections; inject a fake transport')
    monkeypatch.setattr(socket,'create_connection',denied)
    monkeypatch.setattr(socket.socket,'connect',denied)
