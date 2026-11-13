# Sandbox configuration

[Back to README](../README.md)

The launcher reads `.opencode-sandbox.json` from the directory where you run it.
Start small and add only the settings you need:

```json
{
  "schema_version": 1,
  "image": "opencode2:latest",
  "workspace": ".",
  "containers": "nested",
  "seccomp": null,
  "env": { "set": { "TZ": "UTC" } }
}
```

Restart the launcher after changing settings. The
[full example](../examples/opencode-sandbox.json.example) shows more options;
adapt its secret selections and volume name before using it.

## Settings reference

| Setting | Purpose and default |
| --- | --- |
| `schema_version` | Use `1`. |
| `image` | Image to run; defaults to `opencode2:latest`. |
| `build` | Optional local build: `containerfile`, `context`, and string-valued `args`. The Containerfile defaults to `Containerfile`; context defaults to its directory. |
| `workspace` | Directory to mount; defaults to the launch directory. |
| `workdir` | Starting directory inside the workspace. Relative paths are supported. |
| `containers` | Container-engine access: `"nested"` (the default; a rootless engine inside the sandbox), `"host"` (mount the host's Podman/Docker socket), or `false` (no engine). `true` is an alias for `"nested"`. See [container access](#container-access). |
| `mounts` | Extra mounts with `source` and optional `target`. A relative source resolves against the workspace; an omitted target mirrors the absolute source path. A target may not shadow a baked config/plugin path or a container system path; see [environment boundaries](#environment-boundaries). |
| `env.pass` | Host variable names to forward when set. |
| `env.set` | Literal environment values. |
| `provider_secrets` | Keys this project can use; see [credentials](CREDENTIALS.md). |
| `persistence.data_volume` | Override the project's saved-state volume. An empty string disables persistence. |
| `network` | Optional Podman network mode, such as `slirp4netns`. |
| `capabilities` | Extra capabilities, drawn from a fixed allow-list. See [capabilities](#capabilities). |
| `seccomp` | Main-sandbox syscall policy. Missing or `null` keeps the container engine default; `"unconfined"` disables seccomp filtering; any other nonempty string is a profile path resolved from this config's directory. |
| `runtime_args` | Only `--add-host=`, `--pids-limit=`, and `--ulimit=` entries are allowed. |
| `command` | Defaults to `["opencode2", "--standalone"]`. The image built
  from this repository ships its own `CMD` with `--auto`; see
  [permission posture](#permission-posture). |

Model routing is not a sandbox setting. The ai-devtools plugin reads
`.opencode/aid.json` from the workspace, or the global file the launcher mounts;
see [model routing](SETUP.md#model-routing). The global file's `guards` and
`sensitive` rules are not a sandbox boundary: an in-container agent can write
`.opencode/aid.json` in the workspace, which aid prefers over the read-only
global file.

## Files and Git

The workspace is mounted at its canonical host path in every container-access
mode, and that path is the container's working directory. Relative `workdir`
values resolve beneath it, and an absolute `workdir` must stay inside it. Keeping
the host path means bind mounts the nested or host engine resolves name the same
host directory.

Linked Git worktrees are supported: the launcher validates the `.git` pointer
and mounts the external Git common directory at its original host path. Because
the workspace and that metadata keep their host paths, a submodule's relative
`.git` pointer into the worktree's metadata resolves inside the sandbox too.

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

## Container access

`containers` selects how the sandbox reaches a container engine:

| Value | What the sandbox gets |
| --- | --- |
| `"nested"` (default) | A rootless Podman engine inside the sandbox, with its own image store and no host socket. |
| `"host"` | The host's Podman or Docker socket, mounted into the sandbox. |
| `false` | No engine. |

Nested is the default because it gives the agent containers without handing it
the host's engine: the default exposes as little of the host as the feature
allows. `true` is an alias for `"nested"`. The base image ships an engine, so
every mode works without a derived image; in host mode its `podman` follows the
socket the launcher mounts, and in nested mode it uses the engine's local store.

### Nested mode

This is the default. Leave `containers` out, or set it to `"nested"` (or
`true`). The sandbox runs its own rootless Podman engine: the agent gets an image
store, network namespace, and cgroup subtree inside the container, and the
launcher mounts no host socket. Inner containers can mount only what the sandbox
already has — the workspace and your `mounts` — so host paths outside the sandbox
stay unreachable.

The price is a looser outer profile than `false`:

- `CAP_SYS_ADMIN` in the container's bounding set, which setuid `newuidmap`
  needs to write an inner user namespace's id map.
- `--security-opt systempaths=unconfined`, so `/proc/sys` is not masked
  (crun writes `ping_group_range`).
- `/dev/net/tun`, for pasta networking.
- The container starts as `--user 0`; the image's entrypoint prelude delegates
  the cgroup and then drops to the image user for good.

Nested mode exposes strictly less of the host than host mode, and strictly more
kernel surface than `false`.

Every root start also verifies the image's privilege policy before the container
gets its root: the only setuid files may be the engine's `newuidmap` and
`newgidmap`, and no file may carry file capabilities. A derived image that
installs packages after its build is refused, with the offending files named,
because each extra setuid file or file capability is a path to container root.
The check costs about 0.1 s per start: the setuid listing about 0.04 s and the
capability sweep about 0.05 s. Both walk the whole image filesystem from `/`
on its own device; a capability set on a file outside `/usr`, `/opt` or `/etc`
is caught too. The image ships `getcap`, and an image without it refuses the
start rather than skip the sweep. The launcher also pins the entrypoint at
`/usr/local/libexec/opencode-entrypoint` by absolute path, so a derived image's
own `ENTRYPOINT` cannot skip the prelude.

`systempaths=unconfined` is the masking change with a residual surface: it
unmasks `/proc/kcore`, `/proc/keys`, `/proc/timer_list`, and `/sys/firmware`.
In testing the agent uid still cannot read `/proc/kcore` or `/proc/timer_list`
(root-only, mode 0400), and the DMI and ACPI table files stay root-only;
`/proc/keys` lists only the caller's own keyrings, and the readable
`/sys/firmware` files are firmware and memory-map metadata. That residual
exposure is accepted rather than worked around.

Every launch checks what nested mode needs, whether the setting is explicit or
not, and refuses to start when something is missing:

- At least 65,536 subordinate uids **and** 65,536 subordinate gids for your host
  user (`/etc/subuid` and `/etc/subgid`, or NSS). The image's id ranges are sized
  to the sandbox's own 65,536-id user namespace. Otherwise:

  ```text
  opencode-container: containers "nested" (the default) needs at least 65536 subordinate uids for host user <user>, found <n> (see /etc/subuid); the inner engine's id ranges would not fit in the sandbox's user namespace. To launch anyway, set "containers": false in .opencode-sandbox.json for no engine, or "containers": "host" to mount the host engine socket (which exposes the host engine to the sandbox)
  ```

  The gid check reports the same with `subordinate gids` and `/etc/subgid`.
- An image carrying `io.opencode.nested-podman="1"`, which is what
  `NESTED_PODMAN=1` (the `Containerfile` default) labels. Derived images inherit
  the label; an engine-free image built with `NESTED_PODMAN=0` carries `0`.
  Otherwise:

  ```text
  opencode-container: containers "nested" (the default) needs an image built with the nested Podman engine, but image <image> does not carry io.opencode.nested-podman="1"; rebuild it with NESTED_PODMAN=1 (the Containerfile default). To launch anyway, set "containers": false in .opencode-sandbox.json for no engine, or "containers": "host" to mount the host engine socket (which exposes the host engine to the sandbox)
  ```
- An image carrying `io.opencode.agent-env="1"`, which every image built from
  this `Containerfile` labels and derived images inherit. An image that predates
  project-environment delivery would run the agent without its configured
  variables, so it is refused:

  ```text
  opencode-container: containers "nested" (the default) needs an image that delivers the project environment after its root start, but image <image> does not carry io.opencode.agent-env="1": the image predates project-env delivery; rebuild it. To launch anyway, set "containers": false in .opencode-sandbox.json for no engine, or "containers": "host" to mount the host engine socket (which exposes the host engine to the sandbox)
  ```
- cgroup v2 with controllers delegated to the sandbox. The container refuses to
  start when it is missing:

  ```text
  opencode-entrypoint: cgroup v2 is not mounted at /sys/fs/cgroup; the nested engine needs a private cgroup v2 namespace
  opencode-entrypoint: /sys/fs/cgroup is not writable; a root start is for the launcher's nested mode (--cap-add SYS_ADMIN --security-opt systempaths=unconfined); start as the image user otherwise
  ```

  A controller the host withholds is reported at start
  (`opencode-entrypoint: warning: the host did not delegate the memory cgroup
  controller; inner containers cannot be limited by it`) and the matching limit
  fails in the inner runtime rather than being silently ignored.

Root starts are reserved for nested mode. An image whose `USER` is root — for
example a derived image that ends with `USER root` — therefore refuses to start
in `false` and `"host"` mode too, with the
`/sys/fs/cgroup is not writable` line above: those modes grant no writable
cgroup. End a derived image as its unprivileged user.

So a host that cannot do nested refuses every launch, explicit or not, until the
setting says `false` or `"host"`.

The inner engine's graph root is a per-project named volume,
`opencode2-containers-<cwd-hash>`, mounted at `/var/lib/opencode-containers`.
Inner images and containers persist there across sessions. Removing that volume
on the host starts the inner engine empty; an extra mount may not target the
path. Inner `podman` can pull or build its own images when the sandbox has
network access, and the host's image store is not visible.

`--memory` and `--cpus` on inner containers are enforced: the prelude hands the
cpu, memory, and pids controllers to the image user before dropping privileges.

Project environment values take a separate route in this mode. The prelude
starts as root before the drop, so `env.pass` and `env.set` values are not passed
with `-e`: the launcher writes them to a launcher-owned 0600 file and mounts it
read-only at `/run/opencode/agent-env`, and the image's loader applies them to the
agent process after the drop, one NUL-separated `KEY=VALUE` entry at a time. The
launcher runs Podman as a child and waits for it instead of replacing itself, so
that file is removed when the run ends; `SIGINT` reaches Podman through the
terminal's foreground process group, and `SIGTERM`/`SIGHUP` aimed at the launcher
alone are forwarded to it. Provider secrets selected in `provider_secrets` keep
their fixed launcher-owned names and stay ordinary process environment, injected
by the engine.

The saved-state volume is mounted without `:U` in nested mode. The container
starts as root, so `:U` would chown the volume to root; the image's user-owned
mount point is copied up instead. A volume that already exists but is not
writable by the image user stops the launch, naming the uid, the volume, and the
fix:

```text
opencode-entrypoint: the OpenCode data mount /var/lib/opencode-data is not writable by uid <uid> (volume <name>): run 'podman volume rm <name>' to start fresh, or set its ownership to <uid>:<gid> once
```

### Host mode

Set `containers` to `"host"`. The launcher prefers a rootless Podman
socket, then a rootless or system Docker socket. For user Podman:

```sh
systemctl --user start podman.socket
```

The socket is mounted at `/run/opencode-container-engine.sock`.
The launcher sets `CONTAINER_HOST` and `DOCKER_HOST` automatically. The
workspace is already mounted at its canonical host path (see
[Files and Git](#files-and-git)), so nested bind mounts using `$PWD` resolve
correctly. Extra mounts cannot overlap the workspace, and the workdir must stay
inside it.

**In host mode, socket access lets the sandbox control the host container
engine**, including starting containers and bind-mounting any host path the host
user can reach. Choose it only for trusted projects and images; the default
nested mode needs no socket.

If connections fail, check for a stale socket or stopped service. Discovery
checks file access, not the engine API. Sockets requiring a supplementary host
group need a Podman OCI runtime that supports `--group-add keep-groups`.

## Environment boundaries

The launcher runs with `--rm`, `--init`, `--userns=keep-id`, `--pull=never`,
and `--security-opt label=disable`. It adds an interactive TTY only when needed.
The default [nested mode](#nested-mode) adds `--user 0`, `--cap-add SYS_ADMIN`,
`systempaths=unconfined`, and `/dev/net/tun`; host mode adds the socket mount.

`seccomp` changes syscall filtering for the main sandbox only. Omit it or set it
to `null` to preserve the container engine's default profile. Set it to
`"unconfined"` to pass `--security-opt seccomp=unconfined`, which disables that
filter and materially weakens the sandbox. Any other nonempty string names a
custom regular-file profile; relative paths resolve from the directory where the
launcher was invoked (the directory containing `.opencode-sandbox.json`), and
the launcher passes Podman the profile's absolute canonical path. Empty values,
wrong types, missing paths, and directories are rejected.

This field requires a launcher version that supports it. Older launchers do not
reject unknown top-level fields and may silently ignore `seccomp`, leaving the
engine default in effect. It is deliberately separate from `runtime_args`, whose
security-option filtering remains unchanged.

The baked OpenCode config and plugin directories stay root-owned. Project
`opencode.json` files and external Claude/agent skill scans are disabled.
The standalone server exits with the foreground CLI.

Host OpenCode application state and `.agents`, `.claude`, and `.mcp` directories
cannot be mounted, including through parent directories. Mounts cannot overlap
baked config or plugin paths, and in every mode a mount target may not be
`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, or `/usr/local/libexec`:
shadowing one of them would replace the interpreter, the startup script, or the
runtime beneath it before the sandbox's command runs. `/sys`, `/proc`, `/dev`,
`/var/run`, `/var/lock`, and `/var/lib/rpm` are reserved too: the first three
are the kernel's own filesystems, and the last three are paths the image
resolves into `/run` or `/usr`. The workspace target follows the same rules: it
may not be at or contain a baked config/plugin path, a container system path, the
managed engine socket, the OpenCode data mount, or the nested engine's storage
volume. Explicit environment settings cannot override
`HOME`, `PATH`, `XDG_*`, or `OPENCODE_*`, nor bash's readonly variables
(`SHELLOPTS`, `BASHOPTS`, `BASH_VERSINFO`, `UID`, `EUID`, `PPID`), which the
image's loader could not export.

The supported user-config mounts are individual files: the model-routing
`aid.json`, local providers, and Git config. Treat sandbox configuration as
trusted repository code because it controls additional mounts and runtime
settings.

### Capabilities

`capabilities` is an allow-list, not a passthrough. A capability is accepted only
if it cannot bypass file permission checks, change a uid or gid, or act on
processes or kernel state outside the agent's own. The allow-list is:

`NET_RAW`, `NET_BIND_SERVICE`, `NET_ADMIN`, `NET_BROADCAST`, `SYS_NICE`,
`IPC_LOCK`, `SYS_RESOURCE`.

`NET_ADMIN` and `NET_RAW` reach only the sandbox's own network namespace. Every
other name stops the launch, reporting the rule it would break:

```text
opencode-container: capabilities: SYS_PTRACE is not allowed: it can inspect and control other processes
```

An unrecognised name is refused with the allow-list. Entries are normalised
before matching: case does not matter and an optional `CAP_` prefix is dropped,
so `net_raw` and `CAP_NET_RAW` both mean `NET_RAW`. The policy applies in every
mode; a refused capability stops the launch instead of being granted.

In nested mode the container starts as root, and the entrypoint carries exactly
the configured set across the drop: `setpriv` starts from `-all` and adds each
configured capability to the inheritable and ambient sets, so the agent keeps the
configured set and never the pre-drop process's own. The launcher always passes
the list to the prelude, empty when nothing is configured, so an image `ENV`
cannot supply one; the prelude checks every name against its own copy of this
allow-list and refuses the start otherwise.

Debugging needs no capability here. ptrace of the agent's own processes works
without `SYS_PTRACE`: in the image this repository builds, `strace` traces a
command it starts and `lldb -p <pid>` attaches, with no capabilities in the
effective set. If `lldb` cannot launch a debuggee at all, the error is
`personality set failed`: the engine's default seccomp profile blocks
`personality(2)` (which LLDB uses to disable ASLR), and the `seccomp` setting
above — or LLDB's own ASLR setting — is what changes it, not a capability.

### Permission posture

OpenCode auto-approves every permission that is not explicitly denied when the
CLI runs with `--auto` (`opencode2 --help`: "Auto-approve permissions that are
not explicitly denied"). Two independent settings decide whether a session has
it:

- The image `CMD`, which this repository ships as
  `["opencode2", "--standalone", "--auto"]`. It applies to a bare `podman run`
  of the image. The launcher always passes a command explicitly, so the image
  `CMD` does not reach a launcher session.
- The `command` setting. Its launcher default is `["opencode2", "--standalone"]`
  with no `--auto`; a project that names `--auto` in `command` runs its sessions
  with auto-approval.

What auto-approval changes, and what it does not:

- An explicit `deny` still wins, so it grants nothing beyond what an agent
  already denies. `aid-explorer` keeps its `edit` deny — no edit tool, though its
  shell can still write files.
- Every baked `aid-*` agent opens with an allow rule covering every action and
  resource (`*`/`*`). The only exceptions are `ask` on reading `.env` files,
  `deny` on the browser, and `aid-explorer`'s `edit` deny; there are no `shell`,
  `grep`, or `git` ask rules. Shell commands, `grep`, and `git` therefore run
  without a prompt. Under `--auto`, any `effect: ask` an agent adds also stops
  prompting: the enforceable boundaries are `deny` rules and what the container
  mounts.

The `Containerfile` command is pinned by a policy guard in
`tests/image-config.test.py`, so changing that flag set is a deliberate edit
rather than drift.
