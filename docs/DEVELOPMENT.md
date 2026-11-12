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
only where both are present and otherwise prints a skip line and exits 0. It is
deliberately not one of the broker's checks — the verify worker image ships no
engine, and a check the worker cannot run must be reported as unavailable rather
than passed.

## Verify checks in a disposable worker

`bin/oc2-verify` runs any of the checks above inside a throwaway container, so a
check can gather evidence without any possibility of changing the checkout.
Build the worker image first; it is independent of the runtime images:

```sh
make verify-image          # builds localhost/opencode2-verify:latest
bin/oc2-verify --list      # names configured in .opencode-sandbox.json
bin/oc2-verify --check lint
bin/oc2-verify -- ruff check .
```

The workspace is mounted read-only at `/src`, copied to `/tmp/w`, and the command
runs against the copy; the container starts `--rm`, so its writable layer is
discarded on exit. Only the workspace is ever mounted — no engine socket, no other
host paths — and the environment is a fixed minimal set, so provider credentials
held by the agent container do not reach workspace code. Container *creation* is
serialized under a lock because nested engine calls race; runs themselves proceed
in parallel, and four concurrent checks complete in about 0.7s each.

The one exception is a linked worktree whose Git common directory is a `.bare`
enclosing it (`<root>/.bare` plus checkouts such as `<root>/main` or
`<root>/feat/x`). Its `.bare` is also mounted read-only and copied into scratch
beside the checkout, with both worktree pointers rewritten in the copy, so Git
works in the worker without following a host path. The `.git` pointer alone
cannot choose that mount: it must resolve to `.bare/worktrees/<name>` under the
checkout's own root with no symlinks in the chain, and that metadata must point
back at this checkout. Any other `.git` file is refused.

Configuration lives in the `verify` block of `.opencode-sandbox.json`: `image`,
`network` (default `none`, so a check needing the network must opt in),
`timeout_seconds`, `memory`, `cpus`, and `checks`. Point `image` at a
project-supplied image whenever the shared one cannot run that project's tests —
a worker missing the toolchain reports a clean run while having executed nothing,
which is the failure mode this tool exists to avoid. `--json` returns
`status`, `exit_code`, `duration_seconds`, and `output` for citation in findings.

Which copy of that file is read depends on where the broker runs. The base image
carries a root-owned sentinel, `/.opencode-sandbox`. When it is present the broker
reads only the launcher's read-only mount at `/run/opencode/sandbox.json` (or
defaults, when the project has no config and nothing is mounted); it never reads
the workspace's `.opencode-sandbox.json`, which the agent can edit, and takes no
environment override. On the host there is no sentinel, so it reads
`./.opencode-sandbox.json` from the repository root and the named checks work
without the launcher.

The broker stays baked and reads the sandbox config, but no agent is granted it
by name: a caller, including a baked agent, runs it as an ordinary shell
command. The baked aid-* agents allow every action except a few `.env` reads
(ask), the browser (deny), and `aid-explorer`'s edit deny, so that command runs
without a prompt. The default nested engine is the one it talks to, so a run is
inert only when `containers` is `false`; with no engine it exits 125.

### The worker image in nested mode

This repository sets `"containers": "host"` so the broker can use the host's
worker image. In the default nested mode the engine has its own store, so the
broker cannot see the host's copy and will not pull one. Seed it through the
workspace, the only writable path the host and the sandbox share. On the host,
from the repository root:

```sh
make verify-image
podman save -o .verify-image.tar localhost/opencode2-verify:latest
```

Inside the sandbox, load it into the inner store and remove the archive:

```sh
podman load -i /src/.verify-image.tar
rm /src/.verify-image.tar
/opt/opencode/sandbox/oc2-verify --check lint
```

The inner store keeps it in `opencode2-containers-<cwd-hash>`, so this is a
one-time step per project volume, not per session. A rebuilt worker image needs
a fresh save and load. The archive is as large as the worker image, so keep it
out of the repository and delete it after loading.

To rebuild it in the sandbox instead, `podman build -f Containerfile.verify .`
works there when the sandbox has network access; it lands in the same store.

## Known gaps

Open decisions and follow-ups, kept here until they are taken up:

- **Review gates have no consumer.** The launcher still validates `review-gates`
  in project `review-*` agent files and reports their contributions, but nothing
  in the image reads them since the model router was removed. Undecided: teach
  ai-devtools' review stage an optional key, read it from a baked sandbox skill,
  or keep validation only.
- **The verify image is not seeded into the nested store.** In nested mode the
  broker's worker image must be loaded by hand (see
  [The worker image in nested mode](#the-worker-image-in-nested-mode)). Planned:
  export it at `make verify-image` and load it from a read-only tarball. Until
  then this repository's own `.opencode-sandbox.json` uses `"containers": "host"`.
- **`oc2-verify` does not check that its limits applied.** It should fail closed
  when the worker's `memory.max` / `cpu.max` don't match the requested
  `--memory` / `--cpus`. Held while the broker's future is undecided.
