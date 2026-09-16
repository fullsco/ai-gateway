# AI Gateway scalability and workflow upgrade

Status: development implementation and bounded deployed staging qualification verified;
remaining performance/operational gates open (not release-approved).
Production release: deferred until all release gates pass and cutover is scheduled.
Baseline: `230183e` plus pre-existing, uncommitted configuration-cache changes.

## Objective and execution boundary

Support large agent workloads and high request volumes through a bounded interactive
inference lane and a durable asynchronous workflow lane. Preserve OpenAI Chat,
OpenAI Responses, Anthropic Messages, streaming, tools, reasoning, client permissions,
curated credential pools, routing priorities, provider headers, and usage accounting.

All implementation, migrations, seeds, benchmarks, and verification run in development
or explicitly authorized isolated staging. SSH verification on 2026-09-13 found production `ai-gateway.service` at
`100.62.162.5` selects `nebbtrmwzbpmzvinhrch`, the same project as the local `.env`.
The earlier identification of this project as separate development was incorrect.
Do not run database qualification against it. The separate development database is
`rnherugormxtrmxlerjj` (`ai-gateway-dev`, eu-west-1), verified healthy and authorized
by the user. `.env.dev` supplies its transaction-pooler URL as `DEV_DB` and a management
`TOKEN`; only the database URL is consumed by qualification. Inherited shell database
settings point elsewhere and must not be used.
Dedicated isolated qualification uses `kpmxgwflgyzjvitwlfvs` (`ai-gateway-tests`,
eu-west-1), provided by the user in `.env.dbtest` as `POOLER` and `TOKEN`. This project
was verified empty before applying migration files and synthetic fixture rows. The dedicated
project is now migrated through 033 (34 files, including the two historical 003 files).
Use `.venv/bin/python scripts/prepare_dbtest.py --env-file .env.dbtest` to verify/reuse
its checksum-tracked preparation. The script refuses other projects and untracked schemas.
Local PostgreSQL initialization or daemon startup is prohibited in this Pro Distro/OpenCode
environment: an attempted startup froze the session. Do not use `initdb`, `pg_ctl start`,
temporary PostgreSQL instances under `/tmp/opencode`, or other local database daemons.
Use offline mocks/static checks for non-engine behavior and the verified remote dev service
for PostgreSQL semantics. Run remote qualification serially with one connection, bounded
waits, synthetic fixtures, and rollback. Cross-connection/crash tests require an isolated
remote clone/schema; single-transaction tests are not evidence of cross-process correctness.
The dedicated test project supports `scripts/dev_db_check.py --env-file .env.dbtest
--project-ref kpmxgwflgyzjvitwlfvs --isolated`: at most two connections, committed synthetic
data with cleanup. Verified tests cover competing capacity acquisition, SKIP LOCKED
claiming/event visibility, rollback on client connection loss, actual worker subprocess
death after claim/completion, and process death after durable settlement intent followed
by competing replayers. Eight isolated tests passed; six qualification tables were empty
after cleanup. Renewal refusal/stall is injected at the database-call boundary and cancels
the owner; this is not a real network partition test. Synthetic lease expiry is accelerated.
Worker crash probes replace inference with a synthetic step; external-provider side effects
and full end-to-end worker recovery remain separate qualification requirements.
Use isolated disposable databases, synthetic configuration/keys, mock upstreams,
loopback listeners, and separate state directories. Never publish test configuration
to the shared database. No production service restart, deployment, or traffic replay.

The user subsequently authorized AWS staging at `52.72.241.2` (hostname
`ip-172-31-42-222`). Its isolated workspace is `/home/ubuntu/gateway-qualification`;
the separate `gateway_qualification` database uses the host's existing PostgreSQL 16.
The pre-existing `rig` database and other project services are outside qualification.
Staging scripts refuse other hostnames, disable dotenv, use synthetic credentials,
and expose only loopback ports. Credentials remain in mode-0600 staging files.
Database partition drills target only the dedicated TCP proxy port 18432.

Existing work in config refresh/cache files and deploy scripts must be preserved and
qualified independently; it is not automatically part of this upgrade's evidence.

