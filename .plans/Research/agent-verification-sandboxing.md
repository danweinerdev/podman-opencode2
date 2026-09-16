---
title: Agent Verification Sandboxing and Per-Agent Namespaces
type: research
status: active
created: 2026-09-16
updated: 2026-09-16
tags: [permissions, review-lanes, sandboxing, podman, verification, namespaces]
related: []
---

# Agent Verification Sandboxing and Per-Agent Namespaces

## Context

**Primary driver (corrected after the first draft of this document):** review agents have
been changing the repository while they run. The lanes are declared read-only, yet the shared
working tree gets dirtied during a review. The user's interest in podman is *not* about
read-only shares — it is about giving **each agent its own disposable domain** that is thrown
away afterwards, because a domain you intend to throw away can be granted far more freedom
than this container currently grants anyone.

So the primitive under investigation is: **per-agent ephemeral domains, mutually invisible,
discarded on completion.** Read-only verification is merely its degenerate case (a domain with
no writable share). Framing it as "read-only review lanes" was the wrong emphasis and led the
first draft to over-weight the test-execution gap.

Secondary, still-real driver: the lanes (`review-quality`, `review-spec-compliance`,
`review-plan-drift`, `review-blind-spots`) cannot run tests, so they report claims about
behavior that nothing has executed. A disposable domain solves this for free, since a writable
scratch copy can run anything.

Two questions follow:

1. Where is the enforcement point that lets an agent run arbitrary commands while making its
   effects structurally unable to reach the shared tree or another agent's domain — generically,
   across languages and lanes, without per-repo maintenance?
2. How much of the current tool surface is built on state that lives *in the shared tree*, and
   therefore has to move before per-agent domains are workable at all? (See finding 14: the
   answer is "some of it, concretely.")

As-of date for all findings: **2026-09-16**, probed inside this repository's dev container
(`localhost/opencode2-dev:latest`, runtime `crun`, OpenCode CLI 1.18.31). Probes performed no
host writes, no paid model calls, and left the working tree unmodified.

## Findings

### Key Insights

