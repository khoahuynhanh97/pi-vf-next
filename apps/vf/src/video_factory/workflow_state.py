"""hermes_personal/product_video/workflow_state.py

Backward compatibility shim.

The canonical ProductVideoWorkflowState enum and HITL_WAITING_STATES are now
defined in workflow.py. This module re-exports them for backward compatibility
with existing code that imports from workflow_state.

New code should import directly from workflow.py for the canonical definitions.
"""
from __future__ import annotations

from video_factory.workflow import (
    ProductVideoWorkflowState,
    HITL_WAITING_STATES,
)

__all__ = ["ProductVideoWorkflowState", "HITL_WAITING_STATES"]