## Architecture

```text
Clients -> load balancer -> interactive API replicas -> provider APIs
                              |       |
                        admission   durable financial reservations
                              |       |
Workflow API -> PostgreSQL jobs/checkpoints/outbox -> workflow workers
                              |                       |
                         object storage         inference/tool steps
                              |
                  telemetry and maintenance workers

Optional Redis/Valkey: ephemeral distributed counters, leases, health propagation
```

Keep FastAPI/httpx initially. API and workflow workers scale independently.
Start with a PostgreSQL-backed job queue; introduce another broker only when measured
queue contention or delivery requirements justify it. Durable workflow state and
financial accounting remain authoritative in PostgreSQL. Distributed limits are
required before more replicas may consume a shared provider concurrency allowance.

## Milestones and acceptance criteria

### M0 — isolated baseline and instrumentation

- Provide a repeatable dev qualification command that cannot inherit the shared DB.
- Use a disposable remote PostgreSQL clone/schema with synthetic auth/configuration fixtures;
  never start PostgreSQL inside this environment.
- Diagnose existing integration failures there, without production data dependency.
- Benchmark native and translated APIs: short calls, long streams, long reasoning
  pauses, large contexts/tool results, bursts, slow readers, disconnects, and failures.
- Measure ingress/parse, authentication, token estimation, routing, DB pool waits,
  reservations, HTTP pool waits, upstream TTFT, stream relay, settlement, and total time.
- Record hardware, Python/dependency versions, payload distribution, concurrency,
  throughput, p50/p95/p99, RSS, event-loop lag, retries, and expected overload responses.
- Add low-cardinality metrics and sampled traces; keep request/client IDs out of metric
  labels and keep prompt/tool content out of telemetry by default.

Gate: deterministic offline suite and reproducible dev baseline. Mock throughput is
gateway capacity evidence, not an end-to-end provider throughput claim.

### M1 — bounded interactive admission and large-body handling

- Enforce per-process in-flight requests before expensive parsing/DB/upstream work.
- Reject saturated admission immediately with 429 and Retry-After; do not create an
  unbounded waiting queue. Keep health endpoints available during overload.
- Enforce actual upload bytes, including chunked/no Content-Length requests, and a
  total upload deadline. Bound combined admitted input bytes as well as per-request size.
- Hold admission through the full SSE lifetime and release on all cancellation/error paths.
- Add per-client fair admission and separate long/short workload capacity after auth.
- Add model-aware input/output/tool limits. Character-based estimates are not accurate
  tokenizer guarantees. Multimodal payloads need separate size/capability treatment.
- Do not silently compact or modify user prompts; compaction is an explicit workflow policy.

Gate: burst rejection is prompt; slots return after errors/disconnects; bounded payload
tests pass across all protocols; small requests remain serviceable under mixed load.

### M2 — routing, runtime lifecycle, and HTTP efficiency

- Index models by canonical ID/protocol and credentials by provider/route membership.
- Preserve every eligibility rule, priority, weight, access restriction, and fallback.
- Avoid repeated token-estimation serialization and unnecessary large payload copies.
- Make full routing traces sampled/opt-in while retaining diagnostic failure traces.
- Configure connection/keepalive/pool limits and connect/read/write/pool timeouts.
- Separate provider capacity from HTTP connection capacity and egress-proxy capacity.
- Reuse transports safely across configuration updates; retire unused runtime resources
  after active requests drain. Current retired clients remain until process shutdown.
- Preserve active concurrency state across snapshot refreshes.
- Qualify checksum-first config cache WIP and last-known-good behavior under outages.
- Keep SSE incremental, preserve tool/reasoning/usage events, bound parser buffers,
  and propagate disconnects upstream. Never retry after committed client stream output.

Gate: identical routing/translation outcomes and lower measured gateway overhead;
repeated config reloads do not accumulate clients or reset active capacity limits.

### M3 — accounting and persistence

- Measure SQL plans/locks/round trips before tuning the current five-connection pool.
- Budget total DB connections across replicas, workers, reporting, and maintenance.
- Combine related financial reservations transactionally, with stable reservation IDs.
- Compensate partial reservation failures; settle each attempt once, including retries.
- Persist settlement intent durably before acknowledgment; use a transactional outbox,
  deduplication keys, retry/reconciliation, and explicit unknown-usage handling.
