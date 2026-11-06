from dataclasses import dataclass, field
from typing import Dict, List, Any

@dataclass(frozen=True)
class ArtifactRef:
    id: str
    type: str
    slot: str

@dataclass(frozen=True)
class WorkflowEvent:
    event_type: str
    timestamp: str
    payload: Dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class WorkflowCommand:
    command_type: str
    args: Dict[str, Any] = field(default_factory=dict)

class IllegalTransition(Exception):
    def __init__(self, from_state: str, to_state: str, reason: str):
        super().__init__(f"Illegal transition from {from_state} to {to_state}: {reason}")
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason

@dataclass
class WorkflowAggregate:
    name: str
    initial_state: str
    states: List[str]
    transitions: Dict[str, List[str]]
    required_artifacts: Dict[str, List[str]] = field(default_factory=dict)
    approval_required: Dict[str, bool] = field(default_factory=dict)

    def validate_transition(self, from_state: str, to_state: str):
        if to_state not in self.states:
            raise IllegalTransition(from_state, to_state, "Target state not in state machine")
        if to_state not in self.transitions.get(from_state, []):
            raise IllegalTransition(from_state, to_state, "Transition not allowed")
