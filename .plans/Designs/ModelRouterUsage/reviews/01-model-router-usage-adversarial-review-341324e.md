---
title: "Adversarial review: Model Router Usage Reporting"
type: review
status: resolved
created: 2026-09-08
updated: 2026-09-08
tags: [review, model-router, usage]
related: [Designs/ModelRouterUsage/README.md, Specs/ModelRouterUsage/README.md, Research/model-router-usage-source.md]
review_of: Designs/ModelRouterUsage/README.md
rev: "341324e"
findings:
  - id: F-01
    severity: major
    title: "Current session model cannot establish historical expectations"
    status: fixed
  - id: F-02
    severity: major
    title: "Disabled exclusions do not survive later reconciliation"
    status: fixed
  - id: F-03
    severity: major
    title: "Control and health state are not scoped for concurrent runs"
    status: fixed
  - id: F-04
    severity: major
    title: "Crash recovery abandons recoverable usage"
    status: fixed
  - id: F-05
    severity: major
    title: "In-container reporter cannot open host mountpoints"
    status: fixed
  - id: F-06
    severity: major
    title: "Aggregate rows cannot express mixed route results"
    status: fixed
  - id: F-07
    severity: major
    title: "Fixed port breaks concurrent host-network launches"
    status: fixed
  - id: F-08
    severity: minor
    title: "Individual call inspection has no CLI contract"
    status: fixed
followups: []
---

# Adversarial review: Model Router Usage Reporting

Reviewed 2026-09-08 against the working-tree design, approved spec, related research, and launcher source. Repository baseline: 341324e; the design is untracked, so that revision does not contain the reviewed design. Primary review with an independent related-context sweep. No runtime probes rerun and no SQLite files accessed. Search of the planning-root inventory and reverse references found no decision ledger or related plan.

## Findings
### F-01 — Major: Current session model cannot establish historical expectations
**Impugns:** DD-7; FR-12; AC-12; design lines 208–210.
**Scenario:** A call completes using selected model A. Before the next poll, the user selects B. The collector sees the old call with the current session model B and permanently labels the correct A call unexpected. Historical imports have the same problem. Freezing the comparison freezes the mistake.
**Recommendation:** Require verified call-specific temporal provenance for expected models; otherwise use unverified even when the current session model is non-null. Test selection changes between completion and first collection and old-call imports. Research line 115 already requires this fallback.

### F-02 — Major: Disabled exclusions do not survive later reconciliation
**Impugns:** DD-4; DD-10; FR-09; AC-06; design lines 198 and 220–222.
**Scenario:** Enabled → disabled → enabled excludes records on the first resumed run and overwrites JSON state with enabled. The next enabled restart selects full reconciliation; no rule applies stored disabled gaps as exclusion predicates on this path, so excluded history is imported. Consecutive disabled starts also overwrite the interval's original start timestamp.
**Recommendation:** Persist durable exclusion intervals and apply them to every import, replay, correction and recovery path. Preserve the first disabled timestamp across disabled launches and define first-run precedence. Test enabled → disabled → disabled → enabled → enabled with the source history intact.

### F-03 — Major: Control and health state are not scoped for concurrent runs
**Impugns:** DD-5; DD-10; FR-06; FR-09; AC-05; AC-06; design lines 115–139 and 222.
**Scenario:** Projects A and B share a volume. A writes disabled to the single state file; B starts enabled and treats A's state as its own excluded interval. The gaps table has no project key, so report queries cannot isolate health by project. Within one project, collector A can mark the single shutdown marker clean while collector B remains active and subsequently crashes, hiding B's unclean exit. WAL does not coordinate these logical transitions.
**Recommendation:** Specify project-scoped controls, exclusions and gaps, per-run identities and lifecycle markers, and atomic state transitions. Define how simultaneous enabled and disabled launches of the same project interact. Test overlapping writers, independent shared-volume projects, and a clean exit followed by another writer's crash.

### F-04 — Major: Crash recovery abandons recoverable usage
**Impugns:** DD-4; DD-10; FR-10; AC-08; design lines 198, 222 and 240.
**Scenario:** OpenCode persists a completed step, then the container dies before the collector flushes it. DD-10 explicitly says the unclean window is reported as loss and not backfilled, although the pinned API retains the record. This contradicts FR-10's recovery requirement and DD-4's startup reconciliation.
**Recommendation:** Reconcile unintentional gaps from the supported source while honoring disabled exclusions. Report residual unrecoverable coverage as loss. Test a crash after source persistence but before telemetry commit.

### F-05 — Major: In-container reporter cannot open host mountpoints
**Impugns:** DD-12; FR-07; AC-07; design lines 228–230.
**Scenario:** The specified in-container reporter obtains the host engine's volume Mountpoint and opens it inside the sandbox filesystem. The launcher mounts the engine socket and mounts the volume at /var/lib/opencode-data, not at the host storage path (bin/opencode-container lines 1119–1125 and 1159–1165). The database is inaccessible through the proposed path and may be misreported as absent.
**Recommendation:** Keep offline stats host-side regardless of containers mode, or explicitly use the already-mounted volume for in-container reporting. Honor persistence.data_volume overrides (launcher line 1151), retain the original project identity, and distinguish inaccessible from absent storage. Test both filesystem contexts without adding a socket requirement to offline reporting.

