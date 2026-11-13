---
name: opencode2-sandbox
description: Set up or configure this repository's Podman OpenCode2 sandbox using .opencode-sandbox.json and opencode-container. Use ONLY for this image's launcher, mounts, environment forwarding, credentials, or container build settings.
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
   image, workspace, persistence, and command defaults. It is safe to
   install once on `PATH`; all project behavior is derived from the invocation
   directory. Create
   `.opencode-sandbox.json` from the baked template only when the user needs
   build settings, mounts, or environment forwarding. If the
   launcher itself is absent, copy it into the workspace with file tools; a
   convenient name is `opencode-container`.
3. Customize only fields the user needs. Keep `schema_version: 1`, use the
   `opencode2 --standalone` command, and don't pass `OPENCODE2_VERSION` as a
   build arg: the image's Containerfile ARG is the single version pin.
4. Validate JSON with `jq empty .opencode-sandbox.json` and Python syntax with
   `python3 -m py_compile opencode-container`. Build or run the image only when requested.
5. For machine-wide local models, copy
   `/opt/opencode/sandbox/local-providers.json.example` to
   `${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/local-providers.json` on the
   host and customize it. Never add credentials to this runtime-mounted
   catalog.
6. For machine-wide model routing preferences, create
   `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/aid.json` on the host (where
   OpenCode and ai-devtools keep it). The launcher mounts it read-only at
   `/opt/opencode/config/opencode/aid.json`, where the ai-devtools plugin
   reads it. The older `opencode2/aid.json` is ignored; the launcher warns and
   names the new path when only the old file exists. A workspace's
   `.opencode/aid.json` takes precedence over this global file. It is
   deliberately not baked into the image. Restart the launcher/container after
   an edit; do not rebuild the image. Never put credentials in either file.
7. The launcher automatically exposes the host's exact user-global
   `$HOME/.gitconfig` read-only across workspaces. Do not add a per-workspace
   mount for it.

## Sandbox fields

- `image` is optional and defaults to `opencode2:latest`.
- `build` is optional. `containerfile` and `context` resolve from the host
  workspace; `args` are passed as Podman build arguments.
- `workspace` defaults to the host workspace root. In every mode it is mounted
  at its canonical host path, which is also the working directory.
- When the workspace's `.git` is a file, the launcher validates its `gitdir`
  and mounts the external common metadata root at its matching absolute host
  path, in every mode. A stale or malformed worktree pointer is rejected.
  Submodule `.git` files resolve relative to the mounted workspace and metadata.
- `workdir` resolves below the workspace's host path in every mode; a relative
  value is joined to it, and an absolute value must stay inside it.
- `containers` is optional. The default, `"nested"` (also spelled `true`), runs a
  rootless Podman engine inside the sandbox: own image store in the per-project
  `opencode2-containers-<cwd-hash>` volume at `/var/lib/opencode-containers`, own
  network namespace, no host socket, and inner containers can mount only what
  the sandbox already has. Every launch, explicit or not, checks what it needs —
  at least 65,536 subordinate uids **and** gids for the host user, and an image
  labelled `io.opencode.nested-podman="1"` (`NESTED_PODMAN=1`, the Containerfile
  default; derived images inherit it) — and refuses to start otherwise, telling
  you to set `false` (no engine) or `"host"`. The cost is a looser outer profile:
  `SYS_ADMIN` in the bounding set, `systempaths=unconfined`, `/dev/net/tun`, and
  a root start whose prelude delegates the cgroup and drops to the image user.
  Inner `--memory` and `--cpus` are enforced. The base image ships the engine, so
  no derived image is needed, and the engine's `podman` uses the local store.
  See `docs/SANDBOX.md`, "Container access".
  - `"host"` mounts the host container engine instead: the launcher prefers a
    filesystem-accessible rootless Podman socket over a rootless or system Docker
    socket, mounts only the selected socket at
    `/run/opencode-container-engine.sock`, and manages `CONTAINER_HOST` and
    `DOCKER_HOST`. The workspace is mounted at its canonical host path exactly as
    in the other modes, so nested bind paths resolve through the host engine. The
    workdir must remain within that workspace, and additional mounts may not
    overlap it. Discovery checks the
    socket node and permissions, not API liveness. **Host mode gives sandbox
    processes control equivalent to the host container-engine user**, including
    host-path mounts and privileged containers; choose it only for trusted
    repositories and images. Do not add manual socket, workspace, or
    host-variable entries when using it.
  - `false` gives the sandbox no engine at all.