- Keep strict financial limits authoritative; optional telemetry must not be required
  for inference. Do not treat an in-memory queue as durable billing delivery.
- Batch/coalesce passive health and ordinary telemetry; preserve transition ordering.
- Add retention, appropriate indexes, and later partition high-volume event tables.
- Isolate reporting/maintenance contention from critical reservations.

Gate: crash/duplicate/retry tests show no duplicate financial settlement or silent loss;
reconciliation resolves incomplete reservations; query latency improves under load.

### M4 — distributed admission and multi-replica correctness

- Add tenant, provider, route, credential, and workload-class limits with explicit scope.
- Use atomic TTL leases with ownership tokens, renewal, release, and fencing where
  applicable. Lease expiry alone must not allow stale workers to keep dispatching.
- Share cooldown/recovery state; bound half-open probes to avoid recovery stampedes.
- Define dependency-outage behavior explicitly. Do not silently fall back to independent
  per-process counters when a configured global cap is required.
- Schedule fairly between clients and interactive/workflow lanes; add retry budgets,
  exponential backoff/jitter, deadlines, and controlled load shedding.

Gate: tests using multiple worker processes enforce the same global cap, recover
abandoned work, and demonstrate that one client cannot starve the interactive lane.

### M5 — durable workflow API and workers

Proposed API (versioned before public release):

```text
POST /v1/workflows                  -> 202 after durable acceptance
GET  /v1/workflows/{id}              -> tenant-scoped status/checkpoint/usage
POST /v1/workflows/{id}/cancel
POST /v1/workflows/{id}/resume
POST /v1/workflows/{id}/tool-results
GET  /v1/workflows/{id}/events       -> ordered, resumable event stream
```

- Store jobs, steps, attempts, checkpoint versions, leases, terminal results, outbox
  entries, and cumulative usage/cost. States: queued, running, waiting_for_tool,
  waiting_for_approval, completed, failed, cancelled, expired.
- Scope idempotency keys to tenant + operation; reject key reuse with different input.
- Claim bounded work via SKIP LOCKED, persist transitions and next-job enqueue atomically,
  and use lease ownership/version checks for all commits.
- Define cancellation races, deadlines, transient retry eligibility, dead-letter handling,
  and maximum steps/tool calls/tokens/spend. Resume uses a validated checkpoint.
- Support externally executed tools first. Registered worker tools require explicit
  capabilities, credential isolation, input validation, and bounded execution.
- Persist tool invocation IDs/results; do not promise exactly-once external side effects.
  After uncertain upstream/tool completion, reconcile or pause rather than blindly replay.
- Use object references for large artifacts, with tenant authorization, hashes, byte
  limits, encryption, retention, and garbage collection. Never fetch arbitrary URLs.
- Optional webhooks require signed deliveries, deduplication, retry bounds, and destination
  validation; event replay must not re-execute steps.

Gate: process death before/after dispatch/commit, duplicate submissions, expired leases,
lost connections, tool waits, cancellation, and cross-tenant access are all verified.

### M6 — deployment roles, resilience, and operations

- Split API, workflow, telemetry, and singleton/leased maintenance roles.
- Keep per-instance config refresh, but avoid multiplying probes/polling/alerts per replica.
- Add graceful draining, readiness, explicit dependency state, resource limits, and
  autoscaling based on active streams/event-loop lag plus queue age/provider capacity.
- Test API-worker fairness and total connection budgets before increasing replica count.
- Align ingress idle/request timeouts with SSE keepalives; workflow execution continues
  durably without requiring a live client connection.
- Add alerts for queue age, pool exhaustion, event-loop lag, settlement backlog, dropped
  health events, config staleness, unknown workflow outcomes, and provider failures.

Gate: dev rolling restart and dependency fault tests pass; processes drain or recover
without double execution/accounting and without unbounded shutdown waits.

### M7 — qualification and eventual production release

