# Business workflows and Candidate Intake

## Intake boundary

Initial transport is an explicitly delivered CSV/XLSX artifact in an operator-controlled inbox. Treat the Spark-named file as a fixture for column mapping. Map product or discovery URL separately from the direct listing field. Retain source row, raw fields, timestamp and digest. For public Sheet snapshots, reuse the validated download path. Legacy Discovery exports enter through a distinct adapter and follow the same candidate contract. Mark source type explicitly at ingestion, never infer authorship from a filename.

The local delivery adapter stages a copy, verifies size, format and digest, then atomically promotes it and commits a receipt. It only admits committed inputs. If a verified producer cannot write a commit marker, the adapter must create the receipt after acquiring a stable copy from that transport. Operator import of a supplied file uses the same path.

| Proposed contract | Required fields and semantics |
|---|---|
| DiscoveryBatch v1 | producer_kind (spark, legacy, operator, unknown), transport_kind (file, sheet_snapshot), source_namespace, external_id if available, intended_slot if attested, observed_at if supplied, received_at, artifact_uri, artifact_sha256, format_version. Idempotency by source namespace and external batch ID or raw digest. Same ID with changed bytes becomes a conflict. Transport alone never determines provenance. |
| ProductCandidate v1 | candidate_id, batch_id, source_locator (file/tab/row or record ID), raw_url, url_kind, optional normalized_listing_url, title_hint, requested_variant, raw_metadata, observation provenance. Retain estimates as hints. |
| CandidateIntakeResult v1 | batch_id, outcome, counts by state, candidate result IDs and row-level errors. Outcomes are accepted, partial, rejected or replayed. |
| CrawlRequest v1 | request_id, candidate_id, marketplace, normalized_listing_key, canonical product_id if known, normalized_url, requested_variant, freshness_policy_version, intent (crawl or refresh), deadline, idempotency_key. |

An unresolved URL is a valid intake record. It reaches NEEDS_REVIEW or the supported resolver for that source link. A resolver returns an observed listing URL and evidence from the supplied search/shop/category source. It never expands to unrelated discovery. Use marketplace IDs as listing identity and normalized variant evidence for resolution. Keep canonical Product ID distinct. Consolidate multiple listings into one product only when evidence warrants it.

## Crawl and selection

The PI dispatcher chooses reuse, refresh or crawl using canonical/variant resolution, freshness window, a valid media receipt and the latest successful observation. A fresh record with missing bytes or unresolved variant is ineligible for reuse. Start with policy configured per source, measured freshness and explicit refresh requests. A PI job records attempts, source errors, captured payload digest and terminal outcome. Reuse Spaiauto capture/ZIP import and existing marketplace resource adapters behind the same dispatcher. Verify the observed listing belongs to the submitted URL before binding ZIP. Persist raw evidence and normalized product/observations with provenance.

Validate deterministic fields before ranking. Check URL/listing consistency, known requested variant, readable media, freshness and review requirements. Score eligible products using configured weights for reliable source signals, market fit and visual suitability. Let the analyst rerank a bounded shortlist, with explicit reasons and IDs. Pick at most 15 after validation. For load control, crawl in bounded waves, preserving an auditable defer reason for candidates outside the run budget. Missing or failed items can be excluded from the final selection with a recorded outcome.

~~~mermaid
flowchart LR
    A[Await batch] --> B[Validate and ingest]
    B --> C[Resolve and dispatch by candidate]
    C --> D[Reuse or crawl and validate]
    D --> E[Join terminal candidate outcomes]
    E --> F[Rank and pin at most 15]
    F --> G[Freeze snapshots and product locks]
    G --> H[Plan 3 to 5 scenes per project]
    H --> I[Generate and review keyframes]
    I --> J[Assemble approved storyboard]
    J --> K[Render and review clips]
    K --> L[Join accepted scene receipts]
    L --> M[Compose MP4 and verify]
    M --> N[Publish output or await approval]
~~~

~~~mermaid
sequenceDiagram
    participant Spark
    participant Intake as PI Intake
    participant Runner as Automation Runner
    participant PI as PI Catalog
    participant VF as VF
    participant Flow as Provider
    Spark->>Intake: Deliver artifact through verified transport
    Runner->>Intake: Validate and ingest by digest
    Intake-->>Runner: Batch receipt and candidate IDs
    Runner->>PI: Resolve, crawl or reuse, validate and select
    PI-->>Runner: Selected versioned product projections
    Runner->>VF: Prepare immutable snapshot and project
    VF->>VF: Validate plan and reserve operation
    VF->>Flow: Submit with request key where supported
    Flow-->>VF: Provider job ID or ambiguous response
    VF->>Flow: Reconcile the same operation
    VF-->>Runner: Reviewed clips and verified export receipt
~~~

## Definition example

Illustrative internal YAML. Stage names are application handlers. WorkflowDefinition is a versioned stage graph plus policy reference. WorkflowSchedule is an enabled named trigger with timezone, cadence, input source, grace window and overlap policy. WorkflowRun pins the version, trigger slot and input receipts. TaskRun stores one node and scope. TaskAttempt is a leased invocation of that node. TaskDependency identifies prerequisite task and required result. WorkflowExecutionContext carries run ID, policy version, deadline, trace ID and reference IDs.

~~~yaml
id: product-to-video
version: 1
schedule:
  key: spark-daily
  zone: Asia/Bangkok
  cadence: daily
  time: operator-configured
  overlap: defer
  input_source: spark
  input_grace: operator-configured
policy: standard-v1
steps:
  - {id: intake, handler: pi.ingest}
  - {id: validate, handler: pi.resolve_crawl_validate, after: [intake], map: candidates}
  - {id: select, handler: pi.rank_select, after: [validate], join: terminal, limit: 15}
  - {id: plan, handler: vf.snapshot_lock_plan, after: [select], map: products}
  - {id: keyframes, handler: vf.generate_review_keyframes, after: [plan], map: scenes}
  - {id: storyboard, handler: vf.assemble_storyboard, after: [keyframes], join: accepted}
  - {id: clips, handler: vf.generate_review_clips, after: [storyboard], map: scenes}
  - {id: export, handler: vf.compose_verify, after: [clips], join: accepted}
  - {id: publish, handler: vf.publish, after: [export], approval: policy}
~~~

Scene joins are scoped to their own project revision. Candidate join accepts valid, failed and deferred outcomes. Apply per-source resolution deadlines and a run-level selection cutoff. At the cutoff, unresolved review or crawling candidates receive an explicit deferred outcome for this run, while preserving the candidate and any in-flight acquisition receipt. An empty eligible set ends as NO_ELIGIBLE_PRODUCTS with reason counts and no VF jobs. Late results can enter an explicit later selection by candidate ID and receipt. An incomplete scene join never publishes a partial MP4. Per-item retries affect only dependent work. Default scenes are 4, with 3–5 supported. The preflight computes duration, required first attempts and budget from the pinned plan.
