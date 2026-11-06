# Product Automation Platform — Architecture Blueprint

> Current delivery scope is [Consolidation & Pipeline Integration](08-migration-roadmap.md). The [Master Prompt](MASTER-PROMPT.md) governs implementation. Original PI/VF code and behavior are the baseline. The proposed platform architecture below is long-term context.

Assessment date: 2026-11-03, Asia/Bangkok. Status: proposed. This package defines a platform evolution of the current PI → MP4 pilot and is the reference for future implementation.

**Recommendation.** Grow pi-vf-next into one Python modular monolith with PI, VF and Automation modules. Use one deployment, one platform database, one scheduler entrypoint and durable task receipts. Keep the original PI and VF applications operational behind narrow compatibility adapters during cutover.

**Evidence that changes the design.**

| Finding | Consequence |
|---|---|
| Python 3.11+ is common. PI is CLI/MCP and file-oriented on the preferred path. VF additionally uses FastAPI/Studio and a durable worker. | Share contracts and adapters. A common Python runtime does not require a common web framework or a Studio migration. |
| The named November 2 runtime has separate PI research, VF workflow and VF integration SQLite files. The new pilot has its own fourth file. | A single physical platform DB is a target decision. Existing database files require explicit read-only import mapping. |
| A local Spark-named CSV parses in PI as 60 candidates, with 11 direct links, 46 search links and 3 shop links. The pilot parser currently reports all 60 as needing input because its header alias differs. | Use a file adapter and a real fixture. Resolve supplied search/shop links into an evidenced listing before crawling. |
| Spark is the primary discovery source by product direction. The producer schedule, export mechanism and account-specific handoff remain unverified. | Start from a delivered artifact. Accept additional transports only when a real handoff is demonstrated. |
| Original VF has versioned workflow/outbox, provider attempts, artifact receipts and historical live evidence. The pilot has a smaller SQLite runner and offline evidence. | Reuse business behavior and reliability invariants. Prove the new live path before transferring production ownership. |

Google documents Spark task scheduling and approximate execution times. This supports an independent producer schedule and a consumer that waits for a committed batch. It does not establish an API or the provenance of the local sample. [Gemini Spark schedules](https://support.google.com/gemini/answer/17094710?hl=en)

## Package index

| Document | Decision or output |
|---|---|
| [02 — Current architecture](02-current-architecture.md) | Code, runtime, flow and risk inventory |
| [03 — Target architecture](03-target-architecture.md) | Options, domain interfaces, deployment |
| [04 — Business workflows](04-business-workflows.md) | Intake/crawl contracts, DAG, sequence and workflow definition |
| [05 — Database design](05-database-design.md) | Current and target ERD, ownership and migration |
| [06 — Agent and tool contracts](06-agent-tool-contracts.md) | Bounded model roles and execution policies |
| [07 — Reliability and operations](07-reliability-operations.md) | State, scheduling, resource control and reconciliation |
| [08 — Migration roadmap](08-migration-roadmap.md) | Incremental delivery, rollback and exit criteria |
| [09 — ADR index](../adr/README.md) | Numbered proposed decisions and trade-offs |
| [10 — Implementation architecture](10-implementation-architecture.md) | First deployable module layout, SQLite keys, stage semantics and Send protocol |

The first release slice is one delivered Spark batch, one confirmed listing, a frozen snapshot, and one 3–5-scene MP4. A controlled restart must preserve each paid submission count. Expansion to daily unattended operation follows evidence from a real handoff, verified browser/provider behavior and measured run costs.