| # | Question | Observed result | Provenance |
| --- | --- | --- | --- |
| 1 | Can a `shell` allowlist grant test-running and stay read-only? | **No.** Running a project's tests executes the code under review (`conftest.py`, `Makefile` targets, npm lifecycle scripts). `pytest *` grants whatever the suite does. Docs give the same warning independently: "prefer a narrow shell allowlist instead of patterns intended to recognize every dangerous command." | docs: `opencode.ai/v2/docs/permissions/`; inferred |
| 2 | Is lane shell access workspace-scoped today? | **No.** `containers: true` mounts the host engine socket at `/run/opencode-container-engine.sock`; `podman info` from inside returns the host runtime (`crun`, graphroot `/cache/daniel/containers/storage`). Shell in a lane is host-engine authority. | ran |
| 3 | Kernel-enforced read-only without podman? | **Yes.** `unshare -Urm` + bind + `remount,ro,bind`: reads succeed, `touch` fails `Read-only file system`, tree unchanged. `max_user_namespaces` = 2147483647. `/usr/sbin/unshare` present; `bwrap` absent. | ran |
| 4 | Copy-on-write in place (nested overlayfs)? | **No.** `fsconfig() failed: overlay: filesystem on /tmp/opencode/ovl/upper not supported as upperdir`. Upperdir on the container's own overlay rootfs is unsupported, so "writable but invisible" is unavailable in-place. | ran |
| 5 | Sibling container holding a read-only share? | **Yes.** `-v "$PWD:/w:ro"`: reads OK, `git status` matches the outer view, `touch` → EROFS, named volume writable, no leak. Warm start **0.21s**. | ran |
| 6 | Full copy-on-write variant? | **Yes, cheap.** Repo ro at `/src` + `cp -a /src /tmp/w` (95 MB): total **0.81s**, writes into the copy succeed, `/src` write → EROFS, host tree clean. | ran |
| 7 | Can nested containers start concurrently? | **No.** 4 concurrent starts: 1 pass, 3 fail — `crun: write to /proc/sys/net/ipv4/ping_group_range ... Invalid argument`. `--network=none` does not avoid it. With `flock` around the start: **4/4 pass.** | ran |
| 8 | Can a lane mount an arbitrary host path read-write? | **Yes.** A sibling mounting `$HOME/.config` reported `rw` in `/proc/mounts` with 101 entries visible. No write was performed. | ran |
| 9 | Are bind sources container-visible? | **No.** Sources resolve on the host daemon: `-v /tmp/opencode/scratch:/scratch` failed `statfs ... no such file or directory`. Scratch must be a named volume or a host-visible path. | ran |
| 10 | Does the image have a test toolchain? | **No** for this image: `python3 -m pytest` → `No module named pytest`. A verification container generally needs the *project's* image. | ran |
| 11 | Are the plugin hooks usable at this pin? | **Unproven.** The 1.18.31 binary contains `create.before` / `execute.before` (2 occurrences each) but not `permission.evaluate`. The pinned `@opencode-ai/plugin` is **1.18.25**, whose `v2/promise` and `v2/effect` contexts declare only `options, agent, aisdk, catalog, command, integration, plugin, reference, skill` — no `tool`, `shell`, `permission`, or `session`. | ran; hook→domain mapping inferred |
| 12 | Does a plugin-defined tool get its own permission action? | **Unverified.** Docs state only `<server>_<tool>` (resource `*`) for MCP tools and that "plugins may define more actions." | docs; gap explicit |
| 13 | Can a lane perform a tracked-file edit under current config? | **No.** Across all four lane files: the only non-denied actions are `execute`, `search_*`, `code-graph_*`, `read`, `glob`, `list`, and a narrow read-only `shell` allowlist. No `edit`/`write`/`patch`, no `debug_*`, no `sdd_*`. So a tracked-file edit appearing during a review would be a permission bypass, not a configuration effect. | ran (grep of all four lane files) |
| 14 | Is there a permitted in-tree write path that needs no shell? | **Yes.** `code-graph_*` is allowed with resource `*`, and code-graph keeps a daemon in the working tree: `.code-graph/daemon.json`, `daemon.lock`, `daemon.sock` (mode 0700 dir, 0600 files), written at 2026-09-16T07:20:07Z. `daemon.json` records `pid`, `endpoint`, `binary_sha`, and an `owner` `{pid, start_time, nonce}` — a single-owner lock model. | ran |
| 15 | Does `git status` dirty the index? | **Not observed here.** `.git/index` sha256 identical before and after `git status --porcelain` (index was already fresh). An index refresh *can* write when stat data is stale, so treat this as "not triggered in the tested state," not "cannot happen." | ran; caveat inferred |
| 16 | Which tool-state dirs show up as repo dirt? | `.code-graph/`, `.mypy_cache/`, `.ruff_cache/` are gitignored; **`.pytest_cache/` is not** (present, untracked, not matched by `.gitignore`), so it surfaces in `git status` as an untracked change to the repo. | ran |

### Load-bearing conclusions

- **The boundary must move from command text to the mount set.** If isolation comes from what
  is mounted rather than from what a command is called, then arbitrary command strings are
  safe and no test-runner allowlist needs maintaining. That is the genericity the task asked for.
- **The container engine socket cannot sit inside the boundary it is meant to enforce.** It
  grants read-write mounts of arbitrary host paths (finding 8). So the socket belongs to a
  broker, never to a lane. This is what separates "podman for the lanes" from "podman for the
  verification capability."
- **Sibling containers beat `unshare` on one axis that matters**: they provide a writable,
  throwaway filesystem (finding 6), which in-place `unshare` cannot because nested overlay is
  unavailable (finding 4). The cost is the start race (finding 7) and socket authority.
