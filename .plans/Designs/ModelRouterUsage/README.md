---
title: Model Router Usage Reporting
type: design
status: approved
created: 2026-09-08
updated: 2026-09-09
tags: [model-router, usage, observability]
related: [Specs/ModelRouterUsage/README.md, Research/model-router-usage-source.md]
supersedes: ""
superseded_by: ""
implemented_in: ""
waivers: []
---

# Model Router Usage Reporting

## Overview
Design for the visual routing inspector specified in the [spec](../../Specs/ModelRouterUsage/README.md). The pinned runtime (OpenCode2 `0.0.0-beta-19234`, plugin/SDK `1.18.25`) persists complete per-step usage records and exposes them through its HTTP message/session API, but the as-shipped `--standalone` launch mode keeps that API private: an ephemeral loopback port whose per-launch password exists only in the server child's environment, with no supported discovery.

This design adopts the launcher-integrated transport verified in [research](../../Research/model-router-usage-source.md): the launcher's default command becomes a small in-container supervisor that starts `opencode2 serve` on a dynamically allocated loopback port with a per-launch password, attaches the TUI via `--server`, and runs a separate in-container collector process. The collector polls the message/session API and upserts whitelist-projected metadata into a separate SQLite database (`usage/usage.db`) inside the existing project data volume. A host-side `opencode-container stats` subcommand opens that database read-only and renders the terminal routing view — no model call, build, interactive session, or engine socket mount.

All new code is maintained Python 3 standard-library code (the base image already ships `python3` + `sqlite3`; verified on `localhost/opencode2:latest`, Python 3.14.7, SQLite 3.51.2). All SQLite access lives in one maintained module plus the launcher's stats path (FR-11, D-0001). Collection is out-of-process; bounded polling, storage isolation and failure tests enforce NFR-02. Process separation alone does not prove absence of resource contention.

Route verification is provenance-gated (DD-7): calls whose expected model carries verified config-version provenance report `match` or `unexpected`; calls without a verifiable call-time expectation — including the default agent's free model selection — report `unverified`, never a guess.

The spec's Constraints require the design to demonstrate a supported read or hook path on the pinned runtime before implementation is approved. That demonstration exists: the [standalone probe](../../../tests/usage-stats-standalone-probe.py) verified auth, TUI connectivity, a real mock dispatch, a separate-process collector read, and restart recovery on the pin (20/20 assertions, [evidence](../../Research/evidence/model-router-usage/usage-standalone-probe.json)). The Testing Strategy below extends that demonstration into release gates.

## Non-Goals
- No browser or local-web UI surface; the report is terminal-rendered (spec open question resolved by DD-8).
- No network endpoint for reports: no web server, no exposed port; the host reads the volume file directly.
- No changes to model-router routing behavior, the pinned runtime version, or the plugin/SDK API surface.
- No reading or writing of OpenCode's own database (`opencode.db`) or any other runtime storage; the only runtime touch is the documented HTTP API (DD-1, DD-5).
- No cross-project aggregation, hosted telemetry, CSV export, or pricing (spec Non-Goals).
- Custom launch commands run unchanged. The launcher records a project-wide exclusion transition before launching an override, so later reconciliation cannot silently import that interval; returning to the managed enabled command explicitly resumes collection (DD-10).
- Session-level (ModelRouterUsage:FR-03) and per-message (ModelRouterUsage:FR-08) model overrides: retired in the 2026-09-07 scope revision, out of scope here.

## Architecture

### Components
| Component | Repo location | Process / location | Responsibility |
| --- | --- | --- | --- |
| Launcher | `bin/opencode-container` | host (baked copy in image) | Existing run path, plus new `stats` subcommand, `usage_stats` config, and injection of transport parameters (port, per-launch password, project key, enable flag). |
| Supervisor | `bin/oc2-standalone` (new) | container, main command (baked to `/opt/opencode/sandbox/oc2-standalone`) | Starts `opencode2 serve` on an allocated loopback port, waits for health, holds its registered run identity, starts the collector when enabled, runs the TUI with `--server`, ordered teardown on exit. |
| Collector | `bin/oc2-usage collect` (new) | container, child process (baked to `/opt/opencode/sandbox/oc2-usage`) | Polls the message/session API, upserts projected records into `usage/usage.db`, records gaps, bounded queue, ≤2 s graceful flush. |
| Reporter | `bin/oc2-usage report` (same module) | host regardless of `containers` configuration | Read-only render of the routing view from `usage.db`. |
| Runtime server | `opencode2 serve` | container, child process | Existing binary; loopback bind, `OPENCODE_SERVER_PASSWORD` auth. Unchanged. |
| Usage store | `<data volume>/usage/usage.db` | file | Separate SQLite database, WAL mode, file 0600 / directory 0700, versioned schema. Not OpenCode's database (FR-06). |

```mermaid
flowchart LR
  subgraph host
    LC["bin/opencode-container (host launcher)"]
    ST["stats subcommand"]
  end
  subgraph container
    WR["oc2-standalone supervisor"]
    SV["opencode2 serve<br/>127.0.0.1:PORT, Basic auth"]
    TUI["opencode2 TUI (--server)"]
    COL["oc2-usage collect (python3, stdlib)"]
  end
  VOL[("project data volume<br/>opencode.db — runtime-owned<br/>usage/usage.db — collector-owned")]
  LC -->|podman run, wrapper as default command,<br/>-e port / password / project key / enabled| WR
  WR -->|spawn; OPENCODE_SERVER_PASSWORD| SV
  WR -->|spawn; --server URL; OPENCODE_SERVER_PASSWORD| TUI
  WR -->|spawn when enabled| COL
  TUI -->|HTTP Basic| SV
  COL -->|poll /api/session, /api/session/{id}/message| SV
  SV -->|persists runtime state| VOL
  COL -->|whitelist projection, WAL upserts| VOL
  ST -->|podman volume inspect → read-only open| VOL
```

