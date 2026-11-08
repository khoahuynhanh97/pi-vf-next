"""An ambiguous MCP response must never cause a second native submission."""
import pytest

from video_factory import mcp_adapters
from video_factory.job_contracts import TerminalJobError


def test_flow_provider_rejection_preserves_specific_terminal_code():
    with pytest.raises(TerminalJobError) as failure:
        mcp_adapters._check_for_errors({
            "status": "failed",
            "code": "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
            "error": "Google Flow rejected the video request as unusual activity",
        })
    assert failure.value.code == "PUBLIC_ERROR_UNUSUAL_ACTIVITY"


@pytest.mark.parametrize('outcome', ['timeout', 'empty', 'content'])
def test_ambiguous_dispatch_does_not_fall_back_to_native(monkeypatch, outcome):
    native_calls = []
    monkeypatch.setattr(mcp_adapters, '_dispatch_local_video_factory_tool',
                        lambda *args: native_calls.append(args) or {'status': 'submitted'})

    class Registry:
        def dispatch(self, tool, payload):
            if outcome == 'timeout':
                raise TimeoutError('response lost after submission')
            if outcome == 'empty':
                return None
            return {'status': 'submitted', 'description': 'Unknown tool: quoted source text'}

    if outcome == 'timeout':
        with pytest.raises(TimeoutError):
            mcp_adapters._dispatch_tool(Registry(), 'mcp__video_factory__render', {})
    elif outcome == 'empty':
        with pytest.raises(RuntimeError, match='empty response'):
            mcp_adapters._dispatch_tool(Registry(), 'mcp__video_factory__render', {})
    else:
        result = mcp_adapters._dispatch_tool(Registry(), 'mcp__video_factory__render', {})
        assert result['status'] == 'submitted'
    assert native_calls == []


def test_explicit_unknown_tool_response_retains_native_dispatch(monkeypatch):
    native_calls = []
    monkeypatch.setattr(mcp_adapters, '_dispatch_local_video_factory_tool',
                        lambda *args: native_calls.append(args) or {'status': 'submitted'})

    class Registry:
        def dispatch(self, tool, payload):
            return {'error': 'Unknown tool: mcp__video_factory__render'}

    assert mcp_adapters._dispatch_tool(Registry(), 'mcp__video_factory__render', {}) == {'status': 'submitted'}
    assert len(native_calls) == 1
