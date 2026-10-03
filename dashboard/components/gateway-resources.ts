/**
 * What the console shows, and in what order.
 *
 * One entry per view: the admin endpoint behind it, whether it is working configuration
 * an operator may change or an operational record that is read-only, and the columns
 * worth putting on screen. Column order is part of the meaning here - several of these
 * lists are ordered the way they are because the obvious order misled somebody.
 */

import { Activity, Cable, Coins, Gauge, KeyRound, Network, Route, ScrollText, Settings2, ShieldCheck, Users } from "lucide-react";

export type View = "overview" | "operations" | "providers" | "credentials" | "clients" | "models" | "provider-models" | "routing" | "routing-policies" | "requests" | "workflows" | "health" | "usage" | "analytics" | "audit" | "events" | "configuration" | "pools" | "budgets" | "alerts" | "alert-rules";
// overview and operations are bespoke views backed by single-object endpoints, not
// the generic resource table, so they are excluded from the resource map below.
export type ResourceView = Exclude<View, "overview" | "operations">;
export type Notice = { kind: "success" | "error"; message: string } | null;

// The monitored conditions the alert monitor knows how to evaluate, in the same
// order and with the same keys the gateway accepts (gateway/admin/operations.py).
// A rule written against one of these does not need to know internal event names.
export const CONDITION_KINDS: [string, string][] = [
  ["workflow_queue_age", "Workflow queue is falling behind"],
  ["settlement_backlog", "Settlements are backing up"],
  ["unknown_usage", "Usage outcomes are unresolved"],
  ["workflow_approval_backlog", "Workflows are waiting for approval"],
  ["budget_utilization", "Budget nearly spent"],
  ["credential_quota_low", "Credential quota nearly used up"],
  ["credential_balance_low", "Credential balance nearly gone"],
  ["credential_auth_failures", "Credential repeatedly rejected"],
  ["provider_failure_rate", "Provider failing a share of attempts"],
  ["provider_unreachable", "Provider cannot be reached"],
  ["model_no_eligible_route", "Model had nowhere to run"],
  ["credential_pool_exhausted", "Provider has no usable credentials"],
  ["request_failure_rate", "Requests failing for a model"],
  ["cost_spike", "Spend spike in a window"],
  ["unpriced_traffic", "Traffic without pricing"],
];

export const navigation: [View, typeof Activity, string][] = [
  ["overview", Activity, "Overview"],
  ["operations", Gauge, "Operations"],
  ["providers", Cable, "Providers"],
  ["pools", Network, "Provider pools (Advanced)"],
  ["credentials", KeyRound, "Credentials (Advanced)"],
  ["clients", Users, "Clients"],
  ["models", Network, "Models"],
  ["provider-models", Network, "Mappings (Advanced)"],
  ["routing", Route, "Routes (Advanced)"],
  ["routing-policies", Settings2, "Policies (Advanced)"],
  ["requests", ScrollText, "Requests"],
  ["workflows", ScrollText, "Workflows"],
  ["health", ShieldCheck, "Health"],
  ["usage", Coins, "Usage"],
  ["analytics", Activity, "Analytics"],
  ["audit", Activity, "Audit"],
  ["events", Activity, "Activity"],
  ["configuration", Settings2, "Configuration"],
  ["budgets", Coins, "Budgets"],
  ["alerts", ShieldCheck, "Alerts"],
  ["alert-rules", Settings2, "Alert rules"],
];