### Data Flow
1. Before container launch, the launcher resolves the existing CWD-derived project key and configured `persistence.data_volume` (including an empty string for ephemeral usage). It generates a run UUID and password. Control registration happens before any runtime starts, including disabled and custom-command launches (DD-10).
2. In enabled mode the supervisor selects a candidate loopback port in the actual container network namespace, starts `opencode2 serve --hostname 127.0.0.1 --port <candidate>` with `OPENCODE_SERVER_PASSWORD`, and verifies both its own child's survival and authenticated `/api/health`. Candidate selection is not a reservation: bind/auth races terminate only this child and retry a fresh candidate within a 30 s overall deadline. It never attaches to an existing server. It then starts the collector and `opencode2 --server <authenticated endpoint>` with passthrough TUI arguments. Disabled mode runs the original standalone command; custom overrides are passed unchanged.
3. Each collector polls session roots by directory and walks descendants with **unscoped** `parentID` queries, then pages messages. Completed assistant records are candidates; in-flight records are revisited. Every candidate passes the same project identity, collection epoch and exclusion check before commit, including startup rescans, correction replay and gap recovery. The bounded queue holds at most 1,000 records. Pagination resumes across cycles under a request/time budget; completed sessions receive periodic reconciliation so later corrections are discoverable. Never retain full history in memory. Each collector also snapshots the model-router config sources it shares with the plugin at run start and evaluates the per-run provenance condition (DD-7) at import time.
4. On TUI exit the supervisor terminates the collector with a two-second total flush deadline, then stops its server (five seconds, then kill). Only that run is marked clean after successful flush. A missed flush remains recoverable from the runtime API. INT/TERM forwards to the TUI first. Run liveness uses per-run advisory lock ownership, not a global shutdown bit or container-local PID.
5. Host `stats` runs before container-run assembly, regardless of the launch mode setting. Resolve the configured volume through the local engine and open its database read-only. No container or socket mount is created. The explicit in-container `oc2-usage report --db /var/lib/opencode-data/usage/usage.db --project-key KEY` uses the already mounted path and original injected project key, never the engine's host Mountpoint.

```mermaid
sequenceDiagram
  participant L as Launcher / supervisor
  participant C as Collector
  participant S as Runtime HTTP API
  participant G as Project control journal
  participant D as usage.db
  L->>G: locked durable mode transition + run registration
  L->>S: start owned authenticated server
  loop bounded polling and recovery
    C->>S: page sessions and assistant steps
    C->>G: lock; read current epoch and exclusions
    C->>D: upsert eligible records + this run's health
    C->>G: unlock
  end
  L->>C: shutdown within two seconds
  Note over C,D: clean marker belongs only to this run; replay restores missed records
```

### Interfaces
**Runtime HTTP contract (pinned, verified on the pin).** Auth is HTTP Basic `opencode:<password>`, supplied to clients via the `OPENCODE_SERVER_PASSWORD` environment variable — the verified client auth form; URL-embedded credentials are not supported by the shipped clients.

- `GET /api/health` — liveness used by the supervisor.
- `GET /api/session?directory=<workdir>&limit=<n>[&cursor=]` — session list; records carry `id`, `agent`, `model {providerID, id, variant} | null`, `parentID | null`, `projectID`.
- `GET /api/session?parentID=<id>&limit=<n>[&cursor=]` — child discovery.
- `GET /api/session/{id}/message?limit=100&order=asc[&cursor=]` — message records; completed assistant steps carry `{id, type: "assistant", agent, model {providerID, id, variant} | null, tokens {input, output, reasoning, cache {read, write}} | null, finish, error, time}`.
- `GET /api/session/{id}/history` — **returns 404 on this pin; not used** (verified).

Versioned design dependency required by the spec's Constraints — the verified per-step schema, identity rules, and token semantics, from the executable probes (2026-09-07/08):

- **Identity:** message `id` and session `id` are stable across server restart on the same data directory (verified identical after restart); the accounting key is `(session_id, message_id)`. Sessions link via `parentID`; the join path is project → session tree → steps.
- **Token semantics:** reported categories are `input` (uncached input), `output` (visible output), `reasoning`, `cache.read`, `cache.write`. For the OpenAI-compatible adapter path, a provider response of 120 prompt tokens (20 cached) + 30 completion (8 reasoning) is stored as `input=100, output=22, reasoning=8, cache.read=20, cache.write=0` — so display input = `input + cache.read + cache.write` and display output = `output + reasoning`, each category counted once (FR-02, AC-03). Provider normalization is not interchangeable across adapter families (the DeepSeek-adapter path reports differently for the same payload); the report shows cache hit/miss only where category semantics are verified for the path and "unavailable" otherwise. The runtime collapses missing usage to zero (verified), so the view always carries a limitation footnote: reported zeroes are reported values, not confirmed measurements.
- **Coverage:** primary-agent and delegated-worker attribution verified; title and compaction steps not verified and labeled unsupported (FR-10).
- Evidence: [usage-probe.json](../../Research/evidence/model-router-usage/usage-probe.json), [usage-standalone-probe.json](../../Research/evidence/model-router-usage/usage-standalone-probe.json); probes: [tests/usage-stats-probe.py](../../../tests/usage-stats-probe.py), [tests/usage-stats-standalone-probe.py](../../../tests/usage-stats-standalone-probe.py).

