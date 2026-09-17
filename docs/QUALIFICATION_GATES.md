# Qualification gates — evidence snapshot

**2026-09-17: not release-qualified.** Functional qualification has substantial
bounded evidence; latency fails at the four-client operating point and operational
integration, mixed-binary rollback, and real cost evidence remain open.

This report follows `docs/SCALABILITY_UPGRADE_PLAN.md`, particularly M0–M7 and the
execution boundary. It records existing evidence and the lead's supplied verified
results; creating this report did not run tests, load environment files, contact a
network/database/SSH target, or change application code. Only this file is owned by
this reporting task. The lead should update statuses as further work is verified.

## Status and provenance rules

- **PASS**: the stated, limited gate has supporting evidence. It is not a milestone-wide
  claim, a production SLO, or proof for untested combinations.
- **FAIL**: observed evidence misses the stated criterion; preserve the failed result.
- **OPEN**: evidence, integration, an agreed target, or authorization is missing.
- Milestone summaries are FAIL if a constituent acceptance criterion demonstrably
  fails; otherwise OPEN while required scope remains unproven.
- Test counts below are separate execution snapshots, not additive totals. Tests and
  worktree contents changed between runs; overlapping selections must not be summed.
- Historical full-suite results retain their original scope. Later dated verification
  below supersedes them only for the tested source; it is not release approval.
- Raw `qualified: true` in staging JSON covers functional runner assertions; it does
  not override memory, latency, operational, or release gates.

## Production boundary and historical exception

**Historical “production was never touched”: FAIL.** Plan lines 254–257 explicitly
record nine existing PostgreSQL tests, then ten including synthetic publication,
against `nebbtrmwzbpmzvinhrch` before discovery that production used that project.
Configuration writes rolled back, but identity sequences could have advanced. These
runs are excluded from isolated-development evidence; a never-touched claim cannot
be confirmed and would contradict the recorded history.

**Post-separation qualification boundary: PASS for the supplied record.** No new
production activity has been reported since separation. Current DB qualification
rejects the known production project. This is not a retrospective production audit.
The authorized development project is `rnherugormxtrmxlerjj`; committed isolated
tests use `kpmxgwflgyzjvitwlfvs`. Authorized AWS staging uses a separate
`gateway_qualification` database and loopback synthetic services, as documented in
the plan. This report performs no new target verification.

**Production migration, canary, deployment and traffic replay: OPEN — excluded,
not authorized.** They are future approval-dependent work, not failed experiments
and not actions to perform in order to finish this reporting task.

## Latest bounded attribution evidence

The instrumented staging run used `scripts/staging_qualify.py --seconds 120
--clients 4 --skip-faults --separate-services --profile --direct-db`. Evidence is
`/tmp/opencode/attribution-4-120s.json`; systemd invocation
`2fdbdcaab0dd48bba1ba83374afbdf56` exited 0. No qualification services remained
running afterward. The generator and services used separate CPU-limited slices
on the same host, not independent machines.

Functional assertions passed: 2,283 HTTP 200 responses, 209 accepted/completed
workflows, successful reservations settled, and provider active count zero.
Pre-dispatch latency remains **FAIL**: combined mean 68.07 ms, p95 (100,250] ms;
2,014 of 2,283 samples were at or below 100 ms. Total-response p95 was 357.23 ms
and must not substitute for pre-dispatch latency. The 120-second run cannot close
the memory/soak gate; high-load rejection latency was not exercised.

The two `stage_deltas` entries represent API processes, not sequential windows.
The six new non-overlapping foreground spans each recorded 2,283 observations
across those processes with zero errors:

| Stage | Combined mean (ms) |
| --- | ---: |
| Interactive capacity admission | 13.32 |
| Route capacity admission | 12.08 |
| Shared circuit allow | 6.44 |
| Local route concurrency acquisition | 0.039 |
| Ownership verification | 5.95 |
| Durable dispatch | 5.72 |

Together these spans account for approximately 43.56 ms, or 64% of the mean
pre-dispatch duration. Pre-dispatch is their enclosing interval and must not be
added to their sum. Aggregate histograms do not identify which operation caused
an individual slow request; individual stage percentiles cannot be summed.
These measurements support investigating capacity transactions first, but do not
prove that changing them will satisfy the latency target. No coordination or
accounting behavior was intentionally changed by the timing instrumentation.

