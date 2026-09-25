---
name: oc2-verify-and-agents
description: Add and check custom review agents in a project's agents directory, and run workspace verification through the oc2-verify broker, from inside this sandbox. Use when writing a project review lane, naming review gates, granting a lane the verification broker, running lint/test/type checks in a disposable worker, or debugging a launch that refuses its agents. Not for launcher, mount, or routing setup, which opencode2-sandbox covers.
---

# Custom review agents and the verification broker

Everything here is the behavior of the shipped image; you do not need to read
`bin/oc2-verify` or the launcher to use it.

## Where things are

| What | Where |
| --- | --- |
| Verification broker | `/opt/opencode/sandbox/oc2-verify` (**not on `PATH`**; call it by full path) |
| Sandbox config, as the broker reads it | `/run/opencode/sandbox.json` (read-only mount of the project's `.opencode-sandbox.json`) |
| Project agent definitions, in the container | `/opt/opencode/config/opencode/agents` (read-only mount of the project's `.agents`) |
| Image agents | `/opt/opencode/config/opencode/agent` |
| Disposable worker image | `localhost/opencode2-verify:latest` (`make verify-image` on the host) |
| Deeper reference | `docs/SANDBOX.md`, `docs/DEVELOPMENT.md`, `docs/MODEL_ROUTER.md` |

## Running a check

```sh
/opt/opencode/sandbox/oc2-verify --list                 # configured check names
/opt/opencode/sandbox/oc2-verify --check lint           # one named check
/opt/opencode/sandbox/oc2-verify -- ruff check .        # any command, argv is preserved
/opt/opencode/sandbox/oc2-verify --json --check plugin  # machine-readable result
/opt/opencode/sandbox/oc2-verify --timeout 900 --check types
```

Run it from the repository root: the broker refuses when no `.git` is under the
working directory. `--check` takes a name from `verify.checks`; a trailing command
after `--` is passed as argv, so arguments with spaces or metacharacters arrive as
one entry and are not re-parsed. The two forms are mutually exclusive.

Exit codes tell you what happened:

| Code | Meaning |
| --- | --- |
| the command's own code | the run completed; a missing tool shows as `127`, never as a pass |
| `2` | usage or config error (nothing started) |
| `124` | wall-clock timeout; partial output is still reported |
| `125` | the worker could not be started: image not local, engine failure, lock dir unwritable |

`--json` prints `status`, `exit_code`, `duration_seconds`, `image`, `network`,
`command`, `output_truncated`, and `output`. Without it, output goes to stdout and
one summary line `[oc2-verify] ok exit=0 3.42s image=…` goes to stderr.

## What a worker can and cannot see

The broker owns this decision and a caller cannot widen it:

- The workspace enters read-only and is copied into the worker's own throwaway
  layer (`cp -a`); the command runs against the copy, so nothing it does can
  change your checkout. A very large tree therefore costs copy time on every run.
- The worker starts `--rm` from a pinned image with `--pull=never`; nothing is
  fetched at run time and its rootfs is discarded on exit.
- No host path other than the workspace is mounted, and no container-engine socket
  is ever mounted. A worker cannot mount host paths.
- Environment is fixed: `PATH`, `HOME=/tmp/w`, `TMPDIR`, `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_TERMINAL_PROMPT=0`, plus `LANG`, `LC_ALL`, `TZ`. Provider keys, session
  tokens, and OpenCode runtime variables are not forwarded, because the worker runs
  repository code.
- Default `network` is `none`. Set `verify.network` to `slirp4netns` or `pasta` for
  a check that must reach the network, such as `npx --yes …`.
- Worker resources are `memory` (default `2g`), `cpus` (default `2`),
  `timeout_seconds` (default `300`, max `3600`), and `output_cap_bytes` (default
  200000; the truncation is reported, not hidden).

Worker toolchain: Fedora with `python3` + `pip`, pinned `ruff` and `mypy` from
`requirements.txt`, `nodejs24` + `npm`, and `git`, `jq`, `tar`, `which`, `findutils`,
`diffutils`, `procps-ng`. The image build asserts `git jq tar node npm python3 ruff
mypy` are present. No Rust, Go, or GCC, and no container client: a check that needs
a toolchain the image lacks fails with `127`, and you should report that as "this
check cannot run here", not as a pass or a code defect.

## Configuring checks

The `verify` block lives in `.opencode-sandbox.json`; the broker reads the mounted
copy, so a config edit needs only a launcher restart. Allowed keys are `image`,
`timeout_seconds`, `memory`, `cpus`, `network`, `checks`, `output_cap_bytes`. An
unknown key or a malformed value stops the run with exit `2` rather than falling
back to defaults, because a silent default would run checks in the wrong toolchain
and report success.

```json
{
  "schema_version": 1,
  "verify": {
    "image": "localhost/opencode2-verify:latest",
    "network": "none",
    "timeout_seconds": 600,
    "memory": "2g",
    "cpus": "2",
    "checks": {
      "lint": "ruff check .",
      "types": "mypy .",
      "compile": "python3 -m py_compile bin/opencode-container bin/oc2-verify",
      "config": "jq empty container-config.json container-config.schema.json examples/*.example",
      "launcher": "./tests/launcher-env.test.py",
      "plugin": "cd plugins/model-router && node --test"
    }
  }
}
```

A named check is one shell string, parsed once inside the worker. Keep each check
to a single command; chain with `&&` inside the string if you must. Build the
worker image on the host with `make verify-image` (override `VERIFY_IMAGE` to
rename it, and point `verify.image` at the new name). If the image is not already
in local storage, `oc2-verify` refuses with exit `125`; it never pulls.

## Writing a custom review agent

Put one Markdown file per agent in the project's `.agents` directory (or point
`agents` in the sandbox config at another directory; `"agents": false` disables the
pickup). The launcher mounts it read-only and the runtime registers each file as an
agent whose id is the filename without `.md`.

Rules the launcher enforces at launch, by refusing to start:

- Ids are letters, digits, `.`/`_`/`-`; entries are regular `.md` files with no
  subdirectories. A nested path would create an id the router rejects, which
  disables routing for every agent, so it is refused instead.
- A reserved id is refused: the eleven image agents (`orchestrator`, `engineer`,
  `reasoner`, `extractor`, `bulk-researcher`, `bounded-editor`, `implementer`,
  `review-plan-drift`, `review-quality`, `review-spec-compliance`,
  `review-blind-spots`) and OpenCode's own (`build`, `plan`, `general`, `explore`,
  `compaction`, `title`, `summary`). A same-id file would replace the image agent's
  prompt and append its own permission rules after the deny-first set, silently
  widening a read-only lane.