**Expected-model provenance (DD-7, pinned to the plugin source).** The effective route for a call is the model-router plugin's config resolution read from the plugin's own sources: the baked base defaults in the pinned image (immutable at runtime), plus the override files named by `OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG` and `OPENCODE_MODEL_ROUTER_CONFIG` (launcher read-only mounts, which the collector reads the same way the plugin does). Merge semantics mirror the plugin's `mergeConfig` and are cross-checked in tests against the plugin's exported functions. A call's expected model is verifiable only when its agent is mapped in the merged `agents` map, is not the merged `default_agent` while `pin_default_agent_model` is false, and at import time every existing override file's mtime is at or before the start of the run that produced the call. The condition is evaluated at import time, fail-closed: an unreadable or invalid file, an mtime violation, an unmapped or unpinned agent, or a call not attributable to a recorded run yields no expectation and `unverified`. The default agent's free model selection has no verifiable call-time source on this pin and remains `unverified` in v1. End-to-end provenance behavior is a release gate in the Testing Strategy, not assumed from this contract alone.

**Usage store schema (SQLite, WAL, bounded lock waits).** Normal write waits are capped at 250 ms before deferring; shutdown uses the remaining portion of the two-second deadline and never starts a wait exceeding it. `schema_version` in `meta`; upgrades are forward-only migrations that preserve data or refuse with a clear report error — never a reset (NFR-03).

```sql
meta(key TEXT PRIMARY KEY, value TEXT);            -- schema_version only; health is per project/run
sessions(
  session_id TEXT PRIMARY KEY,
  project_key TEXT NOT NULL,                       -- launcher project key (16-hex), not a path
  project_id TEXT,                                 -- runtime projectID from session metadata
  parent_session_id TEXT,
  agent TEXT,
  observed_model TEXT,                             -- current session model; NOT historical expectation
  first_seen INTEGER, last_seen INTEGER
);
calls(
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  project_key TEXT NOT NULL,
  agent TEXT,                                      -- NULL → reported as "unknown"
  model_provider TEXT, model_id TEXT, model_variant TEXT,   -- actual model
  expected_model TEXT,                             -- NULL unless call-time provenance is verified
  expectation_source TEXT, expectation_version TEXT,  -- DD-7 provenance identity: merged-config hash plus file mtimes at import
  route_status TEXT,                               -- match | unexpected | unverified; DD-7
  input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  finished_at INTEGER, first_seen INTEGER, last_updated INTEGER,
  PRIMARY KEY (session_id, message_id)
);
poll_progress(project_key TEXT, run_id TEXT, session_id TEXT, phase TEXT,
     last_message_id TEXT, last_reconciled_at INTEGER,
     PRIMARY KEY (project_key, run_id, session_id, phase));
-- Persist source identifiers only, never opaque cursors containing source content.
-- If the API cannot resume from that identifier, restart pagination for that session.
runs(project_key TEXT, run_id TEXT, started_at INTEGER, last_collection_at INTEGER,
     ended_at INTEGER, clean_shutdown INTEGER,
     PRIMARY KEY (project_key, run_id));
gaps(project_key TEXT, gap_id TEXT, run_id TEXT, kind TEXT,
     started_at INTEGER, ended_at INTEGER, recovery_status TEXT, note TEXT,
     PRIMARY KEY (project_key, gap_id));           -- pending | restored | residual
-- Disabled intervals and mode epochs are authoritative in the project JSON journal;
-- SQLite gaps are idempotent projections, not a second control authority.
CREATE INDEX calls_session ON calls(session_id);
CREATE INDEX calls_report ON calls(project_key, agent, model_id);
CREATE INDEX calls_time ON calls(finished_at);
```

Whitelist projection (NFR-01): only the columns above and the IDs, mode flags, epochs and timestamps defined in DD-10 are ever written. Prompt/response text, headers, environment values, tool content, session titles, and filesystem paths are never stored; the session record's title and directory fields are dropped by the projection regardless of what the API returns.

**Launcher environment contract (host → container).** Injected directly by the launcher into `podman run` (not user-settable via `env.set`; names avoid the reserved `OPENCODE_*` prefix):