Local verification after instrumentation: 1,227 offline tests passed with 77
integration tests deselected; the subsequently added timing-helper tests were
verified in a separate 38-test metrics run. Gateway and metrics-test Ruff checks
passed, and wheel/sdist builds succeeded in `/tmp/opencode/timing-build`.
The helper tests cover all six labels, success, exception identity, cancellation,
trace recording, and rejection of unknown labels. Earlier artifact hashes do not
identify this newly instrumented source. Historical tables below retain their
original execution scopes rather than implying a final release qualification.

## Warm-row capacity optimization — preliminary result

Capacity acquisition now locks each scope first and inserts only if the row is
missing, then re-reads it under lock. Scope ordering, stored-policy limits, the
separate lease-count statement, and atomic lease insertion remain in place.
For N scopes, warm acquisition uses N+3 explicit statements instead of 2N+3;
cold acquisition uses 3N+3 instead of 2N+3, excluding transaction control.

Verification: five isolated PostgreSQL capacity tests passed, including cold/warm
statement counts for 1/3/6 scopes, ordered locking statements, exhaustion, changed
stored policy, workflow-share limits and expired ownership. These rollback-scoped
tests do not prove concurrent cold insertion, policy-update contention or
mixed-binary compatibility. The offline suite passed 1,246 tests with 80 deselected;
Ruff passed for gateway code and the capacity test file. Wheel/sdist builds passed
in `/tmp/opencode/warmrow-build`. No typecheck command is configured in pyproject.toml.

The same 120-second/four-client staging workload completed successfully under
systemd invocation `7f4b4a6382534dd78d3651e012498a0d`. Evidence is
`/tmp/opencode/warmrow-4-120s.json`. It recorded 2,401 HTTP 200 responses and 211
workflow submissions; no qualification services remained running afterward.

| Combined mean | Before (ms) | Warm-row (ms) |
| --- | ---: | ---: |
| Gateway pre-dispatch | 68.07 | 60.50 |
| Interactive capacity admission | 13.32 | 10.60 |
| Route capacity admission | 12.08 | 9.17 |

One API's pre-dispatch p95 remained in (100,250] ms; the other was in (50,100] ms.
The latency gate is **not closed**. This is one sequential before/after comparison,
not proof of a repeatable improvement or a controlled estimate of the change's
benefit. The short run does not close memory, high-load rejection latency,
independent-generator, mixed-binary rollback or production qualification gates.
Follow-up at optimization commit `71f1f0d`: four dedicated-database tests passed
(41 deselected), covering cold/warm two-backend contention, separate-process
capacity enforcement, and stored-policy decreases against stale local settings.
The cold test forces both backends to observe the absent row before either
inserts; exactly one lease is committed at capacity one. Each contender holds a
distinct backend connection, and connections are released before pooled
assertions and cleanup. Only UUID-scoped synthetic rows are removed afterward.
The final offline run passed 1,246 tests with 81 integration cases deselected;
Ruff passed for gateway code and `tests/test_remote_isolation.py`.
This is bounded single-scope concurrency evidence, not simultaneous policy-update
contention, multi-scope cold races, or old/new binary locking interoperability.
Those remain follow-up checks before broader qualification claims.

## Verified execution ledger

### September 17 follow-up: rollback, monitoring and maintenance

- `tests/test_rollback_remote.py` passed forward and rollback switches between
  gateway source exports at `0bac80ee1db12dda4cb6103df00b54ad824958e2` and
  `8bd1ff0299aa4390125a06ae6ffcf635bd4c220c`. Fresh Python processes share a committed
  queued workflow, pending settlement and capacity lease. The consumer completes
  the workflow with the real executor and synthetic HTTP transport, replays the
  prior intent idempotently, and verifies two settled charges totaling 0.50 fixture
  units and one inference attempt. Source roots are verified. Owner-token handoff
  tests representation compatibility, not automatic stale-owner takeover.
- Scope: gateway Python sources, shared interpreter/dependencies, expanded schema,
  sequential processes. No installed-artifact deployment, ingress/lifespan switch,
  simultaneous versions or downgrade DDL. Original baseline `230183e` lacks the
  workflow/accounting modules and cannot drain jobs alone. Full baseline rollback
  remains **OPEN**, requiring compatible drainers and the uncommitted baseline snapshot.
