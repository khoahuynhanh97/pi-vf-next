"""hermes_personal/application/server.py

Consolidated application server module.

This module combines application creation and proxy server functionality
for the Hermes Personal runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.staticfiles import StaticFiles

from video_factory.application.app import create_application

DIGEST_REGEX = re.compile(r"^sha256:[0-9a-f]{64}$")


def compute_static_bundle_digest(directory: Path) -> str:
    """Compute deterministic SHA-256 digest over all static assets.

    Scans the directory recursively, sorts relative POSIX paths stably, and computes
    a SHA-256 digest over the path and contents of each file. Rejects missing directories
    and directories missing an index.html file.
    """
    if not directory.exists() or not directory.is_dir():
        raise ValueError(f"Static bundle directory does not exist or is not a directory: {directory}")
    index_file = directory / "index.html"
    if not index_file.exists() or not index_file.is_file():
        raise ValueError(f"Static bundle directory is missing index.html: {directory}")

    entries: list[tuple[str, bytes]] = []
    for path in directory.rglob("*"):
        if path.is_file():
            rel_posix = path.relative_to(directory).as_posix()
            entries.append((rel_posix, path.read_bytes()))

    entries.sort(key=lambda item: item[0])
    hasher = hashlib.sha256()
    for rel_path, file_bytes in entries:
        hasher.update(rel_path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(file_bytes)
        hasher.update(b"\x00")
    return f"sha256:{hasher.hexdigest()}"


def verify_static_bundle_digest(directory: Path, expected_digest: str) -> None:
    """Verify the static bundle digest matches expected value, raise if mismatched."""
    if not directory.exists() or not directory.is_dir():
        raise ValueError(f"Static bundle directory does not exist or is not a directory: {directory}")
    index_file = directory / "index.html"
    if not index_file.exists() or not index_file.is_file():
        raise ValueError(f"Static bundle directory is missing index.html: {directory}")
    if not DIGEST_REGEX.match(expected_digest):
        raise ValueError(f"Malformed static digest: {expected_digest}")
    actual_digest = compute_static_bundle_digest(directory)
    if not hmac.compare_digest(expected_digest, actual_digest):
        raise ValueError(
            f"Static bundle digest mismatch: expected {expected_digest}, got {actual_digest}"
        )


def create_proxy_application(
    gateway_url: str,
    static_dir: Path | None = None,
    api_key: str = "",
    owner_user_id: str = "local-owner",
    browser_session_token: str = "",
    static_digest: str | None = None,
) -> FastAPI:
    """Create a FastAPI application that proxies requests to the Hermes gateway.

    This application provides the public HTTP API surface that proxies to the
    internal gateway service. It handles static file serving, authentication,
    and request proxying.
    """
    # Validate and configure static files
    verified_static_dir: Path | None = None
    if static_dir is not None:
        if not static_dir.exists():
            raise ValueError(f"Static directory does not exist: {static_dir}")
        if static_digest is None:
            raise ValueError(
                "static_digest is required when static_dir is provided. "
                "Run 'python -c \"from hermes_personal.api.server import compute_static_bundle_digest; "
                "print(compute_static_bundle_digest(Path('path/to/static')))\"' to compute it."
            )
        verify_static_bundle_digest(static_dir, static_digest)
        verified_static_dir = static_dir

    app = create_application(
        max_body_bytes=10 * 1024 * 1024,
        rate_per_minute=120.0,
        burst=60,
        enable_security_middleware=True,
    )

    static_path = "/_static/"
    static_mount_path = f"/{static_path.strip('/')}"

    async def proxy_request(
        target_url: str,
        request: Request,
        timeout: float = 30.0,
    ) -> StreamingResponse:
        incoming_headers = [(k, v) for k, v in request.headers.items() if k.lower() != "host"]
        incoming_headers.append(("X-Hermes-Owner-User-Id", owner_user_id))
        if api_key:
            incoming_headers.append(("Authorization", f"Bearer {api_key}"))

        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        return StreamingResponse(
            client.build_request(
                method=request.method,
                url=target_url,
                headers=incoming_headers,
                content=await request.body(),
            ),
            media_type=None,
        )

    async def proxy_stream(request: Request, endpoint: str) -> Response:
        target_url = f"{gateway_url}{endpoint}"
        return await proxy_request(target_url, request)

    async def proxy_buffered(request: Request, endpoint: str) -> Response:
        target_url = f"{gateway_url}{endpoint}"
        try:
            incoming_headers = [(k, v) for k, v in request.headers.items() if k.lower() != "host"]
            incoming_headers.append(("X-Hermes-Owner-User-Id", owner_user_id))
            if api_key:
                incoming_headers.append(("Authorization", f"Bearer {api_key}"))

            client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
            target_req = client.build_request(
                method=request.method,
                url=target_url,
                headers=incoming_headers,
                content=await request.body(),
            )
            target_resp = await client.send(target_req, stream=False)
            content = target_resp.content
            status_code = target_resp.status_code
            response_headers = dict(target_resp.headers)
            response_headers.pop("transfer-encoding", None)

            return Response(
                content=content,
                status_code=status_code,
                headers=response_headers,
                media_type=response_headers.get("content-type"),
            )
        except httpx.ConnectError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Gateway unavailable",
                    "detail": str(exc),
                },
            )

    @app.post("/api/turn")
    async def turn(request: Request):
        return await proxy_stream(request, "/api/turn")

    @app.post("/api/omni/turn")
    async def omni_turn(request: Request):
        return await proxy_stream(request, "/api/omni/turn")

    @app.api_route("/api/events", methods=["GET"])
    @app.api_route("/api/events/replay", methods=["GET"])
    @app.api_route("/api/operator/command", methods=["POST"])
    async def events(request: Request):
        return await proxy_buffered(request, request.url.path)

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact(artifact_id: str, request: Request):
        return await proxy_buffered(request, f"/api/artifacts/{artifact_id}")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        token = browser_session_token or ""
        owner = owner_user_id or "local-owner"

        base_html = ""
        if verified_static_dir is not None and (verified_static_dir / "index.html").exists():
            base_html = (verified_static_dir / "index.html").read_text(encoding="utf-8")
        else:
            base_html = "<!DOCTYPE html><html><head></head><body><div id='app'></div></body></html>"

        injection = (
            f'<script>\n'
            f'window.__HERMES_SESSION_TOKEN__="{token}";\n'
            f'window.__HERMES_OWNER_ID__="{owner}";\n'
            f'document.cookie = "hermes_session={token}; path=/; SameSite=Strict";\n'
            f'</script>'
        )

        if "</head>" in base_html:
            content = base_html.replace("</head>", f"{injection}\n</head>", 1)
        elif "<body>" in base_html:
            content = base_html.replace("<body>", f"<body>\n{injection}", 1)
        else:
            content = f"{injection}\n{base_html}"

        resp = HTMLResponse(content=content, status_code=200)
        if token:
            resp.set_cookie("hermes_session", token, path="/", samesite="strict")
        return resp

    if verified_static_dir is not None:
        app.mount("/", StaticFiles(directory=verified_static_dir, html=True), name="web")
    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes public application API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--gateway-port", type=int, default=8642)
    parser.add_argument("--static-dir", default=None)
    parser.add_argument("--static-digest", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    app = create_proxy_application(
        f"http://127.0.0.1:{args.gateway_port}",
        Path(args.static_dir) if args.static_dir else None,
        api_key=os.environ.get("HERMES_API_KEY", ""),
        owner_user_id=os.environ.get("HERMES_OWNER_ID", "local-owner"),
        static_digest=args.static_digest,
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


__all__ = [
    "compute_static_bundle_digest",
    "verify_static_bundle_digest",
    "create_proxy_application",
    "create_application",
    "main",
]