| Variable | Value |
| --- | --- |
| `OC2_SERVER_PORT` | Optional preferred port from `usage_stats.port`; absent means automatic selection. Collision retries always choose another candidate. |
| `OC2_RUN_ID` | Unique UUID for this launch; never reused across runs. |
| `OC2_SERVER_PASSWORD` | Per-launch, generated by the launcher (`secrets.token_urlsafe(32)`, 43 chars — the runtime's own format). Never written to disk, never logged. |
| `OC2_PROJECT_KEY` | The launcher's existing 16-hex project key. |
| `OC2_USAGE_STATS_ENABLED` | `0` or `1`, from `usage_stats.enabled`. |

**Configuration (`.opencode-sandbox.json`).** New optional block, validated in the launcher like its siblings (type-checked, `die` on misuse):

```json
{ "usage_stats": { "enabled": true, "port": 18080 } }
```

Both keys are optional. `enabled` defaults to true; omitted `port` selects automatically. A supplied port must be an integer in 1–65535 (booleans rejected) and is a preference, not a guarantee. Collection mode is project-wide with ordered launch transitions (DD-10).

**CLI and view contract.**

```sh
opencode-container stats
opencode-container stats --session SESSION_ID --include-children
opencode-container stats --agent implementer
opencode-container stats --model local/my-model
opencode-container stats --session SESSION_ID --calls
opencode-container stats --session SESSION_ID --call MESSAGE_ID
```

Filters combine with AND; `--include-children` requires `--session`. The reporter (same flags plus validated `--db <path>` and required `--project-key KEY`) renders plain text — meaning never depends on color:

- Main table, one row per (agent, actual model): `Agent | Expected | Actual | Route | Calls | Input | Output | Cache`. `Route` shows counts for all three statuses: `match N / unexpected N / unverified N`; the counts sum to Calls. `Expected` shows one identity only when every contributing call has the same verified expectation; otherwise it shows `mixed (N known models; U unknown calls)`. Missing values remain unknown. Grouping uses provider and model ID, with variants available in call detail. `Cache` is `hit/miss` when category semantics are verified for the path, else `unavailable`. Token cells are "reported tokens" with compact bars (block characters scaled to the maximum) plus visible numbers.
- A per-model summary section (totals across agents) that does not replace the agent view.
- A collection-health line: last update, open gaps, and any excluded (disabled) intervals with their time ranges (FR-09).
- `--calls` lists matching calls in stable `(finished_at, session_id, message_id)` order, with session/message IDs, timestamp, agent, expected and actual identities, status and reported categories. Use `--limit` (default 100, maximum 1,000) and validated opaque `--cursor` for bounded pagination; filters apply before paging. `--call MESSAGE_ID` requires `--session`, excludes `--calls` and `--include-children`, and renders one record including expectation provenance and missing-field explanations. IDs are validated against the pinned identifier grammar; malformed IDs are usage errors, an absent call is an explicit not-found result. No raw SQL is accepted (FR-07, FR-11, AC-07).
- The FR-02 zero-collapsing footnote appears in aggregate, list and detail output. Aggregate route counts preserve mixed outcomes; detail retains each call's single match/unexpected/unverified status.
- Explicit empty state ("no usage recorded for this project — no sessions collected yet" / "collection disabled" / "ephemeral mode: no persisted usage") and explicit incomplete-coverage markers (FR-05, FR-07).

`oc2-usage collect` and `oc2-usage report` are the two maintained entry points behind all of the above; nothing outside `bin/oc2-usage` and the launcher's stats path touches SQLite (FR-11, AC-11).

## Design Decisions
- **DD-1: Collector transport — launcher-integrated `opencode2 serve` plus a separate in-container collector.**
  Context: the spec's Constraints require a demonstrated supported read or hook path on the pinned runtime. Options: (a) reach the as-shipped `--standalone` private server; (b) launcher-integrated `serve` + `--server` client + separate collector; (c) in-process plugin hook (the pinned plugin SDK's language-model replacement); (d) read `opencode.db` directly.
  Decision: (b). Rationale: (a) is verified impossible by supported means — ephemeral loopback port, per-launch password only in the server child's environment, no discovery mechanism, well-known ports closed; using it would mean undocumented `/proc` introspection that the runtime-pin constraint exists to avoid. (c) has unverified session correlation on this pin, and its failure surface is inside the generation process, the weakest form of NFR-02. (d) runs into the FR-11 boundary, which explicitly extends to "any proposed read access to OpenCode storage", and would couple us to the runtime's private schema. Every building block of (b) is verified end-to-end on the pin: auth enforcement, TUI connectivity with zero connection errors, a real mock dispatch with parent/worker attribution, a separate plain-HTTP process reading correct records, and restart recovery with identical records.

- **DD-2: Per-launch password and collision-aware endpoint allocation.**
  Context: the launcher already supports host/shared network namespaces, so a fixed loopback port is not isolated for every configuration. Options: a universal fixed port; runtime port-zero discovery; supervisor-selected candidates with bounded launch retries.
  Decision: select candidates using a temporary loopback bind in the target namespace, close that socket, and start the owned server on the selected port. The intervening race is expected: retry fresh candidates after bind failure or failure to authenticate the owned child, within the startup deadline. Never kill a foreign listener. A configured port is only the first candidate. Use a generated per-launch password in client/server environments, never on disk or in diagnostics. Port-zero discovery is not assumed supported by the pin. Rationale: this uses the already verified explicit-port serve contract and preserves concurrent host-network launches. Allocation/retry behavior must be tested on the pin before release; loopback in host mode is host-visible and is protected by authentication, not by a separate namespace.

- **DD-3: The supervisor is the container's main command.**
  Context: someone must own the lifecycles of serve, collector, and TUI. Options: (a) an in-container supervisor script that is the podman command; (b) host-side orchestration with repeated `podman exec` from the launcher.
  Decision: (a) — `bin/oc2-standalone`, a Python stdlib script baked into the image, becomes both the launcher's `DEFAULT_COMMAND` and the image `CMD` (which passes `--auto` through to the TUI, preserving the current image behavior). Rationale: (b) has no clean signal path to child processes and would turn the launcher into a long-running poller after `os.execv(podman, …)`; (a) keeps teardown atomic and ordered (TUI exit → collector SIGTERM + ≤2 s flush → serve SIGTERM → exit with TUI code), which is exactly NFR-03's graceful-shutdown contract, and the launcher itself stays a thin `podman run` wrapper.

- **DD-4: Bounded polling with exclusion-aware reconciliation.**
  Context: message/session polling is verified; the candidate event path is not established by the research. Options: polling, an unverified event subscription, or an in-process hook.
  Decision: poll with bounded pages and queue memory, revisit in-flight steps and periodically rescan completed history for corrections. Startup and all failure recovery use the same idempotent import path and durable exclusion predicates (DD-10). Budget each cycle (at most 20 requests or one second, then yield before the next cycle); persist progress and resume fairly across sessions, with startup recovery explicitly shown as in progress. Rationale: total history can grow without turning a two-second poll into an unbounded full scan. Successful replay closes restored gaps; only remaining loss is reported.

