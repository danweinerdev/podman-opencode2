---
title: "04-Integration"
type: phase
plan: "ModelRouterUsage"
phase: 4
status: planned
created: 2026-09-09
updated: 2026-09-09
deliverable: "Graph view: 6 node(s) under phase label 04-Integration"
tasks: []
---

# Phase 4: 04-Integration

<!-- GENERATED VIEW — source of truth: ModelRouterUsage-Graph.json. Regenerate with `sdd compile --plan ModelRouterUsage`. Edits here are overwritten. -->

## Overview

Rendered view of 6 node(s) from the plan graph (schema v1, seq 0).
Observations shown are raw records; completion-grade closure derives from
full review gates and is never stored or hand-edited here.

## Nodes

### integration-store

- Contract: the end-to-end probe on the repo-built image verifies over both mock-provider paths the AC-10 release evidence: stable completed-call identity and actual model/agent attribution including parent/worker links, three-step prompts counting three exactly once, project isolation, token normalization display, NFR-01 canaries, file permissions, restart recovery, and offline stats rendering.
- Justifies: `AC-01`, `AC-02`, `AC-05`, `AC-06`, `AC-09`, `AC-10`, `FR-01`, `FR-04`, `FR-06`, `FR-07`, `NFR-01`
- Depends on: `launcher-usage-stats`, `launcher-stats`, `image-packaging`
- Gate: command — `python3 tests/usage_integration_store.py`
- Hazards: computes-number, user-entrypoint
- Artifacts: tests/usage_integration_store.py, .plans/Research/evidence/model-router-usage/usage-integration-store.json
- Inputs: `planning:Specs/ModelRouterUsage/README.md#Acceptance Criteria`, `planning:Designs/ModelRouterUsage/README.md#Testing Strategy`
- Estimate: 3
- Observation: none yet
- Closure: open — state BLOCKED

### integration-controls

- Contract: the lifecycle probe on the repo-built image verifies enabled to disabled to disabled to enabled to enabled: no records from excluded intervals, repeated disabled launches preserve the first boundary, custom-command intervals are excluded, a killed process recovers usable usage with gaps marked, and ephemeral mode leaves no files after exit.
- Justifies: `AC-06`, `FR-09`, `NFR-02`, `DD-10`, `D-0003`
- Depends on: `integration-store`
- Gate: command — `python3 tests/usage_integration_controls.py`
- Hazards: computes-number, persists-state
- Artifacts: tests/usage_integration_controls.py, .plans/Research/evidence/model-router-usage/usage-integration-controls.json
- Inputs: `planning:Specs/ModelRouterUsage/README.md#Acceptance Criteria`
- Estimate: 3
- Observation: none yet
- Closure: open — state BLOCKED

### integration-provenance

- Contract: the provenance probe on the repo-built image verifies routed workers report match with expectation_source set on both provider paths, an off-route worker session reports unexpected, a between-runs override edit gives new-run calls the new expectation while stored calls keep theirs (no relabeling), and a mid-run edit degrades later imports to unverified rather than a false mismatch.
- Justifies: `AC-12`, `FR-12`, `D-0002`
- Depends on: `integration-store`
- Gate: command — `python3 tests/usage_integration_provenance.py`
- Hazards: derives-state, computes-number
- Artifacts: tests/usage_integration_provenance.py, .plans/Research/evidence/model-router-usage/usage-integration-provenance.json
- Inputs: `planning:Specs/ModelRouterUsage/README.md#Acceptance Criteria`
- Estimate: 2
- Observation: none yet
- Closure: open — state BLOCKED

### integration-faults

- Contract: the fault probe verifies port collision, serve death, disk-full or unwritable storage, and queue overflow never alter or block successful model output; the queue stays within 1,000 records, shutdown flushing ends within two seconds, and lost usage is visible in the report after recovery unless reconciliation restored it.
- Justifies: `AC-08`, `FR-10`, `NFR-02`, `NFR-03`
- Depends on: `integration-store`
- Gate: command — `python3 tests/usage_integration_faults.py`
- Hazards: computes-number
- Artifacts: tests/usage_integration_faults.py, .plans/Research/evidence/model-router-usage/usage-integration-faults.json
- Inputs: `planning:Specs/ModelRouterUsage/README.md#Acceptance Criteria`
- Estimate: 2
- Observation: none yet
- Closure: open — state BLOCKED

### integration-concurrency

- Contract: the concurrency probe verifies two simultaneous host-network launches allocate distinct ports and never kill each other's listeners, an occupied preferred port yields another candidate within the deadline, and two projects sharing one volume collect independently with no cross-project rows.
- Justifies: `FR-06`, `NFR-03`, `DD-2`
- Depends on: `integration-store`
- Gate: command — `python3 tests/usage_integration_concurrency.py`
- Hazards: concurrent-access
- Artifacts: tests/usage_integration_concurrency.py, .plans/Research/evidence/model-router-usage/usage-integration-concurrency.json
- Inputs: `planning:Specs/ModelRouterUsage/README.md#Acceptance Criteria`
- Estimate: 2
- Observation: none yet
- Closure: open — state BLOCKED

### tui-smoke

- Contract: the scripted checklist launches the repo-built image exactly as its CMD (including --auto passthrough), verifies the session list, drives one dispatched prompt with a worker, and exits cleanly; the saved TUI log and assertion summary are the evidence.
- Justifies: `AC-10`, `DD-1`
- Depends on: `integration-store`
- Gate: command — `python3 tests/usage_tui_smoke.py`
- Hazards: user-entrypoint
- Artifacts: tests/usage_tui_smoke.py, .plans/Research/evidence/model-router-usage/usage-tui-smoke.json
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Testing Strategy`
- Estimate: 2
- Observation: none yet
- Closure: open — state BLOCKED

## Acceptance Criteria

- [ ] Every node in this phase is truly closed: a passing observation, and
      coverage by a passing frozen full review gate (derived from the graph;
      never checked off by hand).

## Phase Completion Evidence

Pending — not complete.
