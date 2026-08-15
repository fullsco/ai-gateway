# AI Gateway Implementation Plan

Status: approved for implementation
Project: greenfield implementation in `/root/ai-gateway`.
Historical context: `/root/agentrouter_proxy.py` is outside this repository. It may inform client compatibility tests, but the new gateway does not depend on or modify it.

## Product Objective

Build a provider-agnostic AI Gateway between coding clients and upstream AI providers. Clients use one stable gateway endpoint and gateway key while the gateway handles protocol translation, model routing, credential pools, quotas, health, failover, streaming, usage, cost, logging, and administration.

## Architecture Decisions

- Start as a modular monolith.
- Keep the data plane and dashboard control plane separate.
- Use Python FastAPI for the gateway initially because the working proxy already proves its Anthropic streaming behavior.
- Use Next.js on Vercel for the dashboard.
- Use Supabase PostgreSQL/Auth for durable configuration, operational records, and admin identity.
- Package the gateway portably and qualify Hostinger before selecting it for production.
- Use no Redis, Kafka, Kubernetes, or separate queue in V1.
- Keep the existing local proxy available throughout migration.

## Historical Compatibility Baseline

```text
Claude Code -> FastAPI proxy on 127.0.0.1:8318 -> AgentRouter -> upstream model
```

Compatibility behavior worth preserving where it remains relevant:

- FastAPI/Uvicorn/httpx runtime.
- `GET/HEAD /health` and `/_health` return `ok`.
- POST paths are forwarded upstream.
- `/v1/messages` receives `?beta=true` unless already present.
- Anthropic beta headers are merged with required compatibility betas.
- Claude session IDs are preserved when valid, otherwise generated.
- Known AgentRouter model aliases are rewritten before forwarding.
- Per-model concurrency is bounded.
- Transient upstream errors are retried with exponential backoff.
- Streaming responses are relayed without buffering the complete response.
- Existing startup and environment behavior remains the migration reference.

## Target Request Path

```text
Client
  -> unified API
  -> gateway authentication
  -> request ID/deadline
  -> protocol parser
  -> normalized request
  -> model aliases/capabilities
  -> routing engine
  -> provider candidate selection
  -> credential candidate selection
  -> attempt coordinator
  -> provider adapter
  -> streaming bridge
  -> client protocol response
```

The hot path uses an immutable, validated in-memory configuration snapshot. Supabase is not queried for every request. The gateway continues using the last known good snapshot during a short database outage.

## Current Limitations To Resolve

- Single upstream dependency.
- No arbitrary persistent credential pool.
- No gateway-client key management.
- No model registry or capability matrix.
- No health-aware routing, circuit breakers, or explicit failover levels.
- Configuration is process/environment based.
- Operational state is not durable or queryable through a dashboard.
- Logging needs strict structured redaction and prompt-content exclusion.
- Shared Hostinger Node.js capabilities are unverified for long-lived SSE.

## Database Proposal

Core tables:

- `gateway_clients`: client identity, status, limits, permissions.
- `gateway_client_keys`: key prefix, keyed hash, lifecycle metadata.
- `providers`: provider identity, protocol, base URL, settings.
- `provider_credentials`: encrypted secret envelope and health state.
- `models`: canonical model and capability definitions.
- `model_aliases`: aliases to canonical models.
- `provider_models`: provider model IDs and capabilities.
- `model_routes`: ordered/weighted provider routes.
- `routing_policies`: selection and fallback policy.
- `request_logs`: sanitized request-level records.
- `request_attempts`: provider/credential attempt chain.
- `usage_records`: tokens and estimated costs.
- `health_checks`: active/passive health history.
- `provider_events`: quota, cooldown, circuit, and incident events.
- `audit_logs`: sanitized administrative actions.
- `system_settings`: versioned operational settings.
- `config_versions`: atomic configuration publication.

Secrets use envelope encryption with a runtime-held master key. Normal dashboard queries never return plaintext upstream credentials. Gateway client keys use a random value displayed once, with a stored prefix and keyed hash.

## API Design

Data plane:

- `POST /v1/messages`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `GET /v1/models`
- `GET /health`
- `GET /ready`
- `GET /metrics`