- `mounts` entries contain `source`, optional `target`, and optional
  `read_only`. Relative sources resolve from the configured workspace. Do not
  mount host OpenCode state from effective XDG directories, `.agents`,
  `.claude`, or `.mcp` state, and do not target `/etc/opencode`,
  `/opt/opencode`, `/opt/mcp`, or `/run/opencode`. In any mode a target may also
  not be `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, or
  `/usr/local/libexec`: those hold the interpreter, the startup script, and the
  runtime the sandbox starts with.
- On every launch, a present `$HOME/.gitconfig` must resolve to a readable
  regular file. The launcher mounts only its canonical exact file read-only at
  `/run/opencode/gitconfig` and sets `GIT_CONFIG_GLOBAL` to that path; it mounts
  neither host `HOME` nor the parent directory and does not set or forward
  `HOME`. This does not expose Git credential files/helpers or included files.
  Inline secrets in `.gitconfig` become readable in the container; relative
  includes resolve from `/run/opencode`, and other includes or credential
  helpers may require separately available paths or programs.
- `env.pass` forwards a named host variable only when set. `env.set` provides a
  literal value. In nested mode both are delivered by a launcher-owned 0600 file
  mounted read-only at `/run/opencode/agent-env` and applied to the agent after
  the privilege drop, not through the container's start-up environment; provider
  secrets keep their fixed names and stay ordinary process environment. Never
  place provider secrets in `env.set`; supported provider API keys use a
  configured `provider_secrets` entry when selected, then fall back to set host
  variables. Never opt a project into secrets it does not require. Manage the
  Podman secrets themselves from the host with
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
  persistence. The CWD-derived data volume name remains stable even when the
  launcher itself lives elsewhere on `PATH`.
- `persistence.seed_credentials` defaults to `false`. When `true`, the launcher
  copies all of the host's OpenCode credentials into the project data volume
  once, when that volume holds none yet, by piping `opencode2`/`opencode auth
  export` into `opencode2 --standalone auth import`. The copy includes OAuth
  refresh tokens; refreshes inside the sandbox do not flow back to the host, and
  a provider that rotates refresh tokens may invalidate one side's copy, which a
  re-login fixes. It is ignored with a notice when `persistence.data_volume` is
  `""`, applies in every mode, and must be a boolean. Never put credential
  values in the config; seeding reads them from the host CLI at launch.
- Model routing is read by the ai-devtools plugin from the workspace's
  `.opencode/aid.json`, else the user-global
  `${XDG_CONFIG_HOME:-$HOME/.config}/opencode/aid.json`. The launcher mounts
  that exact file read-only at `/opt/opencode/config/opencode/aid.json` and
  leaves it unmounted when absent; the older `opencode2/aid.json` is ignored
  with a warning. Routing itself belongs to ai-devtools; a
  routing edit needs only a launcher/container restart.
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
  so update the launcher before relying on it.
- `network` and the restricted `runtime_args` list control the Podman sandbox.
  Allowed runtime arguments are `--add-host=`, `--pids-limit=`, and `--ulimit=`
  forms only. `capabilities` is a fixed allow-list — `NET_RAW`,
  `NET_BIND_SERVICE`, `NET_ADMIN`, `NET_BROADCAST`, `SYS_NICE`, `IPC_LOCK`,
  `SYS_RESOURCE` — matched case-insensitively with an optional `CAP_` prefix.
  Anything that could bypass file permissions, change uid/gid, or act on
  processes or kernel state outside the agent's own (including `SYS_PTRACE` and
  `SYS_ADMIN`) stops the launch; debugging needs no capability, because the agent
  can already ptrace its own processes. In nested mode the configured set is
  carried across the privilege drop exactly.
- `command` defaults to `["opencode2", "--standalone"]`.

Treat `.opencode-sandbox.json` as executable project tooling: it controls image
builds, mounts, environment values, capabilities, networking, and commands.
Keep secrets out of it and the aid config; use Podman secrets or
provider environment variables, and report every security-relevant change.