- **DD-5: Separate SQLite store inside the existing data volume; WAL; versioned schema with safe refusal.**
  Context: FR-06 requires a separate database in the project volume, concurrent-writer support, and no writes into OpenCode's database; NFR-03 requires non-world-readable files and migration that preserves or refuses, never resets. Options: (a) SQLite at `<volume>/usage/usage.db`; (b) append-only JSONL; (c) a table inside `opencode.db`.
  Decision: (a), WAL mode, directory 0700 / file 0600, `schema_version` in `meta` with forward-only migrations. Rationale: (b) has no concurrent-writer story and no cheap query path for filters and tree totals; (c) is prohibited. WAL gives the required concurrent reader/writer behavior — the host `stats` can read while the in-container collector writes. Ephemeral mode (`persistence.data_volume: ""`) reuses the same code path with `$XDG_DATA_HOME` in the container home, so usage is collected but discarded with `--rm` — "usage is ephemeral" (FR-06) — and `stats` reports that state explicitly.

- **DD-6: Accounting level — one completed assistant step; upsert keyed by `(session_id, message_id)`.**
  Context: FR-04 requires exactly-once totals with one authoritative accounting level, and FR-01 defines a completed call as one completed model-generation step. The probes show one prompt producing multiple assistant records (one per step), stable message ids across restart, and session-level snapshots that diverge from message sums.
  Decision: a completed call is a `type == "assistant"` record with `finish != null`; user, `agent-switched`, and other synthetic records are ignored (FR-10); session-level totals are never added to message totals (verified divergence, research). Corrections re-upsert the same key and change totals once; interrupted generations (`finish == null` at collection horizon) add no completed call and are marked as coverage notes (AC-01, FR-04).

- **DD-7: Route status requires verified config-version provenance; v1 covers pinned routes (D-0002).**
  Context: FR-12 requires comparison against the effective expected model at call time and forbids judging old calls against later configuration. The runtime persists no per-call expectation, and the session model field mutates with the UI selection, so it cannot prove a historical expectation (F-01). The model-router plugin, however, resolves routes from a pinned, inspectable config chain once per runtime process, which is a verifiable source for pinned routes.
  Decision: the expected model for a call is the route resolved for the call's agent from the plugin's own sources — the baked base defaults (immutable in the pinned image) plus the override files named by `OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG` and `OPENCODE_MODEL_ROUTER_CONFIG`, merged with the plugin's exact `mergeConfig` semantics (cross-checked in tests against the plugin's exported functions). A call carries a verified expectation only when (a) its agent is mapped in the merged `agents` map, (b) it is not the merged `default_agent` while `pin_default_agent_model` is false, and (c) at import time every existing override file's mtime is at or before the start of the run that produced the call, attributed by `finished_at` against the run windows in `runs` (the current run's start is known to its collector). Then the call stores the resolved model plus `expectation_source`/`expectation_version` (merged-config hash, file mtimes) and reports `match` or `unexpected` against the actual model. Any condition failing — unreadable or invalid file, mtime violation, unmapped or unpinned agent, call not attributable to a recorded run — stores no expectation and reports `unverified`. Fail-closed throughout; never guessed; never relabeled from later configuration. In v1 this yields match/unexpected for mapped non-default agents (the routed workers) and for the default agent when `pin_default_agent_model` is true; the default agent's free UI selection stays `unverified` because no pinned source timestamps a selection. Rejected: approximating from the current session model (F-01's race); treating today's configuration as historical (FR-12 forbids); inventing per-call timestamps the pinned runtime does not expose. A future source for free selections must pass a pinned temporal-provenance test before enabling live comparisons.
  Rationale: this is the strongest expectation evidence the pinned runtime and plugin actually expose, it is checkable per call, and every fallback degrades to the FR-12-compliant `unverified` instead of a fabricated comparison.

- **DD-8: Terminal rendering; no browser or web surface.**
  Context: the spec leaves the presentation surface open (open question, non-blocking) and requires bars, numbers, text labels, and explicit states.
  Decision: plain-text terminal output from the reporter. Rationale: zero new runtime dependencies (a browser surface would need a web server and a port, contradicting the no-exposure posture), it works wherever the launcher works, and every FR-07 column and state renders unambiguously in text. The choice changes presentation only, as the spec's open question anticipated.

- **DD-9: Disabling restores standalone execution while preserving control bookkeeping.**
  Context: FR-09 requires exclusion across later replay, so launching the old runtime without recording the transition is insufficient. Options: collector-only toggling or project control transitions with standalone fallback.
  Decision: a disabled launch records the project transition through maintained control code, then runs `opencode2 --standalone` with existing arguments and no serve/collector. A custom command is passed unchanged after the same exclusion registration. Bookkeeping is the intentional difference from previous launches. Existing collectors observe the shared project state before every commit; re-enabling is a new managed enabled launch. Runtime failures or telemetry failures never silently change the requested mode.

