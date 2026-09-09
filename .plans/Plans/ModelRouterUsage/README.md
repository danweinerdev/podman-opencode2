---
title: "Model Router Usage Reporting"
type: plan
status: active
created: 2026-09-09
updated: 2026-09-09
tags: [model-router, usage, implementation]
related:
  - Specs/ModelRouterUsage/README.md
  - Designs/ModelRouterUsage/README.md
  - Research/model-router-usage-source.md
phases:
  - id: 1
    title: "01-Collector-Core"
    status: planned
    doc: "01-Collector-Core.md"
  - id: 2
    title: "02-Supervisor-Image"
    status: planned
    doc: "02-Supervisor-Image.md"
  - id: 3
    title: "03-Reporting"
    status: planned
    doc: "03-Reporting.md"
  - id: 4
    title: "04-Integration"
    status: planned
    doc: "04-Integration.md"
  - id: 5
    title: "05-Release-Gates"
    status: planned
    doc: "05-Release-Gates.md"
waivers: []
---

# Model Router Usage Reporting

## Overview
Implement the visual routing inspector approved in [Model Router Usage Reporting (spec)](../../Specs/ModelRouterUsage/README.md) exactly as architected in [the design](../../Designs/ModelRouterUsage/README.md): an in-container supervisor replaces the default launch command, a separate collector polls the verified message/session API into a dedicated SQLite store in the project data volume, and a host-side `opencode-container stats` subcommand renders the terminal routing view. Every node is one red→green cycle gated by named tests; the design's release gates (integration suite, TUI smoke, AC-11 review) are terminal gate nodes, so the plan cannot complete without them. Runtime compatibility evidence comes from the [research probes](../../Research/model-router-usage-source.md); no paid model calls and no direct SQLite access to OpenCode storage anywhere in this plan (D-0001).

## Non-Goals
- No pricing, costs, billing, budgets, or quotas (spec Non-Goals).
- No browser or local-web UI surface; terminal rendering only (design DD-8).
- No changes to model-router routing behavior, the pinned runtime version, or the plugin/SDK API surface (spec Non-Goals).
- No reading or writing of OpenCode's own database or any runtime storage; the only runtime touch is the documented HTTP API, and all SQLite access stays in maintained application code (D-0001).
- No cross-project aggregation, hosted telemetry, CSV export, or general analytics dashboard (spec Non-Goals).
- No collection for user-overridden launch commands; such sessions are excluded via the control journal, not wrapped (design DD-9/DD-10).
- No session-level temporal signal for the default agent's free UI-selected model; those calls remain `unverified` in this plan (design DD-7, D-0002).
- Session-level model override (FR-03) and per-message model override (FR-08) — retired in the 2026-09-07 scope revision — are out of scope for this plan.

## Architecture
Implementation follows the design's component layout one-for-one; the design is the single source of truth for contracts, and this plan does not restate them.

- `bin/oc2-standalone` (new, Python stdlib): in-container supervisor — candidate port allocation, owned `opencode2 serve`, health wait, collector start, TUI via `--server`, ordered teardown, run identity (design DD-2, DD-3).
- `bin/oc2-usage` (new, Python stdlib): `collect` (bounded polling, exclusion-aware import predicate, config-version provenance, WAL store) and `report` (read-only renderer with `--calls`/`--call`), plus the internal control-journal operations the launcher invokes (design DD-4, DD-7, DD-10, DD-12).
- `bin/opencode-container` (changed): `stats` subcommand, `usage_stats` config validation, transport env injection, pre-launch control registration (design DD-10, DD-12).
- `Containerfile` (changed): bake the two new files, swap `CMD` (design DD-3, DD-11).
- `tests/` (new + extended): per-concern unit tests and podman integration probes writing metadata-only evidence to `.plans/Research/evidence/model-router-usage/` (design Testing Strategy).

## Key Decisions
Interview-resolved (2026-09-09, recorded in `.graph/interview.json`):

- **L scope** — multi-component feature; ≤ 3 interview waves, ≤ 5 questions each.
- **Per-concern integration probes (D-0006)** — one focused podman probe script per concern (store contents, lifecycle/controls, provenance, faults, host-network concurrency), each run from the host against a fresh named volume, writing evidence JSON; no aggregate runner in v1.
- **Scripted-checklist TUI smoke (D-0007)** — a plan node with a scripted checklist (launch as image CMD, drive one dispatch, exit cleanly) whose saved TUI log plus assertion summary is the evidence artifact; no pty automation.
- **Repo-built images (D-0005)** — integration and gate tests build via `make build` and run against the repository's own `opencode2-dev:latest`, testing the shipping form.
- **Gates in-graph** — the integration suite, TUI smoke, host-network concurrency test, and AC-11 repository review are terminal gate nodes; the plan cannot complete without them.

Governing ledger entries: code-only SQLite access (D-0001); config-version provenance for route verification (D-0002); ephemeral-mode registration rule (D-0003); pytest unit-test runner (D-0004); release-gate evidence on repo-built images (D-0005); per-concern integration probes (D-0006); scripted-checklist TUI smoke (D-0007).

## Dependencies
- Approved spec and design (related artifacts); research probes already demonstrate the pinned runtime's read path (spec Constraints).
- Repository image build: `make build` (base `opencode2:latest` + dev `opencode2-dev:latest`) with the new baked files; pinned runtime `opencode2 v0.0.0-beta-19234`.
- Host: working Podman (named volumes, local engine), Python 3.10+, `ruff==0.16.6` and `mypy==1.18.2` per `requirements.txt`.
- Deterministic mock providers: existing `tests/mock-model-router-provider.py` (OpenAI-compatible on 18081, local-provider adapter on 18082) and `tests/model-router-dispatch-driver.py`.

## Plan Completion Evidence
Pending — not complete.
<!-- graph-view:begin — generated section, do not edit -->

## Graph View

<!-- GENERATED VIEW — source of truth: ModelRouterUsage-Graph.json. Regenerate with `sdd compile --plan ModelRouterUsage`. Edits here are overwritten. -->

| Phase | Nodes | Doc |
|---|---|---|
| 1: 01-Collector-Core | 7 | `01-Collector-Core.md` |
| 2: 02-Supervisor-Image | 3 | `02-Supervisor-Image.md` |
| 3: 03-Reporting | 3 | `03-Reporting.md` |
| 4: 04-Integration | 6 | `04-Integration.md` |
| 5: 05-Release-Gates | 4 | `05-Release-Gates.md` |

23 node(s) total. The committed graph (`ModelRouterUsage-Graph.json`) is the source of
truth; these documents are projections.

<!-- graph-view:end -->