- Run protocol/tool/reasoning contracts, auth/routing regressions, fresh migrations,
  multi-process contention tests, failure injection, mixed-workload load tests, and soak tests.
- Include healthy traffic, provider outage, rate-limited credentials, slow upstreams,
  fragmented SSE, huge tool payloads, slow readers, DB/coordination outage, worker crashes,
  duplicate jobs, config reloads, and settlement replay.
- Build versioned artifacts only after checks pass; review migrations for expand/contract
  compatibility. Do not change production version identifiers before release qualification.
- Document capacity envelope, operating costs, supported workflow semantics, known limits,
  rollback/draining behavior, and monitoring thresholds.
- Ship the new version only after development is complete and the release is approved.
  Canary, observe, then expand. Rollback must keep already-accepted jobs readable/drainable.

## Capacity targets to validate

Initial benchmark ladder: 1, 8, 32, 64, 128 concurrent requests, increasing only within
measured resource budgets. Include both sustained load and bounded bursts; retain rejected
requests in reports. Use 8 KiB, 256 KiB, 1 MiB, and maximum-size synthetic input classes.

Provisional objective: p95 gateway-only pre-dispatch overhead below 100 ms at the agreed
load. Provider TTFT and total latency are separate measurements; a universal 2-second
TTFT is not an achievable gateway guarantee for reasoning models. Establish p99 and
availability/error budgets from the baseline. No advertised RPS/concurrency before tests.

Concurrency approximately equals arrival rate times average request duration. Capacity
planning must account for active streams, aggregate input bytes, parsed object expansion,
SSE state, HTTP/DB connections, provider quotas, and proxy bandwidth, not RPS alone.

Open decisions: peak RPS/streams, workflow duration and fan-out, instance budget, regional
recovery requirements, retention, Redis availability, and webhook requirements. Proceed
with conservative configurable defaults and local mocks until these are measured/confirmed.

## Development progress

- Plan saved; production remains deferred.
- First increment: offline qualification runner and per-process interactive admission.
- Nine existing PostgreSQL tests, then ten including a synthetic publication test,
  passed against `nebbtrmwzbpmzvinhrch` before SSH verification exposed that it is used
  by production. These runs are not isolated-development qualification evidence.
  Configuration writes rolled back; identity sequences can have advanced.
- Production `.env` predates the running service start and resolves to environment
  `production` and project `nebbtrmwzbpmzvinhrch`; the process has no DB env override.
- `scripts/dev_db_check.py` requires an explicit env file/project reference, rejects
  the known production project and mismatched targets, discards inherited gateway
  settings, and runs only the PostgreSQL integration files. The verified dev target
  is `rnherugormxtrmxlerjj`; use:
  `.venv/bin/python scripts/dev_db_check.py --env-file .env.dev --project-ref rnherugormxtrmxlerjj`.
  Supavisor transaction-pooler connections on port 6543 disable asyncpg statement caching.
- Verified development qualification: all 10 PostgreSQL integration tests passed on
  `rnherugormxtrmxlerjj` through `.env.dev`, including rollback-scoped synthetic
  publication/cache-refresh tests. No configuration changes were committed.
- Latest full offline regression (2026-09-15): 827 passed, 59 database tests deselected,
  in 101.94 seconds through `scripts/dev_check.py`. Ruff passes across gateway/tests
  and the qualification scripts; `git diff --check` passes. Wheel/source builds pass.
- Implemented M0/M1: bounded pre-upload admission, actual-byte/deadline limits,
  per-client and stream capacities, model context estimates, protocol-correct output
  ceilings, tool/media limits, stage histograms, sampled bounded traces, protected
  operational endpoints, and native/translated mock benchmarks.
- Implemented M2: model/protocol and credential indexes, shared transports across
  snapshot reloads, preserved route-capacity state, explicit HTTP pool/timeouts,
  bounded SSE framing, successful-terminal evidence, and no post-commit failover.
- Implemented M3: transactionally coupled financial holds, exact reserved windows,
  durable settlement intent and retries, independent internal telemetry correlation,
  unknown-usage reconciliation, token/cost correction, and admin reconciliation.
