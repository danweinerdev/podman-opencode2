# Sandbox configuration

[Back to README](../README.md)

The launcher reads `.opencode-sandbox.json` from the directory where you run it.
Start small and add only the settings you need:

```json
{
  "schema_version": 1,
  "image": "opencode2:latest",
  "workspace": ".",
  "containers": false,
  "env": { "set": { "TZ": "UTC" } }
}
```

Restart the launcher after changing settings. The
[full example](../examples/opencode-sandbox.json.example) shows more options;
adapt its model routes, secret selections, and volume name before using it.

## Settings reference

| Setting | Purpose and default |
| --- | --- |
| `schema_version` | Use `1`. |
| `image` | Image to run; defaults to `opencode2:latest`. |
| `build` | Optional local build: `containerfile`, `context`, and string-valued `args`. The Containerfile defaults to `Containerfile`; context defaults to its directory. |
| `workspace` | Directory to mount; defaults to the launch directory. |
| `workdir` | Starting directory inside the workspace. Relative paths are supported. |
| `containers` | Share a host container-engine socket; defaults to `false`. |
| `mounts` | Extra mounts with `source` and optional `target`. A relative source resolves against the workspace; an omitted target mirrors the absolute source path. |
| `env.pass` | Host variable names to forward when set. |
| `env.set` | Literal environment values. |
| `provider_secrets` | Keys this project can use; see [credentials](CREDENTIALS.md). |
| `persistence.data_volume` | Override the project's saved-state volume. An empty string disables persistence. |
| `model_router` | Project model overrides; see [model router](MODEL_ROUTER.md). |
| `network` | Optional Podman network mode, such as `slirp4netns`. |
| `capabilities` | Extra capabilities, such as `SYS_PTRACE` for debugging. |
| `runtime_args` | Only `--add-host=`, `--pids-limit=`, and `--ulimit=` entries are allowed. |
| `command` | Defaults to `["opencode2", "--standalone"]`. |

## Files and Git

The workspace is mounted at `/src` and at a stable project path under
`/workspace/`. The stable path is the default working directory. Relative and
`/src`-based workdirs are resolved beneath it.

Linked Git worktrees are supported: the launcher validates the `.git` pointer
and mounts external Git metadata at its original host path.

If `$HOME/.gitconfig` exists, only that file is mounted read-only and exposed
through `GIT_CONFIG_GLOBAL`. Includes, credential files, and helper programs
are not automatically mounted. Relative includes resolve from
`/run/opencode`; included files and helpers must be separately available.
Any secrets written directly in `.gitconfig` are readable inside the container.

## Saved state

Logins, sessions, and UI preferences are saved in a named volume:
`opencode2-data-<cwd-hash>`. The name depends on the launch directory, so each
project normally gets separate state even when using a launcher installed on `PATH`.

- To keep state across a directory move, set `persistence.data_volume` to the
  existing volume's name before moving.
- To share state across projects, give them the same explicit volume name.
  This shares logins and sessions as well as preferences.
- To discard state when the container exits, set `persistence.data_volume` to `""`.

## Run containers from the sandbox

Set `containers` to `true` and use an image with a container client.
The development image provides one; the base image does not.

The launcher prefers a rootless Podman socket, then a rootless or system Docker
socket. For user Podman:

```sh
systemctl --user start podman.socket
```

The socket is mounted at `/run/opencode-container-engine.sock`.
The launcher sets `CONTAINER_HOST` and `DOCKER_HOST` automatically and uses the
workspace's canonical host path as the workdir, so nested bind mounts using
`$PWD` resolve correctly. `/src` remains available. Extra mounts cannot overlap
this workspace mirror.

**Socket access lets the sandbox control the host container engine**, including
starting containers and mounting host paths. Enable it only for trusted projects
and images.

If connections fail, check for a stale socket or stopped service. Discovery
checks file access, not the engine API. Sockets requiring a supplementary host
group need a Podman OCI runtime that supports `--group-add keep-groups`.

## Environment boundaries

The launcher runs with `--rm`, `--init`, `--userns=keep-id`, `--pull=never`,
and `--security-opt label=disable`. It adds an interactive TTY only when needed.

The baked OpenCode config and plugin directories stay root-owned. Project
`opencode.json` files and external Claude/agent skill scans are disabled.
The standalone server exits with the foreground CLI.

Host OpenCode application state and `.agents`, `.claude`, and `.mcp` directories
cannot be mounted, including through parent directories. Mounts cannot overlap
baked config or plugin paths. Explicit environment settings cannot override
`HOME`, `PATH`, `XDG_*`, or `OPENCODE_*`.

The supported user-config mounts are individual files: model routing,
local providers, and Git config. Treat sandbox configuration as trusted
repository code because it controls additional mounts and runtime settings.
