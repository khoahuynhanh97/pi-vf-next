from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Protocol, get_args

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from hermes_personal.contracts.events import AgentEvent
    from hermes_personal.contracts.operator import OperatorCommand, OperatorCommandType
    from hermes_personal.contracts.principal import PrincipalContext
except ImportError:
    from dataclasses import dataclass, field
    from datetime import datetime, timezone
    from typing import Literal

    OperatorCommandType = Literal["start_workflow", "approve", "reject", "cancel"]

    @dataclass(frozen=True)
    class AgentEvent:
        event_id: str
        event_type: str
        principal_id: str
        session_id: str = ""
        turn_id: str = ""
        sequence: int = 0
        data: Mapping[str, Any] = field(default_factory=dict)
        workflow_id: str | None = None
        job_id: str | None = None
        approval_id: str | None = None
        occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @dataclass(frozen=True)
    class OperatorCommand:
        command_type: str
        target_id: str
        owner_user_id: str = "default_owner"
        payload: dict[str, Any] = field(default_factory=dict)
        expected_version: int | None = None
        idempotency_key: str | None = None

    @dataclass(frozen=True)
    class PrincipalContext:
        principal_id: str = "default_owner"
        roles: tuple[str, ...] = ("operator",)


@dataclass(frozen=True)
class ArtifactRecord:
    """Canonical artifact metadata with immutable versioning and owning context."""

    artifact_id: str
    owner_user_id: str
    artifact_type: str
    version: int
    digest: str
    delivery_ref: str
    metadata: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "owner_user_id": self.owner_user_id,
            "artifact_type": self.artifact_type,
            "version": self.version,
            "digest": self.digest,
            "delivery_ref": self.delivery_ref,
            "metadata": dict(self.metadata),
        }


class DurableEventStore(Protocol):
    """Interface for durable committed Agent Events repository."""

    def get_events_for_owner(self, owner_id: str, after_seq: int = 0, limit: int = 100) -> list[AgentEvent]: ...
    def get_event_records_for_owner(
        self, owner_id: str, after_cursor: int = 0, limit: int = 100
    ) -> list[tuple[int, AgentEvent]]: ...
    def save_event(self, event: AgentEvent) -> None: ...


class InMemoryEventStore:
    """Durable in-memory fallback implementation for committed Agent Events."""

    def __init__(self, initial_events: list[AgentEvent] | None = None) -> None:
        self._events: list[AgentEvent] = []
        self._records: list[tuple[int, AgentEvent]] = []
        self._next_cursor_by_owner: dict[str, int] = {}
        for event in initial_events or []:
            self.save_event(event)

    def save_event(self, event: AgentEvent) -> None:
        self._events.append(event)
        cursor = self._next_cursor_by_owner.get(event.principal_id, 0) + 1
        self._next_cursor_by_owner[event.principal_id] = cursor
        self._records.append((cursor, event))

    def get_event_records_for_owner(
        self, owner_id: str, after_cursor: int = 0, limit: int = 100
    ) -> list[tuple[int, AgentEvent]]:
        return [
            (cursor, event)
            for cursor, event in self._records
            if event.principal_id == owner_id and cursor > after_cursor
        ][:limit]

    def get_events_for_owner(self, owner_id: str, after_seq: int = 0, limit: int = 100) -> list[AgentEvent]:
        return [
            event
            for _, event in self.get_event_records_for_owner(
                owner_id, after_cursor=after_seq, limit=limit
            )
        ]


