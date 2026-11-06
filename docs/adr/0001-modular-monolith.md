# ADR-0001: Evolve a modular monolith

Date: 2026-11-03. Status: proposed.

## Context

PI and VF share Python but have different entrypoints, persistence paths and operational contracts. The workload is single-host today. The current Exchange boundary can preserve working behavior while the pilot grows.

## Decision

Use pi-vf-next as the platform monorepo with PI, VF and Automation application interfaces. Keep legacy execution paths available under explicit ownership while routing new work in slices.

## Alternatives and consequences

Separate repositories preserve deployment independence with duplicated coordination. Separate applications in one repo simplify contract versioning while keeping distributed runtime complexity. Specialized services offer isolation with extra infrastructure. A modular monolith reduces deployment and coordination burden and requires enforceable import/repository rules. Revisit service extraction when measured contention, independent ownership or fault isolation warrants it.
