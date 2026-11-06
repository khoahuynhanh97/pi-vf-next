from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from video_factory.job_contracts import JobDeferred
from video_factory.workflows.envelope import (
    MalformedEnvelopeError,
    OutboxEnvelope,
    UnknownTopicError,
    parse_envelope,
)
from video_factory.workflows.outbox_repository import (
    OutboxClaim,
    OutboxRepository,
)


@dataclass
class DispatchReport:
    claimed: int
    succeeded: int
    failed: int
    quarantined: int
    stale: int = 0
    deferred: int = 0


class FatalPersistenceError(RuntimeError):
    """Raised when durable persistence fails and the worker must stop."""


class OutboxDispatcher:
    """Durable outbox dispatcher with typed envelope routing.

    Two operating modes:

    1. **Topic-handler mode** (preferred): pass ``topic_handlers``.
       Each claim's message is parsed as an OutboxEnvelope.  The envelope's
       topic is looked up in ``topic_handlers``.  If the handler is missing or
       the envelope is malformed, ``record_failure`` is called (fail closed) –
       the row is NOT deleted.

    2. **Legacy raw mode**: pass ``delivery_fn`` only.
       ``delivery_fn(workflow_id, message)`` is called directly.  Used by
       existing tests.  If neither ``delivery_fn`` nor ``topic_handlers`` is
       provided, every claim fails with ``delivery_unconfigured``.

    ACK semantics:
    - ``ack()`` returning ``False`` (stale/expired lease) increments ``stale``,
      NOT ``succeeded``.  A stale outcome is never counted as success.
    """

    def __init__(
        self,
        repository: OutboxRepository,
        delivery_fn: Callable[[str, str], None] | None = None,
        worker_id: str = "worker-1",
        lease_duration_seconds: float = 30.0,
        backoff_base_seconds: float = 2.0,
        max_backoff_seconds: float = 300.0,
        topic_handlers: Dict[str, Callable[[OutboxEnvelope], None]] | None = None,
    ):
        self.repository = repository
        self.delivery_fn = delivery_fn
        self.worker_id = worker_id
        self.lease_duration_seconds = lease_duration_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.topic_handlers = topic_handlers

    def process_batch(
        self,
        batch_size: int = 10,
        now_iso: str | None = None,
        workflow_id_prefix: str | None = None,
        workflow_ids: list[str] | None = None,
    ) -> DispatchReport:
        claims = self.repository.claim(
            self.worker_id,
            self.lease_duration_seconds,
            batch_size,
            now_iso,
            workflow_id_prefix=workflow_id_prefix,
            workflow_ids=workflow_ids,
        )
        claimed_count = len(claims)
        succeeded = 0
        failed = 0
        quarantined = 0
        stale = 0
        deferred = 0

        for claim in claims:
            delivery_ok = False
            try:
                self._deliver_claim(claim)
                delivery_ok = True
            except JobDeferred as ex:
                try:
                    deferred_ok = self.repository.defer(
                        claim.outbox_id, claim.lease_token, self.worker_id,
                        ex.delay_seconds, now_iso,
                    )
                except Exception as db_ex:
                    raise FatalPersistenceError(f"Failed to persist outbox deferral: {db_ex}") from db_ex
                if deferred_ok:
                    deferred += 1
                else:
                    stale += 1
                continue
            except Exception as ex:
                retryable = getattr(ex, "retryable", True)
                force_quarantine = not retryable
                try:
                    status = self.repository.record_failure(
                        claim.outbox_id,
                        claim.lease_token,
                        self.worker_id,
                        str(ex),
                        self.backoff_base_seconds,
                        self.max_backoff_seconds,
                        now_iso,
                        force_quarantine=force_quarantine,
                    )
                except Exception as db_ex:
                    raise FatalPersistenceError(f"Failed to persist outbox error state: {db_ex}") from db_ex

                if status == "quarantined":
                    quarantined += 1
                    # Observe only after failure persistence/browser unwinding.
                    # This records maintenance metadata; it cannot Send/retry.
                    try:
                        from video_factory.maintenance.events import observe_failure
                        observe_failure(self.repository.connection, claim, ex, status)
                    except Exception as maintenance_error:
                        import logging
                        logging.getLogger(__name__).warning('UI incident capture failed (%s)', type(maintenance_error).__name__)
                elif status in ("stale", "stale_lease"):
                    stale += 1
                else:
                    failed += 1
                continue

            if delivery_ok:
                ack_ok = self.repository.ack(
                    claim.outbox_id, claim.lease_token, self.worker_id, now_iso
                )
                if ack_ok:
                    succeeded += 1
                else:
                    # Lease was stolen or expired during delivery – stale outcome.
                    # NOT counted as succeeded.
                    stale += 1

        return DispatchReport(
            claimed=claimed_count,
            succeeded=succeeded,
            failed=failed,
            quarantined=quarantined,
            stale=stale,
            deferred=deferred,
        )

    def _deliver_claim(self, claim: OutboxClaim) -> None:
        """Route a claim to the appropriate handler.

        Raises on any failure so process_batch can call record_failure.
        """
        if self.topic_handlers is not None:
            # Typed envelope routing – parse first; raise on bad envelope.
            try:
                envelope = parse_envelope(claim.message)
            except (MalformedEnvelopeError, UnknownTopicError) as exc:
                raise exc  # -> record_failure -> retry/quarantine

            handler = self.topic_handlers.get(envelope.topic)
            if handler is None:
                raise RuntimeError(
                    f"delivery_unconfigured: no handler registered for topic {envelope.topic!r}"
                )
            hb = lambda ext=600.0: self.repository.extend_lease(
                claim.outbox_id, claim.lease_token, self.worker_id, extension_seconds=ext
            )
            import inspect
            sig = inspect.signature(handler)
            accepts_heartbeat = (
                "heartbeat_fn" in sig.parameters
                or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            )
            if accepts_heartbeat:
                handler(envelope, heartbeat_fn=hb)
            else:
                handler(envelope)
        else:
            # Legacy raw delivery mode (backward compat for tests).
            if self.delivery_fn is None:
                raise RuntimeError(
                    "delivery_unconfigured: no delivery_fn or topic_handlers configured"
                )
            self.delivery_fn(claim.workflow_id, claim.message)

    def reclaim_expired(self, now_iso: str | None = None) -> int:
        return self.repository.reclaim_expired(now_iso)
