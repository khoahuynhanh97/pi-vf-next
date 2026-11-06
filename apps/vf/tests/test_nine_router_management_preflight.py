from urllib.error import HTTPError, URLError
import json
import sqlite3

import pytest

from video_factory.integrations import nine_router
from video_factory.qc.service import _run_configured_preflight


def test_local_route_metadata_uses_router_data_dir_override(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "router-data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("APPDATA", str(tmp_path / "different-profile"))
    database_path = data_dir / "db" / "data.sqlite"
    database_path.parent.mkdir()
    with sqlite3.connect(database_path) as database:
        database.execute("CREATE TABLE combos (id TEXT PRIMARY KEY, models TEXT NOT NULL)")
        database.execute("CREATE TABLE settings (data TEXT NOT NULL)")
        database.execute(
            "INSERT INTO combos(id, models) VALUES (?, ?)",
            ("combo-1", json.dumps(["gemini/gemini-3.6-flash"])),
        )
        database.execute("INSERT INTO settings(data) VALUES (?)", (json.dumps({}),))

    route = nine_router.NineRouterRoute.from_mapping(
        "vision_qc",
        {
            "enabled": True,
            "endpoint": "http://127.0.0.1:20128/v1",
            "combo_id": "combo-1",
            "combo_name": "Vision-QC-Check",
            "model_id": "gemini/gemini-3.6-flash",
            "execution_mode": "single_model",
        },
    )
    object.__setattr__(route, "expected_members", ("gemini/gemini-3.6-flash",))
    object.__setattr__(
        route,
        "member_capabilities",
        {"gemini/gemini-3.6-flash": ["json_schema", "vision_single_image"]},
    )

    nine_router.verify_local_router_route(route)
    assert nine_router._router_data_dir() == data_dir


def test_missing_live_combo_is_pre_send_and_reports_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nine_router, "_admin_token", lambda: "test-token")

    def not_found(*_args, **_kwargs):
        raise HTTPError("http://127.0.0.1:20128/api/combos/stale", 404, "Not Found", None, None)

    monkeypatch.setattr(nine_router.urllib.request, "urlopen", not_found)
    with pytest.raises(nine_router.NineRouterConfigurationError, match=r"/api/combos/stale returned HTTP 404"):
        nine_router._admin_get("/api/combos/stale")

    class Evaluator:
        def configured_preflight(self):
            return nine_router._admin_get("/api/combos/stale")

    reason = _run_configured_preflight(Evaluator())
    assert reason == (
        "Configured QC preflight failed before provider request: "
        "9Router management /api/combos/stale returned HTTP 404"
    )


def test_management_transport_error_reports_safe_exception_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nine_router, "_admin_token", lambda: "test-token")

    def unavailable(*_args, **_kwargs):
        raise URLError(ConnectionRefusedError("private detail is not surfaced"))

    monkeypatch.setattr(nine_router.urllib.request, "urlopen", unavailable)
    with pytest.raises(
        nine_router.NineRouterConfigurationError,
        match="local management API could not verify the route \\(ConnectionRefusedError\\)",
    ) as raised:
        nine_router._admin_get("/api/settings")

    assert "private detail" not in str(raised.value)