- All 48 isolated tests passed in disjoint 37- and 11-test batches. An initial
  whole-suite attempt exceeded its 900-second timeout during the protocol matrix.
  Its identified synthetic fixture was removed and the matrix rerun. One expired
  lease from the first failed compatibility-test draft was removed by exact scope
  and owner after inspection. The complete rollback-scoped DB selection then
  passed 36 tests. These cleanups were test cleanup, not financial reconciliation.
- Added monitored queue age, settlement backlog, unknown usage and workflow approval
  backlog. Malformed rule parameters no longer abort later rules or falsely resolve
  unknown observations. Migration 034 expands allowed condition names, seeds no
  rules and was applied only to the dedicated project. Real-DB tests passed for
  rule insertion, SQL breach/recovery, alert persistence, deduplication and resolution.
  External delivery and process/fleet lag/config/scaling integration remain **OPEN**.
- Telemetry pruning has opt-in bounded scheduling: `maintenance_enabled=false`,
  interval 3,600 seconds (60–86,400), timeout 5 seconds (0.1–60), retention 30 days
  (7–365). Existing expired-artifact GC remains automatic. Cleanup runs separately
  from reconciliation with owned cancellation and per-operation deadlines, sharing
  the pool. Batches delete at most 100 rows per table. Real-DB tests removed 105 old
  synthetic rows in 100/5 batches while preserving recent telemetry and unknown
  holds. No financial/job retention or deduplication-evidence deletion was added.
- On schema 034, old/new module and both process switches passed again (3 tests);
  operations/retention passed (6 tests), plus a separate persisted-alert lifecycle
  test (1). Full offline regression passed 1,301 tests with 86 deselected before
  the final persisted-alert integration test was added. Counts are snapshots.

The dedicated DB and AWS staging are migrated through 034; historical entries below
retain their original schema scopes. Staging received tracked source from `48cde32`.
The post-migration operations probe exited 0 with `qualified: true`; archived evidence
is `/tmp/opencode/operations-final-post034.json`.

The subsequent staging command used `--seconds 120 --clients 4 --skip-faults
--separate-services`, with the database proxy enabled. Its evidence is staging file
`evidence/run-4-120s-f0d6d0c64d824a57a1ffcf0fecc1b8be.json`. Functional assertions
passed: 1,556 HTTP 200 responses, 204 completed workflows, successful reservations
settled, provider active count zero and no recorded stage errors. Overall analysis
remains **FAIL**: pre-dispatch p95 is (100,250] ms against a 100 ms target; only
449/1,556 samples met that target. Total-response p95 was 477.30 ms. Memory remains
**OPEN** with only 119 samples, and fault drills were explicitly skipped.
This proxy-enabled run is not an equivalent before/after comparison with the earlier
`--direct-db` warm-row run. Neither functional success nor `qualified: true` closes
latency, representative translated soak, independent-generator, real-cost or release
gates. A fresh offline regression before `48cde32` passed 1,301 tests with 87 deselected;
Ruff passed excluding the two unrelated credential-bearing scripts.

Two subsequent direct-DB runs used `--seconds 120 --clients 4 --skip-faults
--separate-services --direct-db`. Both passed functional assertions but failed
pre-dispatch p95: (100,250] ms against the 100 ms target.

| Direct-DB evidence | HTTP 200 count | Pre-dispatch mean | Samples ≤100 ms | Total-response p95 |
| --- | ---: | ---: | ---: | ---: |
| `/tmp/opencode/directdb-post034.json` | 2,162 | 73.19 ms | 81.73% | 374.04 ms |
| `/tmp/opencode/directdb-post034-repeat.json` | 2,361 | 62.91 ms | 90.94% | 352.83 ms |

The earlier warm-row run had a 60.50 ms pre-dispatch mean and additionally enabled
`--profile`; these executions are not fully matched controlled comparisons.
Observed differences do not establish an outlier, identify a causal regression,
or rule one out. Means do not close the failing tail-latency gate. These short
runs also cannot establish memory stability or independent-generator qualification.