- At most 64 definitions. Hidden `.md` files are skipped with a warning.
- Each file must open with a `---` fence that is later closed. Without a fence the
  runtime registers an unnamed *primary* agent whose prompt is its own frontmatter.

Name it `review-*`. Only that prefix is dispatchable: the primary agents carry
`{ action: subagent, resource: "review-*", effect: allow }` after their deny-all
rule. Any other name still registers, but the primary will not launch it as a
worker, and the launcher warns that it sits outside the convention.

### Template

```markdown
---
description: Checks changes against this project's architecture decisions.
mode: subagent
review-gates: [review_quality, review_blind_spots]
permissions:
  - { action: "*", resource: "*", effect: deny }
  - { action: read, resource: "*", effect: allow }
  - { action: glob, resource: "*", effect: allow }
  - { action: list, resource: "*", effect: allow }
  - { action: grep, resource: "*", effect: ask }
  - { action: "search_*", resource: "*", effect: allow }
  - { action: shell, resource: "git --no-pager diff --no-ext-diff --no-textconv *", effect: ask }
  - { action: shell, resource: "/opt/opencode/sandbox/oc2-verify *", effect: allow }
---
You are the `review-adr` lane for this project: check the diff under review
against the accepted decisions in `docs/adr/`. Report only what you observed, with
file and line. Do not edit files or broaden the lane.
```

