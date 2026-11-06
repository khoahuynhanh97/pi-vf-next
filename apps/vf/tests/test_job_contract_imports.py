from __future__ import annotations


def test_job_contracts_have_stable_new_and_legacy_imports():
    from hermes_personal.product_video import handlers as legacy
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

    assert legacy.JobContext is JobContext
    assert legacy.JobSucceeded is JobSucceeded
    assert legacy.JobFailed is JobFailed
    assert legacy.JobDispatchError is JobDispatchError
    assert legacy.TerminalJobError is TerminalJobError
    assert legacy.ExecutionBindingPreflightError is ExecutionBindingPreflightError
    assert legacy.ExecutionBindingPreflight is ExecutionBindingPreflight
    assert legacy.LeaseLostError is LeaseLostError


def test_workflow_state_has_stable_new_and_legacy_imports():
    from hermes_personal.product_video import handlers as legacy
    from video_factory.workflow_state import (
        HITL_WAITING_STATES,
        ProductVideoWorkflowState,
    )

    assert legacy.ProductVideoWorkflowState is ProductVideoWorkflowState
    assert legacy.HITL_WAITING_STATES is HITL_WAITING_STATES


def test_provider_runtime_has_stable_new_and_legacy_imports():
    from hermes_personal.product_video import handlers as legacy
    from video_factory.provider_runtime import (
        resolve_polling_setting,
        resolve_provider_mode,
        validate_polling_setting,
        validate_provider_mode,
    )

    assert legacy.resolve_polling_setting is resolve_polling_setting
    assert legacy.resolve_provider_mode is resolve_provider_mode
    assert legacy.validate_polling_setting is validate_polling_setting
    assert legacy.validate_provider_mode is validate_provider_mode


def test_mcp_adapters_have_stable_new_and_legacy_imports():
    from hermes_personal.product_video import handlers as legacy
    from video_factory.mcp_adapters import (
        MCPProductIntelligenceAdapter,
        MCPVideoFactoryAdapter,
        _decode_mcp_output,
    )

    assert legacy.MCPProductIntelligenceAdapter is MCPProductIntelligenceAdapter
    assert legacy.MCPVideoFactoryAdapter is MCPVideoFactoryAdapter
    assert legacy._decode_mcp_output is _decode_mcp_output
