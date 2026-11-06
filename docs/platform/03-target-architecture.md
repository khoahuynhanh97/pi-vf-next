# Target architecture

## Options

| Option | Benefits | Costs and fit |
|---|---|---|
| Separate repositories and existing Exchange | Highest deployment independence, lowest initial migration risk | Coordinating versions, DBs and diagnostics remains manual. Best compatibility path during migration. |
| Monorepo with separate apps | Shared CI and contract development, independent deployment | Runtime handoff and failure modes remain distributed. Useful if PI/VF gain distinct operational owners. |
| One modular monolith | Local application calls, one scheduler and one deployment, clear table owners | Requires enforced import and DB boundaries. Recommended for the observed single-host workflow. |
| Specialized services | Independent scaling, fault isolation, resource/security separation | Remote coordination, credentials and deployment overhead. Extract only a measured constraint, such as browser hosts or render capacity. |

## Modules and dependency direction

~~~mermaid
flowchart LR
    Spark[Gemini Spark artifact] --> PI[Product Intelligence]
    Legacy[Legacy Discovery adapter] --> PI
    Intake[Excel or Sheet snapshot] --> PI
    Clock[Host scheduler] --> A[Automation application]
    A --> PI
    A --> VF[Video Factory]
    A --> Agent[Bounded agent runner]
    PI --> Fetch[Marketplace and Spaiauto adapters]
    VF --> Flow[Flow and FFmpeg adapters]
    Agent --> Model[Configured model router]
    PI --> DB[(Platform DB with logical ownership)]
    VF --> DB
    A --> DB
~~~

| Domain | Owns | Proposed application interfaces |
|---|---|---|
| PI | Discovery receipts, candidate resolution, canonical catalog, crawl attempts, media provenance, eligibility and ranking | ingest(batch), resolve(candidate_id), request_crawl(request), validate(observation_id), select(eligible_ids, policy) |
| VF | Projects, immutable video input snapshots, plans, scene artifacts, provider generation/QC and exports | prepare(snapshot_contract), accept_plan(project_id, plan), render_scene(command), reconcile(operation_id), publish(project_id) |
| Automation | Workflow definitions, schedules, run/task state, dependencies, attempts, model dispatch, budget and approvals | start(schedule_key, slot), claim(lane), complete(claim, result), pause(run_id), cancel(run_id), resolve_approval(command) |

Interfaces are proposed in-process commands and queries. Each domain validates commands and writes through its own repositories. VF consumes a versioned product projection returned by PI. Automation passes references and outcomes across interfaces. Adapter dependencies point inward toward contracts. Shared code is restricted to identifiers, digests, artifact references and error categories.

## Deployment

Keep pi-vf-next as the platform repo. Group existing code by capability as it grows and keep product files flat in the existing per-product folder. Run a host scheduler and one runner on the current Windows host initially. Add logical work lanes for network acquisition, browser sessions, LLM, generation and CPU rendering. Browser lanes have explicit profile ownership. Independent worker processes are justified after measured queue pressure or contention, with the same application interfaces and DB leases.

Keep Exchange READY import/export for legacy interoperability. Preserve legacy PI discovery and current guards. Original VF Studio and workers can serve existing jobs during the transition. A routing ledger assigns one production owner to each batch/product/render intent. Use the verified original implementations as compatibility adapters or sources for focused ports, with no runtime import dependency from pi-vf-next on neighboring checkout paths.
