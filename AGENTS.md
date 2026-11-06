# PI/VF Consolidation & Automation Integration

Follow **Move → Verify → Integrate → Automate → Optimize**. The original PI and VF applications are the source of business behavior. The pilot in this repo is reference material for orchestration.

- Preserve existing features, Legacy Discovery controls, providers, entrypoints, models, migrations, job state and recovery semantics.
- Before proposing changes, identify source evidence for what to reuse unchanged, move or reconfigure, adapt minimally, and implement because it is missing.
- Keep source consolidation separate from runtime consolidation. Preserve existing database schemas and application boundaries during the move. Prefer configuration or a small integration adapter when a seam is missing.
- Avoid rewrites, unrelated refactors, duplicated business logic and new frameworks or abstractions without demonstrated need.
- Phase 01 acceptance requires independent installation and offline verification of both transferred apps with temporary data. Final cutover requires the unified pipeline and scheduler to run without either original checkout path.

The [four-phase roadmap](docs/platform/08-migration-roadmap.md) and [Master Prompt](docs/platform/MASTER-PROMPT.md) govern delivery. Older architecture and pilot documents provide context.
