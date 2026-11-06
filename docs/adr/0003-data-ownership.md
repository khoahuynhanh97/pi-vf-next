# ADR-0003: One target database with explicit owners

Date: 2026-11-03. Status: proposed.

## Context

Observed runtime files are separate. Mutable catalog records and historical generation inputs have different lifecycles.

## Decision

Use one platform database. PI owns canonical identity and observations, VF owns frozen snapshots and output artifacts, Automation owns workflow execution. Migrate through versioned read-only projections and additive changes.

## Alternatives and consequences

Separate physical stores preserve stronger isolation at the cost of distributed consistency. A schema merge without ownership exposes internal tables to cross-domain writers. Explicit owners and immutable snapshots simplify consistency and preserve reproducibility. SQLite writes must stay short and be measured. Revisit storage choice if concurrent writing or multi-host operation exceeds local limits.
