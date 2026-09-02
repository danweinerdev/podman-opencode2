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
| `/opt/opencode/config/opencode/opencode.json` | optional build-imported local-provider catalog |
| `/opt/opencode/config/opencode/agent/*.md` | 10 baked native v2 agent definitions |
| `/opt/opencode/config/opencode/command/*.md` | baked code-graph slash commands |
| `/etc/opencode/container-config.json` | baked `OPENCODE_CONFIG` |
| `/etc/opencode/container-config.schema.json` | schema matching the pinned preview config shape |

OpenCode data/state/cache (`~/.local/share`, `~/.local/state`, `~/.cache`)
are writable under the image user's home. The XDG `opencode/` policy directory,
its baked agent/command subdirectories, and the authoritative config under
`/etc` remain root-owned. The default standalone process uses a private stdio
server and creates no background-service metadata there. The launcher never
mounts host OpenCode application state. Its optional exact-file user
configuration mounts are the standalone model-router file and user-global Git
config described below; the optional local-provider catalog is copied into the
image at build time rather than mounted into running containers.

## Build

Rebuild the repository's configured sandbox image without starting OpenCode:

```sh
make build
```

This delegates to the launcher, so `.opencode-sandbox.json`, host UID/GID,
optional local providers, and the launcher's build validation remain the single
source of truth.

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

That plain command intentionally builds without a machine-local provider
catalog. A launcher-driven build imports the optional shared catalog
automatically. For a direct build, provide the same read-only mount and digest:

```sh
providers="${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/local-providers.json"
digest="$(sha256sum "$providers")"; digest="${digest%% *}"
podman build \
  --security-opt label=disable \
  --volume "$providers:/run/opencode2-build-config/local-providers.json:ro" \
  --build-arg "LOCAL_PROVIDERS_SHA256=$digest" \
  --build-arg "USER_UID=$(id -u)" \
  --build-arg "USER_GID=$(id -g)" \
  --build-arg "USERNAME=$(id -un)" \
  -t opencode2:latest -f Containerfile .
```

### Child images

Derive from the baked image with a plain `FROM` — everything (binaries, plugin
assets, config, env) is inherited:

```dockerfile
FROM opencode2:latest
# e.g. add a private provider model or a project-specific skill directory
COPY local-providers.json /opt/opencode/config/opencode/opencode.json
```

`OPENCODE_CONFIG`, `XDG_CONFIG_HOME`, and the disable flags are set by `ENV` in
the base image and survive `FROM`. The exact global configuration filename is
`opencode.json`; the pinned OpenCode2 build does not load an arbitrary
`provider.json` fragment.

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
`{ id, setup }` and uses `ctx.agent.transform` to assign worker agents native
`AgentV2Info.model` (`ModelRef`) values and optional request `headers`/`body`
per agent id, plus `draft.default(...)`. The default agent is UI-controlled
unless explicitly pinned. Routing accepts arbitrary valid agent ids when a
matching definition is already present, so derived images can add and route
their own agent definitions without mappings synthesizing new agents.

Config is assembled from three sources, in increasing precedence:

1. **Defaults** — the native v2 plugin-entry options baked into
   `container-config.json` (`{ schema_version, profiles, agents,
   default_agent, pin_default_agent_model }`); baked profiles demonstrate
   OpenAI + DeepSeek routing and need only `OPENAI_API_KEY` /
   `DEEPSEEK_API_KEY`.
2. **User-global override** — on every launch,
   `${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/model-router.json` is discovered
   as a standalone partial router config. When present, the exact file is
   mounted read-only at `/run/opencode/model-router-global.json`, and
   `OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG` points the plugin to it. The merged
   global layer is validated before any project override is applied.
3. **Project overrides** — the workspace's `$PWD/.opencode-sandbox.json` is
   mounted read-only at `/run/opencode/sandbox.json`, and the launcher sets
    `OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json`. The plugin reads
    that file's top-level `model_router` block and shallow-merges its partial
    `profiles`/`agents`/`default_agent`/`pin_default_agent_model` over the global
    result, then validates the final result. `OPENCODE_MODEL_ROUTER_CONFIG`
    remains compatible for launchers that provide only a workspace config.

Router config is runtime input: editing the user-global or workspace routing
file requires only restarting the launcher/container, not rebuilding the
image. Never put API keys, authorization headers, tokens, passwords, or other
credentials in either model-router config; use Podman secrets or supported
provider environment variables.

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
workspace, a stable CWD-derived workdir, and `opencode2 --standalone`. On every
launch it independently discovers the optional user-global model-router config
and the host's exact `$HOME/.gitconfig`, falling back to baked routing when
neither routing override exists. The launcher itself can therefore live
anywhere on `PATH`; project discovery and session persistence are based on the
invocation directory, not the script's installation directory.

