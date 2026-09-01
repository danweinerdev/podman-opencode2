# OpenCode2 Container

A Fedora 44 container that runs the **OpenCode 1.18.25** CLI with a native
**v2 model router**, the **code-graph** and **debug** MCP servers, and the
**SDD planner** CLI baked in. Every Go/Rust/Cargo toolchain lives in throwaway
builder stages, so the runtime image ships no dev toolchain.

## What the image contains

| Path | Contents |
| --- | --- |
| `/opt/mcp/bin/` | `code-graph-mcp`, `debug-mcp`, `sdd` (all on `PATH`) |
| `/opt/opencode/plugins/code-graph/` | code-graph OpenCode plugin (skills + commands) |
| `/opt/opencode/plugins/debug/` | debug-mcp OpenCode skills |
| `/opt/opencode/plugins/sdd/` | SDD planner OpenCode skills + shared resources + original agents |
| `/opt/opencode/plugins/model-router/` | native v2 model-router plugin |
| `/opt/opencode/config/opencode/agent/*.md` | 18 baked agent definitions |
| `/opt/opencode/config/opencode/command/*.md` | baked code-graph slash commands |
| `/etc/opencode/container-config.json` | baked `OPENCODE_CONFIG` |

OpenCode data/state/cache (`~/.local/share`, `~/.local/state`, `~/.cache`)
are writable under the image user's home. Nothing depends on host state.

## Build

The `Containerfile` is a multi-stage Fedora 44 build:

1. **`sdd-builder`** — `golang:1.26.5-bookworm`; clones
   `danweinerdev/claude-sdd-planner` at `9c1fbdaba6e650df3fa937dfd2e57f8bb76675ef`,
   builds `sdd`, stages `.opencode-plugin`, and sanitizes the eight source
   `agents/*.md` into OpenCode agent definitions (legacy shorthand `model:`
   lines removed, `mode: subagent` added), retaining the originals under the
   plugin tree.
2. **`mcp-builder`** — `fedora:44` with `rustup` (`stable` + `1.97.1`); clones
   `danweinerdev/code-graph-mcp` at `45d53cdd8ec17776ae7a6156e5be5cdb82c4ad4f`
   and `danweinerdev/lldb-debug-mcp` at `a032c18f2f52c9f2b5a3c43f22917cef9e6264dc`,
   builds both servers, and stages their OpenCode skill/plugin assets.
3. **final** — `fedora:44`; installs OpenCode `1.18.25` (npm), Node 24,
   `lldb` (the `lldb-dap` provider), and runtime/debug utilities; copies the
   binaries, plugin assets, agent/command definitions, and baked config.

The base build accepts `USER_UID`, `USER_GID`, `USERNAME` (and
`OPENCODE_VERSION`) build args so the in-image user matches the host caller
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
code-graph/debug/sdd skill paths, and the local model-router plugin, and sets
`share: "disabled"`, `autoupdate: false`, and `subagent_depth: 2`. Agents and
commands are baked under `XDG_CONFIG_HOME=/opt/opencode/config/opencode`.

The image sets `OPENCODE_DISABLE_PROJECT_CONFIG=1` and
`OPENCODE_DISABLE_EXTERNAL_SKILLS=1` /
`OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`, so a project `opencode.json` and the
host `~/.claude` / `~/.agents` skill scans are never consulted. The default
`CMD` is `opencode`.

## Model-router plugin (native v2)

`plugins/model-router/` is a **v2-only** plugin built on
`@opencode-ai/plugin/v2/promise` (pinned `1.18.25`). It default-exports
`{ id, setup }` and uses `ctx.agent.transform` to assign each agent a native
`AgentV2Info.model` (`ModelRef`) and optional request `headers`/`body` per
agent id, plus `draft.default(...)`. Routing is by arbitrary valid agent id,
not a closed role list.

Config is assembled from two sources:

1. **Defaults** — the plugin tuple options baked into
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
- The native v2 `task` tool runs each subagent on its **selected agent's
  model** directly; the router only assigns models, it does not mediate the
  call.
- Consequently there are no v1 runtime guards (webfetch circuit breaker,
  verification allowlist, sensitive-data scrub, review-lane bash guard). Those
  guarantees, where still wanted, belong in the agent prompts/permissions
  (baked) or in project policy.

## Agent definitions

Baked under `/opt/opencode/config/opencode/agent/` (18 total):

- **Native v2 workers** — `orchestrator` (primary), `reasoner`, `extractor`,
  `bulk-researcher`, `bounded-editor`, `implementer`, and the four review lanes
  `review-plan-drift`, `review-quality`, `review-spec-compliance`,
  `review-blind-spots`.
