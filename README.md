# OpenCode2 Container

A Fedora 44 container that runs the **OpenCode2 0.0.0-beta-17823** preview CLI
with a native **v2 model router**, the **code-graph** and **debug** MCP servers, and the
**SDD planner** CLI baked in. Every Go/Rust/Cargo toolchain lives in throwaway
builder stages, so the runtime image ships no dev toolchain.

## What the image contains

| Path | Contents |
| --- | --- |
| `/opt/mcp/bin/` | `code-graph-mcp`, `debug-mcp`, `sdd` (all on `PATH`) |
| `/opt/opencode/plugins/code-graph/` | code-graph OpenCode plugin (skills + commands) |
| `/opt/opencode/plugins/debug/` | debug-mcp OpenCode skills |
| `/opt/opencode/plugins/sdd/` | SDD planner OpenCode skills + shared resources |
| `/opt/opencode/plugins/model-router/` | native v2 model-router plugin |
| `/opt/opencode/sandbox/` | host launcher and sandbox-config templates |
| `/opt/opencode/config/opencode/agent/*.md` | 10 baked native v2 agent definitions |
| `/opt/opencode/config/opencode/command/*.md` | baked code-graph slash commands |
| `/etc/opencode/container-config.json` | baked `OPENCODE_CONFIG` |
| `/etc/opencode/container-config.schema.json` | schema matching the pinned preview config shape |

OpenCode data/state/cache (`~/.local/share`, `~/.local/state`, `~/.cache`)
are writable under the image user's home. The XDG `opencode/` policy directory,
its baked agent/command subdirectories, and the authoritative config under
`/etc` remain root-owned. The default standalone process uses a private stdio
server and creates no background-service metadata there. Nothing depends on
host state.

## Build

The `Containerfile` is a multi-stage Fedora 44 build:

1. **`sdd-builder`** — `golang:1.26.5-bookworm`; clones
   `danweinerdev/claude-sdd-planner` at `9c1fbdaba6e650df3fa937dfd2e57f8bb76675ef`,
   builds `sdd`, and stages its generated portable `.opencode-plugin` skills
   and shared resources. SDD's generated collaboration prompts are dispatched
   through this image's restricted native workers; no parallel agent catalog is
   installed.
2. **`mcp-builder`** — `fedora:44` with `rustup` (`stable` + `1.97.1`); clones
   `danweinerdev/code-graph-mcp` at `45d53cdd8ec17776ae7a6156e5be5cdb82c4ad4f`
   and `danweinerdev/lldb-debug-mcp` at `a032c18f2f52c9f2b5a3c43f22917cef9e6264dc`,
   builds both servers, and stages their OpenCode skill/plugin assets.
3. **final** — `fedora:44`; installs `@opencode-ai/cli` at
   `0.0.0-beta-17823`, explicitly materializes its platform `opencode2` binary,
   verifies that no legacy `opencode` executable exists, and adds Node 24,
   `lldb` (the `lldb-dap` provider), runtime/debug utilities, binaries, plugin
   assets, agent/command definitions, and baked config.

The base build accepts `USER_UID`, `USER_GID`, `USERNAME` (and
`OPENCODE2_VERSION`) build args so the in-image user matches the host caller
under `--userns=keep-id`. The launcher passes the host values automatically:

```sh
podman build \
  --build-arg "USER_UID=$(id -u)" \
  --build-arg "USER_GID=$(id -g)" \
  --build-arg "USERNAME=$(id -un)" \
  -t opencode2:latest \
  -f Containerfile .
```

### Child images

Derive from the baked image with a plain `FROM` — everything (binaries, plugin
assets, config, env) is inherited:

```dockerfile
FROM opencode2:latest
# e.g. add a private provider model or a project-specific skill directory
COPY provider.json /opt/opencode/config/opencode/provider.json
```

`OPENCODE_CONFIG`, `XDG_CONFIG_HOME`, and the disable flags are set by `ENV` in
the base image and survive `FROM`.