- **DD-10: Durable project controls and independent run lifecycle.**
  Context: FR-06 supports concurrent writers/shared volumes; FR-09 excludes intentionally uncollected intervals from all future imports. Options: last-run JSON, a global shutdown flag, or project-scoped control history plus per-run health.
  Decision: maintained `oc2-usage` code owns JSON control operations as well as SQLite operations. The launcher invokes its validated internal control entry point before any runtime starts; the supervisor registers run liveness. Each project has `usage/control/<project_key>/state.json` (0600, parent 0700), a stable advisory lock file, and per-run lock files. The JSON stores schema version, collection start, monotonically increasing epoch, effective enabled flag, durable half-open exclusion intervals `[start,end)`, and run IDs/start/end times. Under the project lock use temporary-file write, fsync and atomic replace, then directory fsync; never lock the replaceable JSON inode. No paths, passwords or message content enter this journal. Ephemeral mode (`persistence.data_volume: ""`) has no durable shared state to protect: the launcher skips host-side registration entirely and launches proceed unconditionally, while the in-container supervisor keeps a container-local control file for its own collector's import predicate (D-0003).
  **Mode semantics:** the latest successfully registered launch transition controls the whole project. A disabled or custom launch opens an exclusion; repeated disabled launches preserve its first start. A managed enabled launch closes it. Other live collectors honor the new state before committing; enabled launches may resume project collection while an older standalone process remains alive. Show effective mode and active runs so this is explicit. Different projects never share controls. Preserve collected rows on disable.
  **Import predicate:** first registration establishes collection start; earlier history is unsupported rather than silently imported. Every candidate must belong to this project, complete at/after collection start, and fall outside every exclusion, including open intervals. Ambiguous/missing completion times are omitted with coverage notes. Missing/corrupt control history fails closed for collection, never defaults to full import. No-store creation does not discard pre-existing controls. All rescans and corrections apply the same predicate. Hold the project control lock across predicate validation and the bounded SQLite commit so transitions cannot race queued writes; a changed epoch causes revalidation. Do not hold it across HTTP calls. Journals survive restarts and are mirrored idempotently into project-scoped gaps for reporting.
  **Runs and recovery:** each supervisor holds its own run lock throughout execution; release without a clean completion record identifies an unclean run on the next inspection. One run cannot mark another clean. Do not infer death from PIDs across namespaces or a late heartbeat. Control registration and liveness-lock acquisition are one serialized operation. Reconcile crashed runs from the runtime API, outside exclusions, and distinguish pending recovery, restored gaps and residual loss. Per-project last update is the maximum successful collection timestamp for its runs, with individual degraded runs still visible.
  **Failure boundary:** no host-only fallback authority is introduced: every live collector must see the same project transition before it takes effect. Registration has a one-second deadline. If an enabled launch cannot register, warn and allow coding without its collector; recovery may import its usage because collection was requested, subject to existing exclusions. If a disabled/custom launch cannot durably publish its transition under the shared project lock, return a clear configuration-control error before launching that run. Do not claim disabling succeeded while another collector can still admit its calls. Existing coding processes continue unaffected. This pre-launch failure of an explicit control change is distinct from SQLite or collector failure after successful registration, which must never block coding (NFR-02). Missing/corrupt controls disable all admission until repaired; repair preserves known exclusions and conservatively excludes any interval whose mode cannot be established.
  Rationale: WAL handles database concurrency, while explicit control history handles privacy and lifecycle semantics. The current single state-file design is replaced; these controls are internal application interfaces, never agent-directed database access (FR-11).

- **DD-11: Python 3 standard library for all new code; no new image packages.**
  Context: the collector runs in the base image; `stats` runs on hosts whose only documented Python dependency is 3.10+; the repo already pins `ruff` and `mypy` for Python. Options: (a) Python stdlib; (b) Node with `node:sqlite`; (c) Go.
  Decision: (a). Rationale: the base image was verified to ship `python3` (3.14.7) with a working `sqlite3` module (3.51.2), so no Containerfile package changes are needed; `http.client`, `sqlite3`, `secrets`, and `threading` cover the whole surface. (b) is experimental in Node 24 and would force a Node dependency on hosts; (c) adds a build stage to an image that deliberately ships no Go toolchain. The Containerfile changes are limited to `COPY` of the two new files and the `CMD` swap (DD-3).

- **DD-12: Offline reporting stays on the host.**
  Context: an engine socket exposes operations, not its host filesystem inside the calling container. Options: host-side read, explicit already-mounted in-container read, or a throwaway report container.
  Decision: `opencode-container stats` always takes a host-side branch before container assembly, even with `containers: true`. Resolve the configured volume name, honoring overrides and empty-string ephemeral mode, using the local engine; open its Mountpoint read-only without creating a container or mounting a socket. For the optional already-running in-container reporter, require the mounted `--db` path and original `--project-key`; never feed an engine Mountpoint into a container-local open. Inaccessible/remote mountpoints are explicit errors, not empty usage; absent volume/database is an empty state. Read project control history alongside projected gaps to show disabled state even before SQLite exists. Rationale: preserves the offline interface and its filesystem identity without new infrastructure.

