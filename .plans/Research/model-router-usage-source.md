---
title: Model Router Usage Source Compatibility
type: research
status: active
created: 2026-09-07
updated: 2026-09-08
tags: [model-router, usage, compatibility]
related: [Specs/ModelRouterUsage/README.md]
---

# Model Router Usage Source Compatibility

## Context
Investigate the blocking collection-source question in the [usage reporting spec](../Specs/ModelRouterUsage/README.md): can the pinned runtime expose reliable model-call usage with session and worker attribution?

**Result: yes, via a deliberate launcher integration.** The runtime exposes useful persisted call records through its HTTP API, but it loses the distinction between missing usage and zero usage. The original exact-accounting proposal could not be satisfied from those records alone; the current visual routing inspector accepts reported token values with this limitation clearly shown. The as-shipped `--standalone` launcher exposes no supported external collector transport, but a deliberate launcher integration — `opencode2 serve` on a fixed loopback port with a password, the interactive CLI connected via `--server` — was verified end-to-end in the launcher's data layout, including TUI connectivity, a non-interactive dispatch, a separate-process collector read, and restart recovery. No runtime upgrade or weakening of requirements is approved by this research.

Research performed 2026-09-07 using synthetic model responses; the standalone transport and lifecycle probe ran 2026-09-08. No paid model calls or direct SQLite reads/writes were performed. The probes talk only to public HTTP APIs; OpenCode itself manages its database.

Scope update: the user removed pricing entirely after the first research. Pricing observations below remain historical evidence only; they create no implementation requirements or approval blockers. The active spec covers calls and tokens.

Purpose clarification: the user wants to visually verify which agents called which models and compare tokens, with cache hit/miss where possible. The current spec therefore treats upstream token ambiguity as a display limitation, not a release blocker. The original observations below remain valid.

## Findings

### Key Insights
| Question | Observed result |
| --- | --- |
| Which model actually ran? | Assistant records contain the actual provider/model, even when the primary session model differs from the router profile. |
| Can calls be separated? | Foreground tool dispatch produced two parent assistant records and one worker record, each with a stable message ID. Background dispatch can produce another parent continuation when the worker finishes. |
| Can workers be attributed? | Child session metadata includes `parentID`, `projectID`, agent, and model. Foreground and background dispatch both worked. |
| Can records be recovered? | Messages remained identical after stopping and restarting the server with the same temporary data directory. One-record cursor pagination matched the full read for all four tested parent/worker sessions. |
| Does the SDK history endpoint work? | No. `GET /api/session/{id}/history?limit=100` returned HTTP 404 for all four sessions. An earlier limit=1000 attempt also failed. |
| Is missing usage distinguishable from zero? | No. Omitting usage and reporting explicit zero both produced identical all-zero token objects. |
| Is unpriced distinguishable from free? | Not from the cost field alone. A model with no configured prices produced `cost: 0` despite nonzero tokens. |
| Does this prove standalone collection? | No. The executable probe uses `opencode2 serve`. The pinned plugin context exposes no session/event/transport domain. |

All observations in this table come from the [probe evidence](evidence/model-router-usage/usage-probe.json) and [probe code](../../tests/usage-stats-probe.py), executed 2026-09-07. Nine assertion checks passed; they assert both available capabilities and verified limitations, not overall compliance with the feature spec.

**Token normalization matters.** Each normal synthetic response supplied 120 prompt tokens, 30 completion tokens, 20 cached prompt tokens, and 8 reasoning tokens. The OpenAI-compatible adapter returned input=100, output=22, reasoning=8, cache-read=20, cache-write=0. For this tested path, input/output are the non-cache/non-reasoning portions. Adding 120 to the cache count would double-count; subtracting reasoning again would under-count.

The DeepSeek adapter returned input=120, output=22, reasoning=8, cache-read=0 for the same payload. The mock supplied OpenAI-style cache detail fields; this result does not establish that real DeepSeek caching is unsupported. It demonstrates that provider normalization cannot be assumed interchangeable. Evidence: per-model records in the same probe output, 2026-09-07.