Accept `x-api-key` for Anthropic compatibility and `Authorization: Bearer` for OpenAI compatibility. Gateway keys are independent of provider credentials.

Control plane:

```text
/api/admin/v1/providers
/api/admin/v1/credentials
/api/admin/v1/models
/api/admin/v1/routes
/api/admin/v1/clients
/api/admin/v1/requests
/api/admin/v1/logs
/api/admin/v1/health
/api/admin/v1/audit
/api/admin/v1/settings
/api/admin/v1/config/publish
```

## Provider Abstraction

Adapters must isolate provider-specific behavior and support configuration validation, model discovery/validation, request compatibility, request preparation, non-streaming send, streaming, response normalization, error normalization, usage extraction, cost estimation, and health probes.

Initial adapter families:

- Native Anthropic.
- Anthropic-compatible.
- Native OpenAI Responses.
- OpenAI Chat Completions.
- Generic OpenAI-compatible.

AgentRouter is a configured provider and is not referenced throughout the core router.

## Routing and Failover

Hard filters: enabled provider/credential, model mapping, capabilities, client permissions, protocol support, cooldown, circuit state, concurrency, rate/budget limits.

Scoring signals: route priority, provider/key health, quota headroom, RPM/TPM headroom, failure rate, latency EWMA, last-used time, weight, cost, and concurrency.

Failover levels:

1. Next credential on the same provider.
2. Explicit alternate route on the same provider.
3. Another provider mapped to the same canonical model.
4. Explicitly configured fallback model.

Retry only before downstream stream content is committed. After the first content event, relay the terminal stream error and record the incomplete attempt. Do not retry invalid requests, permanent model errors, or exhausted deadlines.

## Health and Circuit Breakers

Track provider and credential health independently. States include `HEALTHY`, `DEGRADED`, `RATE_LIMITED`, `AUTH_FAILED`, `QUOTA_EXHAUSTED`, `UNAVAILABLE`, `COOLDOWN`, and `DISABLED`. Repeated failures open a circuit; cooldown is followed by a probe and gradual recovery.

## Security

- TLS only in staging/production.
- Hash gateway keys; encrypt upstream credentials before persistence.
- Supabase Auth plus admin role for dashboard access.
- Secure same-site cookies and CSRF protection for browser mutations.
- RLS for dashboard-readable data; service credentials stay server-side.
- No prompt/response logging by default.
- Always redact authorization headers, provider credentials, cookies, and secrets.
- Add secret and dependency scanning to CI.
- Never use naive entropy scanning on normal AI content; protect actual credential/configuration boundaries instead.

## Dashboard

Next.js/Vercel dashboard pages: Overview, Providers, Provider Detail, Credentials, Models, Routing, Clients, Requests, Request Detail, Logs, Health, Settings, and Audit. Use dense operational tables, filters, masked identifiers, status indicators, mobile-compatible details, and explicit destructive-action confirmations.

## Deployment

Candidate topology:

```text
api.example.com -> TLS/reverse proxy -> gateway runtime
dashboard.example.com -> Vercel Next.js -> Supabase Auth -> admin API
Supabase -> PostgreSQL/Auth/durable operational records
```

Hostinger must pass a qualification test for persistent processes, automatic restart, environment secrets, unbuffered `text/event-stream`, long requests, disconnect propagation, concurrency, logs, TLS, and safe deployment. A failed qualification moves only the gateway to a small managed container runtime; Vercel and Supabase remain unchanged.

## Testing

Unit tests cover authentication, model aliases, capability filtering, scoring, quotas, retries, circuit transitions, validation, error normalization, and cost estimation.

Integration tests cover database repositories/migrations, credential encryption, provider adapters, configuration reload, health scheduling, streaming/disconnects, and database outage caching.

End-to-end tests cover Claude Code Messages API, tools, thinking, compaction, resume, large context, OpenAI Chat/Responses, OpenCode, key exhaustion, rate limits, auth failure, timeout, 5xx, provider outage, restart, and no-restart configuration changes.

## Observability and Cost

Every request receives a sortable `gw_...` ID. Store sanitized request metadata and an attempt chain. Emit JSON logs and OpenTelemetry-compatible boundaries. Track request count, latency, TTFT, open streams, fallback rate, errors, circuit states, cooldowns, tokens, and estimated costs. Mark costs as estimated when pricing is incomplete.