- Implemented M4: PostgreSQL multi-scope TTL ownership, predispatch revalidation,
  cancellation on renewal failure, generation-fenced shared circuits, optimistic
  capacity-policy updates, per-tenant workflow bounds and reserved interactive share.
- Implemented M5: tenant/idempotency-scoped jobs, leases and checkpoint versions,
  tool waits/results, explicit approval after uncertain execution, cumulative cost/
  token/step/tool bounds, encrypted artifact references and bounded event replay.
- Implemented M6 foundations: API/workflow/maintenance roles, combined-worker support,
  bounded resource cleanup on failed startup/shutdown, drain/readiness integration,
  schema gates, isolated staging env selection and opt-in tunnel/worker profiles.
- Remote qualification includes real transaction semantics, concurrent capacity and
  budget reservations, two independent capacity processes, SKIP LOCKED, stale-owner
  rejection, actual worker death after upstream acceptance/usage persistence, durable
  intent replay, all three workflow protocols with mocked provider transports,
  multi-tool continuation, large artifacts, workflow budget exhaustion, and combined
  lifespan execution. All test objects are synthetic and explicitly cleaned up.
- Additional verified gates: real mixed interactive SSE/workflow execution across two
  tenants sharing a capacity-two provider, repeated with 8 KiB and 256 KiB input;
  both lanes completed and all four charges settled. Finite TCP process replacement
  preserved an old in-flight SSE while the replacement served new requests.
- Review fixes qualified: parent-first financial/artifact locking; exact internal
  execution correlation; cancellation-safe local capacity cleanup; terminal SSE evidence;
  protocol-specific output ceilings; consumed tool-result checkpoints; renewed half-open
  ownership; persisted-parent-aware workflow shares; drain waits through lease release.
- Cancellation/completion races produce one terminal event; concurrent duplicate tool
  results produce one transition; event replay paginates 106 ordered events across
  fresh store instances without changing workflow state.
- Deployed partition testing exposed unbounded pool lifecycle waits and repeated
  cancellation leaking an HTTPcore upstream socket. Connection/acquire/release waits
  are now bounded; lease ownership includes streaming work; stream-reader and cleanup
  ownership are shielded against repeated cancellation. Real loopback socket regression
  tests cover double cancellation with and without keepalive.

## Current capacity evidence and remaining release gates

- `/tmp/opencode/scalability-post-review.json`: 200 in-process mock cases, 9,320
  attempted requests, all capacity drained, peak admitted requests 8, sampled peak
  RSS 82.5 MiB, maximum case overload-response p95 1.034 ms. Deliberate overload
  dominates this matrix; these are not provider throughput or production SLO claims.
- Additional bounded matrices: `scalability-final-burst-small.json` (132 cases),
  `scalability-final-burst-high.json` (80), `scalability-final-large-input.json` (16),
  `scalability-final-repeated.json` (16). The TCP regression qualifies fragmentation,
  slow reads and disconnect recovery over a finite loopback server.
- `/tmp/opencode/scalability-bounded-soak.json`: 60.5 seconds, 250 serial mock batches,
  4,000 attempted requests, bounded four-request admission and eight-request worker
  bursts; all scenarios qualified. This is short-duration resource-safe stability
  evidence, not a representative multi-hour or deployed-role soak.
- `/tmp/opencode/scalability-accounting-contention.json`: six real financial attempts
  with one/two workers and at most two remote connections; all six settled exactly.
  Median end-to-end DB sequence time was 7.90/8.84 seconds over the development WAN.
  This does not meet or establish the provisional <100 ms gateway-overhead target.
- AWS deployed evidence (2026-09-15): `/tmp/opencode/aws-staging-600s.json` and
  `/tmp/opencode/aws-staging-metrics-120s.json`. Both runs completed successfully under
  systemd; subsequent runs replace remote `evidence/qualification.json`.
  Hardware: ARM64, one CPU, approximately 3.8 GiB RAM and 2 GiB swap; Python 3.12.3.
  Qualification unit limits: 80% CPU and 1500 MiB memory; PostgreSQL runs separately.
  Topology: two API processes, separate workflow/maintenance processes, actual HTTP
  synthetic provider and TCP DB proxy, with colocated PostgreSQL.