| Evidence | Result | Proven scope and limits |
| --- | --- | --- |
| `scripts/dev_check.py`; offline selection under `tests/` | PASS: 989 passed, 69 deselected, 1 xfail | Lead-verified snapshot before new network fixes. Deselected DB tests and the expected failure are not passes. Exact xfail identity/reason and a final post-fix full run remain to be attached. |
| `scripts/dev_db_check.py`, isolated selection | PASS: 35 passed | Lead-verified committed synthetic DB selection; files listed below. Not a production run or unrestricted concurrency test. |
| `scripts/dev_db_check.py`, transactional selection | PASS: 24 passed | Lead-verified real PostgreSQL transaction semantics, bounded/rollback-scoped. Does not alone prove independent-process behavior. |
| `tests/test_workflow_protocol_matrix_remote.py::test_protocol_matrix_multi_tool_workflow` | PASS: 9 passed after Responses-to-Chat fix | All 3×3 client/upstream protocol pairs, real DB persistence, non-streaming two-turn continuation with three tool invocations, usage/outbox/settlement assertions. Provider transport and client HTTP are mocked/in-process; distributed admission is disabled in this fixture. |
| `tests/test_runtime_lifecycle_remote.py::test_published_runtime_preserves_live_lease_and_recovers_after_repository_outage` | PASS: 1 passed | Actual PostgreSQL repository/cache/builder/RuntimeManager; rollback-scoped publications, transport reuse, held capacity/circuit preservation, last-known-good and recovery. Repository outage is injected; HTTP dispatch is prohibited by the fixture. |
| `tests/test_network_protocol_matrix.py` and related translation regressions | PASS: 59 related tests after fixes | Lead-verified network matrix batch. Exact matrix nodes are below; 59 is the related batch count, not the number of protocol pairs. |
| `tests/test_translation_stream_bounds.py` and related streaming regressions | PASS: 94 passed | Lead-verified bounded-streaming batch. Aggregate retained bytes, fragment/call counts, metadata, interleaving and overflow paths; not a deployed long-stream soak. |
| `tests/test_operating_envelope.py` | PASS: 127 passed | Pure advisory evaluator tests only. Runtime collection, monitoring and scaling integration remain OPEN. |

Counts without an archived execution-log path above are supplied verified results,
not reruns by the report author. Related-batch command lines and final source hashes
should be attached by the lead rather than inferred from current test collection.

Exact DB selection paths in `scripts/dev_db_check.py`:

- Transactional: `tests/test_phase6_integration.py`,
  `tests/test_model_routing_integration.py`, `tests/test_accounting_integration.py`,
  `tests/test_workflow_integration.py`, `tests/test_capacity_integration.py`.
  The new `tests/test_runtime_lifecycle_remote.py` is separately evidenced above;
  its presence in the current runner does not change the historical 24-test count.
- Isolated: `tests/test_remote_isolation.py`, `tests/test_workflow_e2e_remote.py`,
  `tests/test_artifacts_remote.py`, `tests/test_shared_circuit_remote.py`,
  `tests/test_lane_fairness_remote.py`, `tests/test_execution_crash_remote.py`,
  `tests/test_mixed_workload_remote.py`, `tests/test_workflow_races_remote.py`.
  The new `tests/test_workflow_protocol_matrix_remote.py` is separately evidenced
  above; do not retroactively assign its nine cases to the earlier 35-test count.

## M0 — isolated baseline and instrumentation: OPEN

| Gate | Status | Evidence / exact test paths / remaining scope |
| --- | --- | --- |
| Explicit isolated target, synthetic fixtures, inherited DB rejection | PASS | `tests/test_dev_check.py`, `tests/test_dev_db_check.py`; explicit-target runner rejects the known production project and bounds connections. Historical exception remains recorded above. |
| Deterministic offline baseline | PASS | 989/69/1 execution snapshot above. Current post-network-fix full regression is OPEN. |
| Reproducible deployed functional baseline | PASS | Two-hour JSON paths below; `scripts/staging_qualify.py`, `scripts/qualification_report.py`, `tests/test_qualification_report.py`. Synthetic provider and colocated host only. |
| Stage metrics and telemetry cardinality/content controls | PASS | `tests/test_observability.py`, `tests/test_metrics.py`; plan records stage histograms and bounded sampled traces. Exact gateway pre-dispatch scope is below. |
| Complete representative workload/metadata baseline | OPEN | Plan contains ARM64/one-CPU/Python metadata and bounded matrices; latest artifact/dependency provenance, independently driven load, long reasoning/tool-heavy deployed traffic and agreed availability/p99 budgets still need final evidence. |

