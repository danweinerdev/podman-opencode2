# Setup

[Back to README](../README.md)

## Use this repository

Follow the [quick start](../README.md#quick-start). `make build` builds the base
image first, then the development image selected by this repository's sandbox
config. Image builds need network access to download dependencies.

The launcher requires Python 3.10+, Podman, Git, and `id` on the host.
The checked-in config sets `containers` to `"host"`, because its verification
broker uses the host's worker image: start a user Podman socket before launching.
Without that line a project gets the default `"nested"` mode, an engine inside the
sandbox with no host access; `false` disables container access entirely.

## Use another project

Build the image in this repository first:

```sh
make base
```

Put this repository's `bin/` directory on your `PATH`, or invoke
`bin/opencode-container` by its absolute path. Then run it from your project:

```sh
cd /path/to/your-project
opencode-container
```

Without `.opencode-sandbox.json`, the launcher warns and uses the local
`opencode2:latest` image. It mounts the current directory, starts
`opencode2 --standalone`, and saves state in a project-specific volume.
It cannot build a missing image without a `build` block. The default `containers`
mode applies there too, so that image must carry the nested engine; see
[nested mode](SANDBOX.md#nested-mode).

For project settings, create `.opencode-sandbox.json` in that directory:

```json
{
  "schema_version": 1,
  "image": "opencode2:latest",
  "containers": "nested"
}
```

Add [credentials](CREDENTIALS.md) for the models you use. Agents follow the
session model until you map them in `aid.json`; see
[model routing](#model-routing).
The bundled `opencode2-sandbox` skill can also help configure a workspace.
Run the resulting launcher from the host after leaving the container.

## Model routing

The ai-devtools plugin runs each agent on the session model unless `aid.json`
routes it. Put `.opencode/aid.json` in the workspace for project routes. The
launcher separately mounts the host's
`${XDG_CONFIG_HOME:-$HOME/.config}/opencode2/aid.json` read-only at
`/opt/opencode/config/opencode/aid.json` whenever that file exists, whether or
not the workspace has one. aid uses the workspace file instead of the global one
when both are present; it never merges them, so a project file replaces the
global routes entirely. Edit either file on the host and restart the launcher;
routing needs no image rebuild.

```json
{
  "profiles": {
    "reasoning": { "model": "deepseek/deepseek-v4-pro", "reasoning_effort": "high" },
    "implementation": { "model": "deepseek/deepseek-v4-pro#max" },
    "research": { "model": "openai/gpt-5.6-luna" }
  },
  "agents": {
    "aid-orchestrator": "reasoning",
    "aid-mechanic": "implementation",
    "aid-explorer": "research",
    "aid-oracle": "inherit"
  },
  "hide_free_models": true
}
```

`model` is `provider/model` with an optional `#variant`; use `reasoning_effort`
instead of a variant, not both. `agents` maps any agent id to a profile, project
agents included; `"inherit"`, or omitting the agent, leaves it on the session
model. `aid` validates the file and reports an invalid one loudly. Keep
credentials out of it; see [credentials](CREDENTIALS.md).

A leftover top-level `model_router` block in `.opencode-sandbox.json` stops the
launch: move its routes to `.opencode/aid.json`. A leftover
`opencode2/model-router.json` is no longer read and only warns.

## Commands

| Command | Purpose |
| --- | --- |
| `opencode-container` | Start OpenCode using this project's settings |
| `opencode-container run [CMD...]` | Run a command, or the configured default |
| `opencode-container shell` | Open Bash |
| `opencode-container build` | Build the configured image if needed |
| `opencode-container build --force` | Rebuild the configured image |
| `opencode-container secrets add NAME` | Save a provider key and enable it for this project |
| `opencode-container secrets remove NAME` | Delete a provider key and remove this project's selection |

`run`, `shell`, and `build` also accept `--image NAME`. See
[custom image builds](DEVELOPMENT.md#build-images).

## Common problems

| Problem | What to check |
| --- | --- |
| Image is missing | Run `make build` here, or configure a local `build` block in the target project. |
| No container socket is available in host mode | Start `podman.socket`; or drop the `containers` line for the default nested mode; or set it to `false` for no container access. |
| Launch refuses because nested mode is not possible | The default needs 65,536 subordinate uids and gids for your user, and an image labelled `io.opencode.nested-podman=1`. Set `containers` to `"host"` (host socket) or `false`. See [nested mode](SANDBOX.md#nested-mode). |
| Local model cannot connect | Check the server's address and port from inside the container. See [local providers](LOCAL_PROVIDERS.md). |
| Agent uses an unexpected model | Check `.opencode/aid.json` and the global `opencode2/aid.json`. An unmapped agent uses the session model. |
| Sessions disappeared after moving a project | The default volume name depends on the launch directory. See [persistence](SANDBOX.md#saved-state). |
| Git worktree fails to launch | Repair stale or malformed `.git` pointers; the launcher validates linked-worktree metadata. |