## Error Handling
| Failure | Detection and behavior |
| --- | --- |
| Port collision / startup failure | Retry fresh candidates only for this owned server within 30 s. If startup cannot succeed, warn, record degraded transport coverage, and run standalone under the already registered enabled epoch; its missed usage remains eligible for recovery; do not attach to a foreign server. |
| Server dies mid-session | Restart once on the same port/password if available. A foreign bind is never terminated. Collector records pending recovery; TUI reconnect is a release test. Relaunch reconciles persisted eligible records. |
| Poll/auth failure | Bounded backoff, per-run degraded gap, then exclusion-aware reconciliation. Logs contain sanitized error codes, never URLs with credentials or bodies. |
| SQLite failure / stalled writer / overflow | Keep at most 1,000 records, defer or drop oldest with metadata-only warning. Bounded writes and control locks never touch the generation path. Persist/reconcile gaps when storage returns. |
| Shutdown exceeds two seconds | Stop flushing at the absolute deadline; leave this run unclean. Do not attempt an unbounded final gap write. Next startup detects run-lock release and reconciles from the source. |
| Unclean previous run | Reconcile missed eligible steps and show recovery pending until complete; restored is distinct from residual loss. Other live runs retain their own state. |
| Disabled / custom-command interval | Persist and enforce the project exclusion on every import path, including all future restarts; retain prior reports. |
| Control registration failure | Fail closed for telemetry and refuse ambiguous backfill. Enabled launches still run; a disabled/custom transition that cannot be made visible to every collector fails before starting that run (DD-10). |
| Unknown store/control version | Refuse telemetry/report safely with an upgrade message; never reset data or controls. Coding may continue under the registered mode and coverage rules. |
| Stats access failure | Distinguish absent data from permission errors, corrupt store, unsupported remote mountpoint or missing control authority. Read-only snapshots retry a lock once, bounded. |
| Shared volume | Filter calls, runs, gaps and control state by project; reject inconsistent session ownership rather than relabeling it during upsert. |
| Config provenance unavailable | Override file unreadable or invalid at import, file mtime newer than the producing run's start, agent unmapped or unpinned, or call not attributable to a recorded run | No expectation stored; route status unverified with the reason in call detail. Never inferred, never relabeled from later configuration (DD-7). |
| Missing attribution / expectation / usage | Unknown agent; unverified route without call-specific provenance; absent categories unavailable. Reported zeroes keep the upstream limitation. |

## Testing Strategy
Deterministic provider fixtures: the existing [mock provider](../../../tests/mock-model-router-provider.py) loopback servers (OpenAI-compatible on 18081, local-provider adapter on 18082) and the [dispatch driver](../../../tests/model-router-dispatch-driver.py) cover the two provider paths NFR-04 requires. No paid model calls anywhere in the test plan.

**Unit (host, Python stdlib test scripts in `tests/`, same style as `launcher-env.test.py`):**

- Normalization fixture (AC-03): raw `input=100, output=22, reasoning=8, cache.read=20, cache.write=0` renders input 120 / output 30 / hit 20 / miss 100 with no double-count; unverified cache semantics render `unavailable`; zero-forced values carry the limitation label.
- Exactly-once (AC-01): three-step fixture; replay every update twice; collector restart; a corrected token record changes the total exactly once; an in-flight (`finish == null`) record adds no completed call.
- Attribution (AC-02): UI-selected primary model, differently-routed worker on its own row, `agent == null` → `unknown`.
- Tree totals (AC-05): parent + two workers + nested worker counted exactly once in the tree report; direct-session filter excludes children; unknown ancestry marked incomplete; mixed-project shared volume excluded by `project_key`.
- Route status and provenance (AC-12): provenance-backed match/unexpected fixtures; current-session-only records are unverified. Merge cross-check: the Python merge must agree with the pinned plugin's exported `mergeConfig`, `DEFAULT_CONFIG`, and `modelRefFor` on a fixture matrix. Pin semantics: mapped non-default agents verified; default agent unverified while unpinned, verified when `pin_default_agent_model` is true; unmapped agents unverified. Temporal: an override file stable since the run start is proven; one edited after run start is unverified; pre-collection-start history is unverified. Switch A → B between call completion and first poll, import old calls, and correct actual metadata without using later expectations. Mixed aggregate status counts sum exactly to calls; detail preserves each comparison.
- Queue and shutdown (AC-08): queue caps at 1,000 with oldest-drop; flush completes within 2 s; storage-failure injection leaves generation unaffected.
- Migration: forward upgrade preserves rows; newer-schema store is refused with a clear message.
- Canaries (AC-09): canary strings in prompt text, response text, environment, headers, tool output, and session title appear in no stored row, no raw database bytes, and no collector log output; db file mode 0600 and directory 0700 asserted.

**Integration (podman, pinned dev image, extending the existing probe harness) — `tests/usage-stats-integration-probe.py`:**

- Run the *real* supervisor + collector in a container with the mock providers: drive a dispatch (primary + worker + nested), then assert `usage.db` contents: call counts, actual models, agent attribution, parent/worker links, token categories (AC-01, AC-02, AC-05, AC-10) on both provider paths.
- Lifecycle: TUI exit → store persisted; relaunch on the same volume → identical records plus new ones (AC-06, restart recovery).
- Controls: `usage_stats.enabled: false` run adds no call rows and uses standalone execution after control registration; re-enable inserts a `disabled` gap and imports nothing from the disabled interval (AC-06); ephemeral mode leaves no file after exit. Ephemeral custom-command launch proceeds without host-side registration (DD-10).
- Provenance end-to-end (release gate, F-01): (1) route resolution — worker calls report match with `expectation_source` set on both provider paths; (2) unexpected — a worker session created with a model different from its route reports unexpected; (3) temporal — with config unchanged across runs both runs' calls keep provenance; editing the sandbox override between runs gives new-run calls the new expectation while stored calls keep theirs (no relabeling, FR-12); editing mid-run degrades that run's later imports to unverified rather than a false mismatch.
- Faults: unwritable usage directory → dispatch still completes, `degraded` gap present, recovery reconciles (AC-08).
- Reporter offline: `stats` renders all FR-07 columns, filters, two-models-two-rows, and empty states with no container, no network, and no model call (AC-07).
- Evidence is written to `.plans/Research/evidence/model-router-usage/` in the established metadata-only format (no prompts or response text).

