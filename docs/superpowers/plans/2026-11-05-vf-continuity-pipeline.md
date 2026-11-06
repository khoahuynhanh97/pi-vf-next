# VF continuity pipeline implementation plan

Goal. Make the normal VF workflow enforce current content QC and admit a continuous next scene only from a checked prior clip endpoint.

Architecture. Reuse SceneRenderRuntime, SceneRenderOperationStore, CreativeCritic, storyboard package and composition/export boundaries. Keep existing database schemas. Persist bindings in existing request JSON and content receipts on disk, use compare-and-set before any paid submission.

Spec. User-approved review findings in this chat, dated 2026-11-05.

Constraints. Preserve existing provider identities, schemas, paid-operation IDs and recovery semantics. Existing accepted work reconciles under its original request. Three previously authorized live renders are already consumed. Verification uses offline injected providers and actual media decoding.

Source reuse map

- Reuse unchanged. Flow upload/reconcile adapter, provider capacity and operation claiming, native media decoder, hash checks and existing composer.
- Reconfigure. Default new production creative batches to high QC across CLI/Studio/operator entrypoints. Existing persisted policies retain their settings.
- Adapt. Director validation and anchor refresh, final receipt propagation and export admission, per-scene content review.
- Missing. Explicit same-setting continuity admission, durable endpoint binding before the next scene is submitted, final content checks on completed reuse.

Task 1. Prompt consistency

- [x] Reproduce camera-motion rejection and stale identity anchors with focused tests.
- [x] Permit single purposeful camera motion under the four-agent visual contract, preserve legacy validation.
- [x] Refresh existing anchor JSON while retaining actual prompt and repair instructions.
- [x] Verify Director, repair and contract tests.

Task 2. Production policy

- [x] Require final content receipt and review for high QC, retain low/middle explicit semantics.
- [x] Default new creative entrypoints and scheduler to high.
- [x] Test missing, stale, FAIL and UNKNOWN final evidence and explicit level overrides.

Task 3. Scene admission and continuity

- [x] Review each completed clip before admitting a later scene on high QC.
- [x] Allow the Director to opt into a continuous join through an existing continuity field using an explicit marker. Default intentional cuts remain independent.
- [x] Bind a planned next request to decoded tail pixels only after clip and endpoint QC, keep original operation identity and persist changed request digest atomically before claiming submission.
- [x] Do not rebind accepted requests or reinterpret old submitted work on resume.
- [x] Test repeat polling, competing admission, tampering, failed/unknown QC, and all independent-scene behavior.

Task 4. Final QC and verification

- [x] Review final content and joins with CreativeCritic for high, validate both content and technical verdicts.
- [x] Completed reuse must reload/revalidate current receipt; final export requires it for high QC.
- [x] Verify final evidence propagation into workflow and export metadata, malformed planner replies fail cleanly.
- [x] Run scoped and broader offline tests, obtain independent code review and resolve important findings.

Execution decisions

- Source repo has an unborn branch, so a worktree cannot reproduce its untracked source. Snapshot original target files under the user Temp directory and edit the authorized checkout.
- Continuity is explicit per boundary. A scene that changes product arrangement or location uses its independently approved START.
- Keep useful product assets unchanged and avoid synthetic background extension in production.

Verification. Final scoped regression run passed 174 tests in 45.55 seconds. Independent review findings on direct resume, prepared-source defaults, completed reuse and latest recovery attempts were addressed. Offline tests cover real media decoding with injected providers and critics. Broad legacy suites still have baseline failures recorded during the earlier audit. Live Flow authorization of three attempts was exhausted before this implementation round.


