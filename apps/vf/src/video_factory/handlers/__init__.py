"""Product Video job handlers.

Layout:
- common.py: Shared helper functions and config builders
- _base.py: ``ProductVideoJobHandlers`` - the single owner of all job handlers
- registry.py: Module-level registration and workflow starter functions

Video rendering provider selection (Google Flow Playwright or Vertex AI) lives in
``integrations/video_factory/server.py`` behind ``video_factory.render_providers``.
Public API and sub-components are re-exported here for backward compatibility.
"""

from __future__ import annotations

# Re-export budget functions for compatibility
from video_factory.workflows.budgets import reserve_budget, commit_budget, release_budget, get_reservation

# Import the main class from base
from video_factory.handlers._base import ProductVideoJobHandlers

# Import module-level functions from registry
from video_factory.handlers.registry import (
    create_product_video_topic_handlers,
    register_product_video_job_handlers,
    start_product_video_workflow,
)

# Import public utilities and helpers from common
from video_factory.handlers.common import (
    verify_job_digests,
    create_configured_creative_generator,
    _compute_digest,
    _locked_research_dossier,
    _qc_context,
    _paid_budget_kwargs,
    _is_qc_pass,
    _qc_report_payload,
    _confirm_vf_storyboard_write,
)

# Re-export workflow state and contracts that tests/external code may import
from video_factory.workflow import (
    ProductVideoWorkflowState,
    HITL_WAITING_STATES,
)
from video_factory.job_contracts import (
    ExecutionBindingPreflight,
    ExecutionBindingPreflightError,
    JobContext,
    JobDispatchError,
    JobFailed,
    JobSucceeded,
    LeaseLostError,
    TerminalJobError,
)
from video_factory.mcp_adapters import (
    MCPProductIntelligenceAdapter,
    MCPVideoFactoryAdapter,
    _check_for_errors,
    _decode_mcp_output,
    _dispatch_tool,
    _get_global_tool_registry,
    _get_tool_names_from_registry,
    _require_snapshot_wire,
    _resolve_snapshot_execution_wire,
)
from video_factory.provider_runtime import (
    resolve_polling_setting,
    resolve_provider_mode,
    validate_polling_setting,
    validate_provider_mode,
)

# Re-export for backward compatibility
__all__ = [
    "reserve_budget",
    "commit_budget",
    "release_budget",
    "get_reservation",
    "ProductVideoJobHandlers",
    "create_product_video_topic_handlers",
    "register_product_video_job_handlers",
    "start_product_video_workflow",
    "verify_job_digests",
    "create_configured_creative_generator",
    "_compute_digest",
    "_locked_research_dossier",
    "_qc_context",
    "_paid_budget_kwargs",
    "_is_qc_pass",
    "_qc_report_payload",
    "ProductVideoWorkflowState",
    "HITL_WAITING_STATES",
    "ExecutionBindingPreflight",
    "ExecutionBindingPreflightError",
    "JobContext",
    "JobDispatchError",
    "JobFailed",
    "JobSucceeded",
    "LeaseLostError",
    "TerminalJobError",
    "MCPProductIntelligenceAdapter",
    "MCPVideoFactoryAdapter",
    "_check_for_errors",
    "_decode_mcp_output",
    "_dispatch_tool",
    "_get_global_tool_registry",
    "_get_tool_names_from_registry",
    "_require_snapshot_wire",
    "_resolve_snapshot_execution_wire",
    "resolve_polling_setting",
    "resolve_provider_mode",
    "validate_polling_setting",
    "validate_provider_mode",
    "_confirm_vf_storyboard_write",
]
