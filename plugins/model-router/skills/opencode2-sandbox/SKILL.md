---
name: opencode2-sandbox
description: Set up or configure this repository's Podman OpenCode2 sandbox using .opencode-sandbox.json and opencode-container.sh. Use ONLY for this image's launcher, mounts, environment forwarding, model-router overrides, or container build settings.
---

# OpenCode2 sandbox setup

Use the baked templates as the source of truth:

- `/opt/opencode/sandbox/opencode-container.sh`
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
   convenient name is `opencode-container.sh`.
3. Customize only fields the user needs. Keep `schema_version: 1`, use the
   `opencode2 --standalone` command, and retain the exact pinned
   `OPENCODE2_VERSION` unless the image source has been deliberately upgraded.
4. Validate JSON with `jq empty .opencode-sandbox.json` and shell syntax with
   `bash -n opencode-container.sh`. Build or run the image only when requested.
5. For machine-wide local models, copy
   `/opt/opencode/sandbox/local-providers.json.example` to
   `${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/local-providers.json` on the
   host and customize it. Never add credentials to this build-time catalog.
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
  require.
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
  `profiles`, `agents`, `default_agent`, and `pin_default_agent_model`. The last
  setting defaults to `false`, allowing the persisted UI model to control the
  default agent while workers remain profile-routed; set it to `true` to pin the
  default agent to its profile too. The global result must
  be valid before the workspace layer is applied; a workspace cannot repair an
  invalid global cross-reference.
- On every launch, a present global router must be one JSON object in a regular
  file. The launcher canonicalizes and mounts that exact file, never its parent
  directory, read-only at `/run/opencode/model-router-global.json`, and sets
  `OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG`. It independently mounts a present
  workspace sandbox at `/run/opencode/sandbox.json` and retains
  `OPENCODE_MODEL_ROUTER_CONFIG`; both are supplied when both exist. Router
  edits require only a launcher/container restart.
- During an image build, the launcher optionally validates the host's shared
  `opencode2/local-providers.json`, mounts only that file into the build, and
  bakes its canonical JSON as `/opt/opencode/config/opencode/opencode.json`.
  Its SHA-256 invalidates the relevant build layer. Changes require `--rebuild`;
  absent catalogs remain absent, and secrets belong in Podman secrets rather
  than this image layer.
- `network`, `capabilities`, and the restricted `runtime_args` list control the
  Podman sandbox. Allowed runtime arguments are `--add-host=`, `--pids-limit=`,
  and `--ulimit=` forms only.
- `command` defaults to `["opencode2", "--standalone"]`.

Treat `.opencode-sandbox.json` as executable project tooling: it controls image
builds, mounts, environment values, capabilities, networking, and commands.
Keep secrets out of it and all model-router config; use Podman secrets or
provider environment variables, and report every security-relevant change.
