# Setup

[Back to README](../README.md)

## Use this repository

Follow the [quick start](../README.md#quick-start). `make build` builds the base
image first, then the development image selected by this repository's sandbox
config. Image builds need network access to download dependencies.

The launcher requires Python 3.10+, Podman, Git, and `id` on the host.
The checked-in config also enables host container access; start a user Podman
socket or set `containers` to `false` before launching.

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
It cannot build a missing image without a `build` block.

For project settings, create `.opencode-sandbox.json` in that directory:

```json
{
  "schema_version": 1,
  "image": "opencode2:latest",
  "containers": false
}
```

Add [credentials](CREDENTIALS.md) for the models you use. The default worker
routes use OpenAI and DeepSeek; change them with the [model router](MODEL_ROUTER.md).
The bundled `opencode2-sandbox` skill can also help configure a workspace.
Run the resulting launcher from the host after leaving the container.

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
| No container socket is available | Start `podman.socket`, or disable `containers` if you don't need host container access. |
| Local model cannot connect | Check the server's address and port from inside the container. See [local providers](LOCAL_PROVIDERS.md). |
| Agent uses an unexpected model | Check global and project routes. The default agent follows the UI model unless pinned. |
| Sessions disappeared after moving a project | The default volume name depends on the launch directory. See [persistence](SANDBOX.md#saved-state). |
| Git worktree fails to launch | Repair stale or malformed `.git` pointers; the launcher validates linked-worktree metadata. |
