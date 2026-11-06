# Database and data ownership

## Observed current ERD

This is the product-to-video subset. Solid lines represent declared FKs. Dotted lines represent application references or JSON bindings. Each subgraph is a separate observed SQLite file. Schema inventory was read from base files with immutable read-only access. The VF workflow file had a nonempty WAL, so this inventory does not establish the complete live state.

~~~mermaid
flowchart LR
    subgraph PI[PI snapshots.db]
      RS[research_snapshots PK snapshot_id]
      RL[resource_pack_locks PK lock_id]
      RS -. snapshot_id .-> RL
    end
    subgraph WF[VF hermes.db]
      WF1[workflow PK workflow_id]
      WE[workflow_event PK event_id]
      WO[workflow_outbox PK outbox_id]
      PC[product_master_catalog]
      DI[daily_product_video_import]
      WF1 --> WE
      WF1 -. workflow_id .-> WO
      DI -. workflow association .-> WF1
    end
    subgraph VF[VF integration video_factory.sqlite]
      VP[video_factory_projects PK id]
      VA[video_factory_artifacts PK artifact_id]
      VB[video_factory_render_batches PK render_batch_id]
      VS[video_factory_scene_operation]
      VP -. project_id .-> VA
      VP -. project_id .-> VB
    end
    subgraph Next[Pilot state.sqlite]
      R[runs PK id]
      I[items PK id]
      O[operations PK key]
      R --> I
      I --> O
    end
    Exchange[Reviewed acquisition and Exchange manifest] -. file handoff .-> DI
~~~

PI's file-first handoff can originate from reviewed acquisition without a resource-pack lock row. The file handoff edge represents Exchange, with no table join. Legacy Discovery has its own logical tables for discovery_runs, product_history, trend_observations and daily_selections. The VF catalog is an Exchange-derived projection in the audited flow. Source anchors: PI:media/research_persistence/repository.py:17, PI:product_scout/trend_discovery/repository.py:62, VF:src/video_factory/storage_infra/sqlite.py:95, VF:src/video_factory/integrations/repository.py:224, NEXT:src/pi_vf/store.py:24.

## Proposed logical ERD

~~~mermaid
erDiagram
    DISCOVERY_BATCH ||--o{ PRODUCT_CANDIDATE : contains
    PRODUCT o|--o{ PRODUCT_CANDIDATE : resolves
    PRODUCT_CANDIDATE ||--o{ CRAWL_JOB : requests
    CRAWL_JOB ||--o{ CRAWL_ATTEMPT : attempts
    PRODUCT ||--o{ PRODUCT_OBSERVATION : versions
    PRODUCT_OBSERVATION ||--o{ PRODUCT_MEDIA : supplies
    PRODUCT_OBSERVATION ||--o{ PRODUCT_VALIDATION : validates
    PRODUCT_OBSERVATION ||--o{ VIDEO_SNAPSHOT : freezes
    VIDEO_SNAPSHOT ||--o{ VIDEO_PROJECT : drives
    VIDEO_PROJECT ||--o{ VIDEO_OPERATION : renders
    VIDEO_PROJECT ||--o{ VIDEO_ASSET : produces
    WORKFLOW_DEFINITION ||--o{ WORKFLOW_RUN : pins
    WORKFLOW_SCHEDULE o|--o{ WORKFLOW_RUN : triggers
    WORKFLOW_RUN ||--o{ TASK_RUN : contains
    TASK_RUN ||--o{ TASK_ATTEMPT : attempts
    TASK_RUN ||--o{ TASK_DEPENDENCY : requires
    TASK_RUN ||--o{ APPROVAL_REQUEST : gates
    TASK_ATTEMPT ||--o{ AGENT_EXECUTION : records
    AGENT_EXECUTION ||--o{ AGENT_TOOL_CALL : audits
    TASK_ATTEMPT ||--o{ USAGE_RECORD : accounts
~~~

Relationships express logical ownership and references. TaskDependency has two TaskRun IDs, dependent and prerequisite. A WorkflowRun points to an immutable definition version. ProductObservation stores the verified version used for a video, while Product holds current canonical identity and current observation pointer. Crawl attempts may fail before Product exists. Scene plans and QC stay versioned inside VF project or artifact receipts until query patterns justify separate tables.

| Logical owner | State and identifiers | Initial mapping |
|---|---|---|
| PI | DiscoveryBatch unique by source namespace and key. Candidate unique by batch and source locator. Listing binding unique by marketplace and listing ID with explicit variant dimension. Product uses a stable internal ID. Observation unique by product and evidence/version digest. | Reuse intake/research provenance and legacy exports. Add canonical catalog and intake durability where existing repositories do not cover them. |
| VF | VideoSnapshot unique by content digest. VideoProject unique by render intent, snapshot and profile/version. VideoOperation unique by deterministic request key including project revision, scene, kind and attempt. Artifact stores SHA, storage key, parents and role. | Reuse VF project/receipt semantics and pilot operation tracking. Preserve historical profile and provider identifiers. |
| Automation | WorkflowRun unique by schedule and slot. TaskRun unique by run/node/scope/revision. Attempts unique by task/ordinal. Store lease token, epoch, deadline, retry eligibility and outcome. Approval unique by task, revision and artifact digest. | Evolve pilot runs/items/operations in place. Definition/schedule may start as versioned config, dependency graph as pinned definition, and attempts/tool/usage as durable receipts. Materialize dedicated tables when their operations need independent queries or transactional constraints. |

The platform uses one physical database with separate repositories and migration ownership. A shared migration sequence allocates change IDs and validates compatibility. PI writes Product; VF writes snapshots/projects; Automation writes execution and reservation state. Cross-domain references are stable IDs plus version/digest. Current pilot listing-wide uniqueness must evolve to separate canonical listing resolution from per-run intent.

## Consistency and transition

- Intake batch and normalized rows commit together, with a receipt for rejected rows. Use short write transactions. Crawl observation, validation and canonical pointer changes are owned by PI and versioned together when published.
- VF copies the exact product facts, source/variant, evidence image IDs and SHAs, eligibility verdict, lock version and render policy needed by its plan. Resolve reads by snapshot ID. Media bytes stay available through reference retention or verified copies.
- Automation claims, task results and dispatch records commit before acknowledgement. Before provider submission, reserve budget and persist the VF operation plus correlation key. Co-locate writes in a short shared transaction where appropriate. Publish task-ready state only after the durable operation exists. Provider I/O and FFmpeg run outside write transactions.
- Write output to a temporary file, verify, atomically publish, then commit its receipt. Reconcile orphaned files by pinned operation and SHA after restart.
- Import archived observations and completed outputs through read-only projections with source ID/digest mapping. Snapshot SQLite using a supported backup or checkpoint-aware process during implementation. Base-file inspection here does not replace that. Drain active provider jobs on the original owner or import a reconciled receipt with the original operation ID. Record duplicate and identity conflicts for manual resolution.
- Prefer additive migrations with versioned readers for existing four-scene plans. Retain source DBs, original READY manifests and receipt paths for rollback. Media GC checks references from all retained snapshots, in-flight operations, approvals and exports, then quarantines before expiry. Retention and deletion are operator policy.