## M1 — bounded admission and large-body handling: FAIL

| Gate | Status | Evidence / exact test paths / remaining scope |
| --- | --- | --- |
| Pre-parse admission, immediate rejection mechanism, upload byte/deadline limits | PASS | `tests/test_admission.py`, `tests/test_input_limits.py`, `tests/test_benchmark_admission.py`; bounded synthetic overload and actual-byte handling. |
| Protocol payload/output/tool limits; no implicit prompt compaction | PASS | `tests/test_input_limits.py`, `tests/test_output_limits.py`, `tests/test_translation_stream_bounds.py`; bounded protocol fixtures. Character estimates are not tokenizer guarantees or unrestricted multimodal qualification. |
| Full SSE lifetime ownership and cleanup after failures/disconnects | PASS | `tests/test_socket_qualification.py::test_fragmented_sse_over_real_tcp_and_disconnect_capacity_recovery`, `tests/test_transport_double_cancel.py`, `tests/test_request_lifetime_retention.py`; real loopback sockets plus no-cyclic-GC lifetime regression paths. |
| Small requests and both lanes progress under bounded mixed load | PASS | `tests/test_mixed_workload_remote.py`, `tests/test_lane_fairness_remote.py`; finite two-tenant/capacity-two cases and successful four-client staging soak. |
| Prompt overload response at the tested high-load ladder | FAIL | Plan's 1/8/32/64/128-client ladder records 429 p95 up to 2,838.63 ms. Functional boundedness does not close prompt-rejection performance. |
| Sustained adversarial fairness and full payload/concurrency envelope | OPEN | Accepted-job completion does not prove admission fairness; separate-generator saturation and broader tenant/payload mixes remain unproven. |

## M2 — routing, runtime lifecycle and HTTP efficiency: FAIL

| Gate | Status | Evidence / exact test paths / remaining scope |
| --- | --- | --- |
| Routing eligibility, priorities, access, fallback and headers | PASS | `tests/test_routing.py`, `tests/test_live_routing.py`, `tests/test_runtime_builder.py`, `tests/test_provider_user_agent.py`, `tests/test_failover_api.py`; offline regression snapshot scope. |
| Native/translated protocol, multi-tool and reasoning wire contracts | PASS | `tests/test_network_protocol_matrix.py::test_network_protocol_matrix` (nine pairs × JSON/SSE), `::test_network_reasoning_deltas` (nine pairs × opt-in/no-opt-in), `::test_network_interleaved_chat_tools_to_anthropic`; 59 related post-fix tests supplied. Finite loopback synthetic upstreams, not live providers. |
| Incremental bounded SSE and terminal/no-post-commit-retry behavior | PASS | `tests/test_translation_stream_bounds.py`, `tests/test_protocol_translation_streams.py`, `tests/test_stream_completion.py`, `tests/test_transport_double_cancel.py`; 94 bounded-streaming regressions supplied. |
| Reloads reuse transports and preserve active capacity/last-known-good | PASS | `tests/test_runtime_manager.py` plus the one actual-DB RuntimeManager node above. DB outage is injected at repository read; not an actual DB partition during repeated reloads. |
| Lower measured overhead / agreed latency acceptance | FAIL | Batched capacity reduced measured statements and short-run means, but latest two-hour pre-dispatch p95 is (100,250] ms, above the provisional 100-ms target. A universal improvement claim is unproven. |
| Deployed repeated config churn combined with long translated streams | OPEN | Unit/DB lifecycle and socket tests are complementary evidence, not a deployed combined workload qualification. |

## M3 — accounting and persistence: OPEN

