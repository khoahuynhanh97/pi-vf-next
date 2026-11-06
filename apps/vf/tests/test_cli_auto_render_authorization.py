from datetime import datetime, timedelta, timezone
from argparse import Namespace

from video_factory.cli import _auto_render_authorization_error, _render_authorization_for_args


def test_auto_render_requires_persisted_scoped_budget() -> None:
    workflow_id = "pv-test"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert _auto_render_authorization_error({}, workflow_id)
    assert _auto_render_authorization_error({"qc_authorization": {
        "workflow_id": workflow_id,
        "scope": ["image"],
        "max_cost_usd": 5,
        "expires_at": future,
    }}, workflow_id)
    assert _auto_render_authorization_error({"qc_authorization": {
        "workflow_id": workflow_id,
        "scope": ["video"],
        "max_cost_usd": 0.49,
        "expires_at": future,
    }}, workflow_id)


def test_auto_render_accepts_current_video_authorization() -> None:
    auth = {
        "workflow_id": "pv-test",
        "scope": ["video"],
        "max_cost_usd": 5,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    assert _auto_render_authorization_error({"qc_authorization": auth}, "pv-test") is None


def test_auto_render_rejects_authorization_above_vertex_pilot_cap() -> None:
    auth = {
        "workflow_id": "pv-test",
        "scope": ["video"],
        "max_cost_usd": 100.01,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    assert "100 USD pilot cap" in _auto_render_authorization_error(
        {"qc_authorization": auth}, "pv-test"
    )


def test_unlimited_qc_authorization_does_not_bypass_render_budget() -> None:
    auth = {
        "workflow_id": "pv-test",
        "scope": ["video"],
        "qc_budget_mode": "unlimited",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    assert _auto_render_authorization_error({"qc_authorization": auth}, "pv-test")


def test_authorize_cost_cli_builds_workflow_scoped_vertex_pilot_grant() -> None:
    auth = _render_authorization_for_args(Namespace(authorize_cost=True), "pv-test")
    assert auth is not None
    assert auth["workflow_id"] == "pv-test"
    assert auth["scope"] == ["video"]
    assert auth["workflow_max_cost_usd"] == 100.0
    assert auth["campaign_max_cost_usd"] == 100.0
    assert _auto_render_authorization_error({"qc_authorization": auth}, "pv-test") is None


def test_authorize_cost_cli_does_not_allow_over_cap_or_non_video_scope() -> None:
    too_large = Namespace(
        authorize_cost=True, qc_authorization_max_cost_usd=101.0,
        qc_authorization_scope=["video"], qc_authorization_expires_at=None,
        qc_authorization_run_id=None, qc_authorization_campaign_id=None, qc_budget_mode=None,
    )
    try:
        _render_authorization_for_args(too_large, "pv-test")
    except ValueError as exc:
        assert "pilot cap" in str(exc)
    else:
        raise AssertionError("over-cap render authorization should be rejected")

    wrong_scope = Namespace(
        authorize_cost=True, qc_authorization_max_cost_usd=5.0,
        qc_authorization_scope=["image"], qc_authorization_expires_at=None,
        qc_authorization_run_id=None, qc_authorization_campaign_id=None, qc_budget_mode=None,
    )
    try:
        _render_authorization_for_args(wrong_scope, "pv-test")
    except ValueError as exc:
        assert "scope" in str(exc)
    else:
        raise AssertionError("non-video render authorization should be rejected")
