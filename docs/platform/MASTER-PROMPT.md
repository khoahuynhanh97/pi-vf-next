# Master Prompt — PI/VF Consolidation & Pipeline Integration

Use this prompt with the workspace rooted at D:/work/pi-vf-next.

The [repository layout](REPOSITORY-LAYOUT.md) is the physical folder reference for Phase 01 and later phases.

## Preflight reuse map

These classifications use the working-tree audit. Recheck the actual files and call sites at the start of Phase 01.

| Classification | Baseline |
|---|---|
| Reuse unchanged | PI file intake, Legacy Discovery controls, crawl/import and source validation. VF creative/QC, generation, composition, export and durable job handling. |
| Move or reconfigure | PI and VF packages, entrypoints, migrations, dependencies, static assets, scripts and tests. Rebind executable paths, profiles and config during runtime cutover. |
| Adapt minimally | Spark delivery to existing PI file intake, PI acquisition/review to existing Exchange, and Exchange admission to VF. Verify eligibility, manifest and resume contracts before changing them. |
| Implement only if missing | A verified daily Spark delivery binding and the coordination between PI acquisition completion and VF admission. Confirm gaps in the actual runtime before adding workflow or agent code. |

See [current source evidence](02-current-architecture.md). The existing PI parser already reads the observed Spark CSV. The next integration question is delivery and acquisition-to-READY, rather than another parser.

~~~text
CRITICAL PROJECT DIRECTIVE — EXISTING SYSTEMS CONSOLIDATION & AUTOMATION INTEGRATION

You are the technical lead. Deliver one self-contained source repository containing the existing PI and VF applications and a unified Spark → PI crawl/process → VF generation pipeline.

Move → Verify → Integrate → Automate → Optimize.

Move and reuse the original implementations. Preserve business rules, Legacy Discovery and its existing controls, crawlers, providers, configuration semantics, database models, migrations, jobs and recovery. Keep repository consolidation separate from runtime consolidation. Make changes only where an observed integration or compatibility gap requires them. Working implementations take priority over newly proposed frameworks, schemas, abstractions and agent roles.

Before proposing an implementation, produce a four-way source map with file or command evidence: reuse unchanged, move or reconfigure, adapt minimally, and genuinely missing. For each proposed change record the observed gap, existing path considered and acceptance check.

Destination: D:/work/pi-vf-next
Original PI: D:/work/Personal/Product-Intelligence
Original VF: D:/work/video-factory
Read docs/platform/08-migration-roadmap.md first. The four-phase consolidation scope takes precedence over earlier target designs. The existing pi-vf-next prototype is reference material for orchestration. The original PI and VF applications are the source for business behavior.

Target layout:
  apps/pi/           Existing PI source with its packaging and entrypoints
  apps/vf/           Existing VF source with its packaging and entrypoints
  automation/        Integration and orchestration required by later phases
  shared/            Shared code only after a real need is proven
  infrastructure/    Shared runtime configuration and deployment as needed
  docs/              Source map, decisions, and operating instructions

Start with a focused audit, then execute Phase 01 only in the first implementation turn:
1. Inventory actual working trees, including required untracked source, package files, migrations, tests, static assets, configs and scripts. Identify absolute paths and sibling imports. Exclude secrets, virtual environments, caches, live SQLite files and generated media from the source transfer.
2. Transfer required source into apps/pi and apps/vf. Preserve package names, CLI entrypoints, tests, database schemas and migration behavior. Adjust packaging, imports and path resolution only where consolidation requires it. Retain the original checkout copies until cutover acceptance.
3. Install each app from the destination in a clean environment. Run existing offline checks with separate temporary data roots. Verify that the apps can start with the original checkout paths unavailable. Record any existing failures separately from regressions introduced by the move.
4. Deliver the source map, commands actually run, test results, outstanding runtime dependencies and a verified PI-to-VF seam for Phase 02.

Phase 02 connects the verified Spark artifact through PI intake, resolution, crawling and validation to VF using the existing handoff. Use READY/manifest if that is the working interface. Reuse the existing creative, generation, QC and export paths. Keep the observed CSV fixture and preserve raw provenance. Do not infer a Spark API.

Phase 03 reuses existing scheduler, worker, outbox and operation tracking where applicable. Add only missing cross-app coordination, daily run deduplication and recovery. Preserve paid operations across restart and reconcile uncertain Send outcomes by original operation.

Phase 04 optimizes measured bottlenecks and adds only missing AI capabilities. Reuse current creative and QC roles. Treat expanding scene count, agent routes or concurrency as targeted enhancements with explicit acceptance evidence.

Use one source repository, not a mandatory single process or immediate database merge. Consolidate runtime paths and ownership while preserving schemas. Propose physical database consolidation only with a proven compatibility plan and rollback. Use temporary data and offline stubs during consolidation. Live generation and scheduler cutover are separate acceptance steps.

Final cutover requires both apps, all schedules and end-to-end recovery to work using only the new source folder and retained runtime data independent of the original folders. The owner retires the originals after that evidence and a verified backup.
~~~