| Gate | Status | Evidence / exact test paths / remaining scope |
| --- | --- | --- |
| Transactional holds, compensation, duplicate/retry settlement | PASS | `tests/test_accounting_integration.py`, `tests/test_accounting_executor.py`, `tests/test_remote_isolation.py`; bounded transaction and competing-connection evidence. |
| Durable intent survives process death and competing replay | PASS | `tests/test_remote_isolation.py::test_process_death_after_settlement_intent_is_replayed_once_by_competing_workers`, `tests/test_execution_crash_remote.py`; synthetic committed intent and subprocess death. |
| Protocol continuation persists exact per-attempt usage/cost | PASS | Nine `tests/test_workflow_protocol_matrix_remote.py` cases check reservation, outbox, request attempt, usage and cumulative workflow accounting. Prices are fixture values. |
| Healthy soak reservations settle and provider work drains | PASS | Both two-hour artifacts have `successful_reservations_settled: true`, provider active 0, peak 5 against cap 8. Does not settle every historical fault-generated unknown hold. |
| Unknown outcomes and reconciliation operational closure | OPEN | Conservative pause/reconciliation behavior is tested; plan records intentional unknown/dispatched holds from fault runs. No evidence that all operational exceptions have been resolved. |
| SQL/lock/round-trip improvement under representative load | OPEN | Plan records capacity statements 16→9 for three scopes and 31→15 for six, excluding BEGIN/COMMIT; `tests/test_capacity_integration.py`, `tests/test_remote_isolation.py`. Query-logger wall times include background work; full query-plan/lock evidence and p95 improvement gate remain incomplete. |
| Total connection budget and maintenance/reporting isolation | OPEN | Provisional 25-connection budget below; qualification runners use one/two connections. Neither proves all production pools, reporters and maintenance fit under contention. |
| Durable retention/index/telemetry operating policy | OPEN | Request-memory cleanup and a two-hour RSS pass do not establish database retention, partitioning, garbage-collection or long-term maintenance behavior. |

## M4 — distributed admission and multi-replica correctness: OPEN

| Gate | Status | Evidence / exact test paths / remaining scope |
| --- | --- | --- |
| Multiple processes share atomic capacity and reject stale owners | PASS | `tests/test_remote_isolation.py::test_separate_processes_observe_the_same_global_capacity`, `tests/test_capacity_integration.py`; committed synthetic leases, ownership and policy changes. |
| Renewal loss/outage cancels ownership without local fallback | PASS | `tests/test_distributed_lifetime.py`, `tests/test_remote_isolation.py`; real deployed packet-drop evidence in the plan. Injected call-boundary stalls alone are not network partitions. |
| Shared cooldown/half-open ownership | PASS | `tests/test_shared_circuit_remote.py`, `tests/test_route_controls.py`; bounded shared-circuit/generation fencing. |
| Interactive/workflow progress and bounded zero-dispatch retries | PASS | `tests/test_lane_fairness_remote.py`, `tests/test_mixed_workload_remote.py`, `tests/test_workflow_worker.py`; bounded jittered retries only with proof of zero dispatch. |
| General no-starvation, retry scheduling and scale envelope | OPEN | Finite fairness tests do not prove arbitrary tenant/fan-out load. Current retry loop is not a durable scheduled-backoff queue; interrupted/uncertain execution remains conservatively paused. |

## M5 — durable workflow API and workers: OPEN

| Gate | Status | Evidence / exact test paths / remaining scope |
| --- | --- | --- |
| Durable acceptance, tenant/idempotency scope, claims and checkpoints | PASS | `tests/test_workflow_api.py`, `tests/test_workflow_integration.py`, `tests/test_remote_isolation.py`; bounded synthetic HTTP/DB and SKIP LOCKED visibility. |
| Process death/connection loss/stale ownership around execution | PASS | `tests/test_execution_crash_remote.py`, `tests/test_remote_isolation.py`, `tests/test_workflow_e2e_remote.py`; actual subprocess termination with synthetic provider work and conservative uncertain-outcome recovery. |
| Multi-tool wait/results/resume with accounting | PASS | Nine DB protocol-pair cases above, including Responses-to-Chat after its fix. Does not establish streaming workflow execution or real external tool side effects. |
| Cancellation races, duplicate results and ordered replay | PASS | `tests/test_workflow_races_remote.py`; single terminal transition, duplicate result deduplication and 106 ordered replayed events across fresh stores without step re-execution, as recorded in the plan. |
| Limits, cross-tenant access and artifact boundary | PASS | `tests/test_workflow_api.py`, `tests/test_workflow_worker.py`, `tests/test_artifacts_remote.py`; bounded step/token/cost/tool and encrypted artifact fixtures. |
| Broader workflow/public contract and external integrations | OPEN | Production API versioning, representative duration/fan-out, retention/GC operations and any enabled registered tools/webhooks need explicit supported-scope decisions and evidence. No exactly-once external-side-effect promise. |

