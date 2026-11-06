from __future__ import annotations

import json
import uuid
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, List

from .repository import WorkflowRepository, DuplicateCommandError, VersionConflict
from .models import IllegalTransition


@dataclass(frozen=True)
class CommandResult:
    workflow_id: str
    new_version: int
    success: bool = True
    message: str | None = None


class WorkflowEngine:
    """Idempotent command handling for a workflow aggregate.

    Stores workflow state in a simple in‑memory dict and persists events via
    ``WorkflowRepository``. Duplicate commands are detected via the repository's
    ``processed_command`` table and replayed without side effects.
    """

    _state_machine: Dict[str, List[str]] = {
        "initial": ["started"],
        "started": ["approval_requested", "cancelled", "expired"],
        "approval_requested": ["approved", "rejected", "cancelled", "expired"],
        "approved": ["job_dispatched", "cancelled", "expired"],
        "job_dispatched": ["completed", "job_failed", "cancelled", "expired"],
        "job_failed": ["job_retried", "cancelled", "expired"],
        "job_retried": ["completed", "job_failed", "cancelled", "expired"],
        "completed": [],
        "cancelled": [],
        "expired": [],
    }

    def __init__(self, repository: WorkflowRepository) -> None:
        self.repo = repository
        # in‑memory workflow state: {workflow_id: {"state": str, "version": int}}
        self._cache: Dict[str, Dict[str, Any]] = {}

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def handle(self, command: Dict[str, Any]) -> CommandResult:
        """Process a command dictionary.

        Expected keys::
            command_id   – unique identifier for idempotency
            workflow_id  – target workflow
            type         – command type (e.g. "start")
            args         – optional payload
        """
        cmd_id = command.get("command_id")
        wf_id = command.get("workflow_id", "default")
        cmd_type = command.get("type")
        args = command.get("args", {})
        if not cmd_id or not cmd_type:
            raise ValueError("command_id and type are required")

        # The repository is canonical. Refresh before both validation and the
        # optimistic commit so a restarted or long-lived engine cannot use a
        # stale cache entry.
        aggregate, current_version = self._load_workflow(wf_id)

        try:
            events = self._build_events(wf_id, cmd_id, cmd_type, args)
            current_state = aggregate.get("state", "initial")
            next_state = self._next_state(current_state, cmd_type)
            next_aggregate = dict(aggregate)
            next_aggregate["state"] = next_state
        except DuplicateCommandError:
            _, persisted_version = self._load_workflow(wf_id)
            return CommandResult(
                workflow_id=wf_id,
                new_version=persisted_version,
                message="duplicate",
            )

        try:
            result = self.repo.commit_workflow(
                workflow_id=wf_id,
                aggregate_json=json.dumps(next_aggregate),
                events=events,
                expected_version=current_version,
            )
        except DuplicateCommandError:
            _, persisted_version = self._load_workflow(wf_id)
            return CommandResult(
                workflow_id=wf_id,
                new_version=persisted_version,
                message="duplicate",
            )
        except VersionConflict:
            self._load_workflow(wf_id)
            raise

        # Only publish the new cache entry after the atomic repository commit.
        self._cache[wf_id] = {"state": next_state, "version": result.new_version}
        return CommandResult(workflow_id=wf_id, new_version=result.new_version)

    def get_workflow_state(self, workflow_id: str) -> tuple[str, int] | None:
        """Get state and version from cache or underlying repository."""
        if workflow_id in self._cache:
            entry = self._cache[workflow_id]
            return entry.get("state", "initial"), entry.get("version", 0)
        wf_data = self.repo.get_workflow(workflow_id)
        if wf_data is not None:
            aggregate_dict, version = wf_data
            state = aggregate_dict.get("state", "initial")
            entry = dict(aggregate_dict)
            entry["state"] = state
            entry["version"] = version
            self._cache[workflow_id] = entry
            return state, version
        return None

    def set_workflow_state(
        self,
        workflow_id: str,
        state: str,
        version: int = 1,
        project_id: str | None = None,
        owner_user_id: str | None = None,
        **extra_agg: Any,
    ) -> None:
        """Helper to register/update workflow state directly for tests or execution."""
        existing: dict[str, Any] = {}
        if hasattr(self, "repo") and self.repo is not None:
            data = self.repo.get_workflow(workflow_id)
            if data:
                existing = data[0]
        agg = dict(existing)
        agg["state"] = state
        if project_id is not None:
            agg["project_id"] = project_id
        elif "project_id" not in agg and (
            state.startswith("creating_")
            or state.startswith("awaiting_")
            or state in ("researching", "building_resource_pack", "rendering", "exporting")
        ):
            agg["project_id"] = f"proj_{workflow_id}"

        if owner_user_id is not None:
            agg["owner_user_id"] = owner_user_id
        elif "owner_user_id" not in agg and (
            state.startswith("creating_")
            or state.startswith("awaiting_")
            or state in ("researching", "building_resource_pack", "rendering", "exporting")
        ):
            agg["owner_user_id"] = "test_owner"

        agg.update(extra_agg)
        with self.repo.conn:
            cur = self.repo.conn.cursor()
            cur.execute(
                "INSERT INTO workflow (workflow_id, aggregate_json, version) VALUES (?, ?, ?) "
                "ON CONFLICT(workflow_id) DO UPDATE SET aggregate_json=excluded.aggregate_json, version=excluded.version",
                (workflow_id, json.dumps(agg), version),
            )
        self._cache[workflow_id] = dict(agg, version=version)

    # ---------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------
    def _build_events(
        self, workflow_id: str, command_id: str, cmd_type: str, args: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Create event dicts for the repository layer.

        Detects duplicate commands via ``processed_command`` before creating any
        events.
        """
        # Check duplicate via repository directly.
        # This will raise DuplicateCommandError if already present.
        # We perform a cheap SELECT using the repo's connection.
        cur = self.repo.conn.cursor()
        cur.execute(
            "SELECT command_id FROM processed_command WHERE workflow_id = ? AND command_id = ?",
            (workflow_id, command_id),
        )
        if cur.fetchone():
            raise DuplicateCommandError()
        # Build a single event representing the command.
        event = {
            "event_id": str(uuid.uuid4()),
            "command_id": command_id,
            "event_type": self._event_type_for_command(cmd_type),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "payload": args,
        }
        return [event]

    def _load_workflow(self, workflow_id: str) -> tuple[dict[str, Any], int]:
        """Load the canonical aggregate and mirror its state in the cache."""
        wf_data = self.repo.get_workflow(workflow_id)
        if wf_data is None:
            aggregate: dict[str, Any] = {"state": "initial"}
            version = 0
        else:
            aggregate, version = wf_data
        self._cache[workflow_id] = {
            "state": aggregate.get("state", "initial"),
            "version": version,
        }
        return aggregate, version

    def _event_type_for_command(self, cmd_type: str) -> str:
        mapping = {
            "start": "WorkflowStarted",
            "record_artifact": "ArtifactRecorded",
            "request_approval": "ApprovalRequested",
            "approve": "Approved",
            "reject": "Rejected",
            "dispatch_job": "JobDispatched",
            "job_succeeded": "JobSucceeded",
            "job_failed": "JobFailed",
            "retry": "JobRetried",
            "cancel": "Cancelled",
            "expire": "Expired",
        }
        return mapping.get(cmd_type, "Unknown")

    def _apply_state_transition(self, wf_id: str, cmd_type: str, args: Dict[str, Any]) -> None:
        current_state = self._cache.get(wf_id, {}).get("state", "initial")
        self._cache.setdefault(wf_id, {"state": current_state, "version": 0})[
            "state"
        ] = self._next_state(current_state, cmd_type)

    def _next_state(self, current_state: str, cmd_type: str) -> str:
        """Validate a command and return its next state without side effects."""
        transition_map = {
            "start": "started",
            "record_artifact": "started",
            "request_approval": "approval_requested",
            "approve": "approved",
            "reject": "cancelled",
            "dispatch_job": "job_dispatched",
            "job_succeeded": "completed",
            "job_failed": "job_failed",
            "retry": "job_retried",
            "cancel": "cancelled",
            "expire": "expired",
        }
        new_state = transition_map.get(cmd_type)
        if new_state is None:
            raise IllegalTransition(current_state, "unknown", "unsupported command")
        allowed = self._state_machine.get(current_state, [])
        if new_state not in allowed:
            raise IllegalTransition(current_state, new_state, f"command {cmd_type} not allowed")
        return new_state