### F-06 — Major: Aggregate rows cannot express mixed route results
**Impugns:** FR-07; FR-12; AC-07; AC-12; design lines 173–175.
**Scenario:** Two calls have the same agent and actual model A. One expected A and matched; the other expected B and was unexpected. The required single agent/actual-model row has one Expected cell and one Route cell constrained to a single status. Picking either hides a match or a routing failure; unverified discards known evidence.
**Recommendation:** Define lossless aggregation, such as per-status counts with a mixed-expectation label and call detail retaining each comparison. Test match, unexpected and unverified calls in one group.

### F-07 — Major: Fixed port breaks concurrent host-network launches
**Impugns:** DD-2; NFR-02; design lines 72 and 188–190.
**Scenario:** The existing launcher accepts network: host and forwards it unchanged (bin/opencode-container lines 1210–1214; docs/SANDBOX.md line 38). Two default launches then share localhost:18080 rather than separate network namespaces. The second server cannot bind; its per-launch password also cannot authenticate to the first server. The supervisor fails health and never starts coding. Existing standalone launches use ephemeral ports (research line 52).
**Recommendation:** Specify endpoint allocation for shared network namespaces, or explicitly resolve the compatibility change before adopting the default. Test two simultaneous host-network launches and an occupied default port. A configurable port alone does not preserve the current concurrent default behavior.

### F-08 — Minor: Individual call inspection has no CLI contract
**Impugns:** FR-07; AC-07; design lines 164–179 and 268.
**Scenario:** A user sees an unexpected aggregate and wants its message identifier, completion time, expected model and token categories. The documented commands only filter aggregate rows. Call detail is mentioned for its footnote, but no command, output contract or acceptance exercise makes it reachable.
**Recommendation:** Define validated call-list/detail flags or a subcommand keyed by stable session/message identifiers, specify metadata and unavailable states, and add an integration check.

## Resolution Log
### F-01 — fixed (2026-09-08)
Updated DD-7 and the call schema to require call-time provenance, and added temporal-switch/import fixtures. Current HTTP records explicitly remain unverified. Governing facts: FR-12, AC-12. This resolves the design defect; no runtime implementation is claimed.

### F-02 — fixed (2026-09-08)
DD-10 now retains project exclusion history and applies it on every admission/replay/correction. Consecutive disables preserve their first boundary; no-store startup retains existing controls. Added multi-restart and custom-command tests. Governing facts: FR-09, AC-06.

### F-03 — fixed (2026-09-08)
Scoped controls and gaps by project, health by run, and serialized transitions against commits. Latest registered launch controls project mode; explicit disable must be published to every writer before that launch proceeds. Added concurrent exit/crash and shared-volume tests. Governing facts: FR-06, FR-09, AC-05, AC-06. The user requested the reviewed design update; these choices remain reviewable design content, not approved implementation.

### F-04 — fixed (2026-09-08)
DD-4/DD-10 and error handling now replay recoverable crash/flush losses, distinguish pending/restored/residual gaps, and retain enabled mode on transport fallback. Governing facts: FR-10, AC-08.

### F-05 — fixed (2026-09-08)
DD-12 keeps offline stats host-side regardless of launch configuration, honors volume overrides, distinguishes access errors, and specifies the mounted path/original project key for explicit in-container reports. Governing facts: FR-07, AC-07 and launcher mount behavior.

### F-06 — fixed (2026-09-08)
Specified per-status counts summing to Calls and mixed-expectation labels within each agent/actual-model row, backed by individual comparison detail and fixtures. Governing facts: FR-07, FR-12, AC-07, AC-12.

### F-07 — fixed (2026-09-08)
DD-2 now selects candidate ports in the target namespace, retries bind races within a deadline, authenticates only the owned server, and treats configured ports as preferences. Added concurrent host-network and occupied-port release tests. Governing facts: existing launcher network support, NFR-02. Allocation behavior is designed, not newly runtime-verified.

### F-08 — fixed (2026-09-08)
Defined --calls, bounded pagination, --call with required session, ID validation, not-found behavior and detail fields. Added reporter acceptance coverage. Governing facts: FR-07, FR-11, AC-07.

### Follow-up design review (2026-09-08)
Independent review identified a host-intent visibility race and a transport fallback that incorrectly opened exclusions. Removed the host-only authority: a failed explicit disable transition cannot start a new run until visible to all collectors, while enabled collection failures still allow coding. Transport fallback now stays enabled and recoverable. Added the previously missing whitelisted polling-progress schema. Original findings are fixed at design level; runtime implementation and release evidence remain future work.
