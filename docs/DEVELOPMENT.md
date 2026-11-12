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

The first command reuses an existing image unless that image records a
local-provider checksum and a present catalog differs from it. An image built by
`make` records no checksum and is used as built, with a warning. `--force`
rebuilds either way. Catalog contents never enter the build; see
[local providers](LOCAL_PROVIDERS.md).

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

The base image ships the rootless Podman engine that the default `containers`
mode needs. `NESTED_PODMAN` defaults to `1`; `0` builds an engine-free image,
which is smaller but cannot run the default mode, so a project using it must set
`containers` to `false` or `"host"`:

```sh
podman build --build-arg NESTED_PODMAN=0 \
  --build-arg "USER_UID=$(id -u)" --build-arg "USER_GID=$(id -g)" \
  --build-arg "USERNAME=$(id -un)" -t opencode2:latest -f Containerfile .
```

Either way the image labels the result `io.opencode.nested-podman`, and the
launcher reads that label before allowing the default mode; derived images
inherit it.

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

That example installs `podman-remote`, Python tooling, and `make`, then re-runs
the setuid policy. It bakes no `CONTAINER_HOST`/`DOCKER_HOST`: the launcher sets
both in host mode, and a baked value would point the nested engine's `podman` at
a socket that does not exist. Its `podman` is the base image's full engine, which
also follows `CONTAINER_HOST`; only an engine-free base (`NESTED_PODMAN=0`) gets
a `podman` symlink to `podman-remote`.

The base image uses separate Go and Rust builder stages for ai-devtools and the
MCP servers, keeping those build toolchains out of the runtime. The final Fedora
image includes the pinned OpenCode2 CLI, Node, LLDB, and runtime utilities.
See [Containerfile](../Containerfile) for dependency versions and source refs.

## Runtime layout

| Path | Contents |
| --- | --- |
| `/opt/mcp/bin/` | `code-graph-mcp`, `debug-mcp`, `search-mcp`, and `aid`, all on `PATH` |
| `/opt/opencode/plugins/` | code-graph, debug, search, the ai-devtools plugin, and the sandbox skills |
| `/opt/opencode/sandbox/` | Launcher and sandbox setup templates |
| `/opt/opencode/config/opencode/agent/` | Root-owned agent directory; the ai-devtools plugin registers its agents at load time |
| `/opt/opencode/config/opencode/command/` | Code-graph slash commands |
| `/opt/opencode/config/opencode/opencode.json` | Optional local-provider catalog mount |
| `/etc/opencode/container-config.json` | Baked config selected by `OPENCODE_CONFIG` |
| `/etc/opencode/container-config.schema.json` | Schema for the pinned container config |
| `/usr/local/libexec/` | `opencode-entrypoint` (the image `ENTRYPOINT`), `opencode-agent-env` (the loader that applies the agent environment file after the drop), and `opencode-setuid-policy` |
| `/etc/opencode-nested-podman` | `1` when the image ships the nested Podman engine, `0` otherwise |
| `/var/lib/opencode-containers` | Mount point of the nested engine's per-project store volume; `/etc/containers/storage.conf` puts the rootless store inside it |

### Image scripts

[`image/opencode-entrypoint`](../image/opencode-entrypoint) is the image
`ENTRYPOINT`. Started as the image user, it only `exec`s the command. Started as
uid 0 — the nested mode launcher's `--user 0` — it verifies the image's
setuid/file-capability policy, delegates the cgroup controllers to the image
user, repoints `HOME`/`USER`/`LOGNAME`, hands over the terminal, checks that the
data volume is writable by that user, and drops with
`setpriv --inh-caps=-all,+<cap> --ambient-caps=-all,+<cap>`: exactly the
capabilities the launcher configured, never a remainder of the pre-drop
process's own. After the drop it runs
[`image/opencode-agent-env`](../image/opencode-agent-env), which applies the
launcher's agent environment file and `exec`s the command. Any failure there
stops the container, because a nested engine whose resource limits are silently
ignored is worse than none. A root start without the prelude is debugging only:
`podman run --rm -it --user 0 --entrypoint '' opencode2:latest bash`.

[`image/opencode-setuid-policy`](../image/opencode-setuid-policy) clears every
setuid/setgid bit on the root filesystem, then sets setuid on `newuidmap` and
`newgidmap` only when `NESTED_PODMAN=1` (the inner engine's id maps need it), and
fails the build if any other setuid file remains. So `sudo`, `su`, `passwd`, and
`mount` are not paths to container root. A derived image that installs packages
must re-run the policy with the value in `/etc/opencode-nested-podman`;
[Containerfile.dev](../Containerfile.dev) does.

The global provider filename must be `opencode.json`; an arbitrary
`provider.json` fragment is not loaded. For ai-devtools internals, read the
upstream `.opencode-plugin/README.md` at the pinned commit
(<https://github.com/danweinerdev/ai-devtools>).

## Run checks

From the repository root, check the launcher and config:

```sh
python3 -m py_compile bin/opencode-container
tests/launcher-env.test.py
tests/image-config.test.py
jq empty container-config.json container-config.schema.json examples/*.example
npx --yes ajv-cli@5 validate -s container-config.schema.json \
  -d container-config.json --spec=draft2020
```

The launcher suite uses mocked Podman commands. `tests/image-config.test.py`
checks the baked config and pins the image `CMD` and `ENTRYPOINT`, the
`NESTED_PODMAN` build argument and its label, the setuid policy in both images,
and that no `CONTAINER_HOST`/`DOCKER_HOST` is baked into the dev image.

`tests/nested-smoke.test.py` is the live counterpart: it drives the real
launcher against a real Podman and an image built with `make build`, so it runs
only where both are present and otherwise prints a skip line and exits 0.
