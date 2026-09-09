---
title: "Decision Ledger"
type: decision-log
status: active
created: 2026-09-09
updated: 2026-09-09
tags: [decisions]
related: []
waivers: []
decisions:
  - id: D-0001
    kind: decision
    status: accepted
    date: 2026-09-07
    decided_by: user
    statement: "All SQLite access in this repository — collection, queries, aggregation, export, migrations, recovery, and maintenance — is implemented in maintained application code; skills and LLM agents may only invoke documented CLI commands with validated arguments and never open SQLite files, issue SQL, use database tools directly, or generate ad hoc database scripts."
    rejected:
      - "Wrapping direct SQL in an LLM skill"
      - "Ad hoc LLM-generated scripts against the usage or OpenCode databases"
    rationale: "Explicit user requirement from the Model Router Usage scope; the boundary extends to both the telemetry database and any proposed read access to OpenCode storage."
    confirmation: "AC-11 repository review: every SQLite access path is in maintained application modules (bin/oc2-usage, the launcher stats path); no skill or agent prompt instructs direct database access; the reporting CLI accepts no raw SQL."
    scope:
      - Specs/ModelRouterUsage/README.md
      - Designs/ModelRouterUsage/README.md
    tags: [model-router, usage, sqlite, security]
    reversibility: two-way
  - id: D-0002
    kind: decision
    status: accepted
    date: 2026-09-09
    decided_by: user
    statement: "Route verification requires verified config-version provenance: a call's expected model is the model-router route resolved from the plugin's own config sources (baked base defaults plus the launcher-mounted override files named by OPENCODE_MODEL_ROUTER_GLOBAL_CONFIG / OPENCODE_MODEL_ROUTER_CONFIG, merged with the plugin's mergeConfig semantics), applied only when the agent is mapped and pinned per the plugin's pin rules and every existing override file's mtime is at or before the producing run's start; every failure case reports unverified."
    rejected:
      - "Approximating expectations from the current session model (F-01 race)"
      - "Judging stored calls against later configuration"
      - "All-unverified v1 with no provenance source"
    rationale: "User-directed reopening of adversarial review F-01: the plugin resolves routes once per process from inspectable read-only-mounted sources, so config-version provenance gives pinned routes a verifiable expected model while everything else degrades to FR-12-compliant unverified."
    confirmation: "Provenance end-to-end release gate in the plan (route resolution, mtime comparison, between-run config change flips status without relabeling stored calls) and DD-7 in the design."
    scope:
      - Designs/ModelRouterUsage/README.md
      - Specs/ModelRouterUsage/README.md
    tags: [model-router, usage, provenance, fr-12]
    reversibility: two-way
  - id: D-0003
    kind: decision
    status: accepted
    date: 2026-09-09
    decided_by: user
    statement: "In ephemeral mode (persistence.data_volume empty) the launcher performs no host-side control registration, launches proceed unconditionally, and only the in-container supervisor keeps a container-local control file for its own collector, because no durable shared state exists to protect and no interval could later be backfilled."
    rejected:
      - "Refusing ephemeral custom/disabled launches when no journal volume exists (design as originally written)"
    rationale: "The durable control journal exists to protect shared state across launches; with no volume there is nothing to protect, so registration is a pure launch blocker with no safety benefit."
    confirmation: "DD-10 ephemeral rule in the design; plan test asserting an ephemeral custom-command launch succeeds."
    scope:
      - Designs/ModelRouterUsage/README.md
    tags: [model-router, usage, ephemeral, controls]
    reversibility: two-way
  - id: D-0004
    kind: decision
    status: accepted
    date: 2026-09-09
    decided_by: user-approved
    statement: "Unit tests for the Model Router Usage feature are pytest tests under tests/unit/, and requirements.txt pins pytest as a dev-side dependency; make check runs ruff and mypy, and make test runs the pytest suite."
    rejected: ["Plain assertion-script convention (the repo's probe style) for unit tests", Stdlib unittest discover]
    rationale: "User's plan-interview choice; pytest is host/dev-side only and never shipped in the image, so DD-11's stdlib-only stance for shipped code is unaffected."
    scope: [Plans/ModelRouterUsage/README.md]
    tags: [testing, tooling, model-router, usage]
    reversibility: two-way
  - id: D-0005
    kind: decision
    status: accepted
    date: 2026-09-09
    decided_by: user-approved
    statement: "Release-gate evidence for the Model Router Usage feature (every podman integration probe plus the TUI smoke) is produced exclusively on images built by make build from this repository (opencode2, opencode2-dev) — the shipping form — and never on an external probe image with bind-mounted stand-ins."
    rejected: [Running integration tests against the ark-services-opencode2-dev probe image with the new files bind-mounted]
    rationale: "User's plan-interview choice; release-gate evidence must reflect the shipping form that the design's release gates run on the pin."
    scope: [Plans/ModelRouterUsage/README.md, Designs/ModelRouterUsage/README.md]
    tags: [testing, integration, model-router, usage]
    reversibility: two-way
  - id: D-0006
    kind: decision
    status: accepted
    date: 2026-09-09
    decided_by: user-approved
    statement: "The podman-based integration tests for the Model Router Usage feature are one focused probe script per concern (store contents, lifecycle/controls, provenance, faults, host-network concurrency), each run from the host via podman against a fresh named volume and writing metadata-only evidence JSON under .plans/Research/evidence/model-router-usage/."
    rejected: [A single monolithic integration script, An aggregate make runner for the integration probes]
    rationale: "User's plan-interview choice, following the existing probe convention established in the research phase."
    scope: [Plans/ModelRouterUsage/README.md]
    tags: [testing, integration, model-router, usage]
    reversibility: two-way
  - id: D-0007
    kind: decision
    status: accepted
    date: 2026-09-09
    decided_by: user-approved
    statement: "The TUI interactive-fidelity release gate is verified by a scripted checklist node that launches the image exactly as its CMD, drives one dispatch, and records the saved TUI log plus an assertion summary as evidence; no pty-based TUI automation is introduced."
    rejected: [Fully scripted pty-driven TUI automation, Deferring the TUI gate to a post-plan release task]
    rationale: "User's plan-interview choice; pty automation of the TUI is flaky, and the design specifies a scripted smoke with recorded log evidence."
    scope: [Plans/ModelRouterUsage/README.md]
    tags: [testing, tui, model-router, usage]
    reversibility: two-way
---





# Decision Ledger

Machine-readable record of decided truths that outlive the document they were made in — design choices, concept definitions, and answered design questions that constrain work elsewhere. Choices a spec, design, or plan already states in full stay in that artifact. The frontmatter `decisions[]` array is canonical; see `shared/decision-log.md` in the plugin for the admission test, entry schema, lifecycle rules, and collision procedure.

Entries are append-only: an accepted entry is never edited except to mark it superseded. A change of mind is a new entry that supersedes the old one.