## M6 — deployment roles, resilience and operations: OPEN

| Gate | Status | Evidence / exact test paths / remaining scope |
| --- | --- | --- |
| Role separation, startup/shutdown bounds, readiness/drain | PASS | `tests/test_worker_service.py`, `tests/test_lifecycle.py`, `tests/test_draining.py`, `tests/test_dependency_health.py`, `tests/test_measured_pool.py`; bounded lifecycle tests and deployed role evidence in the plan. |
| Same-version API replacement, dependency faults and worker recovery | PASS | `tests/test_socket_restart.py`; `/tmp/opencode/aws-post-ladder-faults.json` and `/tmp/opencode/staging-direct-profile/batched-faults.json` recorded by the plan. Active SSE replacement, DB loss/packet-drop, worker kill and cleanup are configuration-bounded observations. |
| Operating evaluator logic | PASS | `tests/test_operating_envelope.py`: 127 supplied passes, including unknown observations and connection-budget blockers. Pure recommendations only. |
| Live metric collection, alerts, scaling and total pool integration | OPEN | Evaluator not integrated at this snapshot. Validate coherent observations, alert delivery, thresholds, drain review and aggregate connections before using its recommendations. |
| Actual ingress/SSE timeouts and replica expansion | OPEN | Loopback restart evidence does not qualify the eventual production ingress, autoscaler, or expanded fleet. |

## M7 — final qualification and eventual release: FAIL

| Gate | Status | Evidence / exact test paths / remaining scope |
| --- | --- | --- |
| Fresh isolated migration/schema preparation | PASS | Plan records dedicated test DB through migration 033 (34 files, two historical 003 files); `scripts/prepare_dbtest.py`, `tests/test_schema_checks.py`. This is fresh-schema evidence, not production upgrade/rollback compatibility. |
| Broad regression plus targeted network/DB fixes | PASS | Execution ledger gives exact snapshot scopes. Final combined post-fix suite/lint/build/provenance remains OPEN. |
| Two-hour healthy mixed-workload completion/accounting | PASS | Both two-hour artifacts below; synthetic providers, four closed-loop clients, separate service slice, direct DB, no fault drills during these soaks. |
| Baseline two-hour memory gate | FAIL | 125,497 calls, 13,140 completed workflows; post-warmup RSS growth 14.8%. Retained as failed baseline evidence. |
| Post-cleanup two-hour memory gate | PASS | 126,449 calls, 12,637 completed workflows; post-warmup RSS growth 7.55%, 29,671,424 bytes, within both limits. Not proof of zero leaks or indefinite stability. |
| Gateway-only pre-dispatch p95 at four clients | FAIL | Latest p95 histogram interval (100,250] ms. Total response p95 and stage means cannot substitute for this target. |
| Representative deployed translated/tool-heavy soak and saturation | OPEN | Network matrix passes finite wire contracts; a native synthetic soak does not qualify all protocol/payload/failure combinations. Separate-generator measurement still required. |
| Mixed-binary/schema rollback with accepted work readable/drainable | OPEN | Same-version restart and config rollback tests are insufficient. Need authorized isolated old/new binary overlap and rollback evidence covering accepted jobs, leases, outbox and schema compatibility. |
| Monitoring/capacity/cost operating envelope | OPEN | Provisional budget and evaluator exist; integration and real provider/infrastructure prices are missing. |
| Final immutable release artifacts | OPEN | Plan's successful earlier 0.1.0 wheel/sdist builds predate latest fixes; require final tests, source hashes and matching artifact provenance. |
| Production migration/canary/release approval | OPEN | Not authorized and excluded from current work. No release approval is implied by this report. |

## Two-hour artifacts and measurement interpretation

