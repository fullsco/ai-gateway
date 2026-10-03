# Qualification gates — evidence snapshot

**2026-09-18: not release-qualified.** Controlled webhook delivery and installed-wheel
rollback/recovery pass in staging. Latency, representative soak, operating integration,
exact original-baseline provenance, real costs and release approval remain open.

This report follows `docs/SCALABILITY_UPGRADE_PLAN.md`, particularly M0–M7 and its
execution boundary. Dated updates supersede historical results only for their stated scope.

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
tests use `kpmxgwflgyzjvitwlfvs`. AWS staging uses `gateway_qualification` and loopback
synthetic services; the September 18 rollback drill used a fresh disposable staging DB.

**Production migration, canary, deployment and traffic replay: OPEN — excluded,
not authorized.** Staging drill success does not authorize these actions.

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

## Staging concurrency ladder — M1 admission/backlog evidence

All runs: `scripts/staging_qualify.py --seconds 120 --clients N --skip-faults
--separate-services --direct-db` on the authorized staging host, committed gateway
`9ebe182`. Evidence files are on staging under `gateway-qualification/evidence/`
(`ladder-1-120s.json`, `ladder-4-120s.json`, `ladder-8-120s.json`,
`ladder-64-120s.json`; the two failed runs have `failure.json` plus `ladder-32.log`
and `ladder-128.log`). The generator always runs in-process even with
`--separate-services`, so every point is colocated synthetic evidence, not
independent-generator qualification. Memory is **OPEN** at every 120-second point:
93–120 one-second samples against the 900-sample minimum. The 960-second translated
run below closes memory.

| Clients | Qualified | HTTP 200 | HTTP 429 | Workflows | E2E p50/p95/p99 (ms) | Pre-dispatch p95 | ≤100 ms | RSS first/peak (MB) |
| ---: | --- | ---: | ---: | --- | --- | --- | ---: | --- |
| 1 | true | 826 | 0 | 223 (202 completed) | 93.8 / 268.3 / 292.9 | (25,50] ms | 99.64% | 361.1 / 379.2 |
| 4 | true | 2,238 | 0 | 206 (202) | 182.4 / 363.7 / 412.2 | (100,250] ms | 87.80% | 361.1 / 387.8 |
| 8 | true | 2,371 | 148 | 191 (202) | 383.0 / 591.5 / 663.3 | (250,500] ms | 11.77% | — |
| 32 | **false (exit 1)** | — | — | drain failed | — | — | — | — |
| 64 | true | 803 | 16,471 | 54 (202) + 85 (429) | 231.1 / 1,235.0 / 2,030.2 | (1,5] s | 0% | — |
| 128 | **false (exit 1)** | — | — | drain failed | — | — | — | — |

Both failed runs aborted on `assert pending == 0` at `scripts/staging_qualify.py:240`
— workflow jobs still in-flight 90 seconds after load ended — and wrote
`{"error_type": "AssertionError", "message": ""}` to `failure.json` with no ladder
JSON. At 64 clients the 429 p95 was 647 ms and the surviving 200-response p95 was
2,409 ms; `route_capacity_admission` recorded 406 errors and `capacity_admission`
4. At 8 clients `route_capacity_admission` recorded 71 errors and 429 latency was
p50/p95 101.4/176.9 ms.

Per-stage combined means (ms) for the qualified points:

| Stage | 1 client | 4 clients | 8 clients | 64 clients |
| --- | ---: | ---: | ---: | ---: |
| gateway_pre_dispatch | 29.8 | 67.7 | 173.2 | 859.5 |
| durable_settlement | 8.8 | 21.2 | 56.8 | 224.8 |
| capacity_admission | 5.1 | 12.4 | 28.8 | 201.7 |
| durable_reservation | 4.8 | 11.1 | 32.1 | 139.9 |
| route_capacity_admission | 4.3 | 10.9 | 28.8 | 149.2 |
| upstream_headers | 46.2 | 49.1 | 53.8 | 81.9 |
| ownership_verify | 2.8 | 6.5 | 16.0 | 68.1 |
| durable_dispatch | 2.8 | 6.3 | 15.1 | 69.8 |
| shared_circuit_allow | 1.8 | 4.1 | 10.7 | 56.3 |
| db_pool_wait | 0.1 | 0.1 | 1.1 | 24.2 |
| event_loop_lag | 0.7 | 1.4 | 3.0 | 15.4 |

