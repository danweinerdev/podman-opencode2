---
title: "05-Release-Gates"
type: phase
plan: "ModelRouterUsage"
phase: 5
status: planned
created: 2026-09-09
updated: 2026-09-09
deliverable: "Graph view: 4 node(s) under phase label 05-Release-Gates"
tasks: []
---

# Phase 5: 05-Release-Gates

<!-- GENERATED VIEW — source of truth: ModelRouterUsage-Graph.json. Regenerate with `sdd compile --plan ModelRouterUsage`. Edits here are overwritten. -->

## Overview

Rendered view of 4 node(s) from the plan graph (schema v1, seq 0).
Observations shown are raw records; completion-grade closure derives from
full review gates and is never stored or hand-edited here.

## Nodes

### structural-gate

- Contract: make check (ruff 0.16.6 plus mypy 1.18.2) and make test (the full pytest unit suite) pass over every file this plan adds or changes with zero violations.
- Justifies: `NFR-04`
- Depends on: `store-schema`, `controls-journal`, `route-provenance`, `import-predicate`, `polling-engine`, `supervisor`, `launcher-usage-stats`, `image-packaging`, `reporter`, `launcher-stats`, `docs-update`, `integration-store`, `integration-controls`, `integration-provenance`, `integration-faults`, `integration-concurrency`, `tui-smoke`
- Gate: command — `make check && make test`
- Hazards: none (explicit claim)
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Testing Strategy / Structural Verification`
- Estimate: 1
- Observation: none yet
- Closure: open — state BLOCKED

### ac11-repo-review

- Contract: a scripted repository review finds every SQLite access path confined to bin/oc2-usage and the launcher's stats path, confirms no public command accepts raw SQL and no skill or agent prompt instructs direct database access, and verifies the report runs without any LLM.
- Justifies: `FR-11`, `AC-11`, `D-0001`
- Depends on: `structural-gate`
- Gate: command — `python3 tests/usage_ac11_review.py`
- Hazards: none (explicit claim)
- Artifacts: tests/usage_ac11_review.py
- Inputs: `planning:Specs/ModelRouterUsage/README.md#Requirements / Functional Requirements`
- Estimate: 1
- Observation: none yet
- Closure: open — state BLOCKED

### review-stack

- Contract: a recorded four-lane review closes the integrated stack (supervisor, collector, launcher, image, docs) against the approved design and spec before release gates are accepted.
- Justifies: `AC-10`, `DD-1`
- Depends on: `integration-store`, `docs-update`, `review-controls-risk`
- Gate: review — full (carries completion-grade closure)
- Hazards: none (explicit claim)
- Estimate: 1
- Observation: none yet
- Closure: open — state BLOCKED

### review-release

- Contract: a recorded four-lane review closes the plan: every release gate is green, the AC-11 code-only SQLite boundary holds, and the completion evidence cites each green observation.
- Justifies: `AC-11`, `FR-11`, `D-0001`
- Depends on: `integration-controls`, `integration-provenance`, `integration-faults`, `integration-concurrency`, `tui-smoke`, `ac11-repo-review`, `structural-gate`, `review-stack`
- Gate: review — full (carries completion-grade closure)
- Hazards: none (explicit claim)
- Estimate: 1
- Observation: none yet
- Closure: open — state BLOCKED

## Acceptance Criteria

- [ ] Every node in this phase is truly closed: a passing observation, and
      coverage by a passing frozen full review gate (derived from the graph;
      never checked off by hand).

## Phase Completion Evidence

Pending — not complete.