- **SDD planner agents** — `researcher`, `plan-reviewer`, `code-implementer`,
  `quality-scanner`, `spec-reviewer`, `spec-compliance`, `drift-detector`,
  `blind-spot-finder`. These are sourced from the pinned
  `claude-sdd-planner` `agents/*.md` and sanitized for OpenCode (legacy
  shorthand `model:` lines removed, `mode: subagent` added) in the
  `sdd-builder` stage; the original copies are also retained under
  `/opt/opencode/plugins/sdd/agents/`.

Each carries its own prompt and role-appropriate permissions; the model-router
assigns each a model. The orchestrator prompt explains native v2 routing and
maps the SDD stable dispatch descriptions (`implement_task`, `review_*`) to the
correct `subagent_type`.

## Launcher (`examples/opencode-container.sh`)

`PATH`-safe; requires `jq` and `podman`; always reads `$PWD/.opencode-sandbox.json`.

```sh
./examples/opencode-container.sh                 # build if needed, run opencode
./examples/opencode-container.sh --rebuild       # force rebuild, then run
./examples/opencode-container.sh shell           # bash instead
./examples/opencode-container.sh -- <cmd...>     # arbitrary command
```

Behavior:

- `--pull=never --rm --init --userns=keep-id --security-opt label=disable`.
- TTY (`-it`) only when interactive; the selected workspace is always mounted
  at `/src` (default workdir `/src`; a relative `workdir` resolves under
  `/src`); an external git common dir (linked worktree) is detected and
  mounted at the same path so git still works in-container.
- Mounts the sandbox config read-only at `/run/opencode/sandbox.json` and sets
  `OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json` so the
  model-router plugin can apply project overrides.
- Builds only when the image is absent or `--rebuild` is passed, and only from
  a configured local `build` block; always passes host `USER_UID`/`USER_GID`/
  `USERNAME` build args.
- Auto-forwards only provider env vars that are **set** (the common provider
  list plus `AZURE_OPENAI_API_KEY`); rejects `HOME`/`PATH`/`XDG_*`/`OPENCODE_*`
  as explicit env; never mounts host `opencode`/`.agents`/`.claude`/`.mcp`
  state and rejects mount targets that shadow baked config/plugin paths.
- Treats the config as trusted repository code, but never logs secret values.

### Sandbox config schema (schema_version 1)

```json
{
  "schema_version": 1,
  "image": "opencode2:latest",              // required
  "build": {                                 // optional; enables local build
    "containerfile": "Containerfile",       // default "Containerfile"
    "context": ".",                          // default dirname(containerfile)
    "args": { "OPENCODE_VERSION": "1.18.25" } // optional build args
  },
  "workspace": ".",                          // optional; default $PWD; mounted at /src
  "workdir": "app/",                         // optional; default "/src"; relative -> /src/app/
  "mounts": [],                              // optional additional mounts
  "env": {                                   // optional
    "pass": ["GIT_AUTHOR_NAME"],             // forwarded only if set in host
    "set": { "TZ": "UTC" }                   // literal values
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
  "command": ["opencode"]                    // optional; default ["opencode"]
}
```

Mount `source` may be relative (resolved against the workspace) or absolute;
an omitted `target` mirrors the resolved absolute source path. `runtime_args`
accepts only `--add-host=`, `--pids-limit=`, and `--ulimit=` entries.

The `model_router` block is a **partial** override: it shallow-merges its
`profiles` and `agents` maps over the baked defaults (an entry replaces the
same-named default; a new profile/agent may be introduced), and `default_agent`
falls back to the baked value when omitted. It is applied by the model-router
plugin via `OPENCODE_MODEL_ROUTER_CONFIG`.

## Credentials and OAuth

Only provider **API keys** are ever forwarded (and only when set). No OAuth
tokens or account credentials are copied in, and OpenCode OAuth is **not**
persisted: any interactive auth happens inside the throwaway container and is
lost with it. Pass keys per-run from the host environment (e.g.
`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`); the launcher forwards the ones that are
set automatically.

## Verification

```sh
# model-router plugin
cd plugins/model-router
npm install        # generates the lockfile if needed
npm test           # node:test — 27 cases incl. a mock AgentDraft

# shell + config syntax
bash -n examples/opencode-container.sh
shellcheck examples/opencode-container.sh   # if shellcheck is installed
jq empty container-config.json
jq empty examples/.opencode-sandbox.json.example
```

The plugin tests cover multi-provider assignment, variant/request merge and
defaults, arbitrary agent-id routing, shallow-merge project overrides,
malformed override/path handling, setup registering a merged config,
bad-config rejection, and an assertion that the source contains no legacy v1
hook/dispatch markers.
