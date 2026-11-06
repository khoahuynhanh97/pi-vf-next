"""Stable job contracts and execution errors for Product Video jobs.

This module owns the small, dependency-light value objects and error types
shared by job handlers, adapters, workers, and tests.  The historical
``product_video.handlers`` import path remains compatible because handlers
re-exports these imported names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class JobContext:
    workflow_id: str
    job_id: str
    owner_user_id: str = "user"
    expected_version: int | None = None
    expected_digests: dict[str, str] | None = None
    idempotency_key: str | None = None
    heartbeat_fn: Callable[[], bool] | None = None


@dataclass(frozen=True)
class JobSucceeded:
    artifact_ref: Any
    next_command: dict | None = None


@dataclass(frozen=True)
class JobFailed:
    retryable: bool
    code: str
    message: str | None = None
    artifact_ref: Any | None = None


class JobDeferred(RuntimeError):
    """Accepted work is still running; schedule another delivery without failure."""

    def __init__(self, message: str, delay_seconds: float):
        super().__init__(message)
        self.delay_seconds = delay_seconds


class JobDispatchError(RuntimeError):
    """Base error for job dispatch failures."""

    def __init__(self, message: str, code: str = "JOB_FAILED", retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TerminalJobError(JobDispatchError):
    """Non-retryable terminal job error requiring quarantine and operator intervention."""

    def __init__(self, message: str, code: str = "TERMINAL_JOB_ERROR"):
        super().__init__(message, code=code, retryable=False)


class ExecutionBindingPreflightError(JobDispatchError):
    """An unexpected, non-binding failure while executing configured preflight."""

    def __init__(self, capability_id: str, exc: Exception):
        super().__init__(
            f"Configured video client preflight failed unexpectedly for capability "
            f"'{capability_id}': {exc}",
            code="BINDING_PREFLIGHT_FAILED",
            retryable=False,
        )


class ExecutionBindingPreflight(Protocol):
    """Configured execution clients must preflight their immutable stage binding."""

    def require_execution_binding(self, stage_key: str, capability_id: str) -> str:
        """Return the preflighted execution wire or raise TerminalJobError."""


class LeaseLostError(JobDispatchError):
    """Raised when lease renewal fails / lease is lost to another worker."""

    def __init__(self, message: str = "Lease lost during execution"):
        super().__init__(message, code="LEASE_LOST", retryable=False)


__all__ = [
    "JobContext",
    "JobSucceeded",
    "JobFailed",
    "JobDeferred",
    "JobDispatchError",
    "TerminalJobError",
    "ExecutionBindingPreflightError",
    "ExecutionBindingPreflight",
    "LeaseLostError",
]