- **Throwing the domain away changes what has to be trusted.** With disposal, "no changes reach
  the repo" stops being a property you enforce per-command and becomes a property of the
  lifecycle. What remains trusted is narrowed to exactly two things: **what is mounted into the
  domain** (its reachable authority) and **what is promoted out of it** (the artifact path). The
  promotion gate is the only place damage can still be done, so it is where the design effort
  belongs — not in command filtering.
- **More freedom means the domain must not hold the engine socket.** This is the tension in the
  plan: a disposable domain is attractive precisely because you can grant it more, but the host
  engine socket (findings 2, 8) turns any amount of granted freedom into unconstrained host
  authority. Freedom scales only as fast as the domain's reach stays bounded by its mount set.
- **Starting domains from inside the sandbox does not scale to N agents.** The observed
  concurrent nested-start failure rate was 3 of 4 (finding 7), and N disposable domains *is* the
  concurrent case — 4 lanes plus a worker is exactly where it breaks. `flock` fixes it, but it
  serializes the very parallelism that justifies per-agent domains. This makes **placement** the
  first architectural decision, not an implementation detail.
- **Some current tool state cannot survive per-agent domains as configured.** code-graph's daemon
  is single-owner and its lock/socket/manifest live in the shared tree (finding 14). N isolated
  domains either share that daemon through a mount — which reintroduces the shared mutable state
  disposal was meant to remove — or need their own, which requires the state to move out of the
  working tree first. Any other in-tree, lock-owning tool has the same problem.

### Reproduction

```sh
# 3. in-container read-only bind (no new packages)
mkdir -p /tmp/opencode/ro-probe/mnt
unshare -Urm --propagation private sh -c '
  mount --bind "$REPO" /tmp/opencode/ro-probe/mnt
  mount -o remount,ro,bind /tmp/opencode/ro-probe/mnt
  touch /tmp/opencode/ro-probe/mnt/X'        # -> Read-only file system

# 5/6. sibling container, read-only share + copy-on-write
time podman run --rm --pull=never --userns=keep-id --security-opt label=disable \
  -v "$PWD:/src:ro" --entrypoint sh localhost/opencode2-dev:latest -c \
  'cp -a /src /tmp/w && cd /tmp/w && touch /tmp/w/P && touch /src/L'   # only /src fails

# 7. concurrency race, and its mitigation
for i in 1 2 3 4; do ( podman run --rm ... -c 'echo lane-ok' ) & done; wait   # 1/4 pass
for i in 1 2 3 4; do ( flock 9; podman run --rm ... -c 'echo lane-ok' ) 9>/tmp/opencode/locks/nested.lock & done; wait   # 4/4 pass
```

### Sources

- `opencode.ai/v2/docs/permissions/`, `/v2/docs/agents/`, `/v2/docs/tools/`,
  `/v2/docs/build/plugins/` — as of 2026-09-16. Ordered rules, last match wins; `shell`
  resource is the scanner-produced command string and compound commands split; explicit
  configured `deny` is final; `permission.hook("evaluate")` may set `effect` for `allow`/`ask`
  decisions; `shell.hook("create.before")` mutates `command`/`cwd`/`shell`/`env`;
  `tool.hook("execute.before")` inspects or replaces tool input.
- `plugins/model-router/agents/review-*.md` — lanes open with `{ action: "*", resource: "*",
  effect: deny }`, then narrow allows; `grep` and several `git` reads are `effect: ask`.
- `bin/opencode-container:988-995` — in `containers` mode the container workspace is set to
  the canonical **host** path so nested `$PWD`-based mounts resolve on the host daemon;
  `:1119-1138` mounts the discovered engine socket.
- `tests/usage-stats-standalone-probe.py` — existing precedent for `-v "$PWD:/workspace:ro"`.
- `/opt/opencode/plugins/model-router/node_modules/@opencode-ai/plugin/package.json` (1.18.25)
  and `dist/v2/{promise,effect}/context.d.ts`.
- Runtime: `/usr/local/lib/node_modules/@opencode-ai/cli/bin/opencode2.exe` (1.18.31).