## Rollback

Use additive migrations, immutable configuration versions, versioned gateway images, and retained previous deployments. Roll back route configuration atomically and keep the old local proxy until production certification is complete. Do not delete data, providers, or credentials during normal migration.

## Phases

### Phase 1: Compatibility skeleton

Objective: establish a separate, deployable gateway skeleton with typed settings, request IDs, redacted structured logs, and health/readiness endpoints.

Files/modules: `gateway/`, `tests/`, dependency lock/config, deployment manifest.

Database/API: no production database; `/health`, `/ready`, and internal request context only.

Tests: settings validation, request ID propagation, redaction, health/readiness.

Deployment: local on a separate port; current proxy remains authoritative.

Rollback: delete/disable the new process; no impact to port 8318.

### Phase 2: Protocol/provider extraction

Objective: implement normalized requests and AgentRouter through an Anthropic-compatible adapter.

Files/modules: `protocols/`, `providers/`, `routing/`.

Database/API: provider/model configuration interfaces; compatibility endpoints.

Tests: request/response/SSE parity, tools, thinking, beta/session headers.

Deployment: shadow and staging testing; revert to current proxy.

### Phase 3: Credential pool and gateway clients

Objective: arbitrary encrypted credentials and unified client keys.

Database/API: credentials, clients, client keys, admin CRUD.

Tests: encryption, masking, key lookup, permissions, rotation.

Deployment: feature-disabled by default; current environment credential remains fallback.

Rollback: disable pool selection; do not revoke active upstream credentials.

### Phase 4: Model registry and routing

Objective: aliases, capabilities, deterministic configurable routing.

Database/API: models, aliases, provider models, routes, policies.

Tests: eligibility, scoring, mapping, permissions, rate limits.

Deployment: configuration versions and atomic rollback.

### Phase 5: Failover and circuit breakers

Objective: attempt coordination, retries, cooldowns, health, and provider failover.

Database/API: attempts, health, provider events.

Tests: complete upstream failure matrix and stream commit boundary.

Deployment: staging chaos tests; feature flags for failover.

### Phase 6: Supabase persistence and cache

Objective: durable configuration, usage, audit, cached snapshots, and no-restart refresh.

Database/API: remaining tables, indexes, RLS, config publication.

Tests: transient DB outage, stale cache, refresh and reconciliation.

Deployment: last-known-good snapshot protects the data plane.

### Phase 7: Dashboard

Objective: operational Next.js dashboard with Supabase Auth and admin API.

Database/API: admin reads/writes, audit actions.

Tests: RBAC, CSRF, masking, CRUD, responsive behavior.

Deployment: Vercel preview then production; dashboard independently disableable.

### Phase 8: Staging and host qualification

Objective: qualify Hostinger and deploy staging to the selected runtime.

Tests: 15-minute SSE, heartbeats, concurrency, slow consumer, cancellation, restart, deployment during traffic, timeout/failover.

Deployment: failed Hostinger test selects managed container hosting.

Rollback: staging image/config rollback.

### Phase 9: Production migration

Objective: certify Claude Code, Codex, OpenCode, and OpenAI-compatible clients, then migrate traffic gradually.

Tests: end-to-end compatibility, streaming, tools, failover, large context, restart.

Deployment: DNS/reverse-proxy migration with rollback to the old path.

Rollback: restore old route and retain new state.

### Phase 10: Hardening and cleanup

Objective: load/soak/security testing, restore/rollback drills, retention jobs, and removal of obsolete assumptions.

Database/API: cleanup only through approved additive-to-destructive migration sequence.

Tests: load, soak, security, backup restore, rollback drill.

Deployment: production hardening; destructive cleanup requires explicit approval.

## Definition of Done

Claude Code, OpenCode, and OpenAI-compatible clients work; streaming/SSE is reliable; multiple providers and arbitrary key pools rotate correctly; quotas, rate limits, failover, circuits, model routing, gateway keys, protected credentials, request logs, usage, health, dashboard, no-restart configuration, staging, production deployment, tests, and rollback are all verified.
