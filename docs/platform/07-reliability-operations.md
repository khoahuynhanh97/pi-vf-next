# Reliability, security and operations

## State transitions

~~~mermaid
stateDiagram-v2
    [*] --> WaitingInput
    WaitingInput --> Ready: committed input or satisfied dependencies
    Ready --> Running: lease acquired
    Running --> RetryWait: proven retryable failure
    RetryWait --> Ready: backoff elapsed
    Running --> Reconciling: external submission uncertain
    Reconciling --> Running: observed provider job or result
    Reconciling --> NeedsReview: reconciliation deadline
    Running --> NeedsReview: missing evidence or policy gate
    NeedsReview --> Ready: verified correction or approval
    Running --> Succeeded: durable accepted receipt
    Running --> Failed: terminal or exhausted
    WaitingInput --> Failed: input grace elapsed
    Ready --> Cancelled: cancellation requested
    Running --> Cancelled: safe stop or confirmed cancellation
    Reconciling --> Cancelled: confirmed provider cancellation
~~~

Run status is derived from its scoped tasks. Failed candidates can yield a partial batch with valid product exports. An unresolved generation keeps its operation state and receipt even if the run is paused, cancelled or expired. Reopening a terminal task requires an explicit new revision or retry command with lineage and budget policy.

## Scheduling, ownership and execution

- Host scheduler wakes Automation for an operator-configured Bangkok slot. Persist unique schedule key + local slot with UTC execution timestamps. Bind a committed batch to the slot using a trusted producer marker or an operator-assigned delivery receipt. A received timestamp alone cannot prove the intended discovery day. Pin exact batch IDs when intake closes. Late Spark input waits through a configured grace window. Record no-input and do not silently substitute backlog. Provide explicit replay/backfill with source receipt and a separate trigger key. Concurrent wake-ups reuse the same run.
- Start with single-runner execution using the existing pilot OS lock, then add DB task claims only when introducing concurrent workers. Atomic claim increments an attempt/epoch and records owner, token and expiry. Heartbeats extend a matching claim. Completion is fenced by token and epoch. Reclaim only expired leases. Use monotonic time for local deadlines and UTC timestamps for persisted lease comparisons.
- Dispatch candidate, product and scene scopes independently with resource permits. Initially one Chrome job per owned profile, bounded HTTP/model slots, one CPU render until measured otherwise. Persist provider work in flight separately from worker occupancy. Poll through a due queue with backoff and jitter rather than blocking a worker for the whole render.
- Stop dispatching on cancellation. For in-flight provider work, request cancellation only if the adapter verifies the capability. Otherwise keep reconciliation running, record spent or uncertain cost, and withhold publication. Human approval binds artifact digest, task revision, actor and time. Gate ambiguous identity, policy exception, budget extension or configured publication review.

## Retry and paid side effects

| Failure class | Response |
|---|---|
| Rate limit, proven pre-submit transport or temporary read failure | Bounded exponential backoff with jitter. Honor provider retry hints. Scope circuit breaking to source/provider. |
| Invalid input, identity mismatch or permission/configuration error | Explicit actionable blocked state. Re-evaluate after corrected input or config, preserving prior counters. |
| Post-submit timeout, browser crash or missing tile | Keep request key, provider/project ID, pre-submit evidence and reservation. Reconcile the same operation. Send again only after evidence establishes safe disposition and budget allows a new attempt. |
| Output unavailable or checksum differs | Recover using the exact original operation. Verify SHA and identity. Quarantine a mismatch. |
| Confirmed quality failure | Create a new scene revision only with specific repair findings and remaining quota. Reuse accepted unaffected artifacts. |

Budget enforcement counts per scene, project, run and provider/account lane. Reserve before side effects, settle actual usage where observable, release only when evidence proves no charge, and track unknown liabilities separately. Keep configured limits, observed cost and estimates distinct. Use deterministic internal idempotency keys and unique constraints. Delivery is at least once with idempotent local commits. External exactly-once cannot be guaranteed across the send/receipt crash window without a provider idempotency or query guarantee. UNKNOWN is a valid durable outcome.

A worker must hold the relevant profile and valid task claim to acquire the Send permit. Persist the PREPARED to SUBMITTING transition with a conditional update on the operation, token and epoch immediately before Send. Expiry of the task lease does not release a SUBMITTING operation for another Send. A stale process may still have a browser action in flight, because DB fencing does not fence the provider. Before any operator-authorized replacement attempt, confirm that the previous sender is quiescent and reconcile its provider disposition. Unknown remains reserved.

If dispatch is local, a transactional state transition into ready work is sufficient. When publishing external messages, use a transactional outbox with deduplicating consumers. Original VF has an outbox and worker leases worth porting selectively. The pilot already has single-runner lock and operation receipts. A durable workflow engine would add managed timers, distributed workers and workflow history, with infrastructure, replay/versioning and migration overhead. Revisit after recurring operational evidence such as cross-host worker needs or timer/recovery load that the measured DB runner cannot meet.

## Security and visibility

Validate HTTPS marketplace destinations and redirect hops with source-specific allowlists, private/reserved address rejection and outbound restrictions. Treat short URLs as untrusted until resolved. Avoid passing unverified URL strings to browser/file APIs. Bound ZIP member count, compressed/uncompressed size, decode dimensions and paths. Reject traversal, symlinks and duplicate names. Keep secrets in environment or a credential store and scope browser profiles. Redact credentials, query tokens and unnecessary seller data from logs.

Log run/task/attempt IDs, source and artifact digests, provider operation, stage latency, reason code and next action. Measure scheduled vs actual starts, input delays, backlog age, time by stage, invalid/duplicate candidates, QC reasons, retry counts, UNKNOWN age and budget utilization. Start with CLI summary and structured records. Add a read-only operational dashboard when a shared triage workflow warrants it. Alert on missing daily input, prolonged UNKNOWN, repeated schema drift, stale leases and budget exhaustion. Retain provenance and recovery receipts while outputs are retained.