- Workload: four closed-loop interactive clients alternating streaming/non-streaming,
  20% 256-KiB inputs, requested provider delays of 30/200 ms, plus workflow submissions
  every 500 ms with 100-ms synthetic provider delay. Provider concurrency cap 8;
  workflow global/per-client caps 2/1; per-API admission cap 16. This is an exercised
  operating point, not a saturation measurement or open-loop arrival-rate guarantee.
- Ten-minute run: 600.585 seconds, 8,984 HTTP 200s (14.96 interactive completions/s),
  1,128 workflows all completed (1.88/s), no transport errors. End-to-end interactive
  p50/p95/p99: 241.01/425.79/465.74 ms, including provider delay and complete streams.
  Summed process RSS first/last/peak: 355,901,440/393,785,344/394,313,728 bytes.
  Systemd separately reported 317.2 MiB cgroup peak and zero swap peak; these are
  different memory measurements. Ten minutes does not establish long-term RSS stability.
- Metrics run: 120.798 seconds, 1,653 HTTP 200s, 224 workflows all completed;
  end-to-end p50/p95/p99 263.98/450.41/528.29 ms. Weighted stage means across both APIs:
  authentication 0.181 ms, routing 0.134 ms, durable reservation 16.390 ms, durable
  settlement 31.103 ms, DB acquire wait 0.096 ms, event-loop lag 1.207 ms. Captured
  stage error deltas are zero. Means cannot establish p95 pre-dispatch overhead;
  nested stages and mixed endpoint counts must not be summed into a latency percentile.
  Alternating requests tie streaming to one replica, so per-replica upstream-header
  means also represent different workload types. No before/after optimization claim.
- Both deployed runs passed active-SSE rolling API replacement, DB proxy connection
  loss/recovery, real packet-drop partition/recovery, worker kill/restart and drain
  readiness. Live-stream partition cancelled upstream at 22.14 s and terminated
  downstream at 25.98 s in the metrics run. The killed worker's uncertain job paused
  for approval with one reservation and no blind redispatch. These timings bound this
  configuration's observed fault response, not a universal cancellation deadline.
- Metrics-run pre-fault assertion verified no succeeded request linked to an unsettled
  reservation. Final cumulative synthetic accounting: 19,654 settled, 15 unknown,
  one dispatched, including prior debug runs and intentional faults. These unresolved
  holds remain evidence for reconciliation; they must not be silently refunded.
  Run deltas: 1,883 request logs, 225 jobs, 18.81 synthetic settled cost units.
  Ten-minute run deltas: 10,118 logs, 1,129 jobs, 101.16 synthetic cost units.
  Synthetic charges are not infrastructure or real-provider operating costs.
- Essential outstanding qualification: representative multi-hour soak and independently
  driven saturation tests, long reasoning/tool-heavy and translated-protocol deployed workloads,
  meeting the p95 gateway-only pre-dispatch target under load, query-plan/
  round-trip optimization evidence, and complete monitoring/autoscaling thresholds
  and operating-cost envelope. The bounded deployed runs close the prior infrastructure
  and restart/partition evidence gap, but do not close these broader release gates.
- Final staging cleanup verified: qualification systemd unit inactive, ports
  18330/18331/18340/18432 absent, OUTPUT chain restored to ACCEPT with no drill rule.
  Existing PostgreSQL, dueted-api, outflo-web and nginx services remain active.
- Earlier ten-minute/metrics source provenance: staging scripts, distributed capacity and both modified pool
  modules match local SHA-256 hashes. Staging executor differs only in import ordering
  (verified diff); qualified hash `8be0414305abcd4054386c21d6a098d8907adea2a8ef670ce838244e5a6e9823`,
  local hash `aad22b144a07eba756256da5a4779157574978b0e1566d3613c35d1cb879621d`.
  This is an uncommitted worktree snapshot, not a released Git revision.
