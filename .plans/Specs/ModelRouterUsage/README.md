---
title: Model Router Usage Reporting
type: spec
status: approved
created: 2026-09-07
updated: 2026-09-09
tags: [model-router, usage, observability]
related: [Research/model-router-usage-source.md]
---

# Model Router Usage Reporting

## Overview
A visual routing inspector: see which agents ran, which models they actually called, and how many tokens went to and came from each model. The purpose is to check that routing works as intended.

The main view connects **agent → expected model → actual model**, with call counts and token bars. Cache hits and misses appear when the source supports them. Users can inspect one task, including its workers, or the current project.

This is a specification for review, not authorization to implement it. Pricing is excluded. SQLite access belongs exclusively to maintained application code.

## Goals
- Make unexpected agent/model combinations easy to spot.
- Show model call counts and token input/output for each agent and model.
- Show cached versus uncached input when available.
- Let a person understand routing without reading logs, SQL, or raw JSON.

## Non-Goals
- Pricing, costs, billing, budgets, or account quotas.
- Automatically changing routes or choosing models.
- Exact reconstruction of information the provider or runtime did not retain.
- Latency, throughput, retries, or physical HTTP-attempt statistics.
- Hosted telemetry, cross-project analytics, CSV export, or a general analytics dashboard.
- Capturing prompts, responses, tool content, source code, or credentials.

## Requirements
A **completed call** is one completed model-generation step observed by the supported collector. One user prompt can produce several calls as the agent uses tools. A configured route, a streaming update, a whole session, and an HTTP retry are not separate completed calls by themselves.

### Functional Requirements
- **FR-01**: **Actual usage:** Collect the actual provider and model used by each completed call, including a primary agent's UI-selected model. Record project, session, agent, completion time, and a stable source identifier. Do not infer actual usage from router profiles. Attribute unavailable fields to `unknown` rather than inventing values.
- **FR-02**: **Token display:** Show input and output totals per agent/model using the runtime's reported values. Where a verified contract separates categories, input includes uncached input plus cache reads/writes, and output includes visible output plus reasoning; count each category once. Show cached input (hit) and uncached input (miss) only when their semantics are verified; otherwise show “unavailable.” Preserve the underlying categories in code. Label all totals “reported tokens.” If the runtime collapses missing usage into zero, show a clear limitation on the view and call detail; do not claim those zeroes are confirmed measurements. Genuinely absent fields display unavailable. Exact recovery of lost upstream distinctions is not required.
- **FR-03**: Removed during the 2026-09-07 scope revision; the id is retired so numbering stays stable. See the scope update in [Model Router Usage Source Compatibility](../../Research/model-router-usage-source.md).
- **FR-04**: **Exactly-once totals:** Repeated notifications, replay, concurrent collection, and restarts must not duplicate calls. A later correction replaces the same source record. Choose one authoritative accounting level; do not add both message totals and their constituent step totals. An interrupted generation without a completed step is not counted as completed usage.
- **FR-05**: **Session totals:** Support direct-session and task-tree totals. The latter includes each descendant session's own records exactly once, including nested workers. Retain parent links when known. Unknown ancestry must be shown as incomplete coverage; never guess that an unrelated session belongs to a task.
- **FR-06**: **Persistence:** Store usage in a separate SQLite database inside the existing project data volume. Preserve it across launcher restarts, support concurrent writers, and isolate default project volumes. When persistence is disabled, usage is ephemeral. Any shared-volume report must still filter by project identity. Do not write into OpenCode's database.
- **FR-07**: **Visual routing view:** `opencode-container stats` opens a human-readable routing view for the current project. Its main table shows agent, expected model, actual model, route status, completed calls, input tokens, output tokens, and cache hit/miss when available. Use compact bars to compare token volumes, with visible numbers and text labels so meaning never depends on color. Split rows by agent and actual model; one agent using two models must remain visible. Provide a per-model summary without losing the agent view. Filter by session, agent, or model and let users inspect individual call metadata. Empty and unavailable states must be explicit. The view must work after OpenCode exits and must not start an interactive coding session, rebuild an image, call a model, or require provider credentials or a host engine socket mount.
- **FR-08**: Removed during the 2026-09-07 scope revision; the id is retired so numbering stays stable. See the scope update in [Model Router Usage Source Compatibility](../../Research/model-router-usage-source.md).
- **FR-09**: **Collection controls:** `usage_stats.enabled` defaults to true. Disabling stops new collection while preserving prior reports; re-enabling resumes without importing intentionally excluded intervals. Keep collected records in the project volume until it is removed. Show collection state and last update, and mark incomplete coverage when known.
- **FR-10**: **Coverage:** Required coverage is primary agents and delegated workers. Ignore user and synthetic messages when counting calls. Title and compaction calls may be labeled unsupported until verified. Reconcile missed records from the supported source when available, without duplication, and mark known gaps. Accurate agent/model attribution is required; optional cache detail and upstream token limitations must not prevent displaying routes.