## Encapsulation

The baked `/etc/opencode/container-config.json` is loaded via
`OPENCODE_CONFIG`. It registers the `code-graph` and `debug` MCP servers, the
code-graph/debug/sdd/model-router skill paths, and the local model-router plugin,
and sets `share: "disabled"` and `autoupdate: false`. Agents and
commands are baked under `XDG_CONFIG_HOME=/opt/opencode/config/opencode`.

The image sets `OPENCODE_DISABLE_PROJECT_CONFIG=1` and
`OPENCODE_DISABLE_EXTERNAL_SKILLS=1` /
`OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`, so a project `opencode.json` and the
host `~/.claude` / `~/.agents` skill scans are never consulted. The default
`CMD` is `opencode2 --standalone`, which keeps the private server scoped to the
foreground CLI instead of creating persistent background-service metadata.

## Model-router plugin (native v2)

`plugins/model-router/` is a **v2-only** plugin built on
`@opencode-ai/plugin/v2/promise` (pinned `1.18.25`). It default-exports
`{ id, setup }` and uses `ctx.agent.transform` to assign each agent a native
`AgentV2Info.model` (`ModelRef`) and optional request `headers`/`body` per
agent id, plus `draft.default(...)`. Routing accepts arbitrary valid agent ids
when a matching definition is already present, so derived images can add and
route their own agent definitions without mappings synthesizing new agents.

Config is assembled from two sources:

1. **Defaults** — the native v2 plugin-entry options baked into
   `container-config.json` (`{ schema_version, profiles, agents,
   default_agent }`); baked profiles demonstrate OpenAI + DeepSeek routing and
   need only `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`.
2. **Project overrides** — the workspace's `$PWD/.opencode-sandbox.json` is
   mounted read-only at `/run/opencode/sandbox.json`, and the launcher sets
   `OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json`. The plugin reads
   that file's top-level `model_router` block and shallow-merges its partial
   `profiles`/`agents`/`default_agent` over the defaults, then validates the
   merged result.

**v2-only limitations** — this plugin deliberately has no v1 surface:

- No v1 hook function, no `tool.execute.before/after` interception, and no
  `tool.definition`/description rewriting.
- No SDK child-session dispatch and no placeholder/return-ok relay.
- The native v2 `subagent` tool runs each worker on its **selected agent's
  model** directly; the router only assigns models, it does not mediate the
  call.
- Consequently there are no v1 runtime guards (webfetch circuit breaker,
  verification allowlist, sensitive-data scrub, review-lane bash guard). Those
  guarantees, where still wanted, belong in the agent prompts/permissions
  (baked) or in project policy.

## Agent definitions

Baked under `/opt/opencode/config/opencode/agent/` (10 total):

- **Native v2 workers** — `orchestrator` (primary), `reasoner`, `extractor`,
  `bulk-researcher`, `bounded-editor`, `implementer`, and the four review lanes
  `review-plan-drift`, `review-quality`, `review-spec-compliance`,
  `review-blind-spots`.

Each carries its own prompt and role-appropriate permissions; the model-router
assigns each a model. The native workers use V2 `permissions` rules and the V2
`shell` / `subagent` action names. The orchestrator prompt explains native v2
routing and maps the SDD stable dispatch descriptions (`implement_task`,
`review_*`) to these restricted native workers. SDD skills render their bundled
collaboration prompts into the dispatch instead of installing a parallel agent
catalog with different permissions.

## Sandbox setup skill

The baked `opencode2-sandbox` skill configures this repository's host-side
Podman launcher and `.opencode-sandbox.json`. Its source templates live under
`/opt/opencode/sandbox/`; the baked config permits read-only access to that
directory. The skill edits the mounted workspace, validates JSON and shell
syntax, and tells the user to run the launcher from the host after leaving the
container.

## Launcher (`examples/opencode-container.sh`)