- Baseline: `/tmp/opencode/final-qualification/run-4-7200s-d990ebdf72d544e48021caf4d26ec339.json`.
  Duration 7,201.15 s; total-response p50/p95/p99 199.42/387.06/449.25 ms.
- After request-lifetime cleanup: `/tmp/opencode/final-qualification/retention-soak.json`.
  Duration 7,200.61 s; total-response p50/p95/p99 198.48/384.45/433.01 ms.
  Summed process RSS first/last/peak: 356,765,696/422,084,608/423,522,304 bytes.
- Memory criterion from `scripts/qualification_report.py`: discard 300 warmup
  samples; compare median samples [300:600] with the last 300 samples; require both
  growth ≤10% and ≤32 MiB. The lead-supplied 7.55%/29,671,424-byte result uses these
  windows, not raw first/last RSS. Sample timing/gaps cannot be verified from the
  untimestamped array; aggregate medians do not diagnose individual process leaks.
- Pre-dispatch spans HTTP-context entry through first upstream transport send,
  including admission/upload/auth/routing/reservation/persistence/fencing. It excludes
  upstream connect/pool/TTFT, provider work and settlement; rejected requests and
  unscoped worker calls are absent. Aggregate bucket counts establish an interval,
  not an exact percentile. Even the evaluator's inclusive ≤100-ms bound fails here;
  it cannot prove the plan's stricter “below 100 ms” at a bucket boundary.
- Three 60-second repeats are retained in `/tmp/opencode/final-qualification/` as
  `run-4-60s-7a8d60a4f4d54945b3c8b15a077d3491.json`,
  `run-4-60s-8c366518042948e48c53fb99edf935de.json`, and
  `run-4-60s-e3b8014e5e464253b817469f9c3b7fbe.json`.
  Short functional passes cannot meet the 900-sample minimum memory gate.

## Provisional capacity, operating budget and costs

The exercised staging point is four closed-loop interactive clients, two APIs, a
workflow worker and maintenance role on the plan's one-CPU ARM64 host. Provider cap
8, workflow global/per-client caps 2/1, per-API admission 16. Services have 65% CPU /
1200 MiB and the generator 15% CPU / 300 MiB slices; they still share the host.
Observed completions are not an advertised arrival-rate or saturation guarantee.

**Connection planning: 2 APIs × 5 + workers 5 + maintenance 5 + reserve 5 = 25.**
Usable planned connections are 20; reserve is not spare admission capacity. Worker
allocation must cover all workflow/accounting pools; reporting must fit an explicit
allocation. This is a provisional planning budget, not measured safe DB capacity.
Configure the evaluator's actual replica count: its default is one, maximum two.

Provisional evaluator thresholds in `gateway/operating_envelope.py`: queue age 5 s,
oldest settlement 60 s, backlog 100, DB utilization 90%, event-loop lag 100 ms,
active-request utilization 80%, provider utilization 90%, config age 300 s.
Unknown usage/dependency failure/missing observations block automatic scale advice;
low utilization below 25% calls for drain review. Tests do not activate monitoring
or autoscaling, and these defaults are not approved production SLOs.

**Cost gate OPEN:** fixture pricing and recorded synthetic settled cost units test
accounting arithmetic, not real spend. Obtain provider/model input/output/cache
prices, currency and pricing date, plus instance/DB/storage/egress/observability
charges and expected traffic/retention before deriving an operating-cost envelope.

## Highest-priority remaining gates

1. Close the four-client pre-dispatch latency FAIL and high-load rejection latency
   FAIL with independent-generator profiling and repeated bounded measurements.
2. Integrate and verify the operating evaluator's live observations, alert delivery,
   actual role/connection accounting and drain/scaling controls (M3/M6).
3. Qualify mixed-binary/schema rollback in authorized isolation, preserving accepted
   jobs and accounting; retain uncertainty/approval semantics (M5/M7).
4. Run a final combined regression/lint/build snapshot after the network fixes;
   attach exact commands, xfail rationale, logs and matching source/artifact hashes.
5. Extend deployed soak to representative translated reasoning/tool-heavy workloads;
   resolve reconciliation/retention operating policy and obtain real cost inputs.

Production migration and canary remain outside this list of authorized execution
work until explicitly approved. No M0–M7 milestone is declared blanket-complete.
