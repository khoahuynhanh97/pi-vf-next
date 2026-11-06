# Implementation architecture — one source, one runner

> Superseded as an implementation baseline by the [four-phase consolidation roadmap](08-migration-roadmap.md) and [Master Prompt](MASTER-PROMPT.md). The layouts and schemas below are optional design context. Phase 01 moves and reuses the original apps.

Proposed implementation baseline for pi-vf-next, 2026-11-03. This document makes first-release choices concrete. [Platform blueprint](01-executive-summary.md) describes the wider target. README describes the executable pilot.

## Review of the supplied design

| Finding | Impact and resolution |
|---|---|
| The shared framework/database premise is inaccurate for the inspected runtime. | Common Python does not imply a common framework. The Nov 2 runtime binds PI research, VF workflow and VF integration storage separately. Consolidate new writes in the pilot DB through explicit imports. [Runtime evidence](02-current-architecture.md#runtime-and-database-observations) |
| Workflow YAML expresses ordering without task scope or join rules. | A single crawl failure can block a batch or enable incomplete exports, depending on how a builder interprets it. Use the scoped joins below. |
| The media path does not model an approved keyframe/storyboard before render. | Flow could receive generic media that has not passed scene-specific identity checks. Pin the lock and selected scene reference, validate keyframes, then assemble the storyboard. |
| A run key including workflow version can allow two runs for the same schedule slot. | Uniqueness is schedule key plus slot. Version is pinned data. Deliberate reprocessing gets a distinct manual intent. |
| A provider job ID may be unavailable after a paid click. | A timeout alone cannot justify another Send. Commit the permit before clicking and preserve an UNKNOWN operation. |
| Object storage, multiple worker pools and many tables are presented without a first deployable shape. | Start with the local artifact tree, existing OS lock and SQLite. No worker services or queue broker are needed for the first release. |
| YAML uses top 3, 06:00, USD 10 and parallelism 5/2 without an operating decision. | Treat 3 as a pilot cap, with a production ceiling of 15. Configure the trigger time, input grace and budgets explicitly. Flow credits and router costs use different counters. |
| The ERD was pasted as rendered CSS and concatenated labels. | It cannot serve as a schema contract. Use the relationship diagram and concrete write map below. |

## Physical deployment and composition

One Python package and one CLI entrypoint. Windows Task Scheduler may invoke that entrypoint after acceptance. A process acquires the data-root OS lock, loads one config and SQLite, and advances runnable tasks until its time slice ends. A later tick or resume continues the same run. Process isolation for provider/browser work is deferred until observed contention warrants it.

~~~text
src/pi_vf/
  cli.py                     existing entrypoint and composition root
  core.py                    existing configuration and shared value types
  store.py                   SQLite connection, migrations, transactions and OS lock
  automation/
    runner.py                evolve current Pipeline.run and stage dispatcher
    repository.py            schedule slots, items, task receipts and reservations
  pi/
    application.py           intake, prepare candidate and selection
    repository.py            batches, candidates and verified observations
    sheets.py                port existing normalization and snapshot reader
    acquire.py               port Spaiauto and validated local import
  vf/
    application.py           prepare project, advance scene and publish export
    repository.py            snapshots, project revisions and provider operations
    creative.py              port identity, script and QC contracts
    flow.py                  existing provider adapter with explicit reconciliation
    media.py                 existing decoder and FFmpeg composition
~~~

This is a migration map, not an instruction to move every file first. Add application seams around working code, then move code when its callers are behind the seam. Keep current CLI commands. Proposed additions are ingest for a supplied artifact and tick for one due schedule slot. Existing run continues to accept Sheet inputs through the same PI seam. The composition root wires PI/VF implementations into Automation. PI and VF never import each other's implementation. Repositories share transaction infrastructure and write only owned data. Adapters do not receive arbitrary DB access.

| Interface | Postcondition |
|---|---|
| PI.ingest(delivery) → BatchReceipt | Batch and all row outcomes committed. Exact replay returns the existing receipt. |
| PI.prepare_candidate(candidate_id, policy) → EligibilityReceipt | Returns ready with a verified observation, deferred, rejected or retryable with reason. Reuses a fresh matching variant with intact assets. |
| PI.select(run_id, eligible_observation_ids, limit) → SelectionReceipt | Returns a stable ordered list of at most 15 verified observation IDs and reasons. The runner pins this list once. |
| VF.open_project(item_id, observation_projection, profile) → ProjectReceipt | Pins immutable input and revision. An identical repeat returns the same project receipt. |
| VF.advance(project_id, step, scene_id, revision) → StepReceipt | Performs one bounded action. Returns accepted, waiting, needs review or terminal failure. Poll due times are data. |

Agent reasoning is called inside a step through the configured router. It returns structured data. The runner and domain validators own transitions. Antigravity can return a supervised script artifact through the same validator.

## First schema change and write ownership

Use data/state.sqlite. Keep runs/items/operations readable for existing four-scene pilot receipts. In the next schema version, introduce additive tables and columns with migration IDs. If replacing global UNIQUE(listing) requires a table rebuild, copy into a new table inside a controlled SQLite migration, preserve original IDs/FKs and verify foreign keys before cutover.

| Owner | Table and key | Contents |
|---|---|---|
| PI | discovery_batches PK id, UNIQUE(producer_namespace, intake_key) | intake_key = external ID when attested, otherwise file SHA. Store raw digest, transport, source and receipt. Same key with different digest is conflict. |
| PI | product_candidates PK id, UNIQUE(batch_id, source_locator) | Raw link and metadata, link kind, normalized listing/variant if known, status and reasons. Keep duplicate rows as evidence and deduplicate the crawl intent. |
| PI | products PK id, UNIQUE(marketplace, listing_id, variant_key) for resolved records | First-release catalog identity is listing plus verified variant. Cross-listing consolidation is deferred. Unknown variant stays unresolved unless verified not applicable. |
| PI | product_observations PK id, UNIQUE(product_id, evidence_digest) | Immutable capture, timestamps, variant proof, image manifest, eligibility and reason codes. Crawl receipts live in candidate-scoped task results initially. |
| Automation | runs PK id, UNIQUE(schedule_key, slot_key) | Pinned config and definition version, input receipt, selection cutoff, ordered selection and outcome. Manual runs use a distinct trigger/intent key. |
| Automation | items PK id, UNIQUE(run_id, selection_key) | Selected observation and project reference. Remove listing-wide uniqueness for new intents after compatible migration. |
| Automation | task_runs PK id, UNIQUE(run_id, step, scope_key, revision) | State, attempt, next_at, reason, input digest and result receipt. Scope is batch, candidate, project or scene. Dependencies come from the pinned code-defined graph. |
| VF | video_snapshots PK id, UNIQUE(snapshot_digest) | Copy exact observation facts, lock and asset IDs/SHAs used by the project. Preserve product provenance without cascade deletion. |
| VF | video_projects PK id, UNIQUE(item_id, revision) | Snapshot reference, profile, plan, storyboard and export receipts. Scene manifests stay with project receipts. |
| VF | operations PK key | Extend existing receipt with project/revision, kind, scene, attempt ordinal, input hash, state, provider correlation and cost reservation. VF writes provider state and Automation authorizes spend in the same transaction. |

Initial schedules, definitions and policy versions live in config/code. Task attempt history and model usage can start as append-only receipts. Add separate tables only when querying, retention or transactional constraints require them.

## Executable stage semantics

| Stage and scope | Ready when | Writes and join |
|---|---|---|
| ingest / run | Committed source batch bound to slot | Pin batch and enqueue candidate preparation. End NO_INPUT when grace expires. |
| prepare / candidate | Candidate row accepted | Resolve or reuse/crawl, validate, then return eligible, rejected, deferred or retryable. At cutoff, unfinished reviews are deferred for this run. |
| select / run | All candidate tasks terminal or selection cutoff | Pin ordered eligible IDs. Zero eligible ends NO_ELIGIBLE_PRODUCTS. No late candidate mutates selection. |
| freeze and script / project | Selected observation exists | Pin snapshot and 3–5 validated scenes, default 4. Keep a verified preset duration per scene. Reject unsupported actions and missing required fact/reference bindings before media spend. |
| keyframe / scene | Plan pinned | Generate and review against the original reference and required facts. Store verdict and SHA. Ratio is a media preparation concern. |
| storyboard / project | Every required keyframe accepted for the same revision | Assemble scene order, pinned keyframes and QC receipts. A failed scene never produces an accepted storyboard. |
| clip / scene | Storyboard accepted | Render from its approved frame, then inspect sampled frames against lock. Failure affects that scene. Other projects continue. |
| export / project | Every required clip accepted for the same revision | Compose in plan order, decode and verify expected duration/profile, publish atomically. External publishing can require separate approval. |

Runner selects due tasks in deterministic order, commits RUNNING and attempt before execution, then commits the receipt and downstream readiness together. Network and FFmpeg execute outside transactions. Initial lane capacity is one runner action at a time, with one owned Chrome profile. Provider jobs can be in flight while due polls wait. Use a configurable cap and no blind retries.

## Exact Send and restart protocol

1. Derive an operation key from project revision, scene, generation kind, input digest and explicit attempt ordinal. In a short transaction reserve units/credits and create PREPARED.
2. On the owned browser profile, verify input, displayed cost and permitted scope. Compare-and-set PREPARED → SUBMITTING immediately before the paid action. Only the successful claimant may click.
3. Record correlation/receipt as available. After an ambiguous timeout or crash, keep the reservation and mark UNKNOWN. After restart, SUBMITTING and UNKNOWN only enter reconciliation. Poll/download failures do not allocate attempts.
4. Commit SUCCESS only after the expected file is verified. Restore an accepted receipt by its exact operation key and original SHA. Do not call the allocator to restore it. Issue a new attempt only after a known disposition and a policy-approved retry.
5. Derive DONE from accepted export receipt. A stale file or missing clip blocks publication and gets an actionable reason. Cancellation stops new dispatch and keeps in-flight reconciliation.

## First delivery order

1. Establish composition root and three application seams while preserving the existing offline entrypoint.
2. Add durable intake/catalog and scoped task state, using the observed Spark file and temporary SQLite. Reuse current PI acquisition adapters and mock external transport in integration checks.
3. Connect VF snapshot, lock, 3–5-scene plan, storyboard, sampled QC and FFmpeg output. Verify 3/4/5 scenes, partial batch success and old four-scene receipts.
4. Verify restart around Send, exact-operation recovery, duplicate schedule wake and cap exhaustion. Enable scheduled admission only after a supervised live run and one-owner cutover.

Completion means one installed source package can produce a real MP4 offline from a mixed candidate batch with fixture-backed external adapters. A later live acceptance checks actual Spark delivery, Spaiauto, model route and Flow separately.

## Final cutover — retire both original source checkouts

The user intends to remove the original PI and VF checkout folders. pi-vf-next is the sole deployable source at handoff. Port the required Legacy Discovery, acquisition, creative, render and recovery behavior into this package before cutover. Legacy adapters that invoke the original applications are temporary migration aids.

1. Inventory scripts, scheduled tasks, imports, config, browser profile, SQLite files, assets and receipts that point into the original folders. Move required runtime data into platform-owned storage or explicit external paths independent of both checkouts. Keep a verified backup outside both folders.
2. Stop legacy admission and reconcile in-flight paid operations with their original IDs. Rebind the daily trigger and profile ownership to the new runner. Verify that the new owner can read retained snapshots and outputs.
3. Install from the new source in a clean environment with both old checkout paths inaccessible. Verify CLI, intake to MP4, schedule invocation and restart recovery. Then complete a supervised live run with the new owner and verify the output and operation receipts.
4. After those checks pass, the owner can remove the original source folders. Rollback uses the separate verified backup and a deliberate ownership transfer.
