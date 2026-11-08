"""MCP transport adapters for Product Video integrations.

The adapter implementations are kept independent from the orchestration
handlers so the integration boundary can be discovered and tested directly.
The historical handlers import path remains compatible through re-exports.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

from video_factory.capabilities.manifest import INITIAL_CAPABILITIES
from video_factory.capabilities.snapshots import CatalogSnapshot
from video_factory.job_contracts import JobDeferred, LeaseLostError, TerminalJobError


def _get_global_tool_registry() -> Any | None:
    import sys
    from pathlib import Path
    if "tools" in sys.modules and not hasattr(sys.modules["tools"], "__file__"):
        sys.modules.pop("tools", None)
    ws_parent = Path(__file__).resolve().parents[3]
    for extra in (
        Path(r"D:\work\hermes-agent"),
        Path(r"Z:\work\hermes-agent"),
        ws_parent / "hermes-agent",
        Path(r"D:\work\hermes-upstream-rearchitecture"),
        Path(r"Z:\work\hermes-upstream-rearchitecture"),
        ws_parent / "hermes-upstream-rearchitecture",
    ):
        if extra.is_dir() and str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    try:
        from tools.registry import registry
        if hasattr(registry, "get_all_tool_names") and not registry.get_all_tool_names():
            try:
                from tools.mcp_tool import discover_mcp_tools
                discover_mcp_tools()
            except Exception:
                pass
        return registry
    except ImportError:
        return {}


_VF_KNOWN_TOOL_NAMES = {
    "mcp__video_factory__video_project_create",
    "mcp__video_factory__video_project_get",
    "mcp__video_factory__resource_pack_save",
    "mcp__video_factory__resource_pack_get",
    "mcp__video_factory__resource_pack_lock",
    "mcp__video_factory__resource_pack_unlock",
    "mcp__video_factory__raw_idea_save",
    "mcp__video_factory__creative_brief_save",
    "mcp__video_factory__creative_brief_get",
    "mcp__video_factory__creative_brief_approve",
    "mcp__video_factory__scene_plan_save",
    "mcp__video_factory__scene_plan_get",
    "mcp__video_factory__scene_plan_approve",
    "mcp__video_factory__storyboard_generate",
    "mcp__video_factory__storyboard_approve",
    "mcp__video_factory__timeline_get",
    "mcp__video_factory__export_submit",
    "mcp__video_factory__final_video_approve",
    "mcp__video_factory__video_factory_runtime_info",
    "mcp__video_factory__create_brief",
    "mcp__video_factory__create_scene_plan",
    "mcp__video_factory__create_storyboard",
    "mcp__video_factory__render",
    "mcp__video_factory__export",
    "mcp__video_factory__get_job_status",
    "video_project_create",
    "video_project_get",
    "resource_pack_save",
    "resource_pack_get",
    "resource_pack_lock",
    "product_lock_approve",
    "creative_brief_get",
    "brief_approve",
    "scene_plan_approve",
    "storyboard_approve",
    "final_video_approve",
    "get_job_status",
}


def _dispatch_local_video_factory_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from video_factory.integrations import server
    except ImportError:
        return None

    p = payload or {}
    owner_user_id = p.get("owner_user_id", "default_user")
    project_id = p.get("project_id", "")

    if tool_name in ("mcp__video_factory__video_project_create", "video_project_create"):
        return server.video_project_create(owner_user_id=owner_user_id, project_id=project_id)
    if tool_name in ("mcp__video_factory__video_project_get", "video_project_get"):
        return server.video_project_get(owner_user_id=owner_user_id, project_id=project_id)
    if tool_name in ("mcp__video_factory__resource_pack_save", "resource_pack_save"):
        return server.resource_pack_save(
            owner_user_id=owner_user_id,
            project_id=project_id,
            resource_pack=p.get("resource_pack") or p.get("pack_dict") or p,
        )
    if tool_name in ("mcp__video_factory__resource_pack_get", "resource_pack_get"):
        return server.resource_pack_get(owner_user_id=owner_user_id, project_id=project_id)
    if tool_name in ("mcp__video_factory__resource_pack_lock", "resource_pack_lock", "product_lock_approve", "mcp__video_factory__product_lock_approve"):
        return server.resource_pack_lock(
            owner_user_id=owner_user_id,
            project_id=project_id,
            product_identity=p.get("product_identity") or p.get("identity") or {},
            character_identity=p.get("character_identity"),
            approval_request_id=p.get("approval_request_id", ""),
            workflow_id=p.get("workflow_id", ""),
            workflow_version=p.get("workflow_version"),
            eligible_reference_manifest=p.get("eligible_reference_manifest"),
        )
    if tool_name in ("mcp__video_factory__resource_pack_unlock", "resource_pack_unlock"):
        return server.resource_pack_unlock(owner_user_id=owner_user_id, project_id=project_id)
    if tool_name in ("mcp__video_factory__raw_idea_save", "raw_idea_save"):
        return server.raw_idea_save(owner_user_id=owner_user_id, project_id=project_id, raw_idea=p.get("raw_idea") or {})
    if tool_name in ("mcp__video_factory__creative_brief_save", "creative_brief_save"):
        return server.creative_brief_save(owner_user_id=owner_user_id, project_id=project_id, creative_brief=p.get("creative_brief") or p.get("brief") or {})
    if tool_name in ("mcp__video_factory__creative_brief_get", "creative_brief_get"):
        return server.creative_brief_get(owner_user_id=owner_user_id, project_id=project_id)
    if tool_name in ("mcp__video_factory__creative_brief_approve", "brief_approve"):
        return server.creative_brief_approve(
            owner_user_id=owner_user_id,
            project_id=project_id,
            approval_request_id=p.get("approval_request_id", ""),
            workflow_id=p.get("workflow_id", ""),
            workflow_version=p.get("workflow_version"),
        )
    if tool_name in ("mcp__video_factory__scene_plan_save", "scene_plan_save"):
        return server.scene_plan_save(owner_user_id=owner_user_id, project_id=project_id, scene_plan=p.get("scene_plan") or p.get("plan") or {})
    if tool_name in ("mcp__video_factory__scene_plan_get", "scene_plan_get"):
        return server.scene_plan_get(owner_user_id=owner_user_id, project_id=project_id)
    if tool_name in ("mcp__video_factory__scene_plan_approve", "scene_plan_approve"):
        return server.scene_plan_approve(
            owner_user_id=owner_user_id,
            project_id=project_id,
            approval_request_id=p.get("approval_request_id", ""),
            workflow_id=p.get("workflow_id", ""),
            workflow_version=p.get("workflow_version"),
        )
    if tool_name in ("mcp__video_factory__storyboard_generate", "storyboard_generate"):
        return server.storyboard_generate(owner_user_id=owner_user_id, project_id=project_id)
    if tool_name in ("mcp__video_factory__storyboard_approve", "storyboard_approve"):
        return server.storyboard_approve(
            owner_user_id=owner_user_id,
            project_id=project_id,
            approval_request_id=p.get("approval_request_id", ""),
            notes=p.get("notes", ""),
            workflow_id=p.get("workflow_id", ""),
            workflow_version=p.get("workflow_version"),
        )
    if tool_name in ("mcp__video_factory__timeline_get", "timeline_get"):
        return server.timeline_get(owner_user_id=owner_user_id, project_id=project_id)
    if tool_name in ("mcp__video_factory__export_submit", "export_submit"):
        return server.export_submit(owner_user_id=owner_user_id, project_id=project_id)
    if tool_name in ("mcp__video_factory__final_video_approve", "final_video_approve"):
        return server.final_video_approve(
            owner_user_id=owner_user_id,
            project_id=project_id,
            approval_request_id=p.get("approval_request_id", ""),
            notes=p.get("notes", ""),
            workflow_id=p.get("workflow_id", ""),
            workflow_version=p.get("workflow_version"),
        )
    if tool_name in ("mcp__video_factory__video_factory_runtime_info", "video_factory_runtime_info"):
        return server.video_factory_runtime_info()
    if tool_name in ("mcp__video_factory__create_brief", "create_brief"):
        return server.create_brief(
            product_id=p.get("product_id", ""),
            brief_config=p.get("brief_config") or {},
            owner_user_id=owner_user_id,
            project_id=project_id,
        )
    if tool_name in ("mcp__video_factory__create_scene_plan", "create_scene_plan"):
        return server.create_scene_plan(
            brief_result=p.get("brief_result"),
            owner_user_id=owner_user_id,
            project_id=project_id,
            scenes=p.get("scenes"),
        )
    if tool_name in ("mcp__video_factory__create_storyboard", "create_storyboard"):
        return server.create_storyboard(
            scene_plan_result=p.get("scene_plan_result"),
            owner_user_id=owner_user_id,
            project_id=project_id,
            frames=p.get("frames"),
        )
    if tool_name in ("mcp__video_factory__render", "render"):
        return server.render(
            storyboard_result=p.get("storyboard_result"),
            render_params=p.get("render_params"),
            idempotency_key=p.get("idempotency_key"),
            owner_user_id=owner_user_id,
            project_id=project_id,
            provider=p.get("provider"),
            admission_context=p.get("admission_context"),
            approval_request_id=p.get("approval_request_id", ""),
            kwargs=p.get("kwargs"),
        )
    if tool_name in ("mcp__video_factory__export", "export"):
        return server.export(
            render_result=p.get("render_result"),
            export_params=p.get("export_params"),
            idempotency_key=p.get("idempotency_key"),
            owner_user_id=owner_user_id,
            project_id=project_id,
            workflow_id=p.get("workflow_id"),
            approval_request_id=p.get("approval_request_id"),
            target_workflow_id=p.get("target_workflow_id"),
        )
    if tool_name in ("mcp__video_factory__get_job_status", "get_job_status"):
        return server.get_job_status(
            job_id=p.get("job_id", ""),
            provider=p.get("provider"),
            provider_kind=p.get("provider_kind"),
        )
    return None


def _get_tool_names_from_registry(registry: Any) -> set[str]:
    names: set[str] = set()
    if hasattr(registry, "get_all_tool_names"):
        try:
            names.update(registry.get_all_tool_names())
        except Exception:
            pass
    elif hasattr(registry, "get_all_entries"):
        try:
            names.update(entry.name for entry in registry.get_all_entries())
        except Exception:
            pass
    elif isinstance(registry, dict):
        names.update(registry.keys())
    names.update(_VF_KNOWN_TOOL_NAMES)
    return names


def _check_for_errors(obj: Any) -> None:
    if isinstance(obj, dict):
        if obj.get('creative_failure') and obj.get('code') in {
            'CLIP_CONTENT_FAIL','CLIP_CONTENT_UNKNOWN','FINAL_CONTENT_FAIL','FINAL_CONTENT_UNKNOWN',
            'CLIP_TECHNICAL_FAIL','FINAL_TECHNICAL_FAIL'}:
            error=TerminalJobError('Creative media review blocked acceptance',code=obj['code'])
            error.artifact_ref=obj['creative_failure']
            raise error
        if (
            obj.get("code") in ("RECONCILIATION_REQUIRED", "GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN", "UNKNOWN_PAID_OUTCOME")
            or obj.get("status") == "reconciliation_required"
        ):
            code_val = obj.get("code") if obj.get("code") in ("RECONCILIATION_REQUIRED", "GOOGLE_FLOW_VIDEO_ACCEPTANCE_UNKNOWN", "UNKNOWN_PAID_OUTCOME") else "RECONCILIATION_REQUIRED"
            raise TerminalJobError(
                obj.get("error") or obj.get("message") or "Paid provider operation requires reconciliation",
                code=code_val,
            )
        if obj.get("status") in ("failed", "error", "cancelled"):
            err_msg = obj.get("error") or obj.get("message") or f"Operation terminated with status '{obj.get('status')}'"
            if obj.get("code") == "PUBLIC_ERROR_UNUSUAL_ACTIVITY":
                raise TerminalJobError(err_msg, code="PUBLIC_ERROR_UNUSUAL_ACTIVITY")
            raise TerminalJobError(f"Tool operation failed with status '{obj.get('status')}': {err_msg}", code="TOOL_OPERATION_TERMINAL_FAILED")

        if obj.get("isError") is True or obj.get("is_error") is True:
            err_text = ""
            if isinstance(obj.get("content"), list):
                err_text = " ".join(
                    item.get("text", "") for item in obj["content"] if isinstance(item, dict) and item.get("type") == "text"
                )
            err_text = err_text or obj.get("error") or obj.get("message") or "MCP tool reported an error"
            raise RuntimeError(f"MCP tool error: {err_text}")

        if "error" in obj and obj["error"]:
            raise RuntimeError(f"Tool error: {obj['error']}")


def _decode_mcp_output(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"output": raw}

    _check_for_errors(raw)

    if isinstance(raw, dict):
        if isinstance(raw.get("structuredContent"), dict):
            res = dict(raw["structuredContent"])
            _check_for_errors(res)
            if "_meta" in raw:
                res["_meta"] = raw["_meta"]
            return res

        if isinstance(raw.get("content"), list):
            for block in raw["content"]:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    text_val = block["text"]
                    try:
                        parsed = json.loads(text_val)
                        if isinstance(parsed, dict):
                            _check_for_errors(parsed)
                            return parsed
                    except json.JSONDecodeError:
                        return {"output": text_val}

        if "result" in raw:
            res_val = raw["result"]
            if isinstance(res_val, dict):
                _check_for_errors(res_val)
                return res_val
            if isinstance(res_val, str):
                try:
                    parsed_res = json.loads(res_val)
                    if isinstance(parsed_res, dict):
                        _check_for_errors(parsed_res)
                        return parsed_res
                except json.JSONDecodeError:
                    return {"output": res_val}

        return raw

    return {"output": str(raw)}


def _dispatch_tool(registry: Any, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not tool_name:
        raise ValueError("tool_name is required")
    raw = None
    if hasattr(registry, "dispatch"):
        # Dispatch may have submitted paid work before losing its response.
        # Only an explicit pre-execution unknown-tool response permits native
        # dispatch; exceptions and empty responses need reconciliation.
        raw = registry.dispatch(tool_name, payload)
        if raw is None:
            raise RuntimeError(f"Tool '{tool_name}' returned empty response; reconcile before retrying")
        error_text = raw.get("error") if isinstance(raw, dict) else raw
        if isinstance(raw, str):
            try:
                _p = json.loads(raw)
                if isinstance(_p, dict) and "error" in _p:
                    error_text = _p["error"]
            except Exception:
                pass
        if isinstance(error_text, str) and ("Unknown tool:" in error_text):
            local_res = _dispatch_local_video_factory_tool(tool_name, payload)
            if local_res is not None:
                raw = local_res
    elif isinstance(registry, dict) and callable(registry.get(tool_name)):
        raw = registry[tool_name](payload)
    else:
        local_res = _dispatch_local_video_factory_tool(tool_name, payload)
        if local_res is not None:
            raw = local_res
        else:
            raise RuntimeError(f"Tool '{tool_name}' cannot be dispatched on registry {registry!r}")

    decoded = _decode_mcp_output(raw)
    if not decoded:
        raise RuntimeError(f"Tool '{tool_name}' returned empty response")
    _check_for_errors(decoded)
    return decoded


def _require_snapshot_wire(
    wire_tools: dict[str, str], key: str, *, capability_id: str,
    binding_reasons: dict[str, str] | None = None,
) -> str:
    """Return an execution wire already resolved from the active snapshot."""
    tool_name = wire_tools.get(key)
    if not tool_name:
        reason = (binding_reasons or {}).get(key, "missing")
        raise TerminalJobError(
            f"MISSING_PROVIDER_CONFIG: active capability binding unavailable for {capability_id} ({reason})",
            code="BINDING_UNAVAILABLE",
        )
    return tool_name


def _resolve_snapshot_execution_wire(
    snapshot: CatalogSnapshot, capability_id: str
) -> tuple[str | None, str | None]:
    """Resolve one execution binding from the immutable snapshot only."""
    matches = [b for b in snapshot.bindings if b.capability_id == capability_id]
    if not matches:
        return None, "missing"
    if len(matches) != 1:
        return None, "ambiguous"
    binding = matches[0]
    if binding.health != "healthy":
        return None, "unhealthy"
    spec = INITIAL_CAPABILITIES[capability_id]
    if (
        binding.input_schema_version != spec.input_schema_version
        or binding.output_schema_version != spec.output_schema_version
    ):
        return None, "schema_version_mismatch"
    if capability_id == "video.render" and binding.wire_name != "mcp__video_factory__render":
        return None, "noncanonical_wire"
    return binding.wire_name, None


def _require_snapshot_wire(
    wire_tools: dict[str, str], key: str, *, capability_id: str,
    binding_reasons: dict[str, str] | None = None,
) -> str:
    """Return an execution wire already resolved from the active snapshot."""
    tool_name = wire_tools.get(key)
    if not tool_name:
        reason = (binding_reasons or {}).get(key, "missing")
        raise TerminalJobError(
            f"MISSING_PROVIDER_CONFIG: active capability binding unavailable for {capability_id} ({reason})",
            code="BINDING_UNAVAILABLE",
        )
    return tool_name


def _resolve_snapshot_execution_wire(
    snapshot: CatalogSnapshot, capability_id: str
) -> tuple[str | None, str | None]:
    """Resolve one execution binding from the immutable snapshot only."""
    matches = [b for b in snapshot.bindings if b.capability_id == capability_id]
    if not matches:
        return None, "missing"
    if len(matches) != 1:
        return None, "ambiguous"
    binding = matches[0]
    if binding.health != "healthy":
        return None, "unhealthy"
    spec = INITIAL_CAPABILITIES[capability_id]
    if (
        binding.input_schema_version != spec.input_schema_version
        or binding.output_schema_version != spec.output_schema_version
    ):
        return None, "schema_version_mismatch"
    if capability_id == "video.render" and binding.wire_name != "mcp__video_factory__render":
        return None, "noncanonical_wire"
    return binding.wire_name, None


class MCPProductIntelligenceAdapter:
    """Live provider adapter bridging ProductVideoJobHandlers to real/MCP ProductIntelligence tools."""

    def __init__(self, registry: Any, wire_tools: dict[str, str], binding_reasons: dict[str, str] | None = None) -> None:
        self.registry = registry
        self.wire_tools = wire_tools
        self.binding_reasons = binding_reasons or {}

    def require_execution_binding(self, stage_key: str, capability_id: str) -> str:
        return _require_snapshot_wire(
            self.wire_tools,
            stage_key,
            capability_id=capability_id,
            binding_reasons=self.binding_reasons,
        )

    def research(
        self,
        product_name: str,
        keywords: list[str],
        pdp_url: str | None = None,
        workflow_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        tool_name = _require_snapshot_wire(
            self.wire_tools, "research", capability_id="product.research", binding_reasons=self.binding_reasons
        )

        research_snapshot_id = kwargs.get("research_snapshot_id")
        if research_snapshot_id:
            get_tool = self.wire_tools.get("get_research")
            if not get_tool:
                raise TerminalJobError(
                    "MISSING_PROVIDER_CONFIG: research maintenance wire is unavailable",
                    code="BINDING_UNAVAILABLE",
                )
            retrieved = _dispatch_tool(self.registry, get_tool, {"snapshot_id": str(research_snapshot_id)})
            if isinstance(retrieved.get("result"), dict):
                retrieved_snapshot_id = str(retrieved.get("snapshot_id") or "")
                raw = dict(retrieved["result"])
            elif retrieved.get("snapshot_id") and (
                retrieved.get("canonical_products") is not None
                or retrieved.get("persistence_status") is not None
            ):
                # Some registry implementations unwrap the MCP envelope and
                # return the persisted result directly.
                retrieved_snapshot_id = str(retrieved.get("snapshot_id") or "")
                raw = dict(retrieved)
            else:
                raise TerminalJobError(
                    f"PI snapshot {research_snapshot_id!r} is unavailable or malformed",
                    code="SNAPSHOT_NOT_FOUND",
                )
            nested_snapshot_id = str(raw.get("snapshot_id") or retrieved_snapshot_id)
            if nested_snapshot_id != str(research_snapshot_id):
                raise TerminalJobError("PI snapshot identity mismatch", code="IDENTITY_MISMATCH")
            raw["snapshot_id"] = nested_snapshot_id
            # Retrieval itself is the durability proof. Historical snapshots
            # may predate the persistence_status field inside their payload.
            raw["persistence_status"] = raw.get("persistence_status") or "persisted"
        else:
            # Deterministic space-join for query from keywords
            query = " ".join(k.strip() for k in keywords if k and k.strip()) if keywords else None

            # Mapped outgoing payload conforming strictly to located server signature:
            # url from pdp_url, product_name, query from keywords
            # Do NOT send unsupported 'keywords' or 'pdp_url' keys to the server
            payload: dict[str, Any] = {}
            if product_name:
                payload["product_name"] = product_name
            if pdp_url:
                payload["url"] = pdp_url
            elif product_name and str(product_name).startswith(("http://", "https://")):
                payload["url"] = str(product_name).strip()
            if query:
                payload["query"] = query
            elif query and str(query).startswith(("http://", "https://")) and "url" not in payload:
                payload["url"] = str(query).strip()
            for identity_field in ("brand", "model", "variant", "canonical_product_key"):
                identity_value = kwargs.get(identity_field)
                if identity_value:
                    payload[identity_field] = identity_value
            # Enable discover_sources when no direct URL is supplied; when an explicit
            # PDP URL is provided, acquire directly from the source to avoid external rate-limits.
            payload["discover_sources"] = bool(not payload.get("url"))

            raw = _dispatch_tool(self.registry, tool_name, payload)

        # Extract snapshot_id and persistence_status strictly from raw result
        # Do NOT relabel generic id/research_id as snapshot_id or infer persistence from an ID
        snapshot_id = raw.get("snapshot_id")
        persistence_status = raw.get("persistence_status")
        research_status = raw.get("status")

        if research_status in ("failed", "error", "cancelled", "no_product_evidence"):
            err_msg = raw.get("error") or raw.get("reason") or f"Research failed with status '{research_status}'"
            raise TerminalJobError(f"PI research operation failed: {err_msg}", code="PI_RESEARCH_FAILED")

        # Validate that persistence is explicit and successful (service returns 'saved' or 'persisted')
        if not snapshot_id:
            raise TerminalJobError("PI research tool returned no snapshot_id", code="MISSING_SNAPSHOT_ID")

        if persistence_status not in ("saved", "persisted"):
            raise TerminalJobError(
                f"PI research persistence unsuccessful or unconfirmed (persistence_status={persistence_status!r})",
                code="PERSISTENCE_FAILED",
            )

        if "canonical_products" in raw and not raw["canonical_products"]:
            raise TerminalJobError("No canonical product identity found in research evidence", code="NO_PRODUCT_EVIDENCE")

        req_brand = kwargs.get("brand")
        req_model = kwargs.get("model")
        canonical_list = raw.get("canonical_products") or []
        if (req_brand or req_model) and canonical_list:
            def _resolved_value(value: Any) -> str | None:
                if isinstance(value, dict):
                    value = value.get("value")
                return str(value).strip() if value is not None else None

            for cp in canonical_list:
                cp_brand = _resolved_value(cp.get("brand"))
                cp_model = _resolved_value(cp.get("model"))
                if req_brand and cp_brand and req_brand.lower() != cp_brand.lower():
                    raise TerminalJobError(f"Brand mismatch: requested '{req_brand}', found '{cp_brand}'", code="IDENTITY_MISMATCH")
                if req_model and cp_model and req_model.lower() != cp_model.lower():
                    raise TerminalJobError(f"Model mismatch: requested '{req_model}', found '{cp_model}'", code="IDENTITY_MISMATCH")

        # Serialized report text for report field
        report_data = raw.get("report") or raw
        if isinstance(report_data, dict):
            report_str = json.dumps(report_data)
        else:
            report_str = str(report_data)

        if not report_str.strip() or report_str == "{}":
            raise TerminalJobError(f"Tool '{tool_name}' returned empty research report", code="EMPTY_RESEARCH_REPORT")

        return {
            "owner": "product_intelligence",
            "schema": "product_research_result",
            "version": 1,
            "product_name": product_name,
            "keywords": keywords,
            "pdp_url": pdp_url,
            "report": report_str,
            "snapshot_id": str(snapshot_id),
            "persistence_status": persistence_status,
            "research_id": raw.get("research_id"),
            "canonical_products": raw.get("canonical_products", []),
            "media_assets": raw.get("media_assets", []),
            "raw_result": raw,
            "external_provider_id": raw.get("id") or raw.get("external_id") or str(snapshot_id),
        }

    def build_resource_pack(
        self,
        research_result: dict,
        variant_id: str | None = None,
        workflow_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        tool_name = _require_snapshot_wire(
            self.wire_tools,
            "build_resource_pack",
            capability_id="product.resource_pack.build", binding_reasons=self.binding_reasons,
        )
        if not tool_name or tool_name not in _get_tool_names_from_registry(self.registry):
            raise RuntimeError(
                "MISSING_PROVIDER_CONFIG: Wire tool for 'product.resource_pack.build' is not registered in ToolRegistry. "
                "Fails closed without generating mock assets in configured mode."
            )

        # Validate snapshot_id and persistence_status prerequisites strictly
        snapshot_id = research_result.get("snapshot_id")
        persistence_status = research_result.get("persistence_status")
        if not snapshot_id:
            raise TerminalJobError("Cannot build resource pack: missing snapshot_id in research_result", code="MISSING_SNAPSHOT_ID")
        if persistence_status not in ("saved", "persisted"):
            raise TerminalJobError(
                f"Cannot build resource pack: research snapshot is not successfully persisted (persistence_status={persistence_status!r})",
                code="PERSISTENCE_FAILED",
            )

        payload: dict[str, Any] = {"snapshot_id": str(snapshot_id)}
        if variant_id:
            payload["variant_id"] = str(variant_id)

        raw = _dispatch_tool(self.registry, tool_name, payload)

        status_val = raw.get("status")
        if status_val == "not_found":
            raise TerminalJobError(f"Resource pack build failed: snapshot '{snapshot_id}' not found by provider", code="SNAPSHOT_NOT_FOUND")
        if status_val in ("failed", "error", "cancelled"):
            err_msg = raw.get("error") or raw.get("reason") or f"status '{status_val}'"
            raise TerminalJobError(f"Resource pack build failed: {err_msg}", code="PI_PACK_BUILD_FAILED")

        pack_id = raw.get("resource_pack_id")
        if not pack_id:
            raise TerminalJobError(f"Tool '{tool_name}' returned resource pack without resource_pack_id: {raw!r}", code="MISSING_PACK_ID")

        pack_status = raw.get("pack_status") or raw.get("status")
        if not pack_status:
            raise TerminalJobError(f"Tool '{tool_name}' returned resource pack without pack_status", code="MISSING_PACK_STATUS")

        if pack_status not in ("ready_to_lock", "built") and pack_status != "locked":
            reason = raw.get("reason") or "provider did not supply a reason"
            raise TerminalJobError(
                f"Resource pack build failed with non-lockable status '{pack_status}': {reason}",
                code="NON_LOCKABLE_STATUS",
            )

        canonical_products = research_result.get("canonical_products") or []
        if len(canonical_products) == 1 and isinstance(canonical_products[0], dict):
            expected_product_id = str(canonical_products[0].get("canonical_product_id") or "").strip()
            returned_product_id = str(raw.get("canonical_product_id") or "").strip()
            if expected_product_id and not returned_product_id:
                raise TerminalJobError(
                    "Resource pack build returned no canonical_product_id for the researched product",
                    code="MISSING_CANONICAL_PRODUCT_ID",
                )
            if expected_product_id and returned_product_id != expected_product_id:
                raise TerminalJobError(
                    f"Resource pack product mismatch: research identified '{expected_product_id}', pack returned '{returned_product_id}'",
                    code="PRODUCT_MISMATCH",
                )
        returned_variant_id = str(raw.get("variant_id") or "").strip()
        if variant_id and not returned_variant_id:
            raise TerminalJobError(
                f"Resource pack build returned no variant_id for requested variant '{variant_id}'",
                code="MISSING_VARIANT_ID",
            )
        if variant_id and returned_variant_id != str(variant_id).strip():
            raise TerminalJobError(
                f"Resource pack variant mismatch: requested '{variant_id}', provider returned '{returned_variant_id}'",
                code="VARIANT_MISMATCH",
            )

        source_ctx = kwargs.get("source_context") or research_result.get("source_context") or {}
        images = kwargs.get("images") or source_ctx.get("images") or research_result.get("images") or []
        assets_list = list(raw.get("assets") or raw.get("product_references") or [])
        if images:
            for idx, img in enumerate(images, start=1):
                if isinstance(img, dict):
                    loc_path = img.get("absolute_path") or img.get("local_path") or img.get("path")
                    asset_item = {
                        "asset_id": img.get("asset_id") or f"asset_{idx}",
                        "local_path": loc_path,
                        "absolute_path": loc_path,
                        "uri": img.get("uri") or (f"file:///{str(loc_path).replace(chr(92), '/')}" if loc_path else None),
                        "original_url": img.get("original_url"),
                        "width": img.get("width"),
                        "height": img.get("height"),
                        "sha256": img.get("sha256"),
                    }
                    assets_list.append(asset_item)
            raw["assets"] = assets_list
            raw["product_references"] = assets_list

        media_count = raw.get("media_count", len(assets_list or raw.get("assets_mapping", [])))
        if media_count < 1:
            raise TerminalJobError(f"Resource pack build failed: media_count {media_count} < 1", code="INSUFFICIENT_MEDIA_ASSETS")

        raw = dict(raw)
        raw.setdefault("version", 1)
        return {
            "owner": "product_intelligence",
            "schema": "product_resource_pack",
            "product_identity": dict(raw.get("product_identity") or {}),
            "resource_pack_id": str(pack_id),
            "version": int(raw.get("version") or 1),
            "pack_status": str(pack_status),
            "canonical_product_id": raw.get("canonical_product_id"),
            "variant_id": raw.get("variant_id") or variant_id,
            "snapshot_id": str(snapshot_id),
            "summary": raw.get("summary", ""),
            "media_count": max(media_count, len(assets_list)),
            "claims": raw.get("claims", []),
            "restrictions": raw.get("restrictions", []),
            "completeness": raw.get("completeness", {}),
            "assets": assets_list,
            "product_references": assets_list,
            "assets_mapping": raw.get("assets_mapping") or raw.get("assets", {}),
            "scene_plan": raw.get("scene_plan", []),
            "raw_result": raw,
            "external_provider_id": raw.get("id") or raw.get("external_id") or str(pack_id),
        }

    def lock_resource_pack(
        self,
        pack_dict: dict,
        owner_user_id: str | None = None,
        workflow_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not owner_user_id or not str(owner_user_id).strip():
            raise TerminalJobError("Cannot lock resource pack: explicit owner_user_id is required", code="MISSING_OWNER")

        resource_pack_id = pack_dict.get("resource_pack_id")
        if not resource_pack_id:
            raise TerminalJobError("Cannot lock resource pack: missing resource_pack_id in pack_dict", code="MISSING_PACK_ID")

        pack_status = pack_dict.get("pack_status")
        if pack_status != "ready_to_lock":
            raise TerminalJobError(
                f"Cannot lock resource pack with status '{pack_status}': only 'ready_to_lock' status is lockable",
                code="NON_LOCKABLE_STATUS",
            )

        tool_name = _require_snapshot_wire(
            self.wire_tools,
            "lock_resource_pack",
            capability_id="product.resource_pack.lock", binding_reasons=self.binding_reasons,
        )
        if not tool_name or tool_name not in _get_tool_names_from_registry(self.registry):
            raise RuntimeError(
                "MISSING_PROVIDER_CONFIG: Wire tool for 'product.resource_pack.lock' is not registered in ToolRegistry. "
                "Fails closed without generating mock lock in configured mode."
            )

        effective_owner = str(owner_user_id).strip()
        effective_project = kwargs.get("project_id") or (f"proj_{workflow_id}" if workflow_id else None)
        payload: dict[str, Any] = {
            "resource_pack_id": str(resource_pack_id),
            "owner_user_id": effective_owner,
        }
        if effective_project:
            payload["project_id"] = effective_project

        if workflow_id:
            payload["workflow_id"] = workflow_id
        if "approval_request_id" in kwargs and kwargs["approval_request_id"]:
            payload["approval_request_id"] = kwargs["approval_request_id"]

        raw = _dispatch_tool(self.registry, tool_name, payload)

        status_val = raw.get("status")
        if status_val == "not_found":
            raise TerminalJobError(f"Resource pack lock failed: resource_pack '{resource_pack_id}' not found by provider", code="PACK_NOT_FOUND")
        if status_val in ("failed", "error", "cancelled"):
            err_msg = raw.get("error") or raw.get("reason") or f"status '{status_val}'"
            raise TerminalJobError(f"Resource pack lock failed: {err_msg}", code="PI_PACK_LOCK_FAILED")

        lock_id = raw.get("lock_id") or raw.get("id")
        if not lock_id:
            raise TerminalJobError(f"Tool '{tool_name}' returned lock without lock_id: {raw!r}", code="MISSING_LOCK_ID")

        # Validate returned owner matches requested owner
        ret_owner = raw.get("owner_user_id")
        if ret_owner and ret_owner != effective_owner:
            raise TerminalJobError(
                f"Resource pack lock owner mismatch: requested '{effective_owner}', provider returned '{ret_owner}'",
                code="OWNER_MISMATCH",
            )

        # Validate returned pack ID matches requested pack ID
        ret_pack_id = raw.get("resource_pack_id")
        if ret_pack_id and ret_pack_id != str(resource_pack_id):
            raise TerminalJobError(
                f"Resource pack lock ID mismatch: requested '{resource_pack_id}', provider returned '{ret_pack_id}'",
                code="PACK_ID_MISMATCH",
            )

        # Validate snapshot_id and canonical_product_id match input pack if returned
        ret_snapshot = raw.get("snapshot_id")
        expected_snapshot = pack_dict.get("snapshot_id")
        if ret_snapshot and expected_snapshot and ret_snapshot != expected_snapshot:
            raise TerminalJobError(
                f"Resource pack lock snapshot mismatch: pack has '{expected_snapshot}', provider returned '{ret_snapshot}'",
                code="SNAPSHOT_MISMATCH",
            )

        ret_product = raw.get("canonical_product_id")
        expected_product = pack_dict.get("canonical_product_id")
        if ret_product and expected_product and ret_product != expected_product:
            raise TerminalJobError(
                f"Resource pack lock product mismatch: pack has '{expected_product}', provider returned '{ret_product}'",
                code="PRODUCT_MISMATCH",
            )

        ret_variant = raw.get("variant_id")
        expected_variant = pack_dict.get("variant_id")
        if ret_variant and expected_variant and ret_variant != expected_variant:
            raise TerminalJobError(
                f"Resource pack lock variant mismatch: pack has '{expected_variant}', provider returned '{ret_variant}'",
                code="VARIANT_MISMATCH",
            )

        manifest_digest = raw.get("manifest_digest")
        if not manifest_digest:
            raise TerminalJobError(f"Tool '{tool_name}' returned lock without manifest_digest: {raw!r}", code="MISSING_MANIFEST_DIGEST")

        assets = raw.get("assets") or raw.get("assets_mapping")
        if not assets:
            raise TerminalJobError(f"Tool '{tool_name}' returned lock without valid assets", code="MISSING_ASSETS")

        return {
            "owner": "product_intelligence",
            "schema": "product_resource_pack_locked",
            "version": raw.get("version", 1),
            "lock_id": str(lock_id),
            "resource_pack_id": str(resource_pack_id),
            "snapshot_id": ret_snapshot or expected_snapshot,
            "canonical_product_id": ret_product or expected_product,
            "variant_id": pack_dict.get("variant_id"),
            "owner_user_id": effective_owner,
            "manifest_digest": str(manifest_digest),
            "created_at": raw.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "status": "locked",
            "summary": pack_dict.get("summary", ""),
            "assets": assets,
            "assets_mapping": assets if isinstance(assets, dict) else {a.get("asset_id", f"asset_{i}"): a.get("local_path", "") for i, a in enumerate(assets)},
            "scene_plan": pack_dict.get("scene_plan", []),
            "raw_result": raw,
            "external_provider_id": raw.get("id") or raw.get("external_id") or str(lock_id),
        }


class MCPVideoFactoryAdapter:
    """Live provider adapter bridging ProductVideoJobHandlers to real/MCP VideoFactory tools."""

    def __init__(self, registry: Any, wire_tools: dict[str, str], poll_timeout: float = 60.0, poll_interval: float = 0.5, binding_reasons: dict[str, str] | None = None) -> None:
        self.registry = registry
        self.wire_tools = wire_tools
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self.binding_reasons = binding_reasons or {}

    def require_execution_binding(self, stage_key: str, capability_id: str) -> str:
        """Preflight an immutable execution binding without dispatching I/O."""
        return _require_snapshot_wire(
            self.wire_tools,
            stage_key,
            capability_id=capability_id,
            binding_reasons=self.binding_reasons,
        )

    @staticmethod
    def _run_heartbeat(heartbeat_fn: Callable[[], bool] | None, job_id_val: str, stage: str, poll_interval: float = 0.0) -> None:
        if heartbeat_fn is None:
            if poll_interval:
                time.sleep(poll_interval)
            return
        try:
            hb_ok = heartbeat_fn()
        except Exception as exc:
            raise LeaseLostError(f"Heartbeat failure during async video {stage} job '{job_id_val}' polling: {exc}") from exc
        if hb_ok is False:
            raise LeaseLostError(f"Lease lost during async video {stage} job '{job_id_val}' polling. Halting execution.")
        if poll_interval:
            time.sleep(poll_interval)

    def ensure_project_locked(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        resource_pack: dict[str, Any],
        dossier: dict[str, Any],
        approval_receipt: dict[str, Any],
        workflow_id: str,
        workflow_version: int | None,
    ) -> None:
        """Mirror the approved PI lock into Video Factory before a brief exists.

        This is intentionally idempotent: project creation and a matching
        already-locked pack are both safe resume states.  Only Image-QC
        eligible assets cross the bounded-context boundary.
        """
        required = ("project_create", "resource_pack_save", "resource_pack_lock")
        missing = [key for key in required if not self.wire_tools.get(key)]
        if missing:
            raise RuntimeError(f"Video Factory project synchronization tools are unavailable: {', '.join(missing)}")

        identity = dossier.get("product_identity")
        if not isinstance(identity, dict):
            raise ValueError("grounded dossier product identity is required")
        description = str(identity.get("display_name") or "").strip()
        if not description:
            raise ValueError("grounded dossier product display name is required")
        identity_payload = {
            "description": description,
            "shape": str(identity.get("shape") or ""),
            "color": str(identity.get("color") or ""),
            "materials": str(identity.get("materials") or ""),
            "logo_placement": str(identity.get("logo_placement") or ""),
            "distinctive_features": list(identity.get("distinctive_features") or []),
            "silhouette": str(identity.get("silhouette") or ""),
            "main_proportions": str(identity.get("main_proportions") or ""),
            "product_count": str(identity.get("product_count") or ""),
            "control_placement": str(identity.get("control_placement") or ""),
            "port_count": str(identity.get("port_count") or ""),
            "port_placement": str(identity.get("port_placement") or ""),
            "display_shape": str(identity.get("display_shape") or ""),
        }
        eligible_assets = dossier.get("eligible_assets")
        if not isinstance(eligible_assets, (list, tuple)) or not eligible_assets:
            raise ValueError("at least one usable Image-QC reference is required")
        references = [
            {
                "asset_id": str(asset["asset_id"]),
                "uri": str(asset["path"]),
                "metadata": {
                    "sha256": str(asset["sha256"]),
                    "qc_report_id": str(asset["qc_report_id"]),
                    "provenance": "product_intelligence",
                },
            }
            for asset in eligible_assets
        ]
        pack_id = str(resource_pack.get("resource_pack_id") or resource_pack.get("lock_id") or "").strip()
        if not pack_id:
            raise ValueError("locked resource pack ID is required")
        proj_res = _dispatch_tool(self.registry, self.wire_tools["project_create"], {
            "owner_user_id": owner_user_id,
            "project_id": project_id,
        })
        existing_proj = (proj_res.get("project") or {}) if isinstance(proj_res, dict) else {}
        existing_rp = existing_proj.get("resource_pack") or {}
        if existing_rp.get("locked_product_identity"):
            return
        pack_input = {
            "id": pack_id,
            "product_references": references,
            "primary_product_asset_id": references[0]["asset_id"],
            "product_identity_description": description,
            "context": "Grounded product video; source images are reference-only.",
            "visual_style": "Natural vertical product demonstration.",
        }
        try:
            _dispatch_tool(self.registry, self.wire_tools["resource_pack_save"], {
                "owner_user_id": owner_user_id,
                "project_id": project_id,
                "resource_pack": pack_input,
            })
        except Exception as exc:
            if "RESOURCE_IDENTITY_LOCKED" not in str(exc):
                raise
        approval_request_id = str(approval_receipt.get("approval_request_id") or "").strip()
        if not approval_request_id:
            raise ValueError("approved Product Lock receipt is required")
        try:
            _dispatch_tool(self.registry, self.wire_tools["resource_pack_lock"], {
                "owner_user_id": owner_user_id,
                "project_id": project_id,
                "product_identity": identity_payload,
                "approval_request_id": approval_request_id,
                "workflow_id": workflow_id,
                "workflow_version": workflow_version,
                "eligible_reference_manifest": [
                    {"asset_id": ref["asset_id"], "eligible": True, "sha256": ref["metadata"]["sha256"], "uri": ref["uri"]}
                    for ref in references
                ],
            })
        except Exception as exc:
            if "RESOURCE_IDENTITY_LOCKED" not in str(exc):
                raise

    def get_existing_creative_brief(
        self, *, owner_user_id: str, project_id: str
    ) -> dict[str, Any] | None:
        """Return an already persisted brief for crash recovery, if any."""
        tool_name = self.wire_tools.get("creative_brief_get")
        if not tool_name:
            return None
        try:
            raw = _dispatch_tool(self.registry, tool_name, {
                "owner_user_id": owner_user_id,
                "project_id": project_id,
            })
        except Exception as exc:
            if "CREATIVE_BRIEF_NOT_FOUND" in str(exc):
                return None
            raise
        project = raw.get("project")
        if not isinstance(project, dict):
            return None
        brief = project.get("creative_brief")
        return dict(brief) if isinstance(brief, dict) else None

    def create_creative_brief(self, product_id: str = "", brief_config: dict | None = None, **kwargs: Any) -> dict[str, Any]:
        tool_name = _require_snapshot_wire(
            self.wire_tools, "brief", capability_id="video.brief.create", binding_reasons=self.binding_reasons
        )
        cfg = dict(brief_config or {})
        proj_id = kwargs.get("project_id") or cfg.get("project_id") or ""
        if not proj_id and kwargs.get("workflow_id"):
            proj_id = f"proj_{kwargs['workflow_id']}"
        cfg.pop("project_id", None)
        cfg.pop("brief_id", None)
        cfg.pop("resource_pack", None)
        cfg.pop("research_dossier", None)
        payload: dict[str, Any] = {"product_id": product_id, "brief_config": cfg, "project_id": proj_id}
        if "owner_user_id" in kwargs:
            payload["owner_user_id"] = kwargs["owner_user_id"]
        raw = _dispatch_tool(self.registry, tool_name, payload)

        brief_id = raw.get("brief_id") or raw.get("id")
        has_content = bool(raw.get("script") or raw.get("angle") or raw.get("scenes") or raw.get("brief_config") or raw.get("content") or raw.get("brief_id"))
        if not has_content and not brief_id:
            raise RuntimeError(f"Tool '{tool_name}' returned empty creative brief content")

        return {
            "owner": "video_factory",
            "schema": "creative_brief",
            "version": 1,
            "product_id": product_id,
            "project_id": proj_id,
            "brief_id": brief_id or f"brief_{product_id}",
            "payload": json.dumps({"product_id": product_id, "brief_config": cfg, "result": raw}),
            "external_provider_id": raw.get("id") or raw.get("external_id"),
        }

    def create_scene_plan(self, brief_result: dict, **kwargs: Any) -> dict[str, Any]:
        tool_name = _require_snapshot_wire(
            self.wire_tools, "scene_plan", capability_id="video.scene_plan.create", binding_reasons=self.binding_reasons
        )
        brief_dict = dict(brief_result or {})
        proj_id = kwargs.get("project_id") or brief_dict.get("project_id") or ""
        if not proj_id and kwargs.get("workflow_id"):
            proj_id = f"proj_{kwargs['workflow_id']}"
        if proj_id:
            brief_dict.setdefault("project_id", proj_id)
        owner_id = kwargs.get("owner_user_id", "default-owner")
        appr_id = kwargs.get("approval_request_id") or (kwargs.get("approval_receipt", {}) or {}).get("approval_request_id")

        available_tools = _get_tool_names_from_registry(self.registry)
        brief_approve_tool = self.wire_tools.get("brief_approve")
        if brief_approve_tool and brief_approve_tool in available_tools:
            try:
                _dispatch_tool(self.registry, brief_approve_tool, {
                    "owner_user_id": owner_id,
                    "project_id": proj_id,
                    "approval_request_id": appr_id or "auto_approval",
                    "workflow_id": kwargs.get("workflow_id", ""),
                    # Legacy receipts may not carry this field. Let the server
                    # validate the approved request bindings instead of
                    # inventing version 1, which can reject a valid approval.
                    "workflow_version": (kwargs.get("approval_receipt") or {}).get("workflow_version"),
                })
            except Exception:
                pass

        payload: dict[str, Any] = {"brief_result": brief_dict, "project_id": proj_id, "owner_user_id": owner_id}
        raw = _dispatch_tool(self.registry, tool_name, payload)

        scene_plan_id = raw.get("scene_plan_id") or raw.get("id")
        if not scene_plan_id and not raw.get("scenes") and not raw.get("steps"):
            raise RuntimeError(f"Tool '{tool_name}' returned empty scene plan output")

        return {
            "owner": "video_factory",
            "schema": "scene_plan",
            "version": 1,
            "project_id": proj_id,
            "scene_plan_id": scene_plan_id or f"scene_{brief_result.get('product_id', 'unknown')}",
            "payload": json.dumps(raw),
            "external_provider_id": raw.get("id") or raw.get("external_id"),
        }

    def create_storyboard(self, scene_plan_result: dict, **kwargs: Any) -> dict[str, Any]:
        tool_name = _require_snapshot_wire(
            self.wire_tools, "storyboard", capability_id="video.storyboard.create", binding_reasons=self.binding_reasons
        )
        scene_dict = dict(scene_plan_result or {})
        proj_id = kwargs.get("project_id") or scene_dict.get("project_id") or ""
        if not proj_id and kwargs.get("workflow_id"):
            proj_id = f"proj_{kwargs['workflow_id']}"
        if proj_id:
            scene_dict.setdefault("project_id", proj_id)
        dossier = scene_dict.get("research_dossier")
        eligible_ids = [
            str(item.get("asset_id"))
            for item in (dossier or {}).get("eligible_assets", [])
            if isinstance(item, dict) and item.get("asset_id")
        ]
        if eligible_ids:
            for scene in scene_dict.get("scenes", []) or []:
                if isinstance(scene, dict) and not scene.get("reference_asset_ids"):
                    scene["reference_asset_ids"] = eligible_ids[:2]
        owner_id = kwargs.get("owner_user_id", "default-owner")
        appr_id = kwargs.get("approval_request_id") or (kwargs.get("approval_receipt", {}) or {}).get("approval_request_id")

        available_tools = _get_tool_names_from_registry(self.registry)
        scene_approve_tool = self.wire_tools.get("scene_plan_approve")
        if scene_approve_tool and scene_approve_tool in available_tools:
            try:
                _dispatch_tool(self.registry, scene_approve_tool, {
                    "owner_user_id": owner_id,
                    "project_id": proj_id,
                    "approval_request_id": appr_id or "auto_approval",
                    "workflow_id": kwargs.get("workflow_id", ""),
                    "workflow_version": (kwargs.get("approval_receipt") or {}).get("workflow_version", 1),
                })
            except Exception:
                pass

        payload: dict[str, Any] = {"scene_plan_result": scene_dict, "project_id": proj_id, "owner_user_id": owner_id}
        raw = _dispatch_tool(self.registry, tool_name, payload)

        if raw.get("ok") is not True:
            raise RuntimeError(
                f"VF storyboard persistence failed for {proj_id}: "
                f"{raw.get('code') or raw.get('error') or 'unconfirmed result'}"
            )

        storyboard_id = raw.get("storyboard_id") or raw.get("id")
        if not storyboard_id and not raw.get("frames") and not raw.get("scenes"):
            raise RuntimeError(f"Tool '{tool_name}' returned empty storyboard output")

        return {
            "owner": "video_factory",
            "schema": "storyboard",
            "version": 1,
            "storyboard_id": storyboard_id or "sb_custom",
            "ok": True,
            "project_id": proj_id,
            "owner_user_id": owner_id,
            "payload": json.dumps(raw),
            "external_provider_id": raw.get("id") or raw.get("external_id"),
        }

    def render_video(
        self,
        storyboard_result: dict,
        render_params: dict | None = None,
        workflow_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        params = dict(render_params or {})
        existing_job_id = params.get("active_job_id") or params.get("job_id") or kwargs.get("active_job_id")
        on_job_accepted: Callable[[str, str], None] | None = kwargs.get("on_job_accepted")
        heartbeat_fn: Callable[[], bool] | None = kwargs.get("heartbeat_fn")
        owner_id = kwargs.get("owner_user_id", "default-owner")
        proj_id = kwargs.get("project_id") or ""
        if not proj_id and workflow_id:
            proj_id = f"proj_{workflow_id}"
        appr_id = kwargs.get("approval_request_id") or (kwargs.get("approval_receipt", {}) or {}).get("approval_request_id")

        if existing_job_id:
            # Job already accepted by provider — resume polling directly without re-submitting paid job
            raw = {"status": "pending", "job_id": existing_job_id}
        else:
            tool_name = _require_snapshot_wire(
                self.wire_tools, "render", capability_id="video.render", binding_reasons=self.binding_reasons
            )
            if tool_name != "mcp__video_factory__render":
                raise TerminalJobError(
                    "Product Video render requires the active canonical "
                    "mcp__video_factory__render capability binding",
                    code="CANONICAL_RENDER_BINDING_REQUIRED",
                )
            sb_approve_tool = self.wire_tools.get("storyboard_approve")
            if sb_approve_tool and appr_id and sb_approve_tool in _get_tool_names_from_registry(self.registry):
                _dispatch_tool(self.registry, sb_approve_tool, {
                    "owner_user_id": owner_id, "project_id": proj_id,
                    "approval_request_id": appr_id, "workflow_id": workflow_id or "",
                    "workflow_version": (kwargs.get("approval_receipt") or {}).get("workflow_version"),
                })
            payload: dict[str, Any] = {
                "storyboard_result": storyboard_result,
                "render_params": params,
                "owner_user_id": owner_id,
                "project_id": proj_id,
            }
            if idempotency_key:
                payload["idempotency_key"] = idempotency_key
            if appr_id:
                payload["approval_request_id"] = appr_id
            raw = _dispatch_tool(self.registry, tool_name, payload)

        # Check accepted work once per delivery; the durable outbox owns the wait.
        waiting_statuses = {"pending", "processing", "in_progress", "queued", "running", "submitted"}
        if str(raw.get("status", "")).lower() in waiting_statuses:
            job_id_val = str(raw.get("job_id") or raw.get("id") or existing_job_id or "")
            if not job_id_val:
                raise TerminalJobError("Async video render job returned pending status but missing job_id in response", code="MISSING_JOB_ID")

            # Checkpoint provider job_id immediately upon receipt BEFORE starting polling
            if not existing_job_id and on_job_accepted is not None:
                on_job_accepted("render", job_id_val)

            poll_tool = self.wire_tools.get("get_job_status")
            if poll_tool in _get_tool_names_from_registry(self.registry):
                self._run_heartbeat(heartbeat_fn, job_id_val, "render")
                poll_payload = {"job_id": job_id_val}
                if params.get('force_recompose'):
                    poll_payload['force_recompose'] = True
                if params.get("provider_kind"):
                    poll_payload["provider_kind"] = params["provider_kind"]
                raw = _dispatch_tool(self.registry, poll_tool, poll_payload)
                if str(raw.get("status", "")).lower() in waiting_statuses:
                    raise JobDeferred(
                        f"Video render job '{job_id_val}' is still running",
                        delay_seconds=self.poll_interval,
                    )
            else:
                raise TerminalJobError(
                    f"Async render job '{job_id_val}' returned status '{raw.get('status')}', but no status polling tool "
                    f"('{poll_tool}') is registered. Cannot mark render completed without final video output reference.",
                    code="MISSING_POLLING_TOOL",
                )

        if str(raw.get("status", "")).lower() not in {"completed", "ready", "succeeded"}:
            raise TerminalJobError(
                f"Video render outcome requires reconciliation: {raw!r}",
                code="RECONCILIATION_REQUIRED",
            )

        # Enforce that output reference is present and non-empty
        video_url = raw.get("video_url") or raw.get("output_url") or raw.get("url")
        render_id = raw.get("render_id") or (raw.get("id") if raw.get("status") in ("completed", "ready", "succeeded") and not raw.get("job_id") else None)
        if not render_id and not video_url:
            raise RuntimeError(f"Video render response missing required output reference (render_id or video_url): {raw!r}")

        required_lineage = (
            "owner_user_id",
            "project_id",
            "workflow_id",
            "artifact_id",
            "artifact_version",
            "sha256",
        )
        missing_lineage = [
            field
            for field in required_lineage
            if field not in raw
            or raw[field] is None
            or (isinstance(raw[field], str) and not raw[field].strip())
        ]
        if missing_lineage:
            raise TerminalJobError(
                "Completed render response is missing canonical lineage fields "
                f"{missing_lineage}: {raw!r}",
                code="RENDER_LINEAGE_INVALID",
            )

        polled_ver_raw = raw["artifact_version"]
        if (
            isinstance(polled_ver_raw, bool)
            or not isinstance(polled_ver_raw, int)
            or polled_ver_raw < 1
        ):
            raise TerminalJobError(
                "Completed render response artifact_version must be a positive integer: "
                f"{raw!r}",
                code="RENDER_LINEAGE_INVALID",
            )
        polled_ver = polled_ver_raw
        compatibility_version = raw.get("version")
        if compatibility_version is not None and compatibility_version != polled_ver:
            raise TerminalJobError(
                "Completed render response version does not match artifact_version: "
                f"{raw!r}",
                code="RENDER_LINEAGE_MISMATCH",
            )

        canonical_sha = str(raw["sha256"]).strip().removeprefix("sha256:").lower()
        if len(canonical_sha) != 64 or any(ch not in "0123456789abcdef" for ch in canonical_sha):
            raise TerminalJobError(
                f"Completed render response contains an invalid sha256: {raw!r}",
                code="RENDER_LINEAGE_INVALID",
            )

        expected_lineage = {
            "owner_user_id": kwargs.get("owner_user_id"),
            "project_id": kwargs.get("project_id"),
            "workflow_id": workflow_id,
        }
        mismatches = {
            field: (str(expected).strip(), str(raw[field]).strip())
            for field, expected in expected_lineage.items()
            if expected is not None
            and str(expected).strip()
            and str(raw[field]).strip() != str(expected).strip()
        }
        if mismatches:
            raise TerminalJobError(
                "Completed render response lineage does not match the submitted workflow: "
                f"{mismatches}; raw={raw!r}",
                code="RENDER_LINEAGE_MISMATCH",
            )

        return {
            "owner": "video_factory",
            "schema": "video_render",
            "version": polled_ver,
            "render_id": render_id or f"render_{raw.get('job_id', 'output')}",
            "video_url": video_url,
            "owner_user_id": str(raw["owner_user_id"]).strip(),
            "project_id": str(raw["project_id"]).strip(),
            "workflow_id": str(raw["workflow_id"]).strip(),
            "artifact_id": str(raw["artifact_id"]).strip(),
            "artifact_version": polled_ver,
            "sha256": canonical_sha,
            "payload": json.dumps(raw),
            "external_provider_id": raw.get("id") or raw.get("external_id") or raw.get("job_id"),
            **({key:raw[key] for key in ('creative_mode','technical_qc','final_review','review_context',
                'review_media','review_input_digests','content_assessment','human_review','file_path','creative_lineage')
                if key in raw} if raw.get('creative_mode')=='four_agent' else {}),
        }

    def export_video(
        self,
        render_result: dict,
        export_params: dict | None = None,
        workflow_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        params = dict(export_params or {})
        existing_job_id = params.get("active_job_id") or params.get("job_id") or params.get("export_id") or kwargs.get("active_job_id")
        on_job_accepted: Callable[[str, str], None] | None = kwargs.get("on_job_accepted")
        heartbeat_fn: Callable[[], bool] | None = kwargs.get("heartbeat_fn")
        owner_id = kwargs.get("owner_user_id", "default-owner")
        proj_id = kwargs.get("project_id") or ""
        if not proj_id and workflow_id:
            proj_id = f"proj_{workflow_id}"
        appr_id = kwargs.get("approval_request_id") or (kwargs.get("approval_receipt", {}) or {}).get("approval_request_id")

        if existing_job_id:
            # Job already accepted by provider — resume polling directly without re-submitting paid job
            raw = {"status": "pending", "job_id": existing_job_id}
        else:
            tool_name = _require_snapshot_wire(
                self.wire_tools, "export", capability_id="video.export", binding_reasons=self.binding_reasons
            )
            final_approve_tool = self.wire_tools.get("final_video_approve")
            if final_approve_tool and appr_id and final_approve_tool in _get_tool_names_from_registry(self.registry):
                _dispatch_tool(self.registry, final_approve_tool, {
                    "owner_user_id": owner_id, "project_id": proj_id,
                    "approval_request_id": appr_id, "workflow_id": workflow_id or "",
                })
            payload: dict[str, Any] = {
                "render_result": render_result,
                "export_params": params,
                "owner_user_id": owner_id,
                "project_id": proj_id,
                "workflow_id": workflow_id or "",
            }
            if idempotency_key:
                payload["idempotency_key"] = idempotency_key
            if appr_id:
                payload["approval_request_id"] = appr_id
            raw = _dispatch_tool(self.registry, tool_name, payload)

        # Handle async export status polling if applicable
        if isinstance(raw, dict) and raw.get("status") in ("pending", "processing", "in_progress", "queued"):
            job_id_val = str(raw.get("job_id") or raw.get("export_id") or raw.get("id") or existing_job_id or "")
            if not job_id_val:
                raise TerminalJobError("Async video export job returned pending status but missing job_id in response", code="MISSING_JOB_ID")

            # Checkpoint provider job_id immediately upon receipt BEFORE starting polling
            if not existing_job_id and on_job_accepted is not None:
                on_job_accepted("export", job_id_val)

            poll_tool = self.wire_tools.get("get_export_status") or self.wire_tools.get("get_job_status")
            if poll_tool and poll_tool in _get_tool_names_from_registry(self.registry):
                start_mono = time.monotonic()
                deadline = start_mono + self.poll_timeout
                while True:
                    self._run_heartbeat(heartbeat_fn, job_id_val, "export", self.poll_interval)
                    status_raw = _dispatch_tool(self.registry, poll_tool, {"job_id": job_id_val, "export_id": job_id_val})
                    st = str(status_raw.get("status", "")).lower()
                    if st in ("completed", "ready", "succeeded"):
                        raw = status_raw
                        break
                    if st in ("failed", "error", "cancelled"):
                        err_detail = status_raw.get("error") or status_raw.get("message") or f"status '{st}'"
                        raise TerminalJobError(f"Async video export job '{job_id_val}' failed: {err_detail}", code="VF_EXPORT_TERMINAL_FAILED")
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"Async video export job '{job_id_val}' timed out after {self.poll_timeout}s. "
                            f"Action required: operator intervention or resume polling with active_job_id='{job_id_val}'. Do not resubmit paid job."
                        )
            else:
                raise TerminalJobError(
                    f"Async export job '{job_id_val}' returned status '{raw.get('status')}', but no status polling tool is registered. "
                    "Cannot mark export completed without final exported video output reference.",
                    code="MISSING_POLLING_TOOL",
                )

        final_video_url = raw.get("final_video_url") or raw.get("export_url") or raw.get("url")
        export_id = raw.get("export_id") or (raw.get("id") if raw.get("status") in ("completed", "ready", "succeeded") and not raw.get("job_id") else None)
        if not export_id and not final_video_url:
            raise RuntimeError(f"Video export response missing required final output reference (export_id or final_video_url): {raw!r}")

        return {
            "owner": "video_factory",
            "schema": "video_export",
            "version": 1,
            "export_id": export_id or f"export_{render_result.get('render_id', 'output')}",
            "final_video_url": final_video_url,
            "payload": json.dumps(raw),
            "external_provider_id": raw.get("id") or raw.get("external_id") or raw.get("job_id"),
        }

__all__ = [
    "_get_global_tool_registry",
    "_get_tool_names_from_registry",
    "_check_for_errors",
    "_decode_mcp_output",
    "_dispatch_tool",
    "_require_snapshot_wire",
    "_resolve_snapshot_execution_wire",
    "MCPProductIntelligenceAdapter",
    "MCPVideoFactoryAdapter",
]