export const resources: Record<ResourceView, { endpoint: string; title: string; mutable: boolean; columns: string[] }> = {
  providers: {
    endpoint: "providers",
    title: "Providers",
    mutable: true,
    columns: ["name", "provider_type", "base_url", "enabled", "health", "routable_credentials", "healthy_credentials", "credential_count"],
  },
  credentials: {
    endpoint: "credentials",
    title: "Credentials",
    mutable: true,
    columns: [
      // routing_state comes before health deliberately. Health says what happened
      // last; routing_state says whether the router will use the credential now,
      // and whether it will recover on its own. Reading health alone made a
      // provider with 17 usable credentials look like it had 1.
      "name", "provider_name", "masked_hint", "enabled", "routing_state", "health",
      "quota_used", "quota_limit", "quota_confidence", "quota_observed_at", "quota_source",
      // Provenance sits beside the age of the same figure. The poller refreshes quota
      // every fifteen minutes; a balance is only ever typed in and nothing will ever
      // refresh it. Without the source, a nine-minute-old quota and a five-day-old
      // balance looked like two readings of equal standing.
      "balance_amount", "balance_observed_at", "balance_source", "cooldown_until",
    ],
  },
  clients: {
    endpoint: "clients",
    title: "Gateway clients",
    mutable: true,
    // live_access sits next to enabled because access is enforced from the
    // published snapshot. A client can read as disabled here and still be serving.
    columns: ["name", "allowed_protocols", "allowed_models", "enabled", "live_access", "active_keys"],
  },
  models: {
    endpoint: "models",
    title: "Canonical models",
    mutable: true,
    columns: ["display_name", "aliases", "available_through", "protocols", "capabilities", "enabled", "provider_route_count"],
  },
  "provider-models": {
    endpoint: "provider-models",
    title: "Provider mappings",
    mutable: true,
    columns: ["model_id", "provider_name", "upstream_model_id", "protocol", "serves_protocols", "pricing", "enabled", "priority", "max_concurrency"],
  },
  routing: {
    endpoint: "routes",
    title: "Routes",
    mutable: true,
    columns: ["model_id", "provider_name", "upstream_model_id", "protocol", "priority", "enabled", "allow_model_fallback", "policy_name"],
  },
  "routing-policies": {
    endpoint: "routing-policies",
    title: "Routing policies",
    mutable: true,
    columns: ["name", "enabled", "policy", "updated_at"],
  },
  requests: {
    endpoint: "requests",
    title: "Requests",
    mutable: false,
    columns: ["id", "protocol", "requested_model", "status", "latency_ms", "retry_count", "fallback_count"],
  },
  workflows: {
    endpoint: "workflows",
    title: "Workflows",
    mutable: false,
    // status leads: an operator opens this page to find jobs that are stuck waiting
    // or have failed, not to read ids. attempts/max shows how close a job is to
    // giving up; deadline is when the gateway will expire it if it does not finish.
    columns: ["id", "client_name", "status", "version", "attempts", "max_attempts", "deadline", "updated_at"],
  },
  health: {
    endpoint: "health",
    title: "Health checks",
    mutable: false,
    columns: ["provider_name", "credential_name", "status", "routing_eligibility", "latency_ms", "error_category", "checked_at"],
  },
  usage: {
    endpoint: "usage",
    title: "Usage",
    mutable: false,
    columns: ["request_id", "provider_name_snapshot", "canonical_model_snapshot", "route_id_snapshot", "attempt_status_snapshot", "input_tokens", "output_tokens", "cached_tokens", "estimated_cost", "recorded_at"],
  },
  analytics: {
    endpoint: "analytics",
    title: "Seven-day model activity",
    mutable: false,
    columns: ["model", "requests", "succeeded", "failed", "average_latency_ms"],
  },
  audit: {
    endpoint: "audit",
    title: "Audit trail",
    mutable: false,
    columns: ["action", "resource_name", "resource_type", "created_at"],
  },
  configuration: {
    endpoint: "config/versions",
    title: "Configuration versions",
    mutable: false,
    columns: ["id", "status", "schema_version", "checksum", "created_at", "published_at"],
  },
  events: {
    endpoint: "events",
    title: "Activity",
    mutable: false,
    columns: ["event_type", "provider_name", "credential_name", "metadata", "created_at"],
  },
  pools: {
    endpoint: "provider-pools",
    title: "Provider pools",
    mutable: true,
    columns: ["name", "model_id", "strategy", "enabled", "member_count", "routable_members"],
  },
  budgets: {
    endpoint: "budgets",
    title: "Budgets",
    mutable: true,
    columns: ["name", "scope_type", "period", "currency", "limit_amount", "used", "requests_this_period", "enforcement", "enabled"],
  },
  alerts: {
    endpoint: "alerts",
    title: "Alerts",
    mutable: false,
    columns: [
      "severity", "status", "title", "summary", "recommended_action",
      "observed", "occurrence_count", "last_seen_at", "resolved_reason",
    ],
  },
  "alert-rules": {
    endpoint: "alert-rules",
    title: "Alert rules",
    mutable: true,
    columns: [
      "name", "severity", "enabled", "condition_kind", "condition",
      "description", "cooldown_seconds", "updated_at",
    ],
  },
};
