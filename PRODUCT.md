# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Python FastAPI gateway, Supabase PostgreSQL/Auth, and a Next.js dashboard deployed on Vercel.

## Users

The V1 primary user is the gateway owner operating the system personally. Future versions may support a small administration team, so administrative actions retain actor attribution and role boundaries.

## Product Purpose

The AI Gateway gives coding clients one stable API endpoint and gateway key while routing requests across multiple AI providers and credential pools. Success means reliable streaming, automatic key rotation and failover, protected credentials, observable request attempts, and configuration changes without client restarts.

## Positioning

The product is an operational routing and credential-management layer, not a provider-specific relay. Providers, models, credentials, and client protocols are independent configuration dimensions.

## Operating Context

The operator monitors provider and credential health, quotas, requests, failovers, latency, and estimated cost; edits providers, models, credentials, routes, client keys, and system settings; and traces incidents by gateway request ID.

## Capabilities and Constraints

- Supports Anthropic Messages, OpenAI Chat Completions, and OpenAI Responses APIs.
- Streaming and SSE must remain unbuffered and cancellable.
- Prompt and response content is not logged by default.
- Provider credentials are encrypted before persistence and never returned to the browser.
- Gateway keys are independent of provider credentials.
- The data plane must retain a last-known-good configuration during short database outages.
- V1 is a modular monolith without Redis, Kafka, Kubernetes, or a separate queue.

## Evidence on Hand

The repository contains the approved implementation plan, tested backend modules, Supabase migrations, and automated gateway tests. No customer claims, testimonials, or external performance benchmarks are available and none should be fabricated.

## Product Principles

- Stable client contracts over provider-specific behavior.
- Explicit routing and fallback over silent model substitution.
- Streaming correctness over convenient buffering or unsafe retries.
- Deny-by-default credential and administrative access.
- Operational clarity over decorative presentation.