```sh
./examples/opencode-container.sh                 # build if needed, run opencode2 --standalone
./examples/opencode-container.sh --rebuild       # force rebuild, then run
./examples/opencode-container.sh shell           # bash instead
./examples/opencode-container.sh -- <cmd...>     # arbitrary command
```

Behavior:

- `--pull=never --rm --init --userns=keep-id --security-opt label=disable`.
- TTY (`-it`) only when interactive. Normally, the selected workspace is
  mounted at `/src` for command compatibility and at `/workspace/<cwd-hash>` for a stable,
  project-specific OpenCode2 identity. The latter is the default workdir;
  relative workdirs and `/src`-based workdirs are remapped beneath it. A linked
  worktree's `.git` file is validated directly; its external common
  metadata root is mounted at the same absolute host path so Git still works
  in-container. Stale or malformed pointers fail clearly instead of
  silently starting without repository metadata.
- When a sandbox config exists, mounts it read-only at
  `/run/opencode/sandbox.json` and sets
  `OPENCODE_MODEL_ROUTER_CONFIG=/run/opencode/sandbox.json` so the model-router
  plugin can apply project overrides. No config means no control-file mount or
  override environment variable.
- Independently discovers
  `${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/model-router.json`. A present
  path must be one JSON object in a regular file; the launcher canonicalizes
  and mounts that exact file (not its parent directory) read-only at
  `/run/opencode/model-router-global.json`, then sets
  `OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG=/run/opencode/model-router-global.json`.
  Both control files and both environment variables are supplied when both
  configs exist.
- When `containers: true` is selected, discovers the host's filesystem-accessible rootless
  Podman socket first, then a rootless or system Docker socket. It mounts only
  that socket at `/run/opencode-container-engine.sock` and sets both
  `CONTAINER_HOST` and `DOCKER_HOST` to its Unix URI. Because bind paths sent to
  that socket are interpreted by the host engine, the launcher also mirrors the
  workspace at its canonical host path and uses that path as the default
  workdir instead of `/workspace/<cwd-hash>`; `/src` remains available.
  The base image does not install a container client; derived development
  images must provide `podman-remote`, Docker CLI, or another compatible client.
- On every launch, independently checks the host's exact `$HOME/.gitconfig`.
  When present, it canonicalizes and mounts only that readable regular file
  read-only at `/run/opencode/gitconfig` and sets `GIT_CONFIG_GLOBAL` to that
  path, so it works even when a prebuilt image has a different username. It
  does not mount host `HOME`, the file's parent, Git credential files/helpers,
  or files named by `include`/`includeIf`, and it does not forward or set
  `HOME`. No per-workspace `mounts` entry is required. Inline secrets in
  `.gitconfig` become readable inside the container; relative includes resolve
  from `/run/opencode`, and other includes or credential helpers work only when
  their paths or programs are separately available in the container.
- Builds only when the image is absent or `--rebuild` is passed, and only from
  a configured local `build` block; always passes host `USER_UID`/`USER_GID`/
  `USERNAME` build args. Therefore config-free operation expects the default
  `opencode2:latest` image to exist locally.
- When a build occurs, optionally validates and imports the user-wide local
  provider catalog at
  `${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/local-providers.json`. Only that
  file's validated canonical JSON is mounted read-only into the build, and its
  SHA-256 is passed as a cache key and verified by the `Containerfile`. It is
  not mounted at runtime.
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
- Persists OpenCode2's data and state directories in a CWD-derived per-project
  named volume (`opencode2-data-<cwd-hash>`). The pinned preview stores provider
  logins and sessions in the data SQLite database and UI preferences under its
  state directory. Keeping both intact is safer than copying credential rows
  into project directories or exposing every project's state through one
  shared volume.
  Stable CWD-derived volumes allow resume from any PATH-installed launcher;
  workdirs are also CWD-derived except when `containers` mirrors the canonical
  host path. Configure an explicit common volume only when cross-project state
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
  "containers": false,                       // optional host container-engine socket access
  "workdir": "app/",                         // optional; relative -> selected workspace path/app/
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
    "pin_default_agent_model": false,        // let the persisted UI model win
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
`containers` must be a boolean and defaults to `false`. When true, the launcher
prefers a filesystem-accessible user Podman socket over Docker, fails if neither is
available, and manages `CONTAINER_HOST`/`DOCKER_HOST` itself. It mirrors the
workspace at the canonical host path so nested container commands can safely
use `$PWD` in bind mounts; relative and `/src`-based workdirs resolve beneath
that host path and may not escape it. Additional mounts may not overlap this
workspace mirror. Socket discovery validates the node and host-user access, not
the engine API; remove stale socket files or restart the service if connection
fails. The base image has no container client, so a derived image must install
one. A socket that depends on a supplementary host group also requires a Podman
OCI runtime supporting `--group-add keep-groups`. Access to either
socket grants processes in the sandbox control
equivalent to the host user who owns or can access the container engine,
including the ability to start privileged containers and mount host paths. Use
this option only in trusted repositories and images.
Set `persistence.data_volume` to `""` for fully ephemeral OpenCode2 data. A
fixed explicit volume name shares login, session, and UI state across every
project configured with that name. Moving a project changes the default CWD
hash; set an explicit project-specific volume name first if persistence must
survive that move.

