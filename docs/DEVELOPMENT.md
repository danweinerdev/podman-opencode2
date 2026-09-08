# Development

[Back to README](../README.md)

## Build images

Run from this repository:

```sh
make build  # base image, then development image
make base   # only opencode2:latest
make dev    # only opencode2-dev:latest; base must already exist
```

These are plain Podman builds with the host UID, GID, and username passed as
build arguments. The launcher can also build the image selected by a project's
`build` block:

```sh
./bin/opencode-container build
./bin/opencode-container build --force
```

The first command reuses an existing image unless a present local-provider
catalog has a different checksum. `--force` rebuilds it. Catalog contents never
enter the build; see [local providers](LOCAL_PROVIDERS.md).

To choose a fully qualified image name, omit the tag:

```sh
make build IMAGE=quay.io/example/opencode2
./bin/opencode-container build --image quay.io/example/custom
./bin/opencode-container run --image quay.io/example/custom
```

Both forms tag the selected build with `latest` and an eight-character Git
commit hash. The launcher runs the hash-tagged image and requires a build
context with at least one commit. `--image` changes the name, not the configured
Containerfile. `make build IMAGE=...` names the base image and builds the dev
image on top; `DEV_IMAGE` can override the dev image's name.

## Extend the runtime

```dockerfile
FROM opencode2:latest
USER root
# Install project tools or copy additional skills here.
USER dev
```

Replace `dev` with the username used to build your base image. Child images
inherit the tools, plugins, config, and environment settings. Use
[Containerfile.dev](../Containerfile.dev) as a working example.

The base image uses separate Go and Rust builder stages for SDD and the MCP
servers, keeping those build toolchains out of the runtime. The final Fedora
image includes the pinned OpenCode2 CLI, Node, LLDB, and runtime utilities.
See [Containerfile](../Containerfile) for dependency versions and source refs.

## Runtime layout

| Path | Contents |
| --- | --- |
| `/opt/mcp/bin/` | `code-graph-mcp`, `debug-mcp`, and `sdd`, all on `PATH` |
| `/opt/opencode/plugins/` | code-graph, debug, SDD, and model-router assets |
| `/opt/opencode/sandbox/` | Launcher and sandbox setup templates |
| `/opt/opencode/config/opencode/agent/` | Native agent definitions |
| `/opt/opencode/config/opencode/command/` | Code-graph slash commands |
| `/opt/opencode/config/opencode/opencode.json` | Optional local-provider catalog mount |
| `/etc/opencode/container-config.json` | Baked config selected by `OPENCODE_CONFIG` |
| `/etc/opencode/container-config.schema.json` | Schema for the pinned preview config |

The global provider filename must be `opencode.json`; an arbitrary
`provider.json` fragment is not loaded. For router internals, see the
[plugin reference](../plugins/model-router/README.md).

## Run checks

From the repository root, check the launcher and config:

```sh
python3 -m py_compile bin/opencode-container
tests/launcher-env.test.py
jq empty container-config.json container-config.schema.json examples/*.example
npx --yes ajv-cli@5 validate -s container-config.schema.json \
  -d container-config.json --spec=draft2020
```

Run the model-router tests in its own directory:

```sh
cd plugins/model-router
npm install
npm test
```

The launcher suite uses mocked Podman commands. Router tests cover model
assignment, config merging, validation, and v2 plugin behavior.

For a Podman integration check, return to the repository root and run:

```sh
tests/model-router-dispatch.test.py
```

This checks router overrides and native dispatch using containers; inspect the
test's prerequisites before running it.