Proposed entry points:

```sh
opencode-container stats
opencode-container stats --session SESSION_ID --include-children
opencode-container stats --agent implementer
opencode-container stats --model local/my-model
```

Filters combine using AND. `--session` selects direct calls; `--include-children` adds descendants and requires `--session`. Visual layout and UI technology belong in the design.

- **FR-11**: **Code-only database access:** All SQLite access must be implemented in maintained application code: collection, queries, aggregation, export, migrations, recovery, and maintenance. Skills and LLM agents may invoke documented CLI commands with validated arguments and consume their output; they must never open SQLite files, issue SQL, use database tools directly, or generate ad hoc scripts to access the database. The reporting interface accepts no raw SQL. This boundary applies to both the telemetry database and any proposed read access to OpenCode storage (D-0001).

- **FR-12**: **Route verification:** Compare actual models with the effective expected model at call time. Honor the primary agent's UI selection when its model is not pinned. Display “match,” “unexpected,” or “unverified” with text, alongside the actual model. Missing historical route configuration means unverified, not a mismatch. Never compare an old call against today's configuration as if it were the original expectation. Keep successful matches visible; this is an inspection tool, not just an alert list. The expectation-provenance approach for this comparison is decided in (D-0002).

### Non-Functional Requirements
- **NFR-01**: **Privacy:** Store only IDs, agent/model identities, route-comparison metadata, reported token counts, timestamps, and collection health needed by this inspector. No request/response bodies, headers, environment values, tool content, session titles, or full filesystem paths in stored telemetry or diagnostics. No hosted telemetry.
- **NFR-02**: **Failure isolation:** Collection must not change model requests, responses, routing, or permissions. Database writes use a bounded asynchronous queue, never a wait for storage on the generation path. Queue overflow or storage failure drops or defers telemetry with a visible warning; it must not fail or block coding. Once storage is accessible, persist the gap or reconcile it from the source.
- **NFR-03**: **Bounded resources:** Limit the pending queue to at most 1,000 records and graceful-shutdown flushing to at most two seconds. Overflow and shutdown losses follow FR-10. SQLite files must not be world-readable. Schema upgrades must preserve existing data or refuse safely with a clear report error; never silently reset the database.
- **NFR-04**: **Maintainability:** Keep collection, aggregation, and visual presentation separate from route assignment. Pin supported contracts. Test duplication, model changes, unavailable token/cache data, route comparison, session trees, and storage failures. Use deterministic provider fixtures for the hosted-compatible and local-provider paths.

## User Stories
- As a developer, I can check that the implementer called its intended model.
- As a developer, I can spot an agent that used an unexpected model and inspect the calls.
- As a developer, I can compare input/output tokens across the models used in a task.
- As a local-model user, I can see cache hits and misses when reported, with a clear indication when unavailable.

## Acceptance Criteria
- [ ] **AC-01**: A fixture where one prompt causes three completed model steps reports three calls. Replaying every update twice and restarting the collector leaves the total at three. A corrected token record changes the total once. An unfinished generation adds no completed call. Covers FR-01, FR-04.
- [ ] **AC-02**: A primary agent using a UI-selected model different from its route is attributed to the actual model. A worker using another model appears separately; a record without an agent appears under unknown. Covers FR-01.
- [ ] **AC-03**: A fixture with raw input=120 (20 cached) and output=30 (8 reasoning), normalized to input=100/output=22/cache=20/reasoning=8, displays input=120, output=30, hit=20, miss=100 without double-counting. Unknown cache semantics display unavailable. A zero coerced by the runtime is labeled as reported usage with the upstream limitation, not confirmed zero. Covers FR-02.
- **AC-04**: Removed during the 2026-09-07 scope revision; the id is retired so numbering stays stable. See the scope update in [Model Router Usage Source Compatibility](../../Research/model-router-usage-source.md).
- [ ] **AC-05**: A parent, two workers, and a nested worker each contribute exactly once to a tree report. Direct-session filtering excludes children. Unknown ancestry is visibly incomplete, and unrelated projects sharing a volume remain excluded. Covers FR-05, FR-06.
- [ ] **AC-06**: Saved views remain available after application exit and launcher restart. Disabled collection adds no records; re-enabling resumes and marks the excluded interval. Ephemeral mode leaves no telemetry after container exit. Collection state and last-update display match these states. Covers FR-06, FR-09.
- [ ] **AC-07**: The view displays every FR-07 column, numeric token labels and comparison bars. Agent/model/session filters and individual call inspection work. One agent using two models produces two rows. Empty data is explained. Opening the view requires no model call, build, interactive coding session, or host engine socket mount. Covers FR-07.
- [ ] **AC-08**: Disk-full, unwritable storage, a stalled writer, and queue overflow do not alter successful model output. The queue stays within 1,000 records and shutdown flushing ends within two seconds. Reports expose loss after recovery unless reconciliation restored it. Covers FR-10, NFR-02, NFR-03.
- [ ] **AC-09**: Secret and content canaries placed in prompts, headers, environment variables, model responses, tool output, and session titles appear in neither telemetry nor diagnostics. Database permissions prohibit world reads. Covers NFR-01, NFR-03.
- [ ] **AC-10**: Integration evidence verifies stable completed-call identity and actual model/agent attribution, including parent/worker links, on both provider paths. Unsupported auxiliary and cache data and upstream token limitations are labeled. Release remains blocked if primary or worker agent/model attribution is wrong. Covers FR-01, FR-02, FR-04, FR-05, FR-10, NFR-04.

