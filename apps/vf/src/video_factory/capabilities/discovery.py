from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol

from video_factory.capabilities.bindings import ImplementationBinding
from video_factory.capabilities.manifest import INITIAL_CAPABILITIES
from video_factory.capabilities.contracts import CapabilitySource, HealthStatus


class UpstreamToolRegistry(Protocol):
    def get_all_tool_names(self) -> list[str]:
        ...

    def get_definitions(
        self, tool_names: set[str], quiet: bool = False
    ) -> list[dict[str, Any]]:
        ...

    def get_schema(self, name: str) -> dict[str, Any] | None:
        ...

    def get_toolset_for_tool(self, name: str) -> str | None:
        ...


@dataclass(frozen=True)
class WireBindingRule:
    capability_id: str
    accepted_wire_names: tuple[str, ...]
    provider: str
    source: CapabilitySource = "hermes_mcp"

    def __post_init__(self) -> None:
        if self.capability_id not in INITIAL_CAPABILITIES:
            raise ValueError(
                f"unknown capability_id outside INITIAL_CAPABILITIES: {self.capability_id}"
            )
        if not self.accepted_wire_names:
            raise ValueError("accepted_wire_names must not be empty")
        if len(self.accepted_wire_names) != len(set(self.accepted_wire_names)):
            raise ValueError(
                f"duplicate accepted_wire_names in rule for capability_id: {self.capability_id}"
            )


DEFAULT_WIRE_BINDING_RULES: tuple[WireBindingRule, ...] = (
    WireBindingRule(
        capability_id="product.research",
        accepted_wire_names=(
            "mcp__product_intelligence__research_product",
            "product_intelligence_research",
        ),
        provider="product_intelligence",
    ),
    WireBindingRule(
        capability_id="product.resource_pack.build",
        accepted_wire_names=(
            "mcp__product_intelligence__build_resource_pack",
            "product_intelligence_build_resource_pack",
        ),
        provider="product_intelligence",
    ),
    WireBindingRule(
        capability_id="product.resource_pack.lock",
        accepted_wire_names=(
            "mcp__product_intelligence__lock_resource_pack",
            "product_intelligence_lock_resource_pack",
        ),
        provider="product_intelligence",
    ),
    WireBindingRule(
        capability_id="video.brief.create",
        accepted_wire_names=(
            "mcp__video_factory__create_brief",
            "video_factory_create_brief",
        ),
        provider="video_factory",
    ),
    WireBindingRule(
        capability_id="video.scene_plan.create",
        accepted_wire_names=(
            "mcp__video_factory__create_scene_plan",
            "video_factory_create_scene_plan",
        ),
        provider="video_factory",
    ),
    WireBindingRule(
        capability_id="video.storyboard.create",
        accepted_wire_names=(
            "mcp__video_factory__create_storyboard",
            "video_factory_create_storyboard",
        ),
        provider="video_factory",
    ),
    WireBindingRule(
        capability_id="video.render",
        accepted_wire_names=(
            "mcp__video_factory__render",
            "video_factory_render",
        ),
        provider="video_factory",
    ),
    WireBindingRule(
        capability_id="video.export",
        accepted_wire_names=(
            "mcp__video_factory__export",
            "video_factory_export",
        ),
        provider="video_factory",
    ),
    WireBindingRule(
        capability_id="knowledge.candidate.create",
        accepted_wire_names=(
            "mcp__learning__create_candidate",
            "learning_create_candidate",
            "mcp__knowledge__knowledge_candidate_create",
            "knowledge_candidate_create",
        ),
        provider="learning",
    ),
    WireBindingRule(
        capability_id="knowledge.retrieve",
        accepted_wire_names=(
            "mcp__learning__retrieve",
            "learning_retrieve",
            "mcp__knowledge__knowledge_retrieve",
            "knowledge_retrieve",
        ),
        provider="learning",
    ),
)


@dataclass(frozen=True)
class DiscoveryDiagnostic:
    capability_id: str
    code: str
    message: str
    wire_name: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    bindings: tuple[ImplementationBinding, ...]
    by_capability_id: Mapping[str, ImplementationBinding]
    diagnostics: tuple[DiscoveryDiagnostic, ...]


