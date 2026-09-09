---
title: "01-Collector-Core"
type: phase
plan: "ModelRouterUsage"
phase: 1
status: planned
created: 2026-09-09
updated: 2026-09-09
deliverable: "Graph view: 7 node(s) under phase label 01-Collector-Core"
tasks: []
---

# Phase 1: 01-Collector-Core

<!-- GENERATED VIEW — source of truth: ModelRouterUsage-Graph.json. Regenerate with `sdd compile --plan ModelRouterUsage`. Edits here are overwritten. -->

## Overview

Rendered view of 7 node(s) from the plan graph (schema v1, seq 0).
Observations shown are raw records; completion-grade closure derives from
full review gates and is never stored or hand-edited here.

## Nodes

### test-harness

- Contract: requirements.txt pins a working pytest, the Makefile defines check (ruff 0.16.6 plus mypy 1.18.2 over the plan's Python files) and test (pytest over tests/unit) targets, ruff.toml includes the two new extension-less bin files, and a smoke unit test runs green under both targets on the clean tree.
- Justifies: `NFR-04`
- Depends on: (nothing)
- Gate: command — `make check && make test`
- Hazards: none (explicit claim)
- Artifacts: requirements.txt, Makefile, ruff.toml, tests/unit/test_harness_smoke.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Testing Strategy / Structural Verification`
- Estimate: 1
- Observation: none yet
- Closure: open — state READY

### store-schema

- Contract: bin/oc2-usage exposes the versioned SQLite usage store (WAL, directory 0700, file 0600) with forward-only migrations that preserve data or refuse safely, (session_id, message_id)-keyed upserts, per-project rows, and the NFR-01 whitelist projection, all through one maintained module.
- Justifies: `FR-06`, `NFR-01`, `NFR-03`, `DD-5`
- Depends on: `test-harness`
- Gate: tests — `tests/unit/test_usage_store.py::test_migrations_forward_only_preserve_or_refuse` in tests/unit/test_usage_store.py; `tests/unit/test_usage_store.py::test_wal_mode_and_file_permissions` in tests/unit/test_usage_store.py; `tests/unit/test_usage_store.py::test_adversarial_identifiers_round_trip` in tests/unit/test_usage_store.py (satisfies external-format); `tests/unit/test_usage_store.py::test_concurrent_writers_preserve_records` in tests/unit/test_usage_store.py (satisfies concurrent-access); `tests/unit/test_usage_store.py::test_round_trip_reopen_via_public_api` in tests/unit/test_usage_store.py (satisfies persists-state)
- Hazards: persists-state, external-format, concurrent-access
- Artifacts: bin/oc2-usage, tests/unit/test_usage_store.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Architecture / Interfaces`
- Estimate: 2
- Observation: none yet
- Closure: open — state BLOCKED

### controls-journal

- Contract: the control journal at usage/control/<project_key>/state.json implements durable exclusion intervals and the first|restart|disabled resume modes with atomic tmp+fsync+replace under the project-scoped lock, fails closed on missing or corrupt history, records disabled gaps, and skips host-side registration entirely in ephemeral mode (D-0003).
- Justifies: `FR-09`, `DD-10`, `D-0003`, `NFR-02`
- Depends on: `test-harness`, `store-schema`
- Gate: tests — `tests/unit/test_usage_controls.py::test_journal_round_trip` in tests/unit/test_usage_controls.py (satisfies persists-state); `tests/unit/test_usage_controls.py::test_adversarial_journal_values_round_trip` in tests/unit/test_usage_controls.py (satisfies external-format); `tests/unit/test_usage_controls.py::test_concurrent_registration_is_serialized` in tests/unit/test_usage_controls.py (satisfies concurrent-access); `tests/unit/test_usage_controls.py::test_resume_modes_first_restart_disabled` in tests/unit/test_usage_controls.py; `tests/unit/test_usage_controls.py::test_excluded_intervals_preserve_first_boundary` in tests/unit/test_usage_controls.py (satisfies computes-number); `tests/unit/test_usage_controls.py::test_missing_or_corrupt_journal_fails_closed` in tests/unit/test_usage_controls.py; `tests/unit/test_usage_controls.py::test_ephemeral_mode_skips_host_registration` in tests/unit/test_usage_controls.py
- Hazards: persists-state, external-format, concurrent-access, computes-number
- Artifacts: bin/oc2-usage, tests/unit/test_usage_controls.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Design Decisions`
- Estimate: 3
- Observation: none yet
- Closure: open — state BLOCKED

### route-provenance

- Contract: the resolver computes each agent's expected model from the model-router plugin's own sources (baked base defaults plus the env-named override files merged with the plugin's exact mergeConfig semantics) and marks a call's expectation verified only when the agent is mapped and pinned per the plugin's pin rules and every existing override file's mtime is at or before the producing run's start; every failure degrades to unverified, never a guess.
- Justifies: `FR-12`, `DD-7`, `D-0002`, `AC-12`
- Depends on: `test-harness`
- Gate: tests — `tests/unit/test_usage_provenance.py::test_python_merge_matches_plugin_export` in tests/unit/test_usage_provenance.py (satisfies derives-state); `tests/unit/test_usage_provenance.py::test_pin_semantics_pinned_and_unpinned_agents` in tests/unit/test_usage_provenance.py; `tests/unit/test_usage_provenance.py::test_mtime_condition_at_run_start_boundary` in tests/unit/test_usage_provenance.py; `tests/unit/test_usage_provenance.py::test_run_attribution_with_reversed_windows` in tests/unit/test_usage_provenance.py (satisfies order-sensitive); `tests/unit/test_usage_provenance.py::test_unreadable_or_invalid_config_is_unverified` in tests/unit/test_usage_provenance.py
- Hazards: derives-state, order-sensitive
- Artifacts: bin/oc2-usage, tests/unit/test_usage_provenance.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Design Decisions`, `repository:plugins/model-router/index.js`
- Estimate: 3
- Observation: none yet
- Closure: open — state BLOCKED

### import-predicate

- Contract: every candidate record passes one admission predicate (project identity, collection epoch, exclusion intervals) before upsert, normalizes tokens per the verified contract, computes route status against the stored verified expectation, and never counts an unfinished step or duplicates a replay or correction.
- Justifies: `FR-01`, `FR-02`, `FR-04`, `FR-05`, `NFR-01`, `DD-6`, `AC-01`, `AC-02`, `AC-03`, `AC-09`
- Depends on: `store-schema`, `controls-journal`, `route-provenance`
- Gate: tests — `tests/unit/test_usage_import.py::test_three_steps_three_calls_replayed_twice_stays_three` in tests/unit/test_usage_import.py (satisfies computes-number); `tests/unit/test_usage_import.py::test_correction_replaces_record_once` in tests/unit/test_usage_import.py (satisfies computes-number); `tests/unit/test_usage_import.py::test_unfinished_generation_adds_no_call` in tests/unit/test_usage_import.py; `tests/unit/test_usage_import.py::test_token_normalization_contract` in tests/unit/test_usage_import.py; `tests/unit/test_usage_import.py::test_unknown_agent_and_cross_project_exclusion` in tests/unit/test_usage_import.py; `tests/unit/test_usage_import.py::test_whitelist_excludes_paths_titles_bodies` in tests/unit/test_usage_import.py
- Hazards: computes-number
- Artifacts: bin/oc2-usage, tests/unit/test_usage_import.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Architecture / Interfaces`
- Estimate: 2
- Observation: none yet
- Closure: open — state BLOCKED

### polling-engine

- Contract: the collector polls the session/message API with cursor pagination under bounded request and time budgets, revisits in-flight steps until completion, persists per-session progress in poll_progress, and holds at most 1,000 records in the bounded queue whose overflow drops telemetry with a visible warning without ever blocking or failing the coding path.
- Justifies: `FR-04`, `FR-10`, `NFR-02`, `NFR-03`, `DD-4`
- Depends on: `import-predicate`
- Gate: tests — `tests/unit/test_usage_polling.py::test_pagination_over_reversed_fixture` in tests/unit/test_usage_polling.py (satisfies order-sensitive); `tests/unit/test_usage_polling.py::test_replay_fixture_yields_identical_trace` in tests/unit/test_usage_polling.py (satisfies deterministic-replay); `tests/unit/test_usage_polling.py::test_inflight_steps_revisited_until_complete` in tests/unit/test_usage_polling.py; `tests/unit/test_usage_polling.py::test_queue_bounded_at_1000_with_visible_drop` in tests/unit/test_usage_polling.py
- Hazards: order-sensitive, deterministic-replay
- Artifacts: bin/oc2-usage, tests/unit/test_usage_polling.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Architecture / Interfaces`
- Estimate: 2
- Observation: none yet
- Closure: open — state BLOCKED

### review-controls-risk

- Contract: a recorded subset-lane review verifies the control journal's and provenance resolver's concurrency and temporal logic against the pinned design contracts, including that the Python merge matches the plugin semantics and no drift introduced exclusion or provenance errors.
- Justifies: `FR-09`, `FR-12`, `DD-7`, `DD-10`, `D-0002`
- Depends on: `controls-journal`, `route-provenance`
- Gate: review — lanes: review_blind_spots, review_plan_drift, review_spec_compliance
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