The `model_router` block is a **partial** override: it shallow-merges its
`profiles` and `agents` maps over the user-global result (an entry replaces the
same-named entry; new profiles and agent mappings may be introduced), while
`default_agent` and `pin_default_agent_model` fall back through the user-global
layer to their baked values when omitted. The baked value is `false`, leaving
the primary agent's model under OpenCode's persisted UI selection while worker
agents remain profile-routed. Set it to `true` when the default agent must also
use its profile model. Before the first UI selection, OpenCode's provider
fallback supplies the primary model. A new mapping must correspond to an agent
definition supplied by a derived image. The override is applied by the
model-router plugin via `OPENCODE_MODEL_ROUTER_CONFIG`.

## User-global model routing

`examples/model-router.json.example` is the standalone partial-config shape for
machine-wide routing preferences. Install it outside repositories:

```sh
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/opencode2"
cp examples/model-router.json.example \
  "${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/model-router.json"
```

The three-layer precedence is baked/plugin options < user-global standalone
config < workspace `model_router`. Each profile entry is replaced as a whole,
not deep-merged. The global layer must resolve all of its cross-references after
merging over the baked options; a workspace override cannot repair an invalid
global mapping. Changes take effect after restarting the launcher/container and
do not require an image rebuild. Keep credentials out of this file and the
workspace `model_router`; use Podman secrets or provider environment variables.

## Shared local providers

`examples/local-providers.json.example` is a generalized provider/model catalog
for local OpenAI-compatible servers such as llama.cpp, Ollama, or LM Studio.
Install a machine-specific copy outside every repository:

```sh
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/opencode2"
cp examples/local-providers.json.example \
  "${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/local-providers.json"
```

For example, a llama.cpp server reachable from Podman on host port 18080 can be
declared as:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "llama.cpp": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "llama.cpp (local)",
      "options": {
        "baseURL": "http://host.containers.internal:18080/v1"
      },
      "models": {
        "qwen3.8-27b-q8-latest": {
          "name": "Qwen 3.8 27B (local)",
          "limit": { "context": 262144, "output": 32768 }
        }
      }
    }
  }
}
```

The provider is then available as `llama.cpp/qwen3.8-27b-q8-latest` to the model
picker and to `model_router.profiles`. The server must listen on an address
reachable through `host.containers.internal`; unlike a container-loopback URL,
this does not require `network: "host"` for the tested llama.cpp setup.

The launcher consumes the shared catalog only when it builds the image. Run
`opencode-container --rebuild` after adding, changing, or removing it. The
catalog is installed in the image as
`/opt/opencode/config/opencode/opencode.json`, where OpenCode merges it with the
baked `/etc/opencode/container-config.json`. Canonicalization also prevents
discarded duplicate-key bytes from surviving in the image layer. Do not put API
keys, authorization
headers, tokens, passwords, or other secrets in this file: its contents become
an image layer. The launcher rejects those obvious credential field names;
continue to use Podman secrets or forwarded environment variables for
credentials.

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
shellcheck examples/opencode-container.sh
tests/launcher-env.test.sh
tests/local-provider-build.test.sh  # Podman integration; builds present/changed/absent catalogs
jq empty container-config.json
jq empty container-config.schema.json
jq empty examples/opencode-sandbox.json.example
jq empty examples/local-providers.json.example
npx --yes ajv-cli@5 validate -s container-config.schema.json \
  -d container-config.json --spec=draft2020
```

The plugin tests cover multi-provider assignment, variant/request merge and
defaults, arbitrary agent routing, project overrides, malformed override/path
handling, setup registering a merged config, bad-config rejection, and an
assertion that the source contains no legacy v1 hook/dispatch markers.
