# ADR-0005: Limit agents to validated reasoning and evidence

Date: 2026-11-03. Status: proposed.

## Context

Creative work benefits from visual reasoning. Source data can contain untrusted instructions and model outputs can conflict with product identity.

## Decision

Give each reasoning role an input/output contract and scoped evidence. Keep orchestration, spend, persistence, identity enforcement and provider submission under application control. Trial supervised Antigravity against the same script contract.

## Alternatives and consequences

An unrestricted autonomous agent reduces setup effort with weaker replay and spend control. Deterministic rules alone miss useful visual and creative judgments. Bounded model calls preserve traceability and permit route changes for new revisions. Schema validation and lightweight sampled QC still require real-product calibration.