The deny-all-first order is not decoration: the later matching rule wins, which is
why the shipped lanes open with the deny-all and put every grant after it. A rule
written before the deny-all has no effect. Keep `oc2-verify` allowed and
`grep`/`git diff` at `ask` to mirror the shipped lanes.

Give the lane a model with a `model_router.agents` entry — `model_router.agents`
maps any agent id to either a shared `models` name or an inline
`{"model": "provider/model-id", "variant": "…"}`. See the `opencode2-sandbox` skill
for layering. A routed id that never registers logs one warning and leaves every
agent un-routed, so check the filename against the entry.

### Review gates

`review-gates` is a list drawn from exactly `review_plan_drift`, `review_quality`,
`review_spec_compliance`, `review_blind_spots`. The vocabulary is closed by the
review tooling, so any other name is treated as a typo and refused at launch.

- `[]` means "advisory only": findings are reported, attached to no gate.
- A `review-*` file that omits the key warns, because its findings would otherwise
  be dropped at synthesis.
- A contribution merges **into** the named gate's evidence, which must still
  include the standard lane's own run and must name the contributing reviewer. A
  contribution never becomes a gate of its own, and a project cannot lower what a
  gate requires or substitute its own reviewer for a required lane.

One known quirk, recorded in `docs/UPSTREAM_ISSUES.md`: any unknown frontmatter key
makes the runtime copy the raw frontmatter into that agent's `request.body`. On the
shipped runtime that object never reaches a provider, so `review-gates` is safe
today; re-read that file before trusting body params on a newer runtime.

## Checking that your agent works

1. Launch from the host and read the launcher's two lines:
   `workspace agents: 1 definition(s) from /path/.agents` and
   `review contributions: review-adr -> review_quality, review_blind_spots`.
   A refusal here is the fastest signal you get; it names the file and the rule.
2. Inside the container, confirm the mount: `ls /opt/opencode/config/opencode/agents`.
3. Ask the primary to dispatch it, e.g. "dispatch review-adr over the working
   tree". A denial means the name does not match `review-*`.
4. `/opt/opencode/sandbox/oc2-verify --list` confirms which checks the lane can
   run, and `/opt/opencode/sandbox/oc2-verify --check <name>` confirms the worker
   image and toolchain are present before you rely on either in a review.

The deep check, if a lane behaves oddly: start a private server and read the agent
registry.

```sh
opencode2 serve --hostname 127.0.0.1 --port 14901 --print-logs --log-level debug &
# the server password is printed in the log; then
curl -s -u "opencode:PASSWORD" http://127.0.0.1:14901/api/agent | jq -r '.data[].id'
```

The agent list is built lazily, so retry it a few times before concluding the agent
is missing. `opencode2 api` does not work in a launcher session: there is no
background service to talk to, and it fails with "Server process exited".

## Limits worth knowing before you design around them

- A project cannot add a *skill* at run time: skill roots are baked into the image
  and project config is disabled. A derived image adds one with
  `COPY my-skill /opt/opencode/plugins/model-router/skills/` (see
  `docs/DEVELOPMENT.md`, "Extend the runtime").
- Agent definitions enter the container as a read-only mount, and the launcher
  validates the whole set at launch. Restart the container after editing a
  definition rather than expecting a mid-session reload; a lane cannot rewrite its
  own permissions from inside.
- A lane cannot create a fifth review gate, cannot become a gate, and cannot widen
  an image agent. Those are enforced, not conventional.
- The broker is the only sanctioned way for a lane to execute project commands.
  Do not add a mount or engine access to get around it.
