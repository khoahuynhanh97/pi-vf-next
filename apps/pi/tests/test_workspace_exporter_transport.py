import asyncio
from types import SimpleNamespace

from tools.workspace_exporter.run_research import _tool_payload,run_workflow
from tools.workspace_exporter.exporter import WorkspaceExporter


def test_non_json_mcp_response_is_preserved_as_structured_error(tmp_path):
    result=SimpleNamespace(content=[SimpleNamespace(text='search timed out')])
    payload=_tool_payload(result)
    assert payload['error_code']=='MCP_NON_JSON_RESPONSE'
    assert payload['raw_response']=='search timed out'
    calls=[]
    async def fake_call(name,arguments):
        calls.append(name)
        return payload
    exported=asyncio.run(run_workflow('Synthetic query',fake_call,WorkspaceExporter(tmp_path)))
    assert exported.operator_status=='MCP_TOOL_ERROR'
    assert calls==['research_product']