class EventsAPI:
    """Events, Operator Command, and Artifact Metadata API service for Web and Omni ingress."""

    def __init__(
        self,
        event_store: DurableEventStore | list[AgentEvent] | None = None,
        artifact_store: dict[str, ArtifactRecord] | None = None,
        command_handler: Callable[[OperatorCommand], dict[str, Any]] | None = None,
        auth_validator: Callable[[str, str], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if event_store is None:
            self._store: DurableEventStore = InMemoryEventStore()
        elif isinstance(event_store, list):
            self._store = InMemoryEventStore(event_store)
        else:
            self._store = event_store

        self._artifacts: dict[str, ArtifactRecord] = artifact_store if artifact_store is not None else {}
        self._command_handler = command_handler
        self._auth_validator = auth_validator or (lambda token, owner: False)
        self._clock = clock

    def add_event(self, event: AgentEvent) -> None:
        self._store.save_event(event)

    def add_artifact(self, artifact: ArtifactRecord) -> None:
        self._artifacts[artifact.artifact_id] = artifact

    def get_events(
        self,
        owner_id: str,
        after_seq: int = 0,
        limit: int = 100,
        auth_token: str = "",
    ) -> tuple[int, dict[str, Any]]:
        """Fetch committed events by owner and monotonic sequence for replay/resume."""
        if not self._auth_validator(auth_token, owner_id):
            return 403, {"error": "unauthorized", "message": "Invalid authentication credentials"}

        if after_seq < 0 or limit < 1 or limit > 500:
            return 400, {
                "error": "bad_request",
                "message": "after_seq must be non-negative and limit must be between 1 and 500",
            }

        records = self._store.get_event_records_for_owner(
            owner_id=owner_id, after_cursor=after_seq, limit=limit
        )

        events_data = [
            {
                "cursor": cursor,
                "event_id": e.event_id,
                "event_type": e.event_type,
                "principal_id": e.principal_id,
                "session_id": e.session_id,
                "turn_id": e.turn_id,
                "sequence": e.sequence,
                "data": dict(e.data),
                "workflow_id": e.workflow_id,
                "job_id": e.job_id,
                "approval_id": e.approval_id,
                "occurred_at": e.occurred_at.isoformat(),
            }
            for cursor, e in records
        ]
        next_seq = records[-1][0] if records else after_seq
        return 200, {
            "events": events_data,
            "next_cursor": next_seq,
            "count": len(events_data),
        }

    def format_sse_event(self, event: AgentEvent, cursor: int) -> str:
        """Format an AgentEvent into a Server-Sent Event frame."""
        data_payload = json.dumps({
            "cursor": cursor,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "principal_id": event.principal_id,
            "session_id": event.session_id,
            "turn_id": event.turn_id,
            "sequence": event.sequence,
            "data": dict(event.data),
            "workflow_id": event.workflow_id,
            "job_id": event.job_id,
            "approval_id": event.approval_id,
            "occurred_at": event.occurred_at.isoformat(),
        })
        return f"id: {cursor}\nevent: {event.event_type}\ndata: {data_payload}\n\n"

    def format_sse_heartbeat(self) -> str:
        """Format a separate SSE heartbeat ping."""
        data_payload = json.dumps({"timestamp": self._clock()})
        return f"event: heartbeat\ndata: {data_payload}\n\n"

    def stream_events_generator(
        self,
        owner_id: str,
        after_seq: int = 0,
        auth_token: str = "",
    ) -> Iterator[str]:
        """Generator streaming committed events followed by a separate heartbeat."""
        if not self._auth_validator(auth_token, owner_id):
            yield "event: error\ndata: {\"error\": \"unauthorized\"}\n\n"
            return

        if after_seq < 0:
            yield "event: error\ndata: {\"error\": \"bad_request\"}\n\n"
            return
        records = self._store.get_event_records_for_owner(
            owner_id=owner_id, after_cursor=after_seq, limit=500
        )
        for cursor, event in records:
            yield self.format_sse_event(event, cursor)

        yield self.format_sse_heartbeat()

    def handle_operator_command(
        self,
        auth_token: str,
        owner_id: str,
        command_type: OperatorCommandType,
        target_id: str,
        expected_version: int,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Submit an authenticated operator command (approval, rejection, pause, etc.)."""
        if not self._auth_validator(auth_token, owner_id):
            return 403, {"error": "forbidden", "message": "Operator authentication failed"}

        if not target_id:
            return 400, {"error": "bad_request", "message": "target_id required"}
        if command_type not in get_args(OperatorCommandType):
            return 400, {"error": "bad_request", "message": "unsupported command_type"}
        if not isinstance(expected_version, int) or expected_version < 0:
            return 400, {"error": "bad_request", "message": "expected_version must be non-negative"}

        principal = PrincipalContext(
            actor_id=owner_id,
            owner_user_id=owner_id,
            channel="web",
            session_id=f"op_{owner_id}",
        )
        cmd = OperatorCommand(
            principal=principal,
            command_type=command_type,
            target_id=target_id,
            expected_version=expected_version,
            payload=payload or {},
        )

        if self._command_handler is not None:
            try:
                res = self._command_handler(cmd)
                return 200, {"status": "ok", "command_id": cmd.command_id, "result": res}
            except Exception as ex:
                return 400, {"error": "command_failed", "message": str(ex)}

        return 200, {"status": "accepted", "command_id": cmd.command_id}

    def get_artifact(
        self,
        artifact_id: str,
        owner_id: str,
        auth_token: str = "",
    ) -> tuple[int, dict[str, Any]]:
        """Retrieve canonical artifact metadata with owner isolation."""
        if not self._auth_validator(auth_token, owner_id):
            return 403, {"error": "forbidden", "message": "Authentication required"}

        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            return 404, {"error": "not_found", "message": f"Artifact '{artifact_id}' not found"}

        if artifact.owner_user_id != owner_id:
            return 403, {"error": "forbidden", "message": "Access denied to foreign artifact"}

        return 200, artifact.as_dict()


def create_events_router(events_api: EventsAPI | None = None) -> APIRouter:
    """Create FastAPI router for /api/events, /api/operator/command, and /api/artifacts endpoints."""
    router = APIRouter(prefix="/api", tags=["events"])
    api = events_api or EventsAPI()

    @router.get("/events")
    def stream_events(
        owner_id: str = Query(...),
        after_seq: int = Query(0, ge=0),
        auth_token: str = Query(""),
        authorization: str | None = Header(None),
    ) -> StreamingResponse:
        token = auth_token or (authorization.replace("Bearer ", "") if authorization else "")
        return StreamingResponse(
            api.stream_events_generator(owner_id=owner_id, after_seq=after_seq, auth_token=token),
            media_type="text/event-stream",
        )

    @router.get("/events/replay")
    def replay_events(
        owner_id: str = Query(...),
        after_seq: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        auth_token: str = Query(""),
        authorization: str | None = Header(None),
    ) -> JSONResponse:
        token = auth_token or (authorization.replace("Bearer ", "") if authorization else "")
        status_code, body = api.get_events(owner_id=owner_id, after_seq=after_seq, limit=limit, auth_token=token)
        return JSONResponse(status_code=status_code, content=body)

    @router.post("/operator/command")
    async def submit_operator_command(
        request: Request,
        authorization: str | None = Header(None),
    ) -> JSONResponse:
        data = await request.json()
        token = (authorization.replace("Bearer ", "") if authorization else "") or data.get("auth_token")
        owner_id = data.get("owner_id", "")
        command_type = data.get("command_type", "")
        target_id = data.get("target_id", "")
        expected_version = data.get("expected_version", 1)
        payload = data.get("payload", {})

        status_code, body = api.handle_operator_command(
            auth_token=token,
            owner_id=owner_id,
            command_type=command_type,
            target_id=target_id,
            expected_version=expected_version,
            payload=payload,
        )
        return JSONResponse(status_code=status_code, content=body)

    @router.get("/artifacts/{artifact_id}")
    def get_artifact_metadata(
        artifact_id: str,
        owner_id: str = Query(...),
        auth_token: str = Query(""),
        authorization: str | None = Header(None),
    ) -> JSONResponse:
        token = auth_token or (authorization.replace("Bearer ", "") if authorization else "")
        status_code, body = api.get_artifact(artifact_id=artifact_id, owner_id=owner_id, auth_token=token)
        return JSONResponse(status_code=status_code, content=body)

    return router
