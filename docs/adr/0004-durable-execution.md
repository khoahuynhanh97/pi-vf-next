# ADR-0004: Persist execution and reconcile paid operations

Date: 2026-11-03. Status: proposed.

## Context

VF already has outbox claims and provider attempts. The pilot has operation receipts and one runner. Provider Send can succeed while the local acknowledgement is lost.

## Decision

Reuse DB-backed task state and leases as concurrency grows. Persist a pinned operation and cost reservation before Send. Reconcile ambiguous results by original operation. Bind restart and restoration to receipts.

## Alternatives and consequences

Cron and volatile subprocess state are simple with weak recovery. A dedicated durable engine provides distributed timers and histories with replay and operational costs. DB-backed execution needs explicit transition and fencing rules and fits initial host constraints. External uncertainty stays visible for operator resolution.
