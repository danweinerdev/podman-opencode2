---
name: opencode2-sandbox
description: Set up or configure this repository's Podman OpenCode2 sandbox using .opencode-sandbox.json and opencode-container. Use ONLY for this image's launcher, mounts, environment forwarding, model-router overrides, or container build settings.
---

# OpenCode2 sandbox setup

Use the baked templates as the source of truth:

- `/opt/opencode/sandbox/opencode-container`
- `/opt/opencode/sandbox/.opencode-sandbox.json.example`

The launcher runs on the host, not inside this container. Create or update the
files in the mounted workspace, then tell the user to exit the container and run
the launcher from the host workspace root.

## Workflow

1. Confirm the workspace root and read any existing `.opencode-sandbox.json`
   and launcher before editing. Never overwrite an existing file blindly.
2. The launcher can run without a sandbox config: it warns and uses its baked
   image, workspace, routing, persistence, and command defaults. It is safe to
   install once on `PATH`; all project behavior is derived from the invocation
   directory. Create
   `.opencode-sandbox.json` from the baked template only when the user needs
   build settings, mounts, environment forwarding, or routing overrides. If the
   launcher itself is absent, copy it into the workspace with file tools; a
   convenient name is `opencode-container`.
3. Customize only fields the user needs. Keep `schema_version: 1`, use the
   `opencode2 --standalone` command, and retain the exact pinned
   `OPENCODE2_VERSION` unless the image source has been deliberately upgraded.
4. Validate JSON with `jq empty .opencode-sandbox.json` and Python syntax with
   `python3 -m py_compile opencode-container`. Build or run the image only when requested.
5. For machine-wide local models, copy
   `/opt/opencode/sandbox/local-providers.json.example` to
   `${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/local-providers.json` on the
   host and customize it. Never add credentials to this runtime-mounted
   catalog.
6. For machine-wide routing preferences, create the standalone partial config
   `${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/model-router.json` on the host.
   This runtime file is deliberately not baked into the image. Restart the
   launcher/container after an edit; do not rebuild the image. Never put
   credentials in this or the workspace router.
7. The launcher automatically exposes the host's exact user-global
   `$HOME/.gitconfig` read-only across workspaces. Do not add a per-workspace
   mount for it.

## Sandbox fields

- `image` is optional and defaults to `opencode2:latest`.
- `build` is optional. `containerfile` and `context` resolve from the host
  workspace; `args` are passed as Podman build arguments.
- `workspace` defaults to the host workspace root and is mounted at `/src`.
- When the workspace's `.git` is a file, the launcher validates its `gitdir`
  and mounts the external common metadata root at its matching absolute host
  path. A stale or malformed worktree pointer is rejected.
- Relative and `/src`-based `workdir` values resolve below the stable
  `/workspace/<cwd-hash>` project path unless `containers` is enabled.
- `containers` is an optional boolean, defaulting to `false`. When true, the
  launcher prefers a filesystem-accessible rootless Podman socket over a rootless or
  system Docker socket, mounts only the selected socket at
  `/run/opencode-container-engine.sock`, and manages `CONTAINER_HOST` and
  `DOCKER_HOST`. It also mirrors the workspace at its canonical host path and
  resolves default, relative, and `/src`-based workdirs there because nested
  bind paths are interpreted by the host engine. The workdir must remain within
  that mirror, and additional mounts may not overlap it. Discovery checks the
  socket node and permissions, not API liveness. The base image has no container
  client; use this with a derived image that installs `podman-remote`, Docker
  CLI, or another compatible client. Do not add manual socket,
  workspace-mirror, or host-variable entries when using this option. Socket
  access gives sandbox processes control equivalent to the host container-engine
  user, including host-path mounts and privileged containers; enable it only
  for trusted repositories and images.
- `mounts` entries contain `source`, optional `target`, and optional
  `read_only`. Relative sources resolve from the configured workspace. Do not
  mount host OpenCode state from effective XDG directories, `.agents`,
  `.claude`, or `.mcp` state, and do not target `/etc/opencode`,
  `/opt/opencode`, `/opt/mcp`, `/run/opencode`, `/src`, or `/workspace`.
- `agents` names a workspace directory of OpenCode agent Markdown files that the
  launcher mounts read-only at `/opt/opencode/config/opencode/agents`, the agent
  directory it scans in addition to the one the image bakes. It defaults to
  `.agents`; `false` skips the pickup and a path replaces the directory. These
  entries are additions and may not reuse a container or runtime agent id
  (`review-quality`, `orchestrator`, `build`, and the rest): a same-id file
  substitutes its own prompt and concatenates permission rules, silently widening
  the agent it shadowed, so the launch is refused. Entries must be regular files
  named `<agent-id>.md` with no subdirectories, because a nested path produces an
  id the model router rejects and a rejected id disables agent routing for every
  agent. Each file must open with a closed `---` frontmatter fence, since the
  runtime otherwise registers it as an unnamed primary agent whose prompt is its
  own frontmatter. Only `review-*` ids are dispatchable: the primary agents allow
  `review-*` after their deny-all rule. A reviewer declares the gates it feeds with
  `review-gates`, a list drawn from `review_plan_drift`, `review_quality`,
  `review_spec_compliance`, and `review_blind_spots`; the vocabulary is closed, so
  any other name is refused at launch, `[]` means advisory only, and a `review-*`
  agent omitting the key warns. Contributions merge into the named gate's evidence,
  which must still include the standard lane's own run. Route an added agent with
  `model_router.agents`. This is the workspace's own directory and is unrelated to
  the host `$HOME/.agents` state the mount rule above protects.