Findings: admission protects the system above roughly 8 concurrent clients — at 64
clients 95% of responses were 429s — but every measured latency target degrades
monotonically with concurrency, and the 100-ms pre-dispatch target is only met near
idle (99.64% at one client). Workflow drain with a single worker cannot keep up at
32+ clients; the failure is queue backlog under 429 pressure, not a crash or leaked
accounting. After the two failed runs, the one job left in `waiting_for_approval`
(`f35f1b7c-68d6-4f1e-ab46-8a9c4f613d57`, client `8fc303ae…cde5f72521f4`) had
checkpoint `{"reason": "inference_failed", "http_status": 429}` and no reservations,
confirming the backlog origin. It was expired manually; the staging DB was then
verified clean (27 cancelled / 36,576 completed / 3 expired jobs; 381,973 settled /
22 unknown reservations, the unknowns all pre-existing from September 15).

This reproduces and extends the M1 finding on staging with the current revision. It
does not close any gate: latency stays FAIL by operator direction, memory stays OPEN
for insufficient samples at every 120-second point, and separate-generator evidence
is still required.

### September 21: translated 960-second soak — memory gate closed

`scripts/staging_qualify.py --translated --seconds 960 --clients 4 --skip-faults
--separate-services --direct-db` on the same host and revision. Evidence:
`evidence/ladder-4-960s.json` (`run-4-960s-2cc34b6f991b4799a487841295bb2463.json`).

| Gate | Result |
| --- | --- |
| Overall | **PASS** (qualified) |
| Memory (900-sample minimum) | **PASS** — 953 samples; window medians 390.2 → 393.4 MB; growth 3.2 MB (0.83%) |
| Pre-dispatch ≤100 ms | **PASS** — 13,735 / 14,336 = 95.81% at or below target; p95 (0.05, 0.1] s |

Traffic: 14,336 HTTP responses, all `200`, zero errors. Provider served 14,976
translated turns with peak active 5 and zero validation failures. 320 workflow
submissions, all completed. E2E p50/p95/p99 = 299.0 / 493.6 / 529.8 ms. The
translated-coverage phase exercised all nine two-turn scenarios
(anthropic_messages / openai_chat_completions / openai_responses × json / sse /
workflow), 106 started and 106 completed each, 0 overloaded, 0 failed. RSS
361.1 → 396.5 MB peak. The memory gate for this configuration is now **PASS**;
it does not retroactively change the 120-second ladder points, and the latency
FAIL and independent-generator requirements are unaffected.

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
| `/tmp/opencode/run-4-120s-dcecf5db3bd84e8ca64fe40e15405720.json` (staging, autocommit shared-circuit closed path + `--trace-queries`) | 2,312 | 63.96 ms (147.8714 s over 2,312 requests) | 90.31% (1,037+1,051 of 2,312) | 356.14 ms |

The earlier warm-row run had a 60.50 ms pre-dispatch mean and additionally enabled
`--profile`; these executions are not fully matched controlled comparisons.
Observed differences do not establish an outlier, identify a causal regression,
or rule one out. Means do not close the failing tail-latency gate. These short
runs also cannot establish memory stability or independent-generator qualification.

The autocommit shared-circuit closed path (single `SELECT … FOR UPDATE` statement
with no surrounding transaction on the warm path; select-first admission skips the
redundant insert) was qualified on staging in the latest direct-DB run above. Its
`shared_circuit_allow` stage recorded zero errors with 76.34% of samples ≤5 ms under
soak, and pre-dispatch stayed inside the previously observed 62.7–73.2 ms mean
variance band. The p95 remains bounded in (100,250] ms; the latency gate is still
FAIL and no causal claim between the change and the mean movement is made.

The subsequent 1,800-second run is **INVALID for stable-resource qualification**:
`/tmp/opencode/run-4-1800s-8902cdda017046d08354f68d147cfcab.json`
(SHA-256 `64c046329e888efc42f253938b33bb3e2d61e9c4bb77650c7288d83d580a990d`).
During this run the operator incorrectly limited the ancestor `gateway.slice` to
15% CPU / 300 MiB, constraining the services rather than the generator. The
20,041 HTTP 200 responses and 2,690 completed workflows remain functional
observations, but its 1,104.85 ms response p95 and RSS cannot establish a
stable-budget comparison or close memory qualification. Parent limits were
subsequently reset and verified as unlimited; the service slice retained
65% CPU / 1,200 MiB. The original generator ran outside a capped generator unit.

A five-minute recovery check then completed with unchanged limits: generator
unit `qualification-generator-recovery` in `system.slice` at 15% CPU / 300 MiB,
services at 65% CPU / 1,200 MiB, and unlimited `gateway.slice` parent limits.
Command: `scripts/staging_qualify.py --seconds 300 --clients 4 --skip-faults
--separate-services --direct-db`. Staging evidence:
`evidence/run-4-300s-609cdd315dd24fb6963d06201b0a786a.json`.
Functional validation passed with 5,865 HTTP 200 responses and 520 completed
workflows; response p95 was 352.55 ms. Pre-dispatch p95 remains **FAIL**, bounded
in (100,250] ms, with 5,397/5,865 samples (92.02%) within 100 ms. Memory remains
**OPEN** with 298 samples, below the required 900. These colocated, synthetic,
fault-skipping checks do not close independent-generator or representative soak
qualification. Cleanup verified zero tasks in the staging slices and active
pre-existing API, dashboard and nginx services. No further soak is scheduled.