`PATH`-safe; requires Bash, `jq`, `podman`, `git`, and `sha256sum`. It detects
`$PWD/.opencode-sandbox.json`: when present it applies and mounts that file;
when absent it warns and uses `opencode2:latest`, the current directory as the
workspace, a stable CWD-derived workdir, baked model routing, and
`opencode2 --standalone`. The launcher itself can therefore live anywhere on
`PATH`; project discovery and session persistence are based on the invocation
directory, not the script's installation directory.

```sh
./examples/opencode-container.sh                 # build if needed, run opencode2 --standalone
./examples/opencode-container.sh --rebuild       # force rebuild, then run
./examples/opencode-container.sh shell           # bash instead
./examples/opencode-container.sh -- <cmd...>     # arbitrary command
```

Behavior:

- `--pull=never --rm --init --userns=keep-id --security-opt label=disable`.
- TTY (`-it`) only when interactive. The selected workspace is mounted at
  `/src` for command compatibility and at `/workspace/<cwd-hash>` for a stable,
  project-specific OpenCode2 identity. The latter is the default workdir;
  relative workdirs and `/src`-based workdirs are remapped beneath it. An
  external git common dir (linked worktree) is detected and mounted at the same
  absolute path so git still works in-container.
- When a sandbox config exists, mounts it read-only at
  `/run/opencode/sandbox.json` and sets
  `OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json` so the model-router
  plugin can apply project overrides. No config means no control-file mount or
  override environment variable.
- Builds only when the image is absent or `--rebuild` is passed, and only from
  a configured local `build` block; always passes host `USER_UID`/`USER_GID`/
  `USERNAME` build args. Therefore config-free operation expects the default
  `opencode2:latest` image to exist locally.
- Auto-forwards only provider env vars that are **set** (the common provider
  list plus `AZURE_OPENAI_API_KEY`); rejects `HOME`/`PATH`/`XDG_*`/`OPENCODE_*`
  as explicit env; never mounts host `opencode` state from the effective XDG
  directories, or host `.agents`/`.claude`/`.mcp` state (including through an
  ancestor mount), and rejects mount targets that overlap baked config/plugin
  paths.
- Forwarded host values use Podman's name-only `--env NAME` form so secrets are
  not embedded in the launcher's process arguments. Treats the config as
  trusted repository code, but never logs secret values.
- Injects only the known Podman provider secrets explicitly selected by the
  project's `provider_secrets` list, using
  `--secret ...,type=env,target=...`. Merely creating a user-global secret does
  not expose it to every project. A selected secret takes precedence over a
  same-named host environment variable or `env.set` value.
- Persists OpenCode2's complete data store in a CWD-derived per-project named
  volume (`opencode2-data-<cwd-hash>`). The pinned preview stores provider
  logins and sessions in the same SQLite database, so keeping each project's
  database intact is safer than copying credential rows into project
  directories or exposing every project's state through one shared volume.
  Stable CWD-derived workdirs and volumes allow resume from any PATH-installed
  launcher. Configure an explicit common volume only when cross-project state
  sharing is intentional.

### Sandbox config schema (schema_version 1)