**Release gates (spec Constraints):** (1) the integration probe above re-verifies the supported read path and the end-to-end provenance behavior on the pin at implementation time; Per D-0005, the pin runs on the image this repository builds via make build, so the gate exercises the shipping form. (2) a manual TUI smoke in the dev image — interactive session launched exactly as the image `CMD` composes it (including `--auto` passthrough), with a real dispatch and a worker, verifying interactive fidelity of the `--server` mode the probe could only verify as liveness plus a non-interactive dispatch — recorded as evidence; (3) the AC-11 repository review: every SQLite access path lives in `bin/oc2-usage` and the launcher's stats path, no skill or agent prompt instructs direct database access, and the report runs headless.


**Review regression gates (F-01–F-08):**

- Run enabled → disabled → disabled → enabled → enabled; repeat with no initial database, a custom command, queued writes and corrected excluded records. No excluded call is ever inserted; repeated disabled starts preserve the first boundary (AC-06).
- Run two collectors concurrently, then cleanly exit one and crash the other after runtime persistence but before telemetry commit. Replay restores eligible calls exactly once. Shared-volume projects have independent modes, gaps and last-update times (AC-01, AC-05, AC-08).
- Inject disk-full during control replacement; verify failed disable is explicit and starts no new runtime, existing coding continues, and enabled startup failure still allows coding. Repair controls and prove replay excludes ambiguous intervals. Test mode changes against an in-flight commit and enforce bounded lock/shutdown deadlines. Control files join the privacy canary/permission checks (AC-06, AC-08, AC-09).
- Start two host-network launches concurrently with an occupied preferred port; each must authenticate only to its own server, complete a mock dispatch, and leave foreign listeners untouched. Repeat the bind-close race deliberately. Verify enabled standalone fallback remains recoverable on exhausted retries (NFR-02).
- Host stats with `containers: true`, custom/shared volume and no running container must work without a socket mount. Explicit in-container reporting uses the mounted data path and original project key. Inaccessible mountpoints must not render empty data (AC-07).
- Verify mixed aggregate outcomes, bounded call-list pagination, individual call lookup, malformed IDs, missing calls and combined filters. Calls without verified provenance (e.g. the default agent's free selection) show unverified with the provenance limitation rather than fabricated comparisons (AC-07, AC-12).

### Structural Verification
Per the project's Python tooling (pinned in `requirements.txt`, configured in `ruff.toml`):

- `ruff check` over `bin/` and `tests/` with the pinned `ruff==0.16.6`; the two new extension-less executables are added to `ruff.toml`'s `extend-include` alongside `bin/opencode-container`.
- `mypy` with the pinned `mypy==1.18.2` over the new modules and the changed launcher file; new code is fully type-annotated (matching the launcher's existing annotation style), and the gate is zero new findings on touched files.
- The new scripts are the repo's only additional Python; no other structural tooling is introduced.

## Migration / Rollout
1. **Image:** `COPY` the two new files into `/opt/opencode/sandbox/`; swap `CMD ["opencode2", "--standalone", "--auto"]` to `CMD ["/opt/opencode/sandbox/oc2-standalone", "--auto"]` (passthrough preserves the `--auto` behavior). No package changes (DD-11).
2. **Launcher:** `DEFAULT_COMMAND` becomes `("/opt/opencode/sandbox/oc2-standalone")`; new `stats` subcommand (host-side branch like `secrets`, before container-run assembly); new `usage_stats` config validation; transport env injection (DD-2). A user-set command executes unchanged after the exclusion control transition (DD-9, DD-10).
3. **Behavior change for all default launches:** the TUI now runs against `opencode2 serve` on loopback instead of `--standalone`. This is the user-visible part of the feature and is released with a note; the verified evidence and the TUI smoke gate it, and the existing `command` override is the escape hatch.
4. **Existing volumes:** create versioned control history at first registration and the SQLite store at first collection; earlier history is marked unsupported. Preserve any pre-existing controls even when no database exists. Store upgrades preserve rows and refuse unknown versions. OpenCode storage is untouched.
5. **Docs:** `docs/SANDBOX.md` gains the `usage_stats` keys, the `stats` subcommand and its filters, and the transport note; README quickstart mentions `opencode-container stats`.
6. **Rollback:** retain the control-aware launcher and register a disabled transition before restoring standalone defaults. Preserve controls and usage.db so later re-enable excludes rollback activity. Downgrading to a launcher without control registration invalidates continuity; a future collector must refuse historical replay until a conservative exclusion is established. Stats continues to read compatible prior records.
7. **Residual risks:** TUI interactive fidelity under `--server` (gated by the TUI smoke); TUI behavior if `serve` dies mid-session (unverified reconnect; worst case is a container relaunch, data safe per verified restart recovery); both are tracked in Open Questions.

## Open Questions
- Full interactive TUI fidelity under `--server` (keybinds, menus, and session UI beyond process liveness and the non-interactive dispatch the probe verified) — **non-blocking** — the probe already verified server connectivity, a completed mock dispatch through the `run` client, and restart recovery; the interactive smoke is a named release gate in the Testing Strategy, and a `command` override restores `--standalone` if the fidelity is unacceptable.
- Pinned TUI behavior when the `serve` process dies mid-session (automatic reconnect versus user relaunch) — **non-blocking** — worst case is relaunching the container, for which restart recovery is verified; the supervisor restarts `serve` once and the collector records a `degraded` gap, and the spec imposes no mid-session availability requirement.
- Call-time verification of the default agent's free (UI-selected) model — **non-blocking** — DD-7 reports these calls `unverified` rather than guessing, which FR-12 explicitly permits; enabling live comparison requires a session-level temporal signal (a selection-change timestamp or event) that this pin does not expose, and any such source must pass a pinned temporal-provenance test first.
