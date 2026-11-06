from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


KNOWN_TOPICS: frozenset[str] = frozenset({"job.dispatch"})


class UnknownTopicError(ValueError):
    """Raised when an envelope topic is not in KNOWN_TOPICS."""


class MalformedEnvelopeError(ValueError):
    """Raised when an envelope cannot be parsed or is missing required fields."""


@dataclass(frozen=True)
class OutboxEnvelope:
    """Typed outbox envelope for structured workflow side-effects."""

    message_id: str
    workflow_id: str
    topic: str
    idempotency_key: str
    schema_version: str
    payload: dict[str, Any] = field(default_factory=dict)


def parse_envelope(raw_message: str) -> OutboxEnvelope:
    """Parse and validate a raw JSON string into an OutboxEnvelope.

    Raises:
        MalformedEnvelopeError: if JSON is invalid or required fields are missing.
        UnknownTopicError: if topic is not in KNOWN_TOPICS.
    """
    try:
        data = json.loads(raw_message)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MalformedEnvelopeError(f"Invalid JSON: {exc}") from exc

    required = {"message_id", "workflow_id", "topic", "idempotency_key", "schema_version", "payload"}
    missing = required - set(data.keys())
    if missing:
        raise MalformedEnvelopeError(f"Missing required fields: {sorted(missing)}")

    topic = data["topic"]
    if topic not in KNOWN_TOPICS:
        raise UnknownTopicError(f"Unknown topic: {topic!r}. Known topics: {sorted(KNOWN_TOPICS)}")

    payload = data["payload"]
    if not isinstance(payload, dict):
        raise MalformedEnvelopeError(f"payload must be a dict, got {type(payload).__name__}")

    return OutboxEnvelope(
        message_id=str(data["message_id"]),
        workflow_id=str(data["workflow_id"]),
        topic=topic,
        idempotency_key=str(data["idempotency_key"]),
        schema_version=str(data["schema_version"]),
        payload=payload,
    )


def build_envelope(
    workflow_id: str,
    topic: str,
    idempotency_key: str,
    payload: dict[str, Any],
    schema_version: str = "1",
) -> str:
    """Build a JSON envelope string ready for storage in workflow_outbox.message."""
    return json.dumps(
        {
            "message_id": str(uuid.uuid4()),
            "workflow_id": workflow_id,
            "topic": topic,
            "idempotency_key": idempotency_key,
            "schema_version": schema_version,
            "payload": payload,
        }
    )
