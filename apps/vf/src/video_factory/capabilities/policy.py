from __future__ import annotations

from dataclasses import dataclass, field

from video_factory.capabilities.bindings import ImplementationBinding
from video_factory.capabilities.errors import CapabilityError
from video_factory.capabilities.manifest import CapabilitySpec
from video_factory.workflows.budgets import (
    BudgetAllocation,
    BudgetPolicy,
    BudgetRepository,
)


@dataclass(frozen=True)
class PolicyRequestContext:
    skill_id: str | None = None
    workflow_id: str | None = None
    authenticated: bool = False
    user_confirmed_approval: bool = False
    granted_policies: frozenset[str] = field(default_factory=frozenset)
    budget_available: float | None = None
    channel_id: str | None = None
    session_id: str | None = None
    owner_user_id: str = "default-owner"
    scope_id: str | None = None
    idempotency_key: str | None = None
    operation_input_digest: str = ""
    amount: int = 1
    currency: str = "USD"
    external_reference: str | None = None


@dataclass(frozen=True)
class PolicyWarning:
    code: str
    message: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    capability_id: str
    error: CapabilityError | None = None
    warnings: tuple[PolicyWarning, ...] = ()
    reservation: BudgetAllocation | None = None


class CapabilityPolicyEvaluator:
    def __init__(
        self,
        budget_repository: BudgetRepository | None = None,
        budget_policy: BudgetPolicy | None = None,
    ) -> None:
        self.budget_repository = budget_repository
        self.budget_policy = budget_policy

    def evaluate(
        self,
        spec: CapabilitySpec,
        binding: ImplementationBinding,
        context: PolicyRequestContext,
        budget_repository: BudgetRepository | None = None,
        budget_policy: BudgetPolicy | None = None,
    ) -> PolicyDecision:
        cap_id = spec.capability_id
        active_reservation: BudgetAllocation | None = None

        # 1. Authentication check
        if not context.authenticated:
            err = CapabilityError(
                code="unauthenticated",
                message="Request context is unauthenticated",
                retryable=False,
                capability_id=cap_id,
                actionable_recovery="Authenticate request context before executing capability",
            )
            return PolicyDecision(allowed=False, capability_id=cap_id, error=err)

        # 2. Capability ID match check
        if spec.capability_id != binding.capability_id:
            err = CapabilityError(
                code="capability_id_mismatch",
                message=f"Binding capability_id {binding.capability_id!r} does not match spec capability_id {spec.capability_id!r}",
                retryable=False,
                capability_id=cap_id,
                actionable_recovery="Ensure binding capability_id matches spec capability_id",
                details={
                    "binding_capability_id": binding.capability_id,
                    "spec_capability_id": spec.capability_id,
                },
            )
            return PolicyDecision(allowed=False, capability_id=cap_id, error=err)

        # 3. Skill authorization check (skill_id must be provided and allowed)
        if context.skill_id is None or context.skill_id not in spec.allowed_skills:
            err = CapabilityError(
                code="unauthorized_skill",
                message=f"Skill {context.skill_id!r} is not authorized for capability {cap_id!r}",
                retryable=False,
                capability_id=cap_id,
                actionable_recovery="Specify an authorized skill in PolicyRequestContext",
                details={
                    "provided_skill_id": context.skill_id,
                    "allowed_skills": list(spec.allowed_skills),
                },
            )
            return PolicyDecision(allowed=False, capability_id=cap_id, error=err)

        # 4. Workflow authorization check (workflow_id must be provided and allowed)
        if (
            context.workflow_id is None
            or context.workflow_id not in spec.allowed_workflows
        ):
            err = CapabilityError(
                code="unauthorized_workflow",
                message=f"Workflow {context.workflow_id!r} is not authorized for capability {cap_id!r}",
                retryable=False,
                capability_id=cap_id,
                actionable_recovery="Specify an authorized workflow in PolicyRequestContext",
                details={
                    "provided_workflow_id": context.workflow_id,
                    "allowed_workflows": list(spec.allowed_workflows),
                },
            )
            return PolicyDecision(allowed=False, capability_id=cap_id, error=err)

        # 5. Binding Health check (unavailable)
        if binding.health == "unavailable":
            err = CapabilityError(
                code="binding_unavailable",
                message=f"Capability {cap_id!r} binding is unavailable (wire={binding.wire_name!r})",
                retryable=True,
                capability_id=cap_id,
                actionable_recovery="Check underlying MCP server health or re-run discovery",
                details={"wire_name": binding.wire_name, "provider": binding.provider},
            )
            return PolicyDecision(allowed=False, capability_id=cap_id, error=err)

        # 6. Schema Version Match check
        if (
            binding.input_schema_version != spec.input_schema_version
            or binding.output_schema_version != spec.output_schema_version
        ):
            err = CapabilityError(
                code="schema_version_mismatch",
                message=(
                    f"Capability {cap_id!r} schema version mismatch: "
                    f"binding=(in:{binding.input_schema_version}, out:{binding.output_schema_version}), "
                    f"spec=(in:{spec.input_schema_version}, out:{spec.output_schema_version})"
                ),
                retryable=False,
                capability_id=cap_id,
                actionable_recovery="Update implementation binding to match manifest schema version",
                details={
                    "binding_input": binding.input_schema_version,
                    "binding_output": binding.output_schema_version,
                    "spec_input": spec.input_schema_version,
                    "spec_output": spec.output_schema_version,
                },
            )
            return PolicyDecision(allowed=False, capability_id=cap_id, error=err)

        # 7. Approval / Precondition Policy check
        policy_warnings: list[PolicyWarning] = []

        if spec.approval_policy == "none":
            pass
        elif spec.approval_policy == "review_required":
            policy_warnings.append(
                PolicyWarning(
                    code="post_execution_review_required",
                    message=f"Capability {cap_id!r} requires post-execution human review",
                )
            )
        elif spec.approval_policy == "approved_only":
            is_satisfied = (
                "approved_only" in context.granted_policies
                or spec.approval_policy in context.granted_policies
            )
            if not is_satisfied:
                err = CapabilityError(
                    code="unmet_precondition",
                    message=f"Capability {cap_id!r} requires approved retrieval scope ({spec.approval_policy!r})",
                    retryable=True,
                    capability_id=cap_id,
                    actionable_recovery="Grant approved retrieval scope before retrieving knowledge",
                    details={"approval_policy": spec.approval_policy},
                )
                return PolicyDecision(allowed=False, capability_id=cap_id, error=err)
        elif spec.approval_policy.startswith("hitl:"):
            is_satisfied = (
                spec.approval_policy in context.granted_policies
                or context.user_confirmed_approval
            )
            if not is_satisfied:
                err = CapabilityError(
                    code="unmet_precondition",
                    message=f"Capability {cap_id!r} requires human approval grant ({spec.approval_policy!r})",
                    retryable=True,
                    capability_id=cap_id,
                    actionable_recovery="Solicit explicit user approval before executing capability",
                    details={"approval_policy": spec.approval_policy},
                )
                return PolicyDecision(allowed=False, capability_id=cap_id, error=err)
        elif spec.approval_policy.startswith("precondition:"):
            required_grant = spec.approval_policy.split("precondition:", 1)[1]
            is_satisfied = (
                required_grant in context.granted_policies
                or spec.approval_policy in context.granted_policies
            )
            if not is_satisfied:
                err = CapabilityError(
                    code="unmet_precondition",
                    message=f"Capability {cap_id!r} requires precondition grant {required_grant!r}",
                    retryable=True,
                    capability_id=cap_id,
                    actionable_recovery=f"Satisfy precondition {required_grant!r} before calling capability",
                    details={
                        "approval_policy": spec.approval_policy,
                        "required_grant": required_grant,
                        "granted_policies": list(context.granted_policies),
                    },
                )
                return PolicyDecision(allowed=False, capability_id=cap_id, error=err)

        # 8. Budget Permission check
        if spec.cost_class in ("metered", "paid"):
            effective_repo = budget_repository or self.budget_repository
            effective_pol = budget_policy or self.budget_policy or BudgetPolicy(max_total=1000, per_channel={"metered": 500, "paid": 500})
            if effective_repo is not None:
                try:
                    active_reservation = effective_repo.reserve_budget(
                        policy=effective_pol,
                        channel=spec.cost_class,
                        amount=context.amount,
                        idempotency_key=context.idempotency_key,
                        owner_user_id=context.owner_user_id,
                        scope_id=context.scope_id or context.workflow_id or context.owner_user_id,
                        currency=context.currency,
                        capability_id=spec.capability_id,
                        operation_input_digest=context.operation_input_digest,
                        external_reference=context.external_reference,
                    )
                except Exception as ex:
                    err = CapabilityError(
                        code="budget_exceeded",
                        message=f"Capability {cap_id!r} requires cost authorization ({spec.cost_class!r}), but durable budget reservation failed: {ex}",
                        retryable=False,
                        capability_id=cap_id,
                        actionable_recovery="Allocate sufficient budget before calling capability",
                        details={
                            "cost_class": spec.cost_class,
                            "error": str(ex),
                        },
                    )
                    return PolicyDecision(allowed=False, capability_id=cap_id, error=err)
            else:
                if context.budget_available is None or context.budget_available <= 0:
                    err = CapabilityError(
                        code="budget_exceeded",
                        message=f"Capability {cap_id!r} requires cost authorization ({spec.cost_class!r}), but budget_available is {context.budget_available}",
                        retryable=False,
                        capability_id=cap_id,
                        actionable_recovery="Allocate positive budget before calling capability",
                        details={
                            "cost_class": spec.cost_class,
                            "budget_available": context.budget_available,
                        },
                    )
                    return PolicyDecision(allowed=False, capability_id=cap_id, error=err)

        # 9. Degraded Health check
        if binding.health == "degraded":
            requires_approval = (
                spec.approval_policy != "none"
                and spec.approval_policy != "review_required"
            )
            if (
                spec.risk == "low"
                and spec.cost_class == "free"
                and not requires_approval
            ):
                policy_warnings.append(
                    PolicyWarning(
                        code="degraded_capability_warning",
                        message=f"Capability {cap_id!r} is operating in degraded mode; results may be incomplete",
                    )
                )
                return PolicyDecision(
                    allowed=True,
                    capability_id=cap_id,
                    warnings=tuple(policy_warnings),
                    reservation=active_reservation,
                )

            err = CapabilityError(
                code="degraded_policy_denied",
                message=f"Capability {cap_id!r} is degraded and cannot be executed due to risk/cost policy (risk={spec.risk!r}, cost_class={spec.cost_class!r})",
                retryable=True,
                capability_id=cap_id,
                actionable_recovery="Wait for MCP server recovery before executing degraded medium/high risk capability",
                details={"risk": spec.risk, "cost_class": spec.cost_class},
            )
            return PolicyDecision(allowed=False, capability_id=cap_id, error=err)

        # 10. Default Allow
        return PolicyDecision(
            allowed=True,
            capability_id=cap_id,
            warnings=tuple(policy_warnings),
            reservation=active_reservation,
        )
