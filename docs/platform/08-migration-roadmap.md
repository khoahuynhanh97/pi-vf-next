# Consolidation and integration roadmap

Move → Verify → Integrate → Automate → Optimize.

The original PI and VF applications are the implementation baseline. Place their existing source in apps/pi and apps/vf, preserving package and application boundaries. Follow the [repository layout](REPOSITORY-LAYOUT.md) and [Master Prompt](MASTER-PROMPT.md). Earlier architecture and schema proposals are context for later decisions.

| Phase | Delivery | Exit gate |
|---|---|---|
| 01 Source Consolidation | Audit the working trees and trace dependencies, config, migrations and the handoff. Transfer source, including required untracked files. Apply only compatibility fixes. Preserve package names, entrypoints, schema and migration behavior. | Both apps install and run from the new repo with temporary data and the original checkout paths unavailable. Record baseline failures separately from migration regressions. |
| 02 Unified Pipeline | Reuse the PI file parser for the verified Spark artifact. Connect existing resolution, crawl, processing and review to VF through the working Exchange contract. Keep Legacy Discovery under its current controls. | One product reaches MP4 with traceable receipts, correct identity and no duplicated business logic. |
| 03 Automation Orchestration | Reuse scheduler, worker, outbox and provider operation receipts. Add coordination only for observed gaps in intake readiness, daily deduplication and recovery. | Daily batch handles late input, item failures and restart. Uncertain paid operations are reconciled by their original IDs. |
| 04 Agent & Operational Optimization | Measure bottlenecks. Reuse existing creative and QC roles. Add agent capabilities or resource lanes where the current behavior falls short. | Repeatable operation with measured cost, throughput and actionable failure reporting. |

## Runtime cutover

Source consolidation and runtime consolidation have separate acceptance gates. Keep existing schema ownership and runtime bindings during the source move. Physical database consolidation requires a demonstrated need, compatibility evidence and rollback. Move runtime data through an explicit cutover, independent of source copying.

For each transferred render intent, record source batch, listing and variant, profile, input digest, old and new owner, and active operation IDs. Stop old admission and reconcile pending provider work before transferring an intent. Keep pending and unknown receipts attached to the original operation on rollback. Use a verified backup outside both original checkout folders.

Retire the original checkouts only after the platform passes the [standalone cutover gate](10-implementation-architecture.md#final-cutover--retire-both-original-source-checkouts).