### Current-revision follow-up: schema 036 and translated journeys

External delivery remains **OPEN**; the operator selected **staging receiver only**.
The opt-in worker/outbox (`ed01be5`, sequence fix `9ebe182`) passed six dedicated-DB tests.
`scripts/staging_webhook_check.py` passed with real DB and verified loopback TLS:
two unique events, seven attempts, real retry backoff and a 31.92-second lease-expiry wait.
Ack/upserts were suppressed; crash-after-ack duplicate, stale token and dead-letter cases passed.
Invalid signed input was rejected. Receiver data/secrets were removed; service delivery stays disabled.
Evidence: `/tmp/opencode/webhook-delivery-drill.json`,
SHA-256 `45927751b86cc812172f881e5cc2c24279eb26eb3850018391e4b563cd8ea0c7`.

Staging received gateway source `9ebe182` and migrations 035/036. Both journal
entries, enabled capture/sequence triggers, the outbox table and subscribed sequence
guard were verified. Operations evidence: `/tmp/opencode/operations-final-post036.json`,
SHA-256 `cac5b1b05b28ff8db06595087648fdb4e938340683c2356f4a05053bbce2815c`.
The operations probe exited 0 with `qualified: true`. No production action occurred.

Source-process switches `0bac80e ↔ 9ebe182` passed on the dedicated DB (2 passed, 60 deselected).

**Installed-wheel rollback/recovery: PASS for committed `9ebe182 → 230183e → 9ebe182`.**
Separate Python 3.12.3 environments verified every installed gateway file and matching dependencies.
Temporary loopback nginx switched inference with old/new binaries overlapping; active SSE completed.
New workflows were blocked during rollback; retained current job API/workers drained two accepted jobs.
The tool workflow dispatched twice, the queued workflow once; settlement replay did not double-charge.
Forward recovery restored inference and workflow acceptance while retaining prior idempotency keys.
Seven lifecycle/pool closures passed; 0.33 fixture charges plus the 0.50 unknown hold stayed accounted.
All 37 migrations through 036 stayed applied with an unchanged journal; disposable DB/ports cleaned up.
Evidence: `/tmp/opencode/release-rollback-verified.json`,
SHA-256 `62d1e2e428cbb29878f92c89ad98085eca372e1358dbbe544438c09f06f86dd2`.
First-attempt failure is retained in `/tmp/opencode/release-rollback-failed-first.json`: the harness
needed an nginx listener-handoff barrier and shutdown evidence before Uvicorn re-raised SIGTERM.
Exact original-baseline rollback stays **OPEN**: `230183e`'s unrecorded config-cache changes are excluded.
The drill uses synthetic runtime/provider configuration; production ingress/configuration is unqualified.

A bounded translated staging check completed with invocation
`c910896b13304f7480f8ca9ab67433bd`, exit 0. Command:
`scripts/staging_qualify.py --translated --seconds 120 --clients 4 --skip-faults
--separate-services --direct-db`. Evidence: `/tmp/opencode/translated-120-output.json`,
SHA-256 `b19d969aab700fffc9dc6500221c6c4c1dbfcfac74e65e7a4516654119358ccf`.
The generator ran in `system.slice` at 15% CPU / 300 MiB, services retained
65% CPU / 1,200 MiB, and the parent was verified unlimited before and after.
No qualification service units remained afterward.

| Client protocol → Chat upstream | JSON journeys | SSE journeys | Durable workflows |
| --- | ---: | ---: | ---: |
| Chat | 150 | 151 | 14 |
| Responses | 150 | 151 | 14 |
| Anthropic Messages | 150 | 150 | 14 |

Each journey verifies fixed synthetic reasoning, two tool-call IDs/arguments,
matching canned results, and a second-turn final answer. Tools are never executed.
All nine scenarios had zero failed/overloaded journeys: 1,804 HTTP 200 responses,
42 completed workflows, settled successful reservations, zero provider transcript
validation failures, and 1,888 translated-profile provider turns. The independent
report validator rejects missing scenarios, failures and unfinished journeys.

