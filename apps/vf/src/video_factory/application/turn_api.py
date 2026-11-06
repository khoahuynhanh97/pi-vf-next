from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from hermes_personal.contracts import AgentTurnRequest, to_primitive
    from hermes_personal.gateway.normalizers import ChannelTurnNormalizer
    from hermes_personal.orchestration import HermesTurnService, TurnInProgress
except ImportError:
    AgentTurnRequest = Any  # type: ignore[misc,assignment]
    to_primitive = lambda x: x  # type: ignore[assignment]
    ChannelTurnNormalizer = Any  # type: ignore[misc,assignment]
    HermesTurnService = Any  # type: ignore[misc,assignment]
    TurnInProgress = Exception  # type: ignore[misc,assignment]


@dataclass(frozen=True)
class AuthenticatedTurnIdentity:
    owner_user_id: str
    external_subject: str


class TurnAuthenticator(Protocol):
    def authenticate(self, token: str) -> AuthenticatedTurnIdentity | None: ...


class StaticBearerAuthenticator:
    """Bind one deployment API key to one configured Hermes owner."""

    def __init__(self, api_key: str, owner_user_id: str, external_subject: str) -> None:
        if not api_key or not owner_user_id.strip() or not external_subject.strip():
            raise ValueError("api_key, owner_user_id, and external_subject are required")
        self._api_key = api_key
        self._identity = AuthenticatedTurnIdentity(owner_user_id, external_subject)

    def authenticate(self, token: str) -> AuthenticatedTurnIdentity | None:
        if token and hmac.compare_digest(token, self._api_key):
            return self._identity
        return None


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _error(status_code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error, "message": message})


def create_turn_router(
    *,
    turn_service: HermesTurnService | None,
    normalizers: Mapping[str, ChannelTurnNormalizer] | None,
    authenticator: TurnAuthenticator | None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["turns"])
    configured_normalizers = dict(normalizers or {})

    async def submit(
        channel: str,
        request: Request,
        authorization: str | None,
        idempotency_key: str | None,
    ):
        if turn_service is None or authenticator is None:
            return _error(503, "service_unavailable", "Hermes Turn service is not configured")

        identity = authenticator.authenticate(_bearer_token(authorization))
        if identity is None:
            return _error(401, "unauthorized", "Valid bearer authentication is required")

        normalizer = configured_normalizers.get(channel)
        if normalizer is None:
            return _error(503, "service_unavailable", f"{channel} ingress is not configured")

        try:
            body = await request.json()
        except Exception:
            return _error(400, "bad_request", "Request body must be valid JSON")
        if not isinstance(body, dict):
            return _error(400, "bad_request", "Request body must be an object")
        requested_owner = body.get("owner_id")
        if requested_owner is not None and requested_owner != identity.owner_user_id:
            return _error(403, "forbidden", "Authenticated owner does not match request owner")

        try:
            turn_request: AgentTurnRequest = normalizer.normalize(
                external_subject=identity.external_subject,
                external_conversation_id=body.get("conversation_id", ""),
                message=body.get("message", ""),
                authenticated_idempotency_key=idempotency_key or body.get("idempotency_key"),
                workflow_id=body.get("workflow_id"),
                locale=body.get("locale", "vi-VN"),
            )
        except PermissionError as error:
            return _error(403, "forbidden", str(error))
        except (TypeError, ValueError) as error:
            return _error(400, "bad_request", str(error))

        async def stream():
            try:
                async for event in turn_service.handle(turn_request):
                    yield json.dumps(
                        to_primitive(event),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n"
            except TurnInProgress as error:
                failed_event = {
                    "event_type": "failed",
                    "principal_id": turn_request.principal.owner_user_id,
                    "session_id": turn_request.session_id,
                    "turn_id": turn_request.turn_id,
                    "sequence": 0,
                    "data": {
                        "error": f"Turn in progress for session: {error}",
                        "code": "TURN_IN_PROGRESS",
                    },
                    "workflow_id": turn_request.workflow_id,
                }
                yield json.dumps(
                    failed_event,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @router.post("/turn")
    async def submit_web_turn(
        request: Request,
        authorization: str | None = Header(None),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return await submit("web", request, authorization, idempotency_key)

    @router.post("/omni/turn")
    async def submit_omni_turn(
        request: Request,
        authorization: str | None = Header(None),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return await submit("omni", request, authorization, idempotency_key)

    return router
