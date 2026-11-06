from __future__ import annotations

import re
import mimetypes
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from video_factory.application.candidate_api import create_candidate_router
from fastapi.responses import RedirectResponse
from video_factory.application.events_api import EventsAPI, create_events_router
from video_factory.application.health_api import HealthAPI, create_health_router
from video_factory.application.kanban_api import create_kanban_router
from video_factory.application.middleware import (
    TokenBucketRateLimiter,
    configure_security_middleware,
)
from video_factory.application.turn_api import TurnAuthenticator, create_turn_router
try:
    from hermes_personal.gateway.normalizers import ChannelTurnNormalizer
    from hermes_personal.orchestration import HermesTurnService
except ImportError:
    ChannelTurnNormalizer = Any  # type: ignore[misc,assignment]
    HermesTurnService = Any  # type: ignore[misc,assignment]

DIGEST_REGEX = re.compile(r"^sha256:[0-9a-f]{64}$")


def create_application(
    health_api: HealthAPI | None = None,
    events_api: EventsAPI | None = None,
    turn_service: HermesTurnService | None = None,
    turn_normalizers: dict[str, ChannelTurnNormalizer] | None = None,
    turn_authenticator: TurnAuthenticator | None = None,
    max_body_bytes: int = 10 * 1024 * 1024,
    rate_per_minute: float = 120.0,
    burst: int = 60,
    cors_allowed_origins: list[str] | None = None,
    rate_limiter: TokenBucketRateLimiter | None = None,
    enable_security_middleware: bool = True,
    kanban_connection: Any | None = None,
) -> FastAPI:
    """Create and configure the canonical Hermes personal application FastAPI instance."""
    app = FastAPI(
        title="Hermes Personal Application API",
        version="1.0.0",
        description="Unified Health, Events Stream, Operator Commands, and Artifact Metadata API",
    )

    if enable_security_middleware:
        configure_security_middleware(
            app=app,
            max_body_bytes=max_body_bytes,
            rate_per_minute=rate_per_minute,
            burst=burst,
            cors_allowed_origins=cors_allowed_origins,
            rate_limiter=rate_limiter,
        )

    health_router = create_health_router(health_api)
    events_router = create_events_router(events_api)
    turn_router = create_turn_router(
        turn_service=turn_service,
        normalizers=turn_normalizers,
        authenticator=turn_authenticator,
    )
    kanban_router = create_kanban_router(connection=kanban_connection)
    candidate_router = create_candidate_router(connection=kanban_connection)

    app.include_router(health_router)
    app.include_router(events_router)
    app.include_router(turn_router)
    app.include_router(kanban_router)
    app.include_router(candidate_router)

    # Mount the operator dashboard static assets + entrypoint
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        # Windows registry associations can otherwise serve ES modules as text/plain.
        mimetypes.add_type("application/javascript", ".mjs")
        app.mount("/operator/assets", StaticFiles(directory=static_dir), name="operator-assets")
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.get("/operator", include_in_schema=False)
        @app.get("/dashboard", include_in_schema=False)
        async def operator_dashboard() -> FileResponse:
            """Serve the Operator Dashboard HTML entrypoint."""
            index = static_dir / "dashboard.html"
            if not index.exists():
                raise HTTPException(status_code=404, detail="Dashboard assets not built")
            return FileResponse(index)

        @app.get("/studio", include_in_schema=False)
        async def video_factory_studio() -> RedirectResponse:
            """Open the personal Video Factory Studio UI."""
            studio = static_dir / "studio.html"
            if not studio.exists():
                raise HTTPException(status_code=404, detail="Video Factory Studio assets not found")
            return RedirectResponse(url="/operator/assets/studio.html")

        @app.get("/operator/dashboard.js", include_in_schema=False)
        @app.get("/dashboard.js", include_in_schema=False)
        async def operator_dashboard_js() -> FileResponse:
            """Serve dashboard.js directly for convenience."""
            js_file = static_dir / "dashboard.js"
            if not js_file.exists():
                raise HTTPException(status_code=404, detail="dashboard.js not found")
            return FileResponse(js_file, media_type="application/javascript")

    return app