- [ ] **AC-11**: Repository review finds every SQLite access path in maintained application modules, with no direct database instructions in skills or agent prompts. Public commands accept no raw SQL. The visual report works without an LLM; an agent-assisted report invokes only documented commands. Covers FR-11.

- [ ] **AC-12**: A worker whose expected and actual models match shows match; a different actual model shows unexpected. An unpinned primary using its selected UI model shows match when that expectation is known. Missing historical expectation shows unverified. Changing routing later does not relabel historical calls. Covers FR-12.

## Constraints
Compatibility is a release gate, not an assumption. The design must demonstrate a supported read or hook path on the pinned runtime before implementation is approved. Do not substitute session counts for model calls. Runtime-reported token limitations are acceptable when visibly labeled under FR-02; accurate agent/model attribution remains mandatory.

External-contract baseline, inspected 2026-09-07:

| Source | Pin and permitted conclusion |
| --- | --- |
| [Containerfile](../../../Containerfile) | Repository baseline `22270ba`; OpenCode2 `0.0.0-beta-19234`. This is the target runtime, not proof that every SDK type is emitted. |
| [Plugin dependencies](../../../plugins/model-router/package-lock.json) | `@opencode-ai/plugin` and `@opencode-ai/sdk` 1.18.25. Installed `dist/v2/promise/context.d.ts` exposes agent and AI SDK hooks, but no promise session/event domain. |
| [Plugin package](https://www.npmjs.com/package/@opencode-ai/plugin/v/1.18.25) | Installed `dist/v2/promise/aisdk.d.ts` exposes a language-model replacement hook. This is a candidate collection mechanism; runtime coverage and session correlation remain unverified. |
| [SDK package](https://www.npmjs.com/package/@opencode-ai/sdk/v/1.18.25) | Installed `dist/v2/gen/types.gen.d.ts` declares v2 `SessionMessageAssistant` (optional tokens), `SessionNextStepStarted` / `SessionNextStepEnded` (step identity and usage), and `SessionV2Info` (parent links). Legacy `AssistantMessage` / `StepFinishPart` types in the same file are not the v2 contract. Types establish candidate data shapes, not runtime completeness or token overlap semantics. |
| [Current v2 plugin documentation](https://opencode.ai/v2/docs/build/plugins/) | Consulted 2026-09-07; describes newer session, HTTP, retry, and event APIs. Unversioned reference only; not an approved contract for the pinned plugin. |

Before implementation, capture the verified per-step schema, identity rules, token semantics and coverage as a versioned design dependency. Do not derive these details from newer documentation or model memory.

The code-only SQLite boundary (FR-11) is an explicit user requirement. It applies equally to implementation and operating instructions; wrapping direct SQL in an LLM skill does not satisfy it.

Runtime research now verifies the message/session read path in serve mode and identifies lost token-usage presence. See [Model Router Usage Source Compatibility](../../Research/model-router-usage-source.md) for the executable evidence and remaining gates. These findings supersede the earlier unverified source-availability assumption, but do not approve implementation or a runtime upgrade.

## Dependencies
- Existing launcher project identity and data-volume behavior in [sandbox documentation](../../../docs/SANDBOX.md).
- Existing route assignment in [model-router code](../../../plugins/model-router/index.js).
- A verified runtime usage source meeting AC-10; direct mutation of OpenCode storage is prohibited.
- A SQLite runtime available in the shipped image and a reporting path usable without an active OpenCode session. Library choice belongs in the design.

The linked research supplies runtime compatibility evidence. Its original strict accounting interpretation is superseded by this routing-inspection scope.

## Open Questions
- Collector transport mechanics (serve-based launcher integration, in-process plugin hook, or another launcher change) are deferred to the design phase — **non-blocking** — the spec's requirements are transport-agnostic, the serve-based launcher integration is verified end-to-end as a fallback, and the Constraints already require the design to demonstrate a supported read or hook path on the pinned runtime before implementation approval.
- Terminal versus local browser presentation for the routing view — **non-blocking** — both surfaces must satisfy the same FR-07 and FR-12 columns, filters, and empty/unavailable states, so the choice changes presentation only, not the information reported or the checks performed.
