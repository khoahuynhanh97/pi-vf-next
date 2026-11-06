from __future__ import annotations

from typing import Any
from fastapi import APIRouter
from fastapi.responses import JSONResponse

try:
    from hermes_personal.runtime.readiness import ReadinessProbe, ReadinessState
except ImportError:
    from dataclasses import dataclass

    @dataclass
    class ReadinessState:
        is_live: bool = True
        is_ready: bool = True
        failure_code: str | None = None
        component_states: dict | None = None

    class ReadinessProbe:
        def evaluate(self) -> ReadinessState:
            return ReadinessState()


class HealthAPI:
    """HTTP endpoint handlers for runtime liveness, readiness, and component health."""

    def __init__(self, readiness_probe: ReadinessProbe | None = None) -> None:
        self._probe = readiness_probe

    def set_readiness_probe(self, probe: ReadinessProbe) -> None:
        self._probe = probe

    def handle_live(self) -> tuple[int, dict[str, Any]]:
        """Liveness probe: verifies process is running and responding."""
        if self._probe is not None:
            state = self._probe.evaluate()
            if not state.is_live:
                return 503, {"status": "error", "live": False}
        return 200, {"status": "ok", "live": True}

    def handle_ready(self) -> tuple[int, dict[str, Any]]:
        """Readiness probe: verifies all 8 startup steps, dependency probes, and components are ready."""
        if self._probe is None:
            return 503, {
                "status": "not_ready",
                "ready": False,
                "failure_code": "NO_READINESS_PROBE",
            }

        state: ReadinessState = self._probe.evaluate()
        state_dict = state.as_dict()

        if state.is_ready:
            return 200, {
                "status": "ready",
                "ready": True,
                "details": state_dict,
            }

        return 503, {
            "status": "not_ready",
            "ready": False,
            "failure_code": state.startup_report.failure_code or "NOT_READY",
            "details": state_dict,
        }

    def handle_component(self, component_name: str) -> tuple[int, dict[str, Any]]:
        """Query health of a specific named component."""
        if self._probe is None:
            return 503, {"component": component_name, "status": "unavailable"}

        status = self._probe.evaluate_component(component_name)
        status_code = 200 if status in ("healthy", "degraded") else 503
        return status_code, {
            "component": component_name,
            "status": status,
        }


def create_health_router(health_api: HealthAPI | None = None) -> APIRouter:
    """Create FastAPI router for /health endpoints."""
    router = APIRouter(prefix="/health", tags=["health"])
    api = health_api or HealthAPI()

    @router.get("/live")
    def get_live() -> JSONResponse:
        status_code, body = api.handle_live()
        return JSONResponse(status_code=status_code, content=body)

    @router.get("/ready")
    def get_ready() -> JSONResponse:
        status_code, body = api.handle_ready()
        return JSONResponse(status_code=status_code, content=body)

    @router.get("/components/{name}")
    def get_component(name: str) -> JSONResponse:
        status_code, body = api.handle_component(name)
        return JSONResponse(status_code=status_code, content=body)

    return router
