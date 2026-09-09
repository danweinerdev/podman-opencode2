---
title: "03-Reporting"
type: phase
plan: "ModelRouterUsage"
phase: 3
status: planned
created: 2026-09-09
updated: 2026-09-09
deliverable: "Graph view: 3 node(s) under phase label 03-Reporting"
tasks: []
---

# Phase 3: 03-Reporting

<!-- GENERATED VIEW — source of truth: ModelRouterUsage-Graph.json. Regenerate with `sdd compile --plan ModelRouterUsage`. Edits here are overwritten. -->

## Overview

Rendered view of 3 node(s) from the plan graph (schema v1, seq 0).
Observations shown are raw records; completion-grade closure derives from
full review gates and is never stored or hand-edited here.

## Nodes

### reporter

- Contract: the oc2-usage report subcommand renders the FR-07 view from a read-only connection: agent-by-actual-model rows with expected model and mixed aggregation, per-status route counts that sum to the call total, compact token bars with visible numbers and text labels, the session/agent/model filters, paginated --calls and --call detail with expectation provenance, and explicit empty, unavailable, and reported-token limitation states including collection state and gaps.
- Justifies: `FR-02`, `FR-05`, `FR-07`, `FR-09`, `FR-12`, `DD-8`, `AC-03`, `AC-05`, `AC-06`, `AC-07`, `AC-12`
- Depends on: `test-harness`, `store-schema`
- Gate: tests — `tests/unit/test_usage_report.py::test_main_table_columns_and_model_split_rows` in tests/unit/test_usage_report.py; `tests/unit/test_usage_report.py::test_token_bars_and_numeric_labels` in tests/unit/test_usage_report.py; `tests/unit/test_usage_report.py::test_filters_combine_and_call_inspection` in tests/unit/test_usage_report.py; `tests/unit/test_usage_report.py::test_route_status_counts_sum_to_calls` in tests/unit/test_usage_report.py (satisfies computes-number); `tests/unit/test_usage_report.py::test_calls_pagination_over_reversed_fixture` in tests/unit/test_usage_report.py (satisfies order-sensitive); `tests/unit/test_usage_report.py::test_empty_unavailable_and_limitation_states` in tests/unit/test_usage_report.py; `tests/unit/test_usage_report.py::test_collection_state_and_gap_display` in tests/unit/test_usage_report.py; `tests/unit/test_usage_report.py::test_report_opens_database_read_only` in tests/unit/test_usage_report.py
- Hazards: computes-number, order-sensitive
- Artifacts: bin/oc2-usage, tests/unit/test_usage_report.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Architecture / Interfaces`
- Estimate: 3
- Observation: none yet
- Closure: open — state BLOCKED

### launcher-stats

- Contract: opencode-container stats resolves the project volume via the local engine, opens usage/usage.db read-only, and renders the report with the spec filters without starting a container, model call, image build, or engine socket mount; inaccessible mountpoints and ephemeral state produce explicit errors or state, never empty results or invented data.
- Justifies: `FR-07`, `DD-12`, `AC-07`
- Depends on: `test-harness`, `reporter`
- Gate: tests — `tests/unit/test_usage_launcher_stats.py::test_stats_renders_fixture_volume_via_subprocess` in tests/unit/test_usage_launcher_stats.py (satisfies user-entrypoint); `tests/unit/test_usage_launcher_stats.py::test_stats_requires_no_container_or_model_call` in tests/unit/test_usage_launcher_stats.py (satisfies user-entrypoint); `tests/unit/test_usage_launcher_stats.py::test_inaccessible_mountpoint_is_explicit_error` in tests/unit/test_usage_launcher_stats.py; `tests/unit/test_usage_launcher_stats.py::test_ephemeral_state_reported_not_invented` in tests/unit/test_usage_launcher_stats.py
- Hazards: user-entrypoint
- Artifacts: bin/opencode-container, tests/unit/test_usage_launcher_stats.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Architecture / Interfaces`
- Estimate: 2
- Observation: none yet
- Closure: open — state BLOCKED

### docs-update

- Contract: docs/SANDBOX.md documents the usage_stats keys, the stats subcommand with its filters, and the transport, ephemeral, and collection-control behavior, and the README quickstart mentions opencode-container stats; every command in the updated prose runs against a fixture and every referenced artifact exists.
- Justifies: `FR-07`, `FR-09`, `NFR-04`
- Depends on: `launcher-stats`, `image-packaging`
- Gate: command — `python3 tests/usage_docs_check.py`
- Hazards: ships-prose
- Artifacts: docs/SANDBOX.md, README.md, tests/usage_docs_check.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Migration / Rollout`
- Estimate: 1
- Observation: none yet
- Closure: open — state BLOCKED

## Acceptance Criteria

- [ ] Every node in this phase is truly closed: a passing observation, and
      coverage by a passing frozen full review gate (derived from the graph;
      never checked off by hand).

## Phase Completion Evidence

Pending — not complete.