- On every launch, a present `$HOME/.gitconfig` must resolve to a readable
  regular file. The launcher mounts only its canonical exact file read-only at
  `/run/opencode/gitconfig` and sets `GIT_CONFIG_GLOBAL` to that path; it mounts
  neither host `HOME` nor the parent directory and does not set or forward
  `HOME`. This does not expose Git credential files/helpers or included files.
  Inline secrets in `.gitconfig` become readable in the container; relative
  includes resolve from `/run/opencode`, and other includes or credential
  helpers may require separately available paths or programs.
- `env.pass` forwards a named host variable only when set. `env.set` provides a
  literal value. Never place provider secrets in `env.set`; supported provider
  API keys use a configured `provider_secrets` entry when selected, then fall
  back to set host variables. Never opt a project into secrets it does not
  require. Manage the Podman secrets themselves from the host with
  `opencode-container secrets add NAME [--in FILE|-]` and `secrets remove
  NAME`, which create/delete the secret and add/drop the name in the current
  workspace's `provider_secrets` in one step (known provider names only; the
  value is never echoed or placed on a command line).
- `persistence.data_volume` defaults to a CWD-derived per-project named volume,
  `opencode2-data-<cwd-hash>`. The pinned preview stores provider logins and
  sessions in one SQLite database and UI preferences under `XDG_STATE_HOME`, so
  the launcher persists both trees in one isolated volume by default. A fixed
  explicit name intentionally shares all of that state across projects; an
  empty string disables
  persistence. The CWD-derived container workdir and volume remain stable even
  when the launcher itself lives elsewhere on `PATH`.
- Model-router precedence is baked/plugin options < the standalone user-global
  `${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/model-router.json` < the
  workspace's top-level `model_router`. Each layer shallow-merges partial
  `models`, `agents`, `default_agent`, and `pin_default_agent_model`, replacing an
  entry whole rather than deep-merging it. `schema_version` must be 2; `profiles`
  was v1's name for `models` and is refused. `agents` maps any valid agent id —
  baked, a project's `.agents`, or a derived image's — to either a `models` name or
  an inline `{model, variant?, request?}` selection, so routing one added agent to
  its own model is one entry with no shared declaration. `pin_default_agent_model`
  defaults to `false`, leaving the persisted UI model in charge of the default
  agent while workers follow the model named for them; set it to `true` to pin the
  default agent too. The global result must be valid before the workspace layer is
  applied; a workspace cannot repair an invalid global cross-reference.
- On every launch, a present global router must be one JSON object in a regular
  file, in a shape this image's router reads (`schema_version` 2, no `profiles`
  key); a retired shape stops the launch and names the rename, since the plugin's
  own rejection would only disable routing and leave one log line behind. The same
  check applies to a workspace `model_router` block. The launcher canonicalizes and mounts that exact file, never its parent
  directory, read-only at `/run/opencode/model-router-global.json`, and sets
  `OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG`. It independently mounts a present
  workspace sandbox at `/run/opencode/sandbox.json` and retains
  `OPENCODE_MODEL_ROUTER_CONFIG`; both are supplied when both exist. Router
  edits require only a launcher/container restart.
- On `build`, `run`, and `shell`, the launcher validates the host's shared
  `opencode2/local-providers.json` and mounts its exact canonical file
  read-only at `/opt/opencode/config/opencode/opencode.json`; it never exposes
  the catalog to an image build or bakes it into an image. A changed present
  catalog causes the next launcher-managed build to use a new SHA-256 cache
  key, while its runtime contents take effect immediately on the next launch.
  Removing the file removes the mount on the next launch. Secrets belong in
  Podman secrets rather than this catalog.
- `seccomp` controls syscall filtering for the main sandbox only. Missing or
  `null` emits no seccomp flag and preserves the container engine default;
  `"unconfined"` emits `--security-opt seccomp=unconfined`, disabling syscall
  filtering and materially weakening the sandbox. Any other nonempty string is
  a custom regular-file profile path. Relative paths resolve from the host
  invocation/config directory, not from a redirected `workspace`, and the
  launcher gives Podman the canonical absolute path. Wrong types (including
  booleans), empty or whitespace-only strings, missing paths, and directories
  are rejected. Do not put security options in `runtime_args`; its whitelist is
  unchanged. Older launchers may silently ignore this unknown top-level field,
  so update the launcher before relying on it. The setting never applies to
  `oc2-verify` workers, whose broker retains its own fixed container security
  policy.
- `network`, `capabilities`, and the restricted `runtime_args` list control the
  Podman sandbox. Allowed runtime arguments are `--add-host=`, `--pids-limit=`,
  and `--ulimit=` forms only.
- `command` defaults to `["opencode2", "--standalone"]`.

Treat `.opencode-sandbox.json` as executable project tooling: it controls image
builds, mounts, environment values, capabilities, networking, and commands.
Keep secrets out of it and all model-router config; use Podman secrets or
provider environment variables, and report every security-relevant change.
