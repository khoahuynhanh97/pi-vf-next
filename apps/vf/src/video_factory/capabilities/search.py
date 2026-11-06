from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from video_factory.capabilities.bindings import ImplementationBinding
from video_factory.capabilities.errors import CapabilityError
from video_factory.capabilities.manifest import INITIAL_CAPABILITIES, CapabilitySpec
from video_factory.capabilities.policy import (
    CapabilityPolicyEvaluator,
    PolicyRequestContext,
    PolicyWarning,
)
from video_factory.capabilities.snapshots import CatalogSnapshot


def _tokenize_text(text: str) -> list[str]:
    """Adapter wrapping upstream tokenization logic for capability search indexing."""
    try:
        from tools.tool_search import _tokenize
        return _tokenize(text)
    except Exception:
        return [t.lower() for t in re.findall(r"[A-Za-z0-9]+", text)]


def _score_bm25(
    query_tokens: list[str],
    doc_tokens: list[str],
    doc_lengths: list[int],
    avg_dl: float,
    doc_freq: dict[str, int],
    n_docs: int,
) -> float:
    """Adapter wrapping upstream BM25 scoring logic for capability search."""
    try:
        from tools.tool_search import _bm25_score
        return _bm25_score(
            query_tokens, doc_tokens, doc_lengths, avg_dl, doc_freq, n_docs
        )
    except Exception:
        return 0.0


@dataclass(frozen=True)
class CapabilitySearchResultItem:
    capability_id: str
    display_name: str
    description: str
    risk: str
    cost_class: str
    approval_policy: str


@dataclass(frozen=True)
class CapabilitySearchResult:
    query: str
    total_available: int
    matches: tuple[CapabilitySearchResultItem, ...]


@dataclass(frozen=True)
class CapabilityDescription:
    capability_id: str
    wire_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    warnings: tuple[PolicyWarning, ...] = ()


class GovernedCapabilitySearch:
    def __init__(
        self,
        snapshot: CatalogSnapshot,
        policy_evaluator: CapabilityPolicyEvaluator | None = None,
        registry: Any | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.evaluator = policy_evaluator or CapabilityPolicyEvaluator()
        self.registry = registry

    def search(
        self,
        query: str,
        context: PolicyRequestContext,
        limit: int = 5,
    ) -> CapabilitySearchResult:
        query_str = query.strip()
        if not query_str:
            return CapabilitySearchResult(query="", total_available=0, matches=())

        candidates: list[tuple[CapabilitySpec, ImplementationBinding]] = []
        for binding in self.snapshot.bindings:
            cap_id = binding.capability_id
            spec = INITIAL_CAPABILITIES.get(cap_id)
            if spec is None:
                continue

            decision = self.evaluator.evaluate(spec, binding, context)
            if decision.allowed:
                candidates.append((spec, binding))

        if not candidates:
            return CapabilitySearchResult(
                query=query_str,
                total_available=0,
                matches=(),
            )

        query_tokens = _tokenize_text(query_str)
        doc_tokens_list = [
            _tokenize_text(
                f"{spec.capability_id} {spec.owner} {binding.wire_name} {binding.provider}"
            )
            for spec, binding in candidates
        ]

        doc_lengths = [len(dt) for dt in doc_tokens_list]
        avg_dl = sum(doc_lengths) / max(len(doc_lengths), 1)
        doc_freq: dict[str, int] = {}
        for dt in doc_tokens_list:
            for t in set(dt):
                doc_freq[t] = doc_freq.get(t, 0) + 1
        n_docs = len(candidates)

        scored: list[tuple[float, CapabilitySpec, ImplementationBinding]] = []
        for idx, (spec, binding) in enumerate(candidates):
            s = _score_bm25(
                query_tokens,
                doc_tokens_list[idx],
                doc_lengths,
                avg_dl,
                doc_freq,
                n_docs,
            )
            if s > 0:
                scored.append((s, spec, binding))

        if not scored and query_tokens:
            ql = query_str.lower()
            for spec, binding in candidates:
                if (
                    ql in spec.capability_id.lower()
                    or ql in spec.owner.lower()
                    or ql in binding.wire_name.lower()
                ):
                    scored.append((0.1, spec, binding))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_matches = scored[: max(1, limit)]

        items = tuple(
            CapabilitySearchResultItem(
                capability_id=spec.capability_id,
                display_name=spec.capability_id,
                description=f"{spec.capability_id} (owner={spec.owner})",
                risk=spec.risk,
                cost_class=spec.cost_class,
                approval_policy=spec.approval_policy,
            )
            for _, spec, binding in top_matches
        )

        return CapabilitySearchResult(
            query=query_str,
            total_available=len(candidates),
            matches=items,
        )

    def describe(
        self,
        capability_id: str,
        context: PolicyRequestContext,
    ) -> CapabilityDescription:
        spec = INITIAL_CAPABILITIES.get(capability_id)
        if spec is None:
            raise CapabilityError(
                code="tool_not_found",
                message=f"Capability {capability_id!r} is not found in manifest",
                retryable=False,
                capability_id=capability_id,
                actionable_recovery="Specify a valid capability ID from the manifest",
            )

        binding = next(
            (b for b in self.snapshot.bindings if b.capability_id == capability_id),
            None,
        )
        if binding is None:
            raise CapabilityError(
                code="binding_unavailable",
                message=f"Capability {capability_id!r} has no active implementation binding",
                retryable=True,
                capability_id=capability_id,
                actionable_recovery="Re-run capability discovery to bind wire tool",
            )

        decision = self.evaluator.evaluate(spec, binding, context)
        if not decision.allowed:
            if decision.error is not None:
                raise decision.error
            raise CapabilityError(
                code="degraded_policy_denied",
                message=f"Capability {capability_id!r} is denied by policy",
                retryable=False,
                capability_id=capability_id,
            )

        input_schema: dict[str, Any] | None = None
        output_schema: dict[str, Any] = {}

        if self.registry is not None:
            if hasattr(self.registry, "get_schema"):
                raw_schema = self.registry.get_schema(binding.wire_name)
                if isinstance(raw_schema, dict):
                    fn = (
                        raw_schema.get("function")
                        if raw_schema.get("type") == "function"
                        else raw_schema
                    )
                    if isinstance(fn, dict) and "parameters" in fn:
                        input_schema = fn.get("parameters") or {}
                        output_schema = fn.get("returns") or {}
            elif isinstance(self.registry, dict):
                raw_schema = self.registry.get(binding.wire_name)
                if isinstance(raw_schema, dict):
                    fn = (
                        raw_schema.get("function")
                        if raw_schema.get("type") == "function"
                        else raw_schema
                    )
                    if isinstance(fn, dict) and "parameters" in fn:
                        input_schema = fn.get("parameters") or {}
                        output_schema = fn.get("returns") or {}

        if input_schema is None:
            raise CapabilityError(
                code="schema_unavailable",
                message=f"Schema for capability {capability_id!r} (wire={binding.wire_name!r}) is unavailable",
                retryable=True,
                capability_id=capability_id,
                actionable_recovery="Register tool or configure schema provider before describing capability",
                details={"wire_name": binding.wire_name},
            )

        return CapabilityDescription(
            capability_id=spec.capability_id,
            wire_name=binding.wire_name,
            description=f"{spec.capability_id} (owner={spec.owner})",
            input_schema=input_schema,
            output_schema=output_schema,
            warnings=decision.warnings,
        )