**Cost is a derived estimate.** With synthetic rates input=2, output=4, cache-read=1 per million, the compatible path produced 0.00034: `(100*2 + (22+8)*4 + 20*1)/1,000,000`. The DeepSeek path produced 0.00036. These are configured test rates, not market prices. The API emits a number without a currency field; source/configuration must supply the currency contract. An absent price becomes zero, so cost provenance must be collected separately. Evidence: probe configuration and records, 2026-09-07.

**Do not add session totals to message totals.** The captured parent session metadata exceeded the first two visible assistant messages; auxiliary title work and background continuation can make session snapshots differ from selected message usage. The probe does not establish complete title or compaction coverage. Use a single verified message-level accounting source and label auxiliary coverage unsupported until proven. Evidence: session versus message records in the probe, 2026-09-07.

**Standalone transport and lifecycle (2026-09-08).**

| Question | Observed result |
| --- | --- |
| What does as-shipped `opencode2 --standalone` spawn? | A child process `opencode2.exe serve --stdio --port 0`. The child additionally opens a loopback-only (127.0.0.1) TCP listener on an ephemeral random port; the port differs on every run (45425 and 42875 across the probe's two runs). |
| How is that private listener authenticated? | HTTP Basic, user `opencode`, with a 43-character password generated per launch. The password exists only in the child's `OPENCODE_PASSWORD` environment — not in the CLI parent's environment and nowhere on disk. No or wrong credentials give 401; the child-environment password gives 200. |
| Can a shipped client reach a running standalone instance? | No. `opencode2 api` (background-service default) fails and `opencode2 service status` reports `stopped`; `opencode2 api --standalone` spawns its own separate private server (its health-response PID differs from the running server's); `opencode2 pair` fails. No service metadata file is written. |
| Does the standalone server outlive the CLI? | No. Killing the CLI terminates the `serve --stdio` child. |
| Can a launcher integration provide a supported collector transport? | Yes, verified end-to-end. `opencode2 serve --hostname 127.0.0.1 --port <fixed>` with `OPENCODE_SERVER_PASSWORD` enforces auth (401 no credentials / 401 wrong / 200 correct). A TUI started with `opencode2 --server URL` stays connected with zero connection errors. A non-interactive `opencode2 run --server URL` completes a real mock dispatch (orchestrator -> extractor). A separate plain-HTTP process reads the same sessions with correct models, agents, `parentID`, `projectID`, and token categories. |
| Does the integration preserve launcher isolation and persistence? | The server binds 127.0.0.1 only inside the container. Runtime data (opencode.db, logs, snapshots, state) lands under the launcher's data-volume path (`/var/lib/opencode-data`). The server stays healthy after every client exits; restarting it on the same data directory returns identical message IDs and token values. |

All observations in this table come from the [standalone probe evidence](evidence/model-router-usage/usage-standalone-probe.json) and [standalone probe code](../../tests/usage-stats-standalone-probe.py), executed 2026-09-08. Twenty assertion checks passed; they assert both available capabilities and verified limitations. URL-embedded credentials in `--server` URLs were not supported by the shipped clients (the `OPENCODE_SERVER_PASSWORD` environment variable is the working client auth form).

### Sources
- Executable source: [tests/usage-stats-probe.py](../../tests/usage-stats-probe.py). Synthetic DeepSeek-adapter parent and OpenAI-compatible worker; foreground/background dispatch, explicit primary model selection, missing/zero usage, missing pricing, pagination, and server restart. Executed 2026-09-07.
- Captured metadata: [usage-probe.json](evidence/model-router-usage/usage-probe.json). Contains synthetic session/message usage and assertion results, not prompts or model response text.
- Executable source: [tests/usage-stats-standalone-probe.py](../../tests/usage-stats-standalone-probe.py). Phase A runs `opencode2 --standalone --auto` twice with the baked image config and attributes the loopback listener, auth behavior, and process lifecycles. Phase B runs the `serve` + `--server` integration at the launcher's data-volume path with mock loopback providers while a plain-HTTP collector reads the dispatch. Phase C verifies server survival after client exit and restart recovery. Executed 2026-09-08; all 20 assertion checks passed.
- Captured metadata: [usage-standalone-probe.json](evidence/model-router-usage/usage-standalone-probe.json). Contains process/listener/auth metadata and session/message usage records, not prompts or model response text.
- Tested image: `localhost/ark-services-opencode2-dev:latest`, immutable image ID `e288eb7e607cfa03fe4d6d02683fb085a5b5474041894e4cf6da1d7913bc3936`. Verified executable version `opencode2 v0.0.0-beta-19234`; installed plugin and SDK both `1.18.25`. This is an existing derived development image, not a fresh build of the current repository.
- [Repository Containerfile](../../Containerfile), baseline `22270ba`: same OpenCode2 version pin. [Lockfile](../../plugins/model-router/package-lock.json): package pins. Inspected 2026-09-07.
- Installed SDK `@opencode-ai/sdk@1.18.25`, `dist/v2/gen/types.gen.d.ts`: `SessionMessageAssistant`, `SessionV2Info`, `SessionMessagesResponse`, and `V2SessionMessagesData`. The runtime probe verifies the relevant message fields; declarations of history/events are insufficient evidence of endpoint availability.
- Installed plugin `@opencode-ai/plugin@1.18.25`, `dist/v2/promise/context.d.ts` and `aisdk.d.ts`: language model replacement is available in the declared API, but session correlation and a server endpoint are not exposed there. Inspected 2026-09-07.
- [Versioned v2 normalization source](https://github.com/anomalyco/opencode/blob/cb7d8b2f5e44876ef98b661dc10590c915af3a9f/packages/core/src/session/runner/publish-llm-event.ts#L17-L29), public v1.18.25 tag, inspected 2026-09-07: absent values normalize to zero. This source tag is corroborating evidence; it is not proven to be the source commit of beta-19234.
- [Current v2 plugin docs](https://opencode.ai/v2/docs/build/plugins/), consulted 2026-09-07: newer hooks and event APIs differ from this pin. They are not a compatibility guarantee.

Reproduction, from this repository on the tested host:

```sh
podman run --rm --pull=never --userns=keep-id --security-opt label=disable \
  -v "$PWD:/workspace:ro" \
  -v "$PWD/.plans/Research/evidence/model-router-usage:/evidence" \
  -w /workspace --entrypoint python3 \
  localhost/ark-services-opencode2-dev:latest \
  /workspace/tests/usage-stats-probe.py --output /evidence/usage-probe.json
```

Standalone probe reproduction, from this repository on the tested host (fresh named volume each run):

```sh
podman volume rm -f opencode2-data-standalone-probe
podman run --rm --pull=never --userns=keep-id --security-opt label=disable \
  -v "$PWD:/workspace:ro" \
  -v "$PWD/.plans/Research/evidence/model-router-usage:/evidence" \
  -v opencode2-data-standalone-probe:/var/lib/opencode-data:U \
  -w /workspace --entrypoint python3 \
  localhost/ark-services-opencode2-dev:latest \
  /workspace/tests/usage-stats-standalone-probe.py --output /evidence/usage-standalone-probe.json
```

The initial offline probe stalled before reaching the mock providers. The successful runs permitted networking for initialization and disabled unrelated MCP servers. Therefore offline initialization was not verified. All configured model endpoints remained loopback mocks with synthetic keys. The temporary container and server data are removed after the run.

## Analysis

### Implications
The basic accounting question is answered positively: this runtime can provide stable persisted assistant-step IDs, actual models, token numbers, and worker relationships through message/session reads. Join on project/session/message identity and store one record per completed assistant step. Filter out user and synthetic messages. Poll with pagination, revisit in-flight records, and upsert completed records to recover after a restart.

The strict completeness question is answered negatively for API records alone. Once missing and zero values collapse, no SQL query or later report can recover the lost distinction. The revised FR-02 accepts reported token totals with a visible upstream limitation. An independent usage-presence observer is therefore no longer required for this feature. Pricing has been removed from scope.

The 2026-09-08 standalone probe resolves that question in both directions. The as-shipped `--standalone` launcher gives a collector no supported endpoint: the private server's loopback listener is on an ephemeral port, and its per-launch password is visible only in the child process's environment, so reaching it would require undocumented `/proc` introspection that the runtime-pin constraint exists to avoid. The deliberate launcher integration is verified instead: `opencode2 serve` on a fixed loopback port with a launcher-managed password, the interactive CLI attached via `--server`, and a separate in-container collector process reading the message API. The server binds loopback only, all state stays in the project data volume, and per-project isolation is unchanged. A collector must not discover credentials by rummaging through host state or access SQLite through a skill.

Two candidate collector topologies remain for the design to choose. (1) **Launcher integration:** a small maintained wrapper in the container starts `opencode2 serve` on a fixed loopback port with a per-project password, starts the interactive TUI with `--server`, and runs an in-container collector process that polls the message API and upserts a separate usage database in the data volume; every building block is verified by the standalone probe. (2) **In-process plugin hook:** the model-router plugin already runs in-process in both launch modes, and the pinned plugin SDK declares a language-model replacement hook; a hook-based collector needs no transport at all, but session correlation at that hook is unverified on this pin. The spec's constraint ("a supported read or hook path on the pinned runtime") accepts either, provided the design demonstrates the chosen path.

### Recommendations
1. Keep the message/session API as the preferred reconciliation source; do not implement against the unavailable history endpoint.
2. Use reported token counts for the visual inspector, with a clear upstream-zero limitation and unavailable cache fields where necessary. Do not block routing visibility on exact recovery of usage presence.
3. Adopt the verified launcher integration as the default collector transport: the launcher starts `opencode2 serve` bound to loopback on a fixed port, sets a per-project `OPENCODE_SERVER_PASSWORD`, runs the interactive CLI with `--server`, and runs the collector in the container. The as-shipped `--standalone` private listener (ephemeral port, child-environment password) works today only via undocumented `/proc` discovery and must not be relied on. Keep the in-process plugin hook as the alternative, conditioned on verifying session correlation.
4. Keep all database access in maintained code behind validated CLI/API operations, as FR-11 requires. No SQLite access is needed to reproduce these findings.
5. Present agent, expected model, actual model, and route status together. Capture call-time expectations when available; otherwise display unverified. Historical routes must not be judged against later configuration.

## Open Questions
- **Resolved — basic source availability:** Pinned-runtime message APIs support the tested per-call identity, model, worker attribution, and restart/pagination cases. The SDK's history API does not exist on the tested runtime.
- **Non-blocking — missing token values:** Runtime ambiguity is verified. The revised visual-inspection scope permits reported counts with explicit limitations, so no raw-usage observer is required for release.
- **Resolved — standalone integration:** As-shipped `--standalone` exposes no supported external collector path: its private server is stdio-attached, adds a loopback-only ephemeral-port listener, and holds its per-launch password only in the child process's environment. A deliberate launcher integration is verified end-to-end instead: `opencode2 serve` on a fixed loopback port with `OPENCODE_SERVER_PASSWORD`, the interactive CLI attached via `--server`, a separate-process collector reading the message API, and restart recovery in the launcher's data layout. The concrete topology (in-container serve + collector process, versus an in-process plugin hook) is a design decision; the spec's constraint still requires the design to demonstrate the chosen path on the pinned runtime before implementation approval. See the [standalone probe](evidence/model-router-usage/usage-standalone-probe.json).
- **Non-blocking — auxiliary coverage:** Title and compaction accounting remain unverified. The first release may label them unsupported, as FR-10 already permits.
