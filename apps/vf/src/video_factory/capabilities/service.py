from __future__ import annotations

from typing import Any, Callable, Sequence

from video_factory.capabilities.bindings import ImplementationBinding
from video_factory.capabilities.discovery import (
    DEFAULT_WIRE_BINDING_RULES,
    CapabilityDiscovery,
    WireBindingRule,
)
from video_factory.capabilities.errors import CapabilityError
from video_factory.capabilities.manifest import INITIAL_CAPABILITIES, CapabilitySpec
from video_factory.capabilities.policy import (
    CapabilityPolicyEvaluator,
    PolicyRequestContext,
)
from video_factory.capabilities.search import (
    CapabilityDescription,
    CapabilitySearchResult,
    GovernedCapabilitySearch,
)
from video_factory.capabilities.snapshots import (
    CapabilitySnapshotRepository,
    CatalogSnapshot,
)


class _DictRegistryAdapter:
    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self._data = data

    def get_all_tool_names(self) -> list[str]:
        return list(self._data.keys())

    def get_definitions(
        self, tool_names: set[str], quiet: bool = False
    ) -> list[dict[str, Any]]:
        return [self._data[name] for name in tool_names if name in self._data]

    def get_schema(self, name: str) -> dict[str, Any] | None:
        return self._data.get(name)

    def get_toolset_for_tool(self, name: str) -> str | None:
        return "mock_toolset"


def _adapt_registry(registry: Any) -> Any:
    if hasattr(registry, "get_all_tool_names"):
        return registry
    if isinstance(registry, dict):
        return _DictRegistryAdapter(registry)
    return registry


class CapabilityCatalogService:
    def __init__(
        self,
        snapshot_repository: CapabilitySnapshotRepository,
        policy_evaluator: CapabilityPolicyEvaluator | None = None,
        registry: Any | None = None,
    ) -> None:
        self.snapshot_repository = snapshot_repository
        self.evaluator = policy_evaluator or CapabilityPolicyEvaluator()
        self.registry = registry

    def get_active_snapshot(self) -> CatalogSnapshot:
        active = self.snapshot_repository.get_active()
        if active is None:
            raise CapabilityError(
                code="binding_unavailable",
                message="No active catalog snapshot found in repository",
                retryable=True,
                capability_id="",
                actionable_recovery="Perform controlled reload() to discover and activate a snapshot",
            )
        return active

    def reload(
        self,
        rules: Sequence[WireBindingRule] = DEFAULT_WIRE_BINDING_RULES,
        registry: Any | None = None,
    ) -> CatalogSnapshot:
        effective_registry = registry if registry is not None else self.registry
        if effective_registry is None:
            raise CapabilityError(
                code="binding_unavailable",
                message="No tool registry configured for catalog discovery reload",
                retryable=True,
                capability_id="",
                actionable_recovery="Configure registry or pass registry to reload()",
            )

        discovery = CapabilityDiscovery(rules=rules)
        adapted = _adapt_registry(effective_registry)
        result = discovery.scan(registry=adapted)

        if len(result.bindings) != len(INITIAL_CAPABILITIES) or result.diagnostics:
            diag_msgs = [f"{d.capability_id}: {d.message}" for d in result.diagnostics]
            combined_msg = "; ".join(diag_msgs) if diag_msgs else "Incomplete discovery scan"
            first_cap_id = result.diagnostics[0].capability_id if result.diagnostics else ""
            raise CapabilityError(
                code="binding_unavailable",
                message=f"Capability discovery reload failed: {combined_msg}",
                retryable=True,
                capability_id=first_cap_id,
                actionable_recovery="Ensure wire tools are registered and healthy before reloading catalog",
            )

        active_snapshot = self.snapshot_repository.save_and_activate(result.bindings)
        return active_snapshot

    def search(
        self,
        query: str,
        context: PolicyRequestContext,
        limit: int = 5,
    ) -> CapabilitySearchResult:
        snapshot = self.get_active_snapshot()
        search_adapter = GovernedCapabilitySearch(
            snapshot=snapshot,
            policy_evaluator=self.evaluator,
            registry=self.registry,
        )
        return search_adapter.search(query=query, context=context, limit=limit)

    def describe(
        self,
        capability_id: str,
        context: PolicyRequestContext,
    ) -> CapabilityDescription:
        snapshot = self.get_active_snapshot()
        search_adapter = GovernedCapabilitySearch(
            snapshot=snapshot,
            policy_evaluator=self.evaluator,
            registry=self.registry,
        )
        return search_adapter.describe(capability_id=capability_id, context=context)

    def execute_capability(
        self,
        spec: CapabilitySpec,
        binding: ImplementationBinding,
        context: PolicyRequestContext,
        provider_fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        decision = self.evaluator.evaluate(spec, binding, context)
        if not decision.allowed:
            if decision.error is not None:
                raise decision.error
            raise CapabilityError(
                code="budget_exceeded",
                message=f"Capability {spec.capability_id!r} execution denied by policy",
                retryable=False,
                capability_id=spec.capability_id,
            )

        reservation = decision.reservation
        budget_repo = getattr(self.evaluator, "budget_repository", None)

        try:
            result = provider_fn(*args, **kwargs)
        except Exception:
            if reservation is not None and budget_repo is not None:
                try:
                    budget_repo.release_budget(reservation)
                except Exception:
                    pass
            raise

        if reservation is not None and budget_repo is not None:
            budget_repo.commit_budget(reservation)

        return result