```json
{
  "schema_version": 1,
  "image": "opencode2:latest",              // optional; this is the default
  "build": {                                 // optional; enables local build
    "containerfile": "Containerfile",       // default "Containerfile"
    "context": ".",                          // default dirname(containerfile)
    "args": { "OPENCODE2_VERSION": "0.0.0-beta-17823" } // optional build args
  },
  "workspace": ".",                          // optional; default $PWD; mounted at /src
  "workdir": "app/",                         // optional; relative -> stable project path/app/
  "mounts": [],                              // optional additional mounts
  "env": {                                   // optional
    "pass": ["GIT_AUTHOR_NAME"],             // forwarded only if set in host
    "set": { "TZ": "UTC" }                   // literal values
  },
  "provider_secrets": [                      // optional explicit opt-in
    "openai-api-key",
    "deepseek-api-key"
  ],
  "persistence": {                           // optional override
    "data_volume": "my-project-opencode2-data" // omitted -> opencode2-data-<cwd-hash>
  },
  "model_router": {                          // optional; shallow-merged over the baked defaults
    "schema_version": 1,
    "profiles": {                            // partial: full profile per name
      "reasoning": { "model": "anthropic/claude-opus-4-1", "variant": "high" }
    },
    "agents": {                              // partial: agent id -> profile name
      "reasoner": "reasoning"
    },
    "default_agent": "orchestrator"          // optional
  },
  "network": "slirp4netns",                  // optional
  "capabilities": ["SYS_PTRACE"],            // optional
  "runtime_args": [                          // optional; restricted allow-list
    "--add-host=host.containers.internal:host-gateway",
    "--pids-limit=2048",
    "--ulimit=nofile=65536:65536"
  ],
  "command": ["opencode2", "--standalone"]   // optional; this is also the launcher default
}
```

Mount `source` may be relative (resolved against the workspace) or absolute;
an omitted `target` mirrors the resolved absolute source path. `runtime_args`
accepts only `--add-host=`, `--pids-limit=`, and `--ulimit=` entries.
Set `persistence.data_volume` to `""` for fully ephemeral OpenCode2 data. A
fixed explicit volume name shares both login and session state across every
project configured with that name. Moving a project changes the default CWD
hash; set an explicit project-specific volume name first if persistence must
survive that move.

The `model_router` block is a **partial** override: it shallow-merges its
`profiles` and `agents` maps over the baked defaults (an entry replaces the
same-named default; new profiles and agent mappings may be introduced), and
`default_agent` falls back to the baked value when omitted. A new mapping must
correspond to an agent definition supplied by a derived image. The override is
applied by the model-router plugin via `OPENCODE_MODEL_ROUTER_CONFIG`.

## Credentials and OAuth

Provider API keys can come from Podman secrets or host environment variables.
Podman secrets are preferred and do not place the value in the container's
saved environment configuration; host variables remain the convenient
fallback. Interactive provider logins performed inside OpenCode2 persist in the
project's named data volume alongside the session tables used by this OpenCode2
build, rather than reading or mounting host OpenCode1 state.

Known `provider_secrets` names are `openai-api-key` (`openapi-api-key` is accepted as a
compatibility alias), `anthropic-api-key`, `deepseek-api-key`, `groq-api-key`,
`google-api-key`, `gemini-api-key`, `google-generative-ai-api-key`,
`mistral-api-key`, `xai-api-key`, `openrouter-api-key`, `perplexity-api-key`,
`cohere-api-key`, `together-api-key`, and `azure-openai-api-key`. Create one
with, for example:

```sh
printf '%s' "$OPENAI_API_KEY" | podman secret create openai-api-key -
```

The secret is exposed to OpenCode2 as the corresponding uppercase provider
variable only when the current sandbox config lists its name. Any process
running as the container user can still use credentials made available to that
container, so use scoped and revocable credentials.

## Verification

```sh
# model-router plugin
cd plugins/model-router
npm install        # generates the lockfile if needed
npm test           # node:test, including a mock AgentDraft

# shell + config syntax
bash -n examples/opencode-container.sh
shellcheck examples/opencode-container.sh   # if shellcheck is installed
tests/launcher-env.test.sh
jq empty container-config.json
jq empty container-config.schema.json
jq empty examples/.opencode-sandbox.json.example
npx --yes ajv-cli@5 validate -s container-config.schema.json \
  -d container-config.json --spec=draft2020
```

The plugin tests cover multi-provider assignment, variant/request merge and
defaults, arbitrary agent routing, project overrides, malformed override/path
handling, setup registering a merged config, bad-config rejection, and an
assertion that the source contains no legacy v1 hook/dispatch markers.