def hash_schema_dict(schema: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"
    except (TypeError, ValueError) as exc:
        raise ValueError(f"schema dict is not JSON serializable: {exc}") from exc


def _sentinel_digest(status: str, capability_id: str, wire_name: str) -> str:
    payload = {
        "sentinel_status": status,
        "capability_id": capability_id,
        "wire_name": wire_name,
    }
    return hash_schema_dict(payload)


class CapabilityDiscovery:
    def __init__(
        self,
        rules: Iterable[WireBindingRule] = DEFAULT_WIRE_BINDING_RULES,
    ) -> None:
        rule_tuple = tuple(rules)
        rule_caps: set[str] = set()
        for rule in rule_tuple:
            if rule.capability_id in rule_caps:
                raise ValueError(
                    f"duplicate WireBindingRule for capability_id: {rule.capability_id}"
                )
            rule_caps.add(rule.capability_id)

        missing_caps = set(INITIAL_CAPABILITIES.keys()) - rule_caps
        if missing_caps:
            raise ValueError(
                f"missing WireBindingRule for manifest capabilities: {sorted(missing_caps)}"
            )

        self._rules = rule_tuple

    def scan(self, registry: UpstreamToolRegistry) -> DiscoveryResult:
        all_registered_names = set(registry.get_all_tool_names())
        candidate_wire_names = {
            wire for rule in self._rules for wire in rule.accepted_wire_names
        }
        registered_candidates = candidate_wire_names & all_registered_names
        get_defs = getattr(registry, "get_definitions", None)
        if callable(get_defs):
            definitions = get_defs(registered_candidates, quiet=True)
        else:
            definitions = [
                {"name": name, "function": {"name": name, "parameters": {}}}
                for name in registered_candidates
            ]

        # Index returned definition envelopes by wire name
        def_envelopes: dict[str, dict[str, Any] | Any] = {}
        for d in definitions:
            if isinstance(d, dict):
                fn = d.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    def_envelopes[fn["name"]] = d
                elif isinstance(d.get("name"), str):
                    def_envelopes[d["name"]] = d
                else:
                    # If function is present or item is present without a clean name,
                    # index by candidate matching if possible
                    for cand in registered_candidates:
                        if cand not in def_envelopes:
                            def_envelopes[cand] = d

        bindings_by_id: dict[str, ImplementationBinding] = {}
        diagnostics: list[DiscoveryDiagnostic] = []
        rules_by_id = {r.capability_id: r for r in self._rules}

        wire_to_capabilities: dict[str, list[str]] = {}
        for rule in self._rules:
            for wire in rule.accepted_wire_names:
                if wire in all_registered_names:
                    wire_to_capabilities.setdefault(wire, []).append(rule.capability_id)

        for cap_id in sorted(INITIAL_CAPABILITIES.keys()):
            rule = rules_by_id[cap_id]
            spec = INITIAL_CAPABILITIES[cap_id]
            input_ver = spec.input_schema_version
            output_ver = spec.output_schema_version

            matching_wires = [
                w for w in rule.accepted_wire_names if w in all_registered_names
            ]

            if len(matching_wires) > 1:
                wire_name = matching_wires[0]
                health: HealthStatus = "unavailable"
                diag_code = "ambiguous_wire_mapping"
                diag_msg = f"multiple registered tools match rule: {matching_wires}"
                diagnostics.append(
                    DiscoveryDiagnostic(
                        capability_id=cap_id,
                        code=diag_code,
                        message=diag_msg,
                        wire_name=wire_name,
                    )
                )
                bindings_by_id[cap_id] = ImplementationBinding(
                    capability_id=cap_id,
                    wire_name=wire_name,
                    source=rule.source,
                    provider=rule.provider,
                    implementation_version="1.0.0",
                    input_schema_version=input_ver,
                    output_schema_version=output_ver,
                    schema_digest=_sentinel_digest("ambiguous", cap_id, wire_name),
                    health=health,
                )
                continue

            if not matching_wires:
                wire_name = rule.accepted_wire_names[0]
                health = "unavailable"
                diag_code = "tool_not_found"
                diag_msg = f"wire tool name {wire_name!r} not found in ToolRegistry"
                diagnostics.append(
                    DiscoveryDiagnostic(
                        capability_id=cap_id,
                        code=diag_code,
                        message=diag_msg,
                        wire_name=wire_name,
                    )
                )
                bindings_by_id[cap_id] = ImplementationBinding(
                    capability_id=cap_id,
                    wire_name=wire_name,
                    source=rule.source,
                    provider=rule.provider,
                    implementation_version="1.0.0",
                    input_schema_version=input_ver,
                    output_schema_version=output_ver,
                    schema_digest=_sentinel_digest("missing", cap_id, wire_name),
                    health=health,
                )
                continue

            wire_name = matching_wires[0]

            claiming_caps = wire_to_capabilities.get(wire_name, [])
            if len(claiming_caps) > 1:
                diagnostics.append(
                    DiscoveryDiagnostic(
                        capability_id=cap_id,
                        code="duplicate_wire_ownership",
                        message=f"wire {wire_name!r} claimed by multiple capabilities: {claiming_caps}",
                        wire_name=wire_name,
                    )
                )
                bindings_by_id[cap_id] = ImplementationBinding(
                    capability_id=cap_id,
                    wire_name=wire_name,
                    source=rule.source,
                    provider=rule.provider,
                    implementation_version="1.0.0",
                    input_schema_version=input_ver,
                    output_schema_version=output_ver,
                    schema_digest=_sentinel_digest("duplicate", cap_id, wire_name),
                    health="unavailable",
                )
                continue

            get_toolset = getattr(registry, "get_toolset_for_tool", None)
            provider = (
                (get_toolset(wire_name) if callable(get_toolset) else None)
                or rule.provider
            )

            if wire_name not in def_envelopes:
                get_schema = getattr(registry, "get_schema", None)
                raw_schema = get_schema(wire_name) if callable(get_schema) else {}
                try:
                    digest = hash_schema_dict(raw_schema)
                except ValueError:
                    digest = _sentinel_digest("check_failed", cap_id, wire_name)

                diagnostics.append(
                    DiscoveryDiagnostic(
                        capability_id=cap_id,
                        code="check_failed",
                        message=f"wire tool {wire_name!r} check_fn failed or unavailable",
                        wire_name=wire_name,
                    )
                )
                bindings_by_id[cap_id] = ImplementationBinding(
                    capability_id=cap_id,
                    wire_name=wire_name,
                    source=rule.source,
                    provider=provider,
                    implementation_version="1.0.0",
                    input_schema_version=input_ver,
                    output_schema_version=output_ver,
                    schema_digest=digest,
                    health="unavailable",
                )
                continue

            envelope = def_envelopes[wire_name]
            fn_dict = envelope.get("function") if isinstance(envelope, dict) else None

            if not isinstance(fn_dict, dict):
                sentinel_digest = _sentinel_digest(
                    "schema_incompatible", cap_id, wire_name
                )
                diagnostics.append(
                    DiscoveryDiagnostic(
                        capability_id=cap_id,
                        code="schema_incompatible",
                        message=f"wire tool {wire_name!r} definition envelope is malformed or missing function mapping",
                        wire_name=wire_name,
                    )
                )
                bindings_by_id[cap_id] = ImplementationBinding(
                    capability_id=cap_id,
                    wire_name=wire_name,
                    source=rule.source,
                    provider=provider,
                    implementation_version="1.0.0",
                    input_schema_version=input_ver,
                    output_schema_version=output_ver,
                    schema_digest=sentinel_digest,
                    health="unavailable",
                )
                continue

            parameters = fn_dict.get("parameters")
            try:
                if parameters is not None and not isinstance(parameters, dict):
                    raise ValueError("schema parameters is not a dict")
                digest = hash_schema_dict(fn_dict)
            except (ValueError, TypeError) as exc:
                sentinel_digest = _sentinel_digest(
                    "schema_incompatible", cap_id, wire_name
                )
                diagnostics.append(
                    DiscoveryDiagnostic(
                        capability_id=cap_id,
                        code="schema_incompatible",
                        message=f"wire tool {wire_name!r} schema is invalid or non-serializable: {exc}",
                        wire_name=wire_name,
                    )
                )
                bindings_by_id[cap_id] = ImplementationBinding(
                    capability_id=cap_id,
                    wire_name=wire_name,
                    source=rule.source,
                    provider=provider,
                    implementation_version="1.0.0",
                    input_schema_version=input_ver,
                    output_schema_version=output_ver,
                    schema_digest=sentinel_digest,
                    health="unavailable",
                )
                continue

            bindings_by_id[cap_id] = ImplementationBinding(
                capability_id=cap_id,
                wire_name=wire_name,
                source=rule.source,
                provider=provider,
                implementation_version="1.0.0",
                input_schema_version=input_ver,
                output_schema_version=output_ver,
                schema_digest=digest,
                health="healthy",
            )

        sorted_bindings = tuple(
            bindings_by_id[k] for k in sorted(bindings_by_id.keys())
        )

        return DiscoveryResult(
            bindings=sorted_bindings,
            by_capability_id=MappingProxyType(bindings_by_id),
            diagnostics=tuple(diagnostics),
        )