- Fresh isolated wheel/source build passed. Archive path audit found 222 source entries
  and 95 wheel entries, with no environment/key files or legacy deploy scripts.
  Artifacts in `/tmp/opencode/ai-gateway-dev-upgrade-dist/` retain version 0.1.0:
  latest source SHA-256 `091d8a3926a399571df2502d79135e2f5d2a2c9320be6c49daf46dd5d0a5ff15`;
  latest wheel SHA-256 `8138793c28d67ca24e9f6037421f70301f8d58928472be67c4c0df567ef2fae1`.
  AWS-only staging helpers are transferred separately and are not packaged in the sdist.

### Deployed concurrency ladder and overload fixes (2026-09-15)

`scripts/staging_qualify.py --seconds 30 --clients N --skip-faults` now records
per-status latency, workflow acceptance, provider active/peak counts, and histogram
bucket deltas. Streaming/non-streaming requests rotate across both replicas.
429 responses have a deliberate 50-ms client retry delay. Default HTTPX client limits
and the load generator share the same CPU-limited unit as the gateway, so high-client
latency includes client pool/CPU contention. This is not an isolated server benchmark.

| Clients | HTTP 200 | HTTP 429 | Success p95 ms | 429 p95 ms | Workflows completed |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 175 | 0 | 304.94 | — | 58 |
| 8 | 459 | 16 | 698.46 | 278.66 | 53 |
| 32 | 257 | 1,868 | 1,740.80 | 848.76 | 35 |
| 64 | 267 | 1,892 | 2,584.18 | 1,116.23 | 14 |
| 128 | 317 | 1,336 | 3,915.22 | 2,838.63 | 9 |

All five final runs passed boundedness/accounting/completion assertions: no transport
errors, all accepted workflows completed, no active provider work at end, peak provider
concurrency at most 6 against cap 8, peak summed RSS at most 385,576,960 bytes. Success
throughput peaks near 14.94/s at eight clients and degrades under heavier offered load.
Above eight clients this host is an overload experiment, not an acceptable low-latency
operating envelope. Workflow submission rate also degrades; completed accepted jobs
alone do not prove admission fairness. The slow high-load 429s do not close the prompt
overload-response performance gate. Start further tuning from the four-client soak
operating point, and separate the load generator before attributing bottlenecks.

The initial ladder failed for two concrete reasons, now fixed and regression tested:
- Shared capacity exhaustion incorrectly became a dependency-failure 503. It now
  returns 429/Retry-After and releases local route/probe ownership; genuine outages
  still return 503.
- Workers paused after proven zero-dispatch capacity rejection. They now make up to
  eight bounded, jittered capacity attempts while retaining their deadline/heartbeat.
  Only an internal executor proof of zero upstream attempts enables these retries;
  upstream 429/503 and uncertain execution still require approval. Exhausted capacity
  retry budgets also pause rather than retry forever. These retries are not yet a
  durable scheduled-backoff queue; crashes remain subject to conservative recovery.

Before-fix evidence is retained in `/tmp/opencode/ladder-before-retry/` (workflow
completion failures are explicit). Final evidence is in
`/tmp/opencode/ladder-after-retry/ladder-{1,8,32,64,128}-30s.json`.
Ladder-qualified executor SHA-256 (before subsequent timing instrumentation):
`08a1b9946e38819de31b996320b534b89fcc63bcfdd2249706e2eca89e4819ac`;
workflow worker SHA-256:
`5a19e5efa98f588ba2280ab18d99a54d3eb92d37ac93fb466da3759ad3dd007d`.
Both hashes matched the ladder worktree. A post-fix four-client, 30-second workload plus
the full restart/partition/worker-kill/drain sequence also passed; evidence:
`/tmp/opencode/aws-post-ladder-faults.json`. Final cleanup again confirmed no staging
listeners or packet-drop rules and healthy pre-existing services.

### Direct gateway overhead and separate resource budgets

`gateway_pre_dispatch` measures from HTTP request-context entry to the first upstream
transport send, including admission/upload, auth, parsing, routing, reservations,
attempt persistence and dispatch fencing. It excludes upstream HTTP pool/connect/TTFT,
provider processing and settlement. Only requests reaching first dispatch are counted;
rejections and worker executions without HTTP context are excluded. Regression coverage
verifies observation occurs before entering the upstream transport and does not count
unscoped worker calls. This is a histogram, not an exact sampled percentile.

