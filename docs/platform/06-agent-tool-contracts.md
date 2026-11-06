# Agent and tool contracts

Agents are bounded model calls with versioned JSON input/output. Automation supplies evidence and owns dispatch. PI/VF validate domain output before applying it.

| Capability | Input | Output and validator | Tools |
|---|---|---|---|
| Product Analyst | Eligible candidate IDs, normalized signals, freshness, media coverage, policy and max count | Ranked IDs and concise reasons. Reject unknown/ineligible IDs, duplicates, unsupported claims and excess count. Deterministic eligibility remains PI-owned. | None by default |
| Product Lock reviewer | Observed listing/variant, selected source image IDs/SHAs, source evidence and requested details | Verified identity, primary/detail images, facts with image/evidence IDs and PASS/FAIL/UNKNOWN per essential detail. Validate source membership and variant semantics. | Read approved artifact by ID |
| Content Strategist and Script Agent | Immutable snapshot, lock, audience, preset and 3–5 scene constraint | One brief and plan. Each scene includes stable ID/order, duration, goal/action, source reference ID, required fact IDs, image prompt, motion prompt and optional narration only when profile supports it. Validate visible construction and capability before spending. | Read approved snapshot and source references |
| Quality Reviewer | Snapshot/lock, scene contract, generated keyframe or sampled clip frames and reference SHAs | Per-detail verdict and evidence, observed mismatch or missing view, repair hint. Validate enum, detail membership and frame IDs. UNKNOWN does not trigger regeneration by itself. | Read pinned input and sampled frame artifacts |

Example envelope fields for all roles: contract_version, task_id, attempt_id, input_digest, policy_version, prompt_version, evidence_refs, max_output_tokens and deadline. The result binds input_digest, model route, verdict/output, evidence IDs and usage. Store structured output and validation errors against the execution. Provider response ID, latency, token usage and cost provenance are optional observed values. An unreported cost remains unknown.

### Contract v1 shapes

Type notation below defines proposed JSON objects. Fields are required unless marked optional. IDs are nonempty strings. Digests are lowercase SHA-256 hex. UTC timestamps use RFC 3339. Unknown fields are rejected by version-specific validators.

~~~text
EvidenceRef = { id: string, sha256: digest, role: string }
Call = { contract_version: 1, role: analyst | lock | script | qc,
         task_id: string, attempt_id: string, input_digest: digest,
         policy_version: string, prompt_version: string,
         evidence_refs: EvidenceRef[], max_output_tokens: positive integer,
         deadline: UTC timestamp, input: RoleInput }
Result = { contract_version: 1, attempt_id: string, input_digest: digest,
           output: RoleOutput }

AnalystInput = { eligible: CandidateSignal[], max_count: integer 1..15 }
CandidateSignal = { candidate_id: string, signals: map of finite number or string }
AnalystOutput = { selected: { candidate_id: string, reason: string }[] }
LockInput = { listing_key: string, requested_variant: string or null,
              facts: { id: string, text: string, evidence_ids: string[] }[] }
LockOutput = { verdict: PASS | FAIL | UNKNOWN, reason: string,
               primary_image_id: string or null, detail_image_ids: string[],
               facts: { id: string, text: string, evidence_ids: string[] }[] }
ScriptInput = { lock_digest: digest, audience: string, preset_id: string }
ScriptOutput = { lock_digest: digest, brief: string, scenes: Scene[3..5] }
Scene = { id: string, order: positive integer, duration_seconds: positive integer,
          action: string, reference_id: string, required_fact_ids: string[],
          image_prompt: string, motion_prompt: string, narration?: string }
QCInput = { lock_digest: digest, scene_id: string, required_fact_ids: string[],
            reference_ids: string[], sample_ids: string[] }
QCOutput = { findings: { fact_id: string, verdict: PASS | FAIL | UNKNOWN,
                         observed: string, sample_ids: string[],
                         repair_hint?: string }[] }
ReadArtifact = { artifact_id: string, sha256: digest, view: preview | original }
ReadArtifactResult = { artifact_id: string, sha256: digest, media_type: string,
                       size_bytes: nonnegative integer, content_ref: string }
~~~

For QC, require one finding per required fact and derive the aggregate in the application. FAIL outranks UNKNOWN, and all required facts must pass for PASS. A scene without a visible product must be explicit in its profile and approved by deterministic scene policy. Lock PASS requires an eligible primary image and evidence for variant-specific facts. A ReadArtifact result returns a bounded task-scoped capability or bytes, rather than a host path. The caller context supplies attempt identity. Reject NOT_ALLOWED, SHA_MISMATCH, NOT_FOUND, TOO_LARGE or DEADLINE with structured error codes. Attach route, usage and cost metadata outside the model-authored result.

The runner sets route/model, timeout, output cap, tool cap and model-call budget per role. Begin with one bounded request for lock, one for brief/script, and one per image or clip QC. Permit one repair request for malformed output only if budget allows. Transient transport retry is separate from content correction. Auth, policy failure, unknown IDs, or unavailable evidence use explicit blocking reasons. No automatic switch to a different model for paid generation.

Read tool calls pass through an application handler scoped to the task, product and asset allowlist. Return bounded bytes or previews and verify SHAs. Record tool name, argument digest, result code, duration and trace ID. Secrets, DB credentials and arbitrary filesystem, browser, shell, provider Send and publishing rights remain outside the agent execution context. Any future side-effect tool requires an explicit command schema, policy/approval check and idempotency receipt before dispatch. Seller text and source documents are data.

For a supervised Antigravity trial, give the script role a pinned lock and approved local artifact copies, then import a JSON result through the same validator. The Gemini Flash API is a candidate for routed reasoning/vision. Model ID and availability must be checked in the actual execution route. Flow remains the media generation provider. Reuse the prior storyboard convention of assembling approved keyframes and script data without a separate model call. [Pilot creative specification](../DESIGN.md)
