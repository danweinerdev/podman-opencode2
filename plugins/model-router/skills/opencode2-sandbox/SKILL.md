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
   image, workspace, routing, and command defaults. Create
   `.opencode-sandbox.json` from the baked template only when the user needs
   build settings, mounts, environment forwarding, or routing overrides. If the
   launcher itself is absent, copy it into the workspace with file tools; a
   convenient name is `opencode-container.sh`.
3. Customize only fields the user needs. Keep `schema_version: 1`, use the
   `opencode2 --standalone` command, and retain the exact pinned
   `OPENCODE2_VERSION` unless the image source has been deliberately upgraded.
4. Validate JSON with `jq empty .opencode-sandbox.json` and shell syntax with
   `bash -n opencode-container.sh`. Build or run the image only when requested.

## Sandbox fields

- `image` is optional and defaults to `opencode2:latest`.
- `build` is optional. `containerfile` and `context` resolve from the host
  workspace; `args` are passed as Podman build arguments.
- `workspace` defaults to the host workspace root and is mounted at `/src`.
- Relative `workdir` values resolve below `/src`.
- `mounts` entries contain `source`, optional `target`, and optional
  `read_only`. Relative sources resolve from the configured workspace. Do not
  mount host OpenCode state from effective XDG directories, `.agents`,
  `.claude`, or `.mcp` state, and do not target `/etc/opencode`,
  `/opt/opencode`, or `/opt/mcp`.
- `env.pass` forwards a named host variable only when set. `env.set` provides a
  literal value. Never place provider secrets in `env.set`; supported provider
  API keys are forwarded automatically when present on the host.
- `model_router` shallow-merges partial `profiles`, `agents`, and an optional
  `default_agent` over the baked routing defaults.
- `network`, `capabilities`, and the restricted `runtime_args` list control the
  Podman sandbox. Allowed runtime arguments are `--add-host=`, `--pids-limit=`,
  and `--ulimit=` forms only.
- `command` defaults to `["opencode2", "--standalone"]`.

Treat `.opencode-sandbox.json` as executable project tooling: it controls image
builds, mounts, environment values, capabilities, networking, and commands.
Keep secrets out of the file and report every security-relevant change.