`--separate-services` puts service processes in `gateway-qualification-services.slice`
(65% CPU, 1200 MiB), outside the load generator unit (15% CPU, 300 MiB). All remain
on the same one-CPU host with PostgreSQL outside these slices. This removes the shared
cgroup budget but not host scheduling, synthetic-provider, database or network-stack
contention; it is not a separate-machine benchmark. Service MainPIDs, rather than
systemd launcher processes, supply the reported RSS. Child units have bounded runtimes
and are explicitly stopped after the run.

| Interactive clients | Duration | Successful calls | Completed workflows | Mean pre-dispatch | Histogram p95 interval | Total p95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 60.50 s | 344 | 116 | 51.99 ms | (50, 100] ms | 304.98 ms |
| 4 | 120.78 s | 1,462 | 221 | 150.79 ms | (100, 250] ms | 511.99 ms |

At one client, 342/344 first dispatches were within 100 ms; at four clients only
204/1,462 were within 100 ms and 1,427/1,462 within 250 ms. Thus the provisional
100-ms p95 target is demonstrated only at the low-load point (inclusive bucket bound),
and clearly fails at four clients with this resource allocation. The evidence does
not identify a particular SQL query as the cause. Next optimization work should
correlate CPU throttling with transaction/lock time and query plans before changing
pool sizes or financial consistency. Peak summed RSS in the four-client run was
384,131,072 bytes; all successful reservations settled and provider work drained.

Evidence: `/tmp/opencode/aws-separated-baseline-60s.json` and
`/tmp/opencode/aws-separated-120s.json`. Both workloads passed functional assertions;
`qualified: true` does not mean the latency target passed. Final child units were
absent, firewall OUTPUT unchanged, and existing project services healthy. Full offline
suite/lint and fresh source/wheel builds passed after instrumentation. Multi-hour soak,
external-generator performance qualification and operating-cost thresholds remain open.

### CPU and database wait profiling

`--profile` samples role CPU and DB waits once/second, bounded to 1,800 samples with no
SQL text. `/tmp/opencode/staging-profile/` records service throttling in 19/606 periods
at one client versus 503/609 at four; DB idle-in-transaction samples rose 18→114.
These are counts, not wait durations. A 10-ms CPU quota period did not improve latency;
100 ms was restored. `/tmp/opencode/staging-role-profile/qualification.json` found the
fault proxy using 12.80/38.53 service CPU seconds (APIs 20.80, worker 3.84).
Use direct DB for performance and proxy for faults: direct runs completed 957/1,085
calls with total p95 418.70/369.28 ms versus proxy 722 calls/503.67 ms. Evidence:
`/tmp/opencode/staging-direct-profile/{qualification,confirmation}.json`. Confirmation
pre-dispatch mean was 78.87 ms, p95 (100,250] ms. Its 47,704 API SELECT logger calls
include startup/background activity; logger wall times are not isolated SQL execution.
Capacity acquisition now batches expiry cleanup, occupancy counts and lease inserts,
retaining sorted per-scope creation/locks for compatibility with older replicas and
a separate post-lock count snapshot. All inserted leases share a statement-time TTL.
Statement counts fall 16→9 for three scopes and 31→15 for six (excluding BEGIN/COMMIT).
Verification: 11 isolated capacity/shared/lane tests and two transactional capacity
tests passed against the dedicated DB, including parent policy, rollback and equal TTLs.
`/tmp/opencode/staging-direct-profile/batched-capacity.json`: 1,155 calls/113 workflows
completed in 60.91 s; total p95 361.62 ms, pre-dispatch mean 67.05 ms, p95 still
(100,250] ms. This single comparison suggests improvement, not a general SLO guarantee.
Pool expansion remains unjustified; multi-hour soak and repeat performance tests remain.
Post-change fault drills passed (`staging-direct-profile/batched-faults.json` under
`/tmp/opencode`); listeners stopped, firewall restored and existing services healthy.
- Production release, canary, traffic replay and production migration are excluded by
  the current user instruction. No production deployment is authorized by this plan.
