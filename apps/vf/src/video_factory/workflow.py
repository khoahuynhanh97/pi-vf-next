"""hermes_personal/product_video/workflow.py

Consolidated workflow state machine for the product-to-video pipeline.

This module centralizes all workflow state definitions, transitions, and helpers
for the product video factory. The canonical ProductVideoWorkflowState enum
and HITL_WAITING_STATES are defined here.

For backward compatibility, workflow_state.py re-exports from this module.
"""
from __future__ import annotations

from enum import Enum


class ProductVideoWorkflowState(str, Enum):
    """Product Video workflow states with string enum values."""
    IDLE = "idle"
    RESEARCHING = "researching"
    BUILDING_RESOURCE_PACK = "building_resource_pack"
    RETRYING_IMAGE_QC = "retrying_image_qc"
    AWAITING_PRODUCT_LOCK = "AwaitingProductLock"
    LOCKING_RESOURCE_PACK = "locking_resource_pack"
    CREATING_BRIEF = "creating_brief"
    AWAITING_BRIEF_APPROVAL = "AwaitingBriefApproval"
    CREATING_SCENE_PLAN = "creating_scene_plan"
    AWAITING_SCENE_APPROVAL = "AwaitingSceneApproval"
    CREATING_STORYBOARD = "creating_storyboard"
    AWAITING_STORYBOARD_APPROVAL = "AwaitingStoryboardApproval"
    RENDERING = "rendering"
    AWAITING_FINAL_APPROVAL = "AwaitingFinalApproval"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    WAITING_FOR_OPERATOR = "WaitingForOperator"


# HITL waiting states where human approval is required
HITL_WAITING_STATES = frozenset({
    ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
    ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL,
    ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL,
    ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL,
    ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL,
})

# Alias for backward compatibility with Gate 5 Task 4 tests
HITL_BLOCKING_STATES: frozenset[ProductVideoWorkflowState] = HITL_WAITING_STATES


class WorkflowTransitionError(Exception):
    """Raised when a state transition is not permitted."""


# Centralized transition matrix - legal next states for each current state
# Format: {current_state: frozenset({allowed_next_states})}
TRANSITIONS: dict[ProductVideoWorkflowState, frozenset[ProductVideoWorkflowState]] = {
    ProductVideoWorkflowState.IDLE: frozenset({
        ProductVideoWorkflowState.RESEARCHING,
    }),
    ProductVideoWorkflowState.RESEARCHING: frozenset({
        ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
    }),
    ProductVideoWorkflowState.BUILDING_RESOURCE_PACK: frozenset({
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
        ProductVideoWorkflowState.CREATING_BRIEF,
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
    }),
    ProductVideoWorkflowState.RETRYING_IMAGE_QC: frozenset({
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
    }),
    ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK: frozenset({
        ProductVideoWorkflowState.LOCKING_RESOURCE_PACK,
        ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
    }),
    ProductVideoWorkflowState.LOCKING_RESOURCE_PACK: frozenset({
        ProductVideoWorkflowState.CREATING_BRIEF,
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
    }),
    ProductVideoWorkflowState.CREATING_BRIEF: frozenset({
        ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL,
    }),
    ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL: frozenset({
        ProductVideoWorkflowState.CREATING_SCENE_PLAN,
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,  # Rollback option
    }),
    ProductVideoWorkflowState.CREATING_SCENE_PLAN: frozenset({
        ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL,
    }),
    ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL: frozenset({
        ProductVideoWorkflowState.CREATING_STORYBOARD,
        ProductVideoWorkflowState.CREATING_SCENE_PLAN,  # Rollback option
    }),
    ProductVideoWorkflowState.CREATING_STORYBOARD: frozenset({
        ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL,
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
    }),
    ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL: frozenset({
        ProductVideoWorkflowState.RENDERING,
        ProductVideoWorkflowState.CREATING_STORYBOARD,  # Rollback option
    }),
    ProductVideoWorkflowState.RENDERING: frozenset({
        ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL,
        ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
    }),
    ProductVideoWorkflowState.WAITING_FOR_OPERATOR: frozenset({
        ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
        ProductVideoWorkflowState.CREATING_BRIEF,
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
        ProductVideoWorkflowState.CREATING_STORYBOARD,
        ProductVideoWorkflowState.AWAITING_STORYBOARD_APPROVAL,
        ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL,
        ProductVideoWorkflowState.RENDERING,
    }),
    ProductVideoWorkflowState.AWAITING_FINAL_APPROVAL: frozenset({
        ProductVideoWorkflowState.EXPORTING,
        ProductVideoWorkflowState.RENDERING,  # Rollback option
    }),
    ProductVideoWorkflowState.EXPORTING: frozenset({
        ProductVideoWorkflowState.COMPLETED,
    }),
    ProductVideoWorkflowState.COMPLETED: frozenset(),  # Terminal state
}


def transition(
    current: ProductVideoWorkflowState,
    target: ProductVideoWorkflowState,
) -> ProductVideoWorkflowState:
    """Validate and return the target state.

    Raises WorkflowTransitionError if the transition is not in the legal set.
    """
    allowed = TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise WorkflowTransitionError(
            f"Cannot transition from {current!r} to {target!r}; "
            f"allowed: {sorted(s.value for s in allowed)}"
        )
    return target


def is_hitl_blocked(state: ProductVideoWorkflowState) -> bool:
    """Return True if the state requires human approval before any job runs."""
    return state in HITL_BLOCKING_STATES


__all__ = [
    "ProductVideoWorkflowState",
    "HITL_WAITING_STATES",
    "HITL_BLOCKING_STATES",
    "TRANSITIONS",
    "WorkflowTransitionError",
    "transition",
    "is_hitl_blocked",
]