This profile's pre-dispatch p95 was (50,100] ms, with 1,740/1,804 samples (96.45%)
within 100 ms. Total-response p95 was 492.25 ms. **The existing latency gate stays
FAIL/open**: different prompt sizes, provider pacing and sequential tool workflows
make this unsuitable as a matched comparison with the earlier failing native run.
Memory stays **OPEN** (119 samples versus 900 required). This is bounded synthetic
translated-journey evidence, not representative production traffic, long reasoning
streams, an independent generator, real-model quality/cost, or release approval.

Source provenance: unchanged committed gateway `9ebe182` plus reviewed uncommitted
staging scripts, copied individually and hash-matched before execution:

- `staging_service.py`: `79114c49ee08ba016b37da4d2bdff10def301354d0de10991aaf93edb78aa577`
- `staging_qualify.py`: `2616a3608e8e3f8763ac19d19b5021b98955aedea1d26a431adfcbd2b5e52418`
- `staging_translated.py`: `d88d7196f518e2a911168ab0479fcae52911e554f94d90b02f6ff8c0682768fb`
- `staging_workload.py`: `a199d329f4de5b4c317e09065a8685d7bee88e6beb734af84bf469b92ff0f234`
- `qualification_report.py`: `4fc6bdc6dff7ca237e3016fb5dcd22040fd0142fe57a80db18b5b0be00d324ba`

September 18 verification: 1,464 offline tests passed, 101 integration tests deselected;
Ruff, diff checks and wheel/sdist build passed. Qualification changes were committed as
`32ab993` on September 21.

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

Historical counts are separate snapshots, not inferred from current test collection.

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
| Deterministic offline baseline | PASS | Historical 989/69/1 snapshot; September 18 guarded offline regression: 1,464 passed, 101 integration cases deselected. |
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
| Prompt overload response at the tested high-load ladder | FAIL | Plan's 1/8/32/64/128-client ladder records 429 p95 up to 2,838.63 ms. The September 21 staging ladder on committed `9ebe182` (section above) reproduces this on staging: 429s begin at 8 clients, 95% of responses are 429 at 64 clients with 429 p95 647 ms, and the 32/128-client points fail workflow-drain assertion outright. Functional boundedness does not close prompt-rejection performance. |
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
| Broad regression plus targeted network/DB fixes | PASS | September 18: `scripts/dev_check.py` 1,464 passed/101 deselected; Ruff excluding the two credential scripts, diff check and wheel/sdist build pass. Remote tests retain their separate execution scopes. |
| Two-hour healthy mixed-workload completion/accounting | PASS | Both two-hour artifacts below; synthetic providers, four closed-loop clients, separate service slice, direct DB, no fault drills during these soaks. |
| Baseline two-hour memory gate | FAIL | 125,497 calls, 13,140 completed workflows; post-warmup RSS growth 14.8%. Retained as failed baseline evidence. |
| Post-cleanup two-hour memory gate | PASS | 126,449 calls, 12,637 completed workflows; post-warmup RSS growth 7.55%, 29,671,424 bytes, within both limits. Not proof of zero leaks or indefinite stability. |
| Gateway-only pre-dispatch p95 at four clients | FAIL | Latest p95 histogram interval (100,250] ms. Total response p95 and stage means cannot substitute for this target. |
| Representative deployed translated/tool-heavy soak and saturation | OPEN | Network matrix passes finite wire contracts; a native synthetic soak does not qualify all protocol/payload/failure combinations. Separate-generator measurement still required. |
| Mixed-binary/schema rollback with accepted work readable/drainable | PASS for pinned wheels; exact baseline OPEN | `scripts/release_rollback.py`: installed `9ebe182 → 230183e → 9ebe182`, nginx overlap/SSE, retained job services, settlement replay and unchanged schema 036 passed. Original unrecorded config-cache changes and actual deployment configuration are excluded. |
| Monitoring/capacity/cost operating envelope | OPEN | Provisional budget and evaluator exist; integration and real provider/infrastructure prices are missing. |
| Final immutable release artifacts | OPEN | Pinned baseline/current wheels are hashed and installation-verified in `/tmp/opencode/release-rollback-20260918/manifest.json`; the current gateway is `9ebe182` with qualification work committed as `32ab993`. Final release approval is pending. |
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

1. Keep latency FAIL open per operator direction; independent-generator evidence is still needed.
2. Integrate live operating observations, role/connection accounting and scaling controls;
   external webhook delivery requires an approved destination beyond the staging receiver.
3. Recover the original baseline's unrecorded changes and qualify actual deployment configuration;
   the pinned-wheel rollback drill now provides bounded accepted-job/accounting evidence.
4. Freeze the reviewed qualification harness and attach matching final artifact provenance.
5. Qualify representative long translated reasoning/tool workloads, retention and real costs.

Production migration/canary remain approval-dependent. No M0–M7 milestone is blanket-complete.