## Analysis

### Implications

1. **Option D (add test-runner patterns to the four agent files) is not an answer.** It grants
   write authority through the suite under review. Rejected as a boundary.
2. **Any lane that can reach the engine socket is already unconfined**, so "read-only review"
   is currently a prompt convention, not an enforced property. Isolation work has a
   prerequisite: lanes denied `shell`, and a decision about whether `containers: true` should
   apply to sessions that may launch review lanes.
3. **The broker becomes the new trusted computing base.** Everything reduces to "the broker
   never mounts a host path read-write," which belongs behind assertions in the style of
   `tests/launcher-env.test.py`'s `has_line` checks, not behind a comment.
4. **The MCP-server shape avoids finding 11 entirely** — no plugin hooks needed, and the lanes
   already permit `<server>_*` actions for `search`, `code-graph`, and `debug`. This is the
   fourth server in an established pattern.
5. **The per-agent-namespace direction is the same mechanism, generalized — but only partly.**
   Filesystem isolation transfers directly. Namespace *invisibility between agents* also needs
   pid, `/tmp`, IPC, ports, env, and MCP/session state considered, none of which this research
   touched. Two candidate substrates: (a) in-container `unshare` user+mount namespaces — cheap,
   no engine, authority bounded by the container, but no copy-on-write; (b) one sibling
   container per agent — strong isolation and CoW, but subject to finding 7 and to the fact
   that containers started from inside the sandbox inherit the socket-authority problem.
6. **Finding 7 hints at a third substrate worth its own probe**: start lane containers from the
   *launcher* (host side) instead of nesting them inside the sandbox. That likely removes the
   `ping_group_range` race and removes socket delegation from the lane entirely. Untested.

### Recommendations

1. **Spike the verification broker** as an MCP server exposing `verify_run`: `-v "$PWD:/src:ro"`,
   copy to `/tmp/w`, `--rm`, `--pull=never`, `--userns=keep-id`, hard timeout, `--memory`/`--cpus`,
   `flock`-serialized starts, returning exit code plus capped stdout/stderr. Wire `verify_*`
   into one lane and have a review cite a real test result. Smallest step that proves the model.
2. **Deny `shell` in the review lanes as a prerequisite**, and keep the read-only property
   structural rather than textual.
3. **Decide verification-container policy explicitly**: network on/off, resource ceiling, image
   source and pull trust, scratch lifecycle (`--rm` reclaims the rootfs; named volumes persist
   and would let test state drift between runs).
4. **Investigate per-agent namespaces as a separate research pass**, comparing the three
   substrates above, and specifically test whether launcher-managed lane containers avoid the
   nested-start race.
5. **Latent bug worth fixing independently**: several lane rules are `effect: ask` (`git status`,
   `git --no-pager diff/log`). A background lane has no client to answer a prompt, so those
   reads may stall or silently fail. Either allow them or verify what a background child does on
   `ask`.

## Open Questions

- Do `ctx.shell.hook` / `ctx.permission.hook` / `ctx.tool.transform` actually fire from a
  plugin at 1.18.31, given the 1.18.25 type gap (finding 11)? A ~15-minute logging spike settles it.
- Does a plugin-registered tool derive its own permission action (finding 12)?
- Which image does a verification container run — the project's own, or a baked one? How is pull
  trust decided for images this repo did not build?
- Does the nested-start race disappear for host-side (launcher-managed) containers?
- What is the CoW copy cost for a large monorepo? Measured 95 MB / 0.81s here; unmeasured elsewhere.
- For per-agent namespaces: what exactly must be invisible between agents beyond the filesystem
  (pid, `/tmp`, IPC, ports, MCP state, session files), and does `--userns=keep-id` vs a dedicated
  per-agent userns change that?
- Should a lane ever be able to *promote* something it produced in its scratch namespace (a
  suggested fix, a reproducer file) into a real artifact, and through what gate?
- Does `containers: true` belong in a config used by sessions that may run review lanes at all?
