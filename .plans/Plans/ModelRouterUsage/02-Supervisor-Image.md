---
title: "02-Supervisor-Image"
type: phase
plan: "ModelRouterUsage"
phase: 2
status: planned
created: 2026-09-09
updated: 2026-09-09
deliverable: "Graph view: 3 node(s) under phase label 02-Supervisor-Image"
tasks: []
---

# Phase 2: 02-Supervisor-Image

<!-- GENERATED VIEW — source of truth: ModelRouterUsage-Graph.json. Regenerate with `sdd compile --plan ModelRouterUsage`. Edits here are overwritten. -->

## Overview

Rendered view of 3 node(s) from the plan graph (schema v1, seq 0).
Observations shown are raw records; completion-grade closure derives from
full review gates and is never stored or hand-edited here.

## Nodes

### supervisor

- Contract: bin/oc2-standalone allocates a collision-aware loopback port (configured port is a preference only), starts opencode2 serve with the per-launch password, waits for authenticated health, starts the collector, attaches the TUI via --server with --auto passthrough, restarts serve once on death with a degraded gap, falls back to standalone under the registered enabled epoch, and tears down in order (collector SIGTERM plus at most two seconds flush, then serve) exiting with the TUI code.
- Justifies: `DD-1`, `DD-2`, `DD-3`, `NFR-02`, `NFR-03`, `AC-08`
- Depends on: `test-harness`, `polling-engine`, `controls-journal`
- Gate: tests — `tests/unit/test_usage_supervisor.py::test_port_allocation_retries_on_race` in tests/unit/test_usage_supervisor.py; `tests/unit/test_usage_supervisor.py::test_ordered_teardown_sequence` in tests/unit/test_usage_supervisor.py (satisfies order-sensitive); `tests/unit/test_usage_supervisor.py::test_serve_death_restarts_once_then_fallback` in tests/unit/test_usage_supervisor.py; `tests/unit/test_usage_supervisor.py::test_tui_started_with_server_and_auto_passthrough` in tests/unit/test_usage_supervisor.py
- Hazards: order-sensitive
- Artifacts: bin/oc2-standalone, tests/unit/test_usage_supervisor.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Architecture / Interfaces`
- Estimate: 3
- Observation: none yet
- Closure: open — state BLOCKED

### launcher-usage-stats

- Contract: the launcher validates usage_stats (enabled boolean, port integer 1-65535 with booleans rejected), injects the OC2_* transport env contract, switches DEFAULT_COMMAND to the supervisor with --auto passthrough, invokes the validated control registration before launching any run (including disabled and custom-command launches), and skips registration entirely in ephemeral mode (D-0003).
- Justifies: `FR-09`, `DD-2`, `DD-9`, `DD-10`, `D-0003`, `ModelRouterUsage:FR-03`, `ModelRouterUsage:FR-08`
- Depends on: `test-harness`, `supervisor`, `controls-journal`
- Gate: tests — `tests/unit/test_usage_launcher_config.py::test_usage_stats_config_validation` in tests/unit/test_usage_launcher_config.py; `tests/unit/test_usage_launcher_config.py::test_transport_env_injection_contract` in tests/unit/test_usage_launcher_config.py; `tests/unit/test_usage_launcher_config.py::test_default_command_and_auto_passthrough` in tests/unit/test_usage_launcher_config.py (satisfies user-entrypoint); `tests/unit/test_usage_launcher_config.py::test_custom_command_registers_exclusion_before_launch` in tests/unit/test_usage_launcher_config.py (satisfies user-entrypoint); `tests/unit/test_usage_launcher_config.py::test_ephemeral_launch_skips_registration` in tests/unit/test_usage_launcher_config.py
- Hazards: user-entrypoint
- Artifacts: bin/opencode-container, tests/unit/test_usage_launcher_config.py
- Inputs: `planning:Designs/ModelRouterUsage/README.md#Design Decisions`
- Estimate: 2
- Observation: none yet
- Closure: open — state BLOCKED

### image-packaging

- Contract: the Containerfile bakes bin/oc2-standalone and bin/oc2-usage into the image, the image CMD runs the supervisor, and the repo-built image starts the supervisor (not the bare TUI) with python3 and sqlite3 available, with no other baked-path change.
- Justifies: `DD-3`, `DD-11`
- Depends on: `supervisor`, `polling-engine`
- Gate: command — `make build && python3 tests/usage_image_check.py`
- Hazards: user-entrypoint
- Artifacts: Containerfile, tests/usage_image_check.py
- Estimate: 1
- Observation: none yet
- Closure: open — state BLOCKED

## Acceptance Criteria

- [ ] Every node in this phase is truly closed: a passing observation, and
      coverage by a passing frozen full review gate (derived from the graph;
      never checked off by hand).

## Phase Completion Evidence

Pending — not complete.
