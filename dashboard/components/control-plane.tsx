"use client";

import { Activity, Cable, Clipboard, Coins, KeyRound, Menu, Network, Pencil, Plus, RefreshCw, Route, ScrollText, Settings2, ShieldCheck, Trash2, Users, X } from "lucide-react";
import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import ProviderSetup, { gatewayApi } from "./provider-setup";
import ModelRouting from "./model-routing";

type Row = Record<string, unknown>;
type View = "overview" | "providers" | "credentials" | "clients" | "models" | "provider-models" | "routing" | "routing-policies" | "requests" | "health" | "usage" | "analytics" | "audit" | "events" | "configuration" | "pools" | "budgets" | "alerts" | "alert-rules";
type ResourceView = Exclude<View, "overview">;
type Notice = { kind: "success" | "error"; message: string } | null;

function readable(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not recorded";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.length ? value.map(readable).join(", ") : "None configured";
  try {
    return JSON.stringify(value);
  } catch {
    return "Unavailable";
  }
}

const navigation: [View, typeof Activity, string][] = [
  ["overview", Activity, "Overview"],
  ["providers", Cable, "Providers"],
  ["pools", Network, "Provider pools (Advanced)"],
  ["credentials", KeyRound, "Credentials (Advanced)"],
  ["clients", Users, "Clients"],
  ["models", Network, "Models"],
  ["provider-models", Network, "Mappings (Advanced)"],
  ["routing", Route, "Routes (Advanced)"],
  ["routing-policies", Settings2, "Policies (Advanced)"],
  ["requests", ScrollText, "Requests"],
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

const resources: Record<ResourceView, { endpoint: string; title: string; mutable: boolean; columns: string[] }> = {
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
      "quota_used", "quota_limit", "quota_confidence", "balance_amount",
      "balance_observed_at", "cooldown_until",
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
    columns: ["model_id", "provider_name", "upstream_model_id", "protocol", "pricing", "enabled", "priority", "max_concurrency"],
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

const api = gatewayApi;

function display(value: unknown, key: string, row?: Row) {
  if (key === "quota_confidence") return QUOTA_CONFIDENCE[String(value)] ?? "No signal";
  if (key === "routing_state") return ROUTING_STATE[String(value)] ?? String(value ?? "Unknown");
  if (key === "live_access") return LIVE_ACCESS[String(value)] ?? String(value ?? "Unknown");
  if (key === "summary" || key === "recommended_action" || key === "description") {
    return value === null || value === undefined || value === "" ? "Not recorded" : String(value);
  }
  if (key === "observed" && value && typeof value === "object") {
    // The measured values that satisfied the condition, read as a sentence.
    return Object.entries(value as Record<string, unknown>)
      .map(([name, observed]) => `${name.replaceAll("_", " ")}: ${readable(observed)}`)
      .join(", ");
  }
  if (key === "condition" && value && typeof value === "object") return describeCondition(value as Row);
  if (key === "condition_kind") return CONDITION_KINDS[String(value)] ?? readable(value);
  if (key === "balance_amount" && (value === null || value === undefined)) return "Not observed";
  if (key.includes("cost") && (value === null || value === undefined || value === "")) return "Pricing unavailable";
  if (value === null || value === undefined || value === "") {
    if (key.includes("cooldown")) return "Not cooling down";
    if (key.includes("pricing")) return "Pricing unavailable";
    return "Not configured";
  }
  if (typeof value === "boolean") return value ? "Enabled" : "Disabled";
  if (key === "event_type" || key === "action") return humanizeEvent(value);
  if (key === "protocol" || key === "health" || key === "status")
    return String(value)
      .replaceAll("_", " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  if (key === "allow_model_fallback") return value ? "Allowed" : "Disabled";
  if (Array.isArray(value)) {
    if (value.length) return value.join(", ");
    if (key.includes("capabilit")) return "No additional capabilities";
    if (key.includes("alias")) return "No aliases";
    if (key.includes("protocol")) return "No protocols configured";
    return "None configured";
  }
  if (typeof value === "object") return "Advanced details available";
  if (key.endsWith("_at")) {
    const formatted = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(String(value)));
    // An observation is only worth as much as its age. balance and quota figures are
    // recorded by hand or by a poller that is off by default, so a six-day-old
    // reading rendered identically to a fresh one and was read as current. Naming
    // the age, and saying outright when it is too old to trust, is the difference
    // between a number and a number somebody can act on.
    return OBSERVATION_KEYS.has(key) ? `${formatted} (${observationAge(value)})` : formatted;
  }
  if (key.includes("cost")) return row?.currency ? `${Number(value).toFixed(4)} ${String(row.currency)}` : "Pricing unavailable";
  if (key.includes("latency") && value !== "--") return `${Number(value).toFixed(0)} ms`;
  return String(value).replaceAll("_", " ");
}

function humanizeEvent(value: unknown): string {
  const events: Record<string, string> = {
    provider_created: "Provider added",
    provider_updated: "Provider configuration updated",
    provider_deleted: "Provider removed",
    provider_model_created: "Model availability added",
    provider_model_updated: "Model routing configuration updated",
    provider_model_deleted: "Model availability removed",
    credential_created: "Credential added",
    credential_updated: "Credential limits updated",
    credential_rotated: "Credential secret rotated",
    credential_deleted: "Credential removed",
    route_created: "Provider route added",
    route_updated: "Provider route updated",
    route_deleted: "Provider route removed",
    config_published: "Working configuration published",
    config_rolled_back: "Configuration rolled back",
  };
  const key = String(value ?? "");
  return events[key] ?? key.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

const labels: Record<string, string> = {
  provider_name: "Provider",
  provider_name_snapshot: "Provider",
  available_through: "Available through",
  routing_eligibility: "Routing eligibility",
  resource_name: "Resource",
  resource_type: "Resource type",
  credential_name: "Credential",
  requested_model: "Requested model",
  resolved_model: "Resolved model",
  canonical_model_snapshot: "Model",
  upstream_model_snapshot: "Upstream model",
  protocol_snapshot: "Protocol",
  requests_per_minute: "Requests per minute",
  tokens_per_minute: "Tokens per minute",
  quota_limit: "Quota limit",
  quota_used: "Quota used",
  quota_threshold: "Quota warning threshold",
  max_concurrency: "Concurrent request limit",
  priority: "Routing priority",
  weight: "Traffic share",
  cooldown_until: "Cooling down until",
  error_category: "Reason",
  fallback_count: "Fallbacks used",
  retry_count: "Retries",
  estimated_cost: "Estimated cost",
  is_estimate: "Estimated value",
  attempt_status_snapshot: "Attempt result",
  response_committed: "Response started",
};

function columnLabel(column: string): string {
  return labels[column] ?? column.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

// A rule should be readable without knowing the condition vocabulary.
const CONDITION_KINDS: Record<string, string> = {
  credential_quota_low: "Credential quota nearly used up",
  credential_balance_low: "Credential balance nearly gone",
  credential_auth_failures: "Credential repeatedly rejected",
  provider_failure_rate: "Provider failing a share of attempts",
  provider_unreachable: "Provider cannot be reached",
  model_no_eligible_route: "Model had nowhere to run",
  credential_pool_exhausted: "Provider has no usable credentials",
  request_failure_rate: "Requests failing for a model",
  cost_spike: "Spend in a window",
  unpriced_traffic: "Traffic without pricing",
};

/** Render a rule's condition as a sentence rather than raw JSON. */
function describeCondition(condition: Row): string {
  const parts: string[] = [];
  const window = condition.window_minutes;
  const threshold = condition.at_least ?? condition.at_most ?? condition.threshold;
  if (threshold !== undefined && threshold !== null) {
    const direction = condition.at_most !== undefined ? "at or below" : "at or above";
    parts.push(`${direction} ${readable(threshold)}`);
  }
  if (window) parts.push(`measured over ${readable(window)} minutes`);
  const floor = condition.min_requests ?? condition.min_samples;
  if (floor) parts.push(`only once there are ${readable(floor)} or more`);
  Object.entries(condition).forEach(([key, value]) => {
    if (["window_minutes", "at_least", "at_most", "threshold", "value", "min_requests", "min_samples"].includes(key)) return;
    if (value && typeof value === "object") {
      const [operator, target] = Object.entries(value as Row)[0] ?? [];
      if (operator) parts.push(`${key.replaceAll("_", " ")} ${String(operator).replaceAll("_", " ")} ${readable(target)}`);
      return;
    }
    parts.push(`${key.replaceAll("_", " ")} is ${readable(value)}`);
  });
  return parts.length ? parts.join(", ") : "Always";
}

// Access is enforced from the published configuration, so what an operator sets
// here and what the gateway does can differ until they publish. Saying so is the
// difference between believing a key is revoked and it actually being revoked.
const LIVE_ACCESS: Record<string, string> = {
  serving: "Serving - enabled here and in the published configuration",
  "STILL SERVING until you publish": "Still serving - you disabled it, but the published configuration has not caught up. Its keys keep working until you publish",
  "not serving until you publish": "Not serving yet - you enabled it, but the published configuration has not caught up. Publish to let its keys work",
  "not serving yet, publish to activate": "Not serving yet - this client has never been published. Publish to activate it",
  "not serving": "Not serving - disabled here and in the published configuration",
};

// What to do about a credential, which "health" cannot express. The distinction
// that matters is between one that will come back by itself and one that will not.
const ROUTING_STATE: Record<string, string> = {
  "in service": "In service - the router is using it",
  "on trial": "On trial - unhealthy, but its cooldown has passed so the next attempt may use it and a success restores it",
  "cooling down": "Cooling down - paused until its cooldown expires, then it is tried again",
  "needs attention": "Needs attention - it has never succeeded and holds no cooldown, so it is never retried. Replace or remove it",
  disabled: "Disabled - excluded by configuration, not by health",
};

const QUOTA_CONFIDENCE: Record<string, string> = {
  known: "Measured - a limit and a usage figure are both known",
  estimated: "Spend known, no limit - trend only, headroom unknown",
  unknown: "No signal - do not read this as remaining capacity",
};

// Timestamps that record when a figure was last observed, rather than when something
// happened. These are the ones whose age changes how the number should be read.
const OBSERVATION_KEYS = new Set(["balance_observed_at", "quota_observed_at"]);
const STALE_AFTER_HOURS = 24;

function observationAge(value: unknown): string {
  const observed = new Date(String(value)).getTime();
  if (Number.isNaN(observed)) return "age unknown";
  const hours = (Date.now() - observed) / 3_600_000;
  if (hours < 0) return "clock skew";
  const age =
    hours < 1 ? `${Math.max(1, Math.round(hours * 60))} min ago`
    : hours < 48 ? `${Math.round(hours)} h ago`
    : `${Math.round(hours / 24)} days ago`;
  return hours >= STALE_AFTER_HOURS ? `${age} - stale, may not reflect reality` : age;
}

function explainError(value: unknown): string {
  // Keys must be the values the gateway actually writes. Two of the previous
  // entries ("rate_limited", "authentication_failed") are health states, not
  // error categories, and were never emitted, so the two most operationally
  // important failures fell through to a raw de-underscored string.
  const explanations: Record<string, string> = {
    upstream_waf_rejection:
      "An edge or bot-protection layer at the provider blocked the request, not the credential. Other eligible routes may still be used.",
    rate_limit:
      "The provider rate limited this credential. It is paused briefly and another eligible credential or provider is used.",
    upstream_authentication_error:
      "The provider rejected this credential. Rotate or disable it; other credentials on the same provider are tried first.",
    authentication_error:
      "The gateway API key sent by the client was not recognised. Nothing upstream is wrong.",
    authorization_error:
      "The client's key is valid, but its client is not allowed this protocol or model, or the key is revoked or expired. Fix it in Clients, not at the provider.",
    quota_exhausted:
      "This credential is out of quota or balance. Another credential or provider with headroom is required.",
    timeout: "The provider did not respond before the configured timeout.",
    provider_unavailable:
      "The provider could not be reached or returned a server error. Traffic moves to another eligible provider.",
    model_unavailable:
      "This provider does not serve the requested model, even though it is mapped to it.",
    no_eligible_route:
      "The model is configured, but no route could serve it at that moment: every candidate was unhealthy, cooling down, out of quota, or at its concurrency limit. Open the request trace to see which and why.",
    invalid_request:
      "The request itself was rejected as malformed. Retrying elsewhere would fail the same way, so no failover was attempted.",
    internal_error: "The gateway failed to complete the request.",
  };
  return explanations[String(value)] ?? String(value ?? "No reason recorded").replaceAll("_", " ");
}

function DataTable({ rows, columns, actions }: { rows: Row[]; columns: string[]; actions?: (row: Row) => ReactNode }) {
  if (rows.length === 1 && rows[0].usage_by_model) return <AnalyticsTables payload={rows[0]} />;
  if (!rows.length)
    return (
      <div className="empty-state">
        <strong>No matching records</strong>
        <span>Try a different search, or add a record when this resource is configurable.</span>
      </div>
    );
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{columnLabel(column)}</th>
            ))}
            {actions && <th className="action-heading">Actions</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={String(row.id ?? index)}>
              {columns.map((column) => (
                <td key={column} data-label={columnLabel(column)}>
                  <span title={column === "error_category" ? explainError(row[column]) : display(row[column], column, row)} className={column === "status" || column === "health" || column === "enabled" ? `value status-value ${String(row[column])}` : "value"}>
                    {column === "error_category" ? explainError(row[column]) : display(row[column], column, row)}
                  </span>
                </td>
              ))}
              {actions && (
                <td data-label="Actions" className="row-actions">
                  {actions(row)}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ControlPlane() {
  const [view, setView] = useState<View>("overview");
  const [rows, setRows] = useState<Row[]>([]);
  const [overview, setOverview] = useState<Row>({});
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [navOpen, setNavOpen] = useState(false);
  const sequence = useRef(0);

  async function load(target = view, quiet = false) {
    const request = ++sequence.current;
    if (!quiet) setLoading(true);
    try {
      const result = await api(target === "overview" ? "overview" : resources[target].endpoint);
      if (request !== sequence.current) return;
      if (target === "overview") setOverview(result);
      else setRows(target === "analytics" ? [result] : result.data);
      setStale(false);
      setUpdatedAt(new Date());
    } catch (reason) {
      if (request !== sequence.current) return;
      setStale(true);
      setNotice({
        kind: "error",
        message: reason instanceof Error ? reason.message : "Unable to load control plane",
      });
    } finally {
      if (request === sequence.current) setLoading(false);
    }
  }

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("view") as View | null;
    if (requested === "overview" || (requested && requested in resources)) setView(requested);
    const onPopState = () => {
      const requested = new URLSearchParams(window.location.search).get("view") as View | null;
      setView(requested === "overview" || (requested && requested in resources) ? requested : "overview");
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  useEffect(() => {
    setNotice(null);
    void load(view);
  }, [view]);
  const metrics: [string, string][] = [
    ["Requests today", Number(overview.requests_today ?? 0).toLocaleString()],
    ["Successful", Number(overview.successful ?? 0).toLocaleString()],
    ["Failed", Number(overview.failed ?? 0).toLocaleString()],
    ["Active providers", String(overview.active_providers ?? 0)],
    ["Fallback rate", overview.fallback_rate === null || overview.fallback_rate === undefined ? "No requests" : `${(Number(overview.fallback_rate) * 100).toFixed(1)}%`],
    ["Estimated month cost", formatCurrencyTotals(overview.costs_by_currency)],
  ];
  return (
    <main className="shell">
      <a className="skip-link" href="#workspace">
        Skip to content
      </a>
      <aside className={navOpen ? "nav-open" : ""}>
        <div className="rail-head">
          <div className="mark">AG</div>
          <button className="nav-close" onClick={() => setNavOpen(false)} aria-label="Close navigation">
            <X size={20} />
          </button>
        </div>
        <nav aria-label="Control plane">
          {navigation.map(([id, Icon, label]) => (
            <button
              aria-current={view === id ? "page" : undefined}
              className={view === id ? "active" : ""}
              key={id}
              onClick={() => {
                setView(id);
                window.history.pushState(null, "", `/?view=${id}`);
                setNavOpen(false);
              }}
              title={label}
            >
              <Icon size={17} aria-hidden="true" />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="operator">
          <span className="lamp" />
          Authenticated operator
        </div>
      </aside>
      <section className="workspace" id="workspace">
        <header>
          <button className="menu-button" onClick={() => setNavOpen(true)} aria-label="Open navigation">
            <Menu size={20} />
          </button>
          <div className="heading">
            <h1>{navigation.find(([id]) => id === view)?.[2]}</h1>
            <p>{stale ? "Showing last loaded data" : updatedAt ? `Updated ${updatedAt.toLocaleTimeString()}` : "Gateway configuration and operational state"}</p>
          </div>
          <button className="refresh" disabled={loading} onClick={() => void load()}>
            <RefreshCw size={15} className={loading ? "spin" : ""} />
            {loading ? "Refreshing" : "Refresh"}
          </button>
        </header>
        {notice && (
          <div className={`notice ${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}>
            <span>{notice.message}</span>
            <button onClick={() => setNotice(null)} aria-label="Dismiss notification">
              <X size={16} />
            </button>
          </div>
        )}
        {view === "overview" ? <Overview runtime={overview} metrics={metrics} loading={loading} /> : view === "models" ? <ModelRouting onNotice={(message, kind = "success") => setNotice({ message, kind })} /> : <Resource view={view} rows={rows} loading={loading} reload={() => load(view, true)} notify={setNotice} />}
      </section>
    </main>
  );
}

function Overview({ runtime, metrics, loading }: { runtime: Row; metrics: [string, string][]; loading: boolean }) {
  if (loading && !Object.keys(runtime).length)
    return (
      <div className="loading-state" role="status">
        Loading runtime status...
      </div>
    );
  return (
    <>
      <div className="strip">
        {metrics.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
      <section className="ledger">
        <div className="section-head">
          <div>
            <h2>Runtime exposure</h2>
            <p>These values describe the active published runtime, not just working tables.</p>
          </div>
          <span className={runtime.runtime_ready ? "status ok" : "status"}>{runtime.runtime_ready ? "Ready" : "Not ready"}</span>
        </div>
        <div className="exposure">
          <div>
            <span>Active snapshot</span>
            <strong>{String(runtime.config_version ?? "None")}</strong>
          </div>
          <div>
            <span>Healthy providers</span>
            <strong>{String(runtime.healthy_providers ?? 0)}</strong>
          </div>
          <div>
            <span>Active credentials</span>
            <strong>{String(runtime.active_keys ?? 0)}</strong>
          </div>
          <div>
            <span>Keys cooling down</span>
            <strong>{String(runtime.keys_in_cooldown ?? 0)}</strong>
          </div>
        </div>
      </section>
      <section className="ledger">
        <div className="section-head">
          <h2>Operator sequence</h2>
        </div>
        <div className="stations">
          <span>Observe</span>
          <span>Diagnose</span>
          <span>Configure</span>
          <span>Verify</span>
          <span>Publish</span>
        </div>
      </section>
    </>
  );
}

function Resource({ view, rows, loading, reload, notify }: { view: ResourceView; rows: Row[]; loading: boolean; reload: () => Promise<void>; notify: (notice: Notice) => void }) {
  const config = resources[view];
  const [search, setSearch] = useState("");
  const [editor, setEditor] = useState<{ row?: Row } | null>(null);
  const [confirm, setConfirm] = useState<{
    row: Row;
    action: "delete" | "rollback";
  } | null>(null);
  const [busy, setBusy] = useState("");
  const [issued, setIssued] = useState<{
    key: string;
    prefix: string;
    client: string;
  } | null>(null);
  const [keys, setKeys] = useState<{ client: Row; rows: Row[] } | null>(null);
  const [publishState, setPublishState] = useState<Row | null>(null);
  const [providerSetup, setProviderSetup] = useState<Row | null | undefined>(null);
  const [requestDetail, setRequestDetail] = useState<Row | null>(null);
  useEffect(() => {
    if (view === "configuration")
      void api("config/status")
        .then(setPublishState)
        .catch(() => setPublishState(null));
  }, [view, rows]);

  async function mutate(label: string, operation: () => Promise<unknown>) {
    if (busy) return false;
    setBusy(label);
    notify(null);
    try {
      await operation();
      await reload();
      notify({ kind: "success", message: `${label} completed.` });
    } catch (reason) {
      notify({
        kind: "error",
        message: reason instanceof Error ? reason.message : `${label} failed.`,
      });
      return false;
    } finally {
      setBusy("");
    }
    return true;
  }
  async function remove() {
    if (!confirm) return;
    const endpoint = view === "routing" ? "routes" : config.endpoint;
    const completed = await mutate("Delete", () => api(`${endpoint}/${String(confirm.row.id)}`, { method: "DELETE" }));
    if (completed) setConfirm(null);
  }
  async function issueKey(row: Row) {
    const label = window.prompt("Key label (optional)")?.trim() || null;
    const expiresAt = window.prompt("Expiry in ISO 8601 format (optional)")?.trim() || null;
    await mutate("Key issue", async () => {
      const result = await api(`clients/${row.id}/keys`, {
        method: "POST",
        body: JSON.stringify({ label, expires_at: expiresAt }),
      });
      setIssued({
        key: result.key,
        prefix: result.key_prefix,
        client: String(row.name),
      });
    });
  }
  async function rotateKey(key: Row) {
    const label = window.prompt("Replacement key label (optional)")?.trim() || null;
    const expiresAt = window.prompt("Replacement expiry in ISO 8601 format (optional)")?.trim() || null;
    await mutate("Key rotation", async () => {
      const result = await api(`client-keys/${key.id}/rotate`, {
        method: "POST",
        body: JSON.stringify({ label, expires_at: expiresAt }),
      });
      setIssued({ key: result.key, prefix: result.key_prefix, client: String(keys?.client.name ?? "Gateway client") });
      if (keys) {
        const refreshed = await api(`clients/${keys.client.id}/keys`);
        setKeys({ ...keys, rows: refreshed.data });
      }
    });
  }
  async function showKeys(row: Row) {
    setBusy("Load keys");
    try {
      setKeys({
        client: row,
        rows: (await api(`clients/${row.id}/keys`)).data,
      });
    } catch (reason) {
      notify({
        kind: "error",
        message: reason instanceof Error ? reason.message : "Unable to load keys",
      });
    } finally {
      setBusy("");
    }
  }
  async function publish() {
    if (!window.confirm("Publish the current working configuration and refresh the gateway runtime?")) return;
    await mutate("Configuration publish", () => api("config/publish", { method: "POST" }));
  }
  async function showRequest(row: Row) {
    setBusy("Request detail");
    try {
      setRequestDetail(await api(`requests/${row.id}`));
    } catch (reason) {
      notify({ kind: "error", message: reason instanceof Error ? reason.message : "Unable to load request detail" });
    } finally {
      setBusy("");
    }
  }
  async function rollback() {
    if (!confirm) return;
    const completed = await mutate("Configuration rollback", () => api(`config/versions/${confirm.row.id}/rollback`, { method: "POST" }));
    if (completed) setConfirm(null);
  }
  const actions = (row: Row) => (
    <div className="action-group">
      {config.mutable && (
        <>
          <button className="icon-button" title="Edit" aria-label={`Edit ${String(row.name ?? row.id)}`} onClick={() => setEditor({ row })}>
            <Pencil size={15} />
          </button>
          <button className="icon-button danger" title="Delete" aria-label={`Delete ${String(row.name ?? row.id)}`} onClick={() => setConfirm({ row, action: "delete" })}>
            <Trash2 size={15} />
          </button>
        </>
      )}
      {view === "credentials" && <button onClick={() => setEditor({ row: { ...row, __rotate: true } })}>Rotate</button>}
      {view === "providers" && <button onClick={() => setProviderSetup(row)}>Configure</button>}
      {view === "clients" && (
        <>
          <button onClick={() => void issueKey(row)} disabled={!!busy}>
            Issue key
          </button>
          <button onClick={() => void showKeys(row)}>Keys</button>
        </>
      )}
      {view === "alerts" && row.status === "open" && <button onClick={() => void mutate("Alert acknowledge", () => api(`alerts/${row.id}/acknowledge`, { method: "POST" }))}>Acknowledge</button>}
      {view === "alerts" && row.status !== "resolved" && <button onClick={() => void mutate("Alert resolve", () => api(`alerts/${row.id}/resolve`, { method: "POST" }))}>Resolve</button>}
      {view === "configuration" && row.status !== "published" && <button onClick={() => setConfirm({ row, action: "rollback" })}>Rollback</button>}
      {view === "configuration" && <span className="immutable-label">Snapshot immutable</span>}
      {view === "requests" && <button onClick={() => void showRequest(row)} disabled={!!busy}>Trace request</button>}
    </div>
  );
  const filteredRows = search.trim() ? rows.filter((row) => Object.values(row).some((value) => display(value, "").toLowerCase().includes(search.trim().toLowerCase()))) : rows;
  return (
    <section className="ledger">
      {view === "configuration" && publishState && (
        <div className={`publish-state ${publishState.has_unpublished_changes ? "draft" : "published"}`}>
          <strong>{publishState.has_unpublished_changes ? "Unpublished working changes" : "Working configuration matches production"}</strong>
          <span>Active snapshot {String(publishState.active_version ?? "None")}. Publishing creates a new immutable snapshot and activates it after runtime refresh.</span>
          {Boolean(publishState.has_unpublished_changes) && Array.isArray(publishState.changes) && (publishState.changes as Row[]).length > 0 && (
            <div className="change-review">
              <span className="change-review-head">{Number(publishState.change_count ?? (publishState.changes as Row[]).length)} change{Number(publishState.change_count ?? (publishState.changes as Row[]).length) === 1 ? "" : "s"} will become active when you publish</span>
              <ul>
                {(publishState.changes as Row[]).map((entry, index) => (
                  <li key={index} className={`change-${String(entry.change)}`}>
                    <span className="change-mark" aria-hidden="true">{entry.change === "added" ? "+" : entry.change === "removed" ? "-" : "~"}</span>
                    <span className="change-copy"><strong>{String(entry.resource)}</strong>{String(entry.summary)}</span>
                  </li>
                ))}
              </ul>
              {Number(publishState.change_count ?? 0) > (publishState.changes as Row[]).length && (
                <span className="change-more">Showing the first {(publishState.changes as Row[]).length} of {String(publishState.change_count)} changes.</span>
              )}
            </div>
          )}
          {Boolean(publishState.has_unpublished_changes) && (!Array.isArray(publishState.changes) || (publishState.changes as Row[]).length === 0) && (
            // The gateway guarantees a claimed change can be named, so this is a
            // fallback rather than the normal path. It must still be honest: "the
            // initial configuration" is only true before anything has been published,
            // and an unitemised difference has to say so rather than imply an empty
            // draft is reviewable.
            <span>
              {publishState.active_version == null
                ? "Nothing has been published yet. Publishing creates the first snapshot."
                : Array.isArray(publishState.changed_sections) && (publishState.changed_sections as string[]).length > 0
                  ? `Changes affect: ${(publishState.changed_sections as string[]).join(", ")}.`
                  : "The working configuration differs from the published snapshot, but the difference could not be itemised. Review before publishing."}
            </span>
          )}
        </div>
      )}
      <div className="section-head">
        <div>
          <h2>{config.title}</h2>
          <p>{config.mutable ? "Working configuration. Changes remain drafts until explicitly published." : view === "configuration" ? "Published snapshots are immutable; rollback creates a new active version." : "Operational records are read-only."}</p>
        </div>
        <div className="section-actions">
          {rows.length > 0 && (
            <label className="search-field">
              <span className="sr-only">Search {config.title}</span>
              <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={`Search ${config.title.toLowerCase()}`} />
            </label>
          )}
          {config.mutable && (
            <button onClick={() => setEditor({})}>
              <Plus size={15} />
              Add record
            </button>
          )}
          {view === "providers" && <button className="primary" onClick={() => setProviderSetup(undefined)}><Plus size={15} />Guided setup</button>}
          {view === "configuration" && (
            <button className="primary" disabled={!!busy || publishState?.has_unpublished_changes === false} onClick={() => void publish()}>
              {busy ? "Publishing..." : "Review and publish"}
            </button>
          )}
        </div>
      </div>
      {loading ? (
        <div className="loading-state" role="status">
          Loading {config.title.toLowerCase()}...
        </div>
      ) : (
        <DataTable rows={filteredRows} columns={config.columns} actions={actions} />
      )}
      {editor && (
        <ResourceEditor
          view={view}
          row={editor.row}
          onClose={() => setEditor(null)}
          onSaved={async (message) => {
            setEditor(null);
            await reload();
            notify({ kind: "success", message });
          }}
        />
      )}
      {confirm && <ConfirmDialog title={confirm.action === "delete" ? "Delete record" : "Rollback configuration"} message={confirm.action === "delete" ? `Delete ${String(confirm.row.name ?? confirm.row.id)}? Related records may also be removed.` : `Make version ${String(confirm.row.id)} the active configuration?`} busy={!!busy} onCancel={() => setConfirm(null)} onConfirm={() => void (confirm.action === "delete" ? remove() : rollback())} />}
      {issued && <OneTimeKey value={issued} onClose={() => setIssued(null)} />}
      {keys && (
        <KeyManager
          value={keys}
          busy={busy}
          onClose={() => setKeys(null)}
          onRevoke={async (key) => {
            const reason = window.prompt("Revocation reason (optional)");
            if (reason === null) return;
            if (!window.confirm(`Revoke key ${String(key.key_prefix)}? This cannot be undone.`)) return;
            await mutate("Key revocation", () =>
              api(`client-keys/${key.id}/revoke`, {
                method: "POST",
                body: JSON.stringify({ reason: reason.trim() || null }),
              }),
            );
            const refreshed = await api(`clients/${keys.client.id}/keys`);
            setKeys({ ...keys, rows: refreshed.data });
          }}
          onRotate={rotateKey}
        />
      )}
      {requestDetail && <RequestDetail value={requestDetail} onClose={() => setRequestDetail(null)} />}
      {providerSetup !== null && <ProviderSetup provider={providerSetup ?? undefined} onClose={() => setProviderSetup(null)} onSaved={async () => { setProviderSetup(null); await reload(); notify({ kind: "success", message: "Provider configuration reconciled. Review and publish when ready." }); }} />}
    </section>
  );
}

function formatCurrencyTotals(value: unknown): string {
  const rows = Array.isArray(value) ? (value as Row[]) : [];
  // Drop rows with no cost rather than coercing them: Number(null).toFixed(4)
  // rendered an unpriced attempt as "0.0000 null", which reads as a measured
  // zero. Amounts are grouped per currency and never summed across currencies.
  const totals = new Map<string, number>();
  rows.forEach((row) => {
    if (row.estimated_cost === null || row.estimated_cost === undefined) return;
    const currency = String(row.currency ?? "").trim();
    if (!currency) return;
    totals.set(currency, (totals.get(currency) ?? 0) + Number(row.estimated_cost));
  });
  if (!totals.size) return "Pricing unavailable";
  const priced = rows.filter((row) => row.estimated_cost !== null && row.estimated_cost !== undefined).length;
  const rendered = [...totals.entries()].sort().map(([currency, amount]) => `${amount.toFixed(4)} ${currency}`).join(" / ");
  // Say so when only part of the request could be priced, so a partial figure is
  // never mistaken for the whole cost.
  return priced < rows.length ? `${rendered} (${rows.length - priced} unpriced)` : rendered;
}

function AnalyticsTables({ payload }: { payload: Row }) {
  const usage = payload.usage as Row | undefined;
  const total = Number(usage?.usage_records ?? 0);
  const coverage = total ? `${((Number(usage?.priced_records ?? 0) / total) * 100).toFixed(1)}%` : "No usage";
  const failover = payload.failover as Row | undefined;
  return (
    <div className="analytics-stack">
      <div className="strip">
        <div>
          <span>Input tokens</span>
          <strong>{Number(usage?.input_tokens ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Output tokens</span>
          <strong>{Number(usage?.output_tokens ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Pricing coverage</span>
          <strong>{coverage}</strong>
        </div>
        <div>
          <span>Estimated cost</span>
          <strong>{formatCurrencyTotals(payload.costs_by_currency)}</strong>
        </div>
      </div>
      <div className="strip">
        <div>
          <span>Upstream retries</span>
          <strong>{Number(failover?.retries ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Provider fallbacks</span>
          <strong>{Number(failover?.fallbacks ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Requests retried</span>
          <strong>{Number(failover?.requests_with_retries ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Requests with fallback</span>
          <strong>{Number(failover?.requests_with_fallbacks ?? 0).toLocaleString()}</strong>
        </div>
      </div>
      <h3>Request activity by model</h3>
      <DataTable rows={(payload.by_model as Row[]) ?? []} columns={["model", "requests", "succeeded", "failed", "average_latency_ms"]} />
      <h3>Provider attempts</h3>
      <DataTable rows={(payload.attempts_by_provider as Row[]) ?? []} columns={["provider", "attempts", "succeeded", "failed", "cancelled", "average_latency_ms", "p95_latency_ms"]} />
      <h3>Model attempts</h3>
      <DataTable rows={(payload.attempts_by_model as Row[]) ?? []} columns={["model", "attempts", "succeeded", "failed", "cancelled", "average_latency_ms", "p95_latency_ms"]} />
      <h3>Route attempts</h3>
      <DataTable rows={(payload.attempts_by_route as Row[]) ?? []} columns={["route", "attempts", "succeeded", "failed", "cancelled", "average_latency_ms", "p95_latency_ms"]} />
      <h3>Usage by provider</h3>
      <DataTable rows={(payload.usage_by_provider as Row[]) ?? []} columns={["provider", "usage_records", "input_tokens", "output_tokens", "cached_tokens", "priced_records"]} />
      <h3>Usage by model</h3>
      <DataTable rows={(payload.usage_by_model as Row[]) ?? []} columns={["model", "usage_records", "input_tokens", "output_tokens", "cached_tokens", "priced_records"]} />
      <h3>Usage by route</h3>
      <DataTable rows={(payload.usage_by_route as Row[]) ?? []} columns={["route", "usage_records", "input_tokens", "output_tokens", "cached_tokens", "priced_records"]} />
      <h3>Daily activity</h3>
      <DataTable rows={(payload.daily as Row[]) ?? []} columns={["day", "requests", "succeeded", "failed", "input_tokens", "output_tokens", "priced_records"]} />
    </div>
  );
}

function ResourceEditor({ view, row, onClose, onSaved }: { view: ResourceView; row?: Row; onClose: () => void; onSaved: (message: string) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [references, setReferences] = useState<Record<string, Row[]>>({});
  const [referencesLoading, setReferencesLoading] = useState(true);
  const editing = !!row?.id && !row.__rotate;
  useEffect(() => {
    let active = true;
    Promise.all([api("providers"), api("models"), api("provider-models"), api("routing-policies")])
      .then(([providers, models, mappings, policies]) => {
        if (active)
          setReferences({
            providers: providers.data ?? [],
            models: models.data ?? [],
            mappings: mappings.data ?? [],
            policies: policies.data ?? [],
          });
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "Unable to load form options.");
      })
      .finally(() => {
        if (active) setReferencesLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const body = buildPayload(view, data, row);
      const endpoint = view === "routing" ? "routes" : resources[view].endpoint;
      const path = row?.__rotate ? `credentials/${row.id}/rotate` : editing ? `${endpoint}/${row?.id}` : endpoint;
      await api(path, {
        method: row?.__rotate ? "POST" : editing ? "PUT" : "POST",
        body: JSON.stringify(body),
      });
      await onSaved(row?.__rotate ? "Credential rotated securely." : editing ? "Changes saved." : "Record created.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Save failed");
      setBusy(false);
    }
  }
  return (
    <div className="dialog-backdrop">
      <section className="editor" role="dialog" aria-modal="true" aria-labelledby="editor-title">
        <div className="editor-head">
          <div>
            <h2 id="editor-title">{row?.__rotate ? "Rotate credential" : editing ? `Edit ${resources[view].title}` : `Add ${resources[view].title}`}</h2>
            <p>{row?.__rotate ? "The replacement secret is encrypted before persistence and is never returned." : "Changes affect the working configuration until published."}</p>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close editor">
            <X size={18} />
          </button>
        </div>
        <form onSubmit={submit}>
          {referencesLoading && !row?.__rotate ? (
            <div className="loading-state" role="status">
              Loading form options...
            </div>
          ) : row?.__rotate ? (
            <Field name="secret" label="Replacement secret" type="password" required />
          ) : (
            <Fields view={view} row={row} references={references} />
          )}
          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}
          <div className="form-actions">
            <button type="button" onClick={onClose}>
              Cancel
            </button>
            <button className="primary" disabled={busy || referencesLoading}>
              {busy ? "Saving..." : editing ? "Save changes" : "Create record"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

function Field({ name, label, defaultValue, type = "text", required = false, min, max, step, readOnly = false }: { name: string; label: string; defaultValue?: unknown; type?: string; required?: boolean; min?: number; max?: number; step?: string; readOnly?: boolean }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input name={name} type={type} defaultValue={String(defaultValue ?? "")} required={required} min={min} max={max} step={step} readOnly={readOnly} />
    </label>
  );
}

function SelectField({ name, label, value, options }: { name: string; label: string; value?: unknown; options: [string, string][] }) {
  return (
    <label className="field">
      <span>{label}</span>
      <select name={name} defaultValue={String(value ?? options[0]?.[0] ?? "")}>
        {options.map(([id, text]) => (
          <option key={id} value={id}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}

function Fields({ view, row = {}, references }: { view: ResourceView; row?: Row; references: Record<string, Row[]> }) {
  const protocols: [string, string][] = [
    ["anthropic_messages", "Anthropic Messages"],
    ["openai_chat_completions", "OpenAI Chat Completions"],
    ["openai_responses", "OpenAI Responses"],
  ];
  const enabled = (
    <SelectField
      name="enabled"
      label="Status"
      value={String(row.enabled ?? true)}
      options={[
        ["true", "Enabled"],
        ["false", "Disabled"],
      ]}
    />
  );
  if (view === "providers")
    return (
      <>
        <Field name="name" label="Provider name" defaultValue={row.name} required />
        <Field name="base_url" label="Base URL" type="url" defaultValue={row.base_url} required />
        <Field name="capabilities" label="Shared capabilities (comma separated)" defaultValue={((row.capabilities as string[]) ?? []).join(", ")} />
        <Field name="priority" label="Priority" type="number" defaultValue={row.priority ?? 100} min={0} /> <Field name="timeout_seconds" label="Timeout seconds" type="number" defaultValue={row.timeout_seconds ?? 600} min={1} />
        {enabled}
      </>
    );
  if (view === "credentials")
    return (
      <>
        <SelectField name="provider_id" label="Provider" value={row.provider_id} options={(references.providers ?? []).map((item) => [String(item.id), String(item.name)])} />
        <Field name="name" label="Credential label" defaultValue={row.name} required />
        {!row.id && <Field name="secret" label="Provider secret" type="password" required />}
        <Field name="priority" label="Priority" type="number" defaultValue={row.priority ?? 100} min={0} />
        <Field name="quota_limit" label="Quota limit" type="number" defaultValue={row.quota_limit} min={0} step="0.01" />
        <Field name="quota_threshold" label="Quota threshold" type="number" defaultValue={row.quota_threshold ?? 0.95} min={0} step="0.01" />
        <Field name="requests_per_minute" label="Requests per minute" type="number" defaultValue={row.requests_per_minute} min={1} />
        <Field name="tokens_per_minute" label="Tokens per minute" type="number" defaultValue={row.tokens_per_minute} min={1} />
        {row.id && enabled}
      </>
    );
  if (view === "clients")
    return (
      <>
        <Field name="name" label="Client name" defaultValue={row.name} required />
        <Field name="allowed_protocols" label="Allowed protocols (comma separated)" defaultValue={((row.allowed_protocols as string[]) ?? []).join(", ")} required />
        <Field name="allowed_models" label="Allowed models (blank means any)" defaultValue={((row.allowed_models as string[]) ?? []).join(", ")} />
        <Field name="requests_per_minute" label="Requests per minute" type="number" defaultValue={row.requests_per_minute} min={1} />
        <Field name="tokens_per_minute" label="Tokens per minute" type="number" defaultValue={row.tokens_per_minute} min={1} />
        <Field name="spending_limit" label="Spending limit" type="number" defaultValue={row.spending_limit} min={0} step="0.01" />
        {enabled}
      </>
    );
  if (view === "models")
    return (
      <>
        <Field name="id" label="Canonical model ID" defaultValue={row.id} required readOnly={!!row.id} />
        <Field name="display_name" label="Display name" defaultValue={row.display_name} required />
        <Field name="aliases" label="Aliases (comma separated)" defaultValue={((row.aliases as string[]) ?? []).join(", ")} />
        <Field name="capabilities" label="Capabilities (comma separated)" defaultValue={((row.capabilities as string[]) ?? []).join(", ")} />
        <Field name="context_window" label="Context window" type="number" defaultValue={row.context_window} min={1} />
        {enabled}
      </>
    );
  if (view === "provider-models")
    return (
      <>
        <SelectField name="provider_id" label="Provider" value={row.provider_id} options={(references.providers ?? []).map((item) => [String(item.id), String(item.name)])} />
        <SelectField name="model_id" label="Canonical model" value={row.model_id} options={(references.models ?? []).map((item) => [String(item.id), String(item.display_name ?? item.id)])} />
        <Field name="upstream_model_id" label="Upstream model ID" defaultValue={row.upstream_model_id} required />
        <SelectField name="protocol" label="How requests are sent" value={row.protocol} options={protocols} />
        <Field name="pricing" label="Pricing JSON (per million tokens)" defaultValue={row.pricing ? JSON.stringify(row.pricing) : "{}"} />
        <Field name="settings" label="Transport settings JSON" defaultValue={row.settings ? JSON.stringify(row.settings) : "{}"} />
        <Field name="capabilities" label="Capabilities (comma separated)" defaultValue={((row.capabilities as string[]) ?? []).join(", ")} />
        <Field name="priority" label="Routing priority" type="number" defaultValue={row.priority ?? 100} min={0} />
        <Field name="weight" label="Traffic weight" type="number" defaultValue={row.weight ?? 1} min={0} step="0.1" />
        <Field name="max_concurrency" label="Maximum concurrent requests" type="number" defaultValue={row.max_concurrency ?? 8} min={1} />
        {enabled}
      </>
    );
  if (view === "pools")
    return (
      <>
        <Field name="name" label="Pool name" defaultValue={row.name} required />
        <SelectField
          name="strategy"
          label="Selection strategy"
          value={row.strategy}
          options={[
            ["priority", "Priority"],
            ["weighted", "Weighted"],
            ["least_loaded", "Least loaded"],
          ]}
        />
        <SelectField name="model_id" label="Canonical model" value={row.model_id ?? ""} options={[["", "All models"], ...(references.models ?? []).map((item) => [String(item.id), String(item.display_name ?? item.id)] as [string, string])]} />
        {enabled}
        <Field name="settings" label="Pool settings JSON" defaultValue={row.settings ? JSON.stringify(row.settings) : "{}"} />
      </>
    );
  if (view === "budgets")
    return (
      <>
        <Field name="name" label="Budget name" defaultValue={row.name} required />
        <SelectField name="scope_type" label="Scope" value={row.scope_type} options={["global", "client", "provider", "credential", "model", "route"].map((item) => [item, item])} />
        <Field name="scope_id" label="Scope ID" defaultValue={row.scope_id} />
        <SelectField
          name="period"
          label="Period"
          value={row.period}
          options={[
            ["daily", "Daily"],
            ["monthly", "Monthly"],
          ]}
        />
        <Field name="currency" label="Currency" defaultValue={row.currency ?? "USD"} required />
        <Field name="limit_amount" label="Limit" type="number" defaultValue={row.limit_amount} min={0} step="0.0001" />
        <Field name="warning_threshold" label="Warning threshold" type="number" defaultValue={row.warning_threshold ?? 0.8} min={0.01} max={1} step="0.01" />
        <SelectField
          name="enforcement"
          label="Enforcement"
          value={row.enforcement}
          options={[
            ["warn", "Warn"],
            ["block", "Block"],
          ]}
        />
        {enabled}
      </>
    );
  if (view === "routing")
    return (
      <>
        <SelectField name="model_id" label="Canonical model" value={row.model_id} options={(references.models ?? []).map((item) => [String(item.id), String(item.display_name ?? item.id)])} />
        <SelectField name="provider_model_id" label="Provider mapping" value={row.provider_model_id} options={(references.mappings ?? []).map((item) => [String(item.id), `${String(item.provider_name)} / ${String(item.upstream_model_id)}`])} />
        <SelectField name="policy_id" label="Routing policy" value={row.policy_id ?? ""} options={[["", "No policy"], ...(references.policies ?? []).map((item) => [String(item.id), String(item.name)] as [string, string])]} />
        <Field name="pool_id" label="Provider pool ID (optional)" defaultValue={row.pool_id} />
        <Field name="priority" label="Priority" type="number" defaultValue={row.priority ?? 100} min={0} />
        <SelectField
          name="allow_model_fallback"
          label="Model fallback"
          value={String(row.allow_model_fallback ?? false)}
          options={[
            ["false", "Disabled"],
            ["true", "Allowed"],
          ]}
        />
        {enabled}
      </>
    );
  if (view === "alert-rules")
    return (
      <>
        <Field name="name" label="Rule name" defaultValue={row.name} required />
        <Field name="event_type" label="Event type" defaultValue={row.event_type} required />
        <SelectField
          name="severity"
          label="Severity"
          value={row.severity}
          options={[
            ["info", "Info"],
            ["warning", "Warning"],
            ["critical", "Critical"],
          ]}
        />
        <Field name="scope_type" label="Scope type" defaultValue={row.scope_type} />
        <Field name="scope_id" label="Scope ID" defaultValue={row.scope_id} />
        <Field name="condition" label="Condition JSON" defaultValue={row.condition ? JSON.stringify(row.condition) : "{}"} />
        <Field name="cooldown_seconds" label="Cooldown seconds" type="number" defaultValue={row.cooldown_seconds ?? 300} min={0} />
        {enabled}
      </>
    );
  const policy = row.policy as Row | undefined;
  return (
    <>
      <Field name="name" label="Policy name" defaultValue={row.name} required />
      <Field name="health_weight" label="Health weight" type="number" defaultValue={policy?.health_weight ?? 3} min={0} step="0.1" />
      <Field name="quota_weight" label="Quota weight" type="number" defaultValue={policy?.quota_weight ?? 2} min={0} step="0.1" />
      <Field name="rate_limit_weight" label="Rate-limit weight" type="number" defaultValue={policy?.rate_limit_weight ?? 2} min={0} step="0.1" />
      <Field name="concurrency_weight" label="Concurrency weight" type="number" defaultValue={policy?.concurrency_weight ?? 1} min={0} step="0.1" />
      <Field name="latency_weight" label="Latency weight" type="number" defaultValue={policy?.latency_weight ?? 1} min={0} step="0.1" />
      <Field name="failure_weight" label="Failure penalty" type="number" defaultValue={policy?.failure_weight ?? 2} min={0} step="0.1" />
      {enabled}
    </>
  );
}

function buildPayload(view: ResourceView, data: FormData, row?: Row) {
  const value = (name: string) => String(data.get(name) ?? "").trim();
  const list = (name: string) =>
    value(name)
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  const optionalNumber = (name: string) => (value(name) ? Number(value(name)) : null);
  const bool = (name: string) => value(name) === "true";
  const json = (name: string) => {
    try {
      return value(name) ? JSON.parse(value(name)) : {};
    } catch {
      throw new Error(`${name} must be valid JSON.`);
    }
  };
  if (row?.__rotate) return { secret: value("secret") };
  if (view === "providers")
    return {
      name: value("name"),
      base_url: value("base_url"),
      capabilities: list("capabilities"),
      priority: Number(value("priority")),
      timeout_seconds: Number(value("timeout_seconds")),
      enabled: bool("enabled"),
    };
  if (view === "credentials")
    return row?.id
      ? {
          name: value("name"),
          priority: Number(value("priority")),
          quota_limit: optionalNumber("quota_limit"),
          quota_threshold: Number(value("quota_threshold")),
          requests_per_minute: optionalNumber("requests_per_minute"),
          tokens_per_minute: optionalNumber("tokens_per_minute"),
          enabled: bool("enabled"),
        }
      : {
          provider_id: value("provider_id"),
          name: value("name"),
          secret: value("secret"),
          priority: Number(value("priority")),
          quota_limit: optionalNumber("quota_limit"),
          quota_threshold: Number(value("quota_threshold")),
          requests_per_minute: optionalNumber("requests_per_minute"),
          tokens_per_minute: optionalNumber("tokens_per_minute"),
        };
  if (view === "clients")
    return {
      name: value("name"),
      allowed_protocols: list("allowed_protocols"),
      allowed_models: list("allowed_models"),
      requests_per_minute: optionalNumber("requests_per_minute"),
      tokens_per_minute: optionalNumber("tokens_per_minute"),
      spending_limit: optionalNumber("spending_limit"),
      enabled: bool("enabled"),
    };
  if (view === "models")
    return {
      id: value("id"),
      display_name: value("display_name"),
      aliases: list("aliases"),
      capabilities: list("capabilities"),
      context_window: optionalNumber("context_window"),
      enabled: bool("enabled"),
    };
  if (view === "provider-models")
    return {
      provider_id: value("provider_id"),
      model_id: value("model_id"),
      upstream_model_id: value("upstream_model_id"),
      protocol: value("protocol"),
      pricing: json("pricing"),
      settings: json("settings"),
      capabilities: list("capabilities"),
      priority: Number(value("priority")),
      weight: Number(value("weight")),
      max_concurrency: Number(value("max_concurrency")),
      enabled: bool("enabled"),
    };
  if (view === "routing")
    return {
      model_id: value("model_id"),
      provider_model_id: value("provider_model_id"),
      policy_id: value("policy_id") || null,
      pool_id: value("pool_id") || null,
      priority: Number(value("priority")),
      allow_model_fallback: bool("allow_model_fallback"),
      enabled: bool("enabled"),
    };
  if (view === "pools")
    return {
      name: value("name"),
      model_id: value("model_id") || null,
      enabled: bool("enabled"),
      strategy: value("strategy"),
      settings: json("settings"),
    };
  if (view === "budgets")
    return {
      name: value("name"),
      scope_type: value("scope_type"),
      scope_id: value("scope_id") || null,
      period: value("period"),
      currency: value("currency").toUpperCase(),
      limit_amount: Number(value("limit_amount")),
      warning_threshold: Number(value("warning_threshold")),
      enforcement: value("enforcement"),
      enabled: bool("enabled"),
    };
  if (view === "alert-rules")
    return {
      name: value("name"),
      enabled: bool("enabled"),
      severity: value("severity"),
      event_type: value("event_type"),
      scope_type: value("scope_type") || null,
      scope_id: value("scope_id") || null,
      condition: json("condition"),
      cooldown_seconds: Number(value("cooldown_seconds")),
    };
  return {
    name: value("name"),
    enabled: bool("enabled"),
    policy: {
      health_weight: Number(value("health_weight")),
      quota_weight: Number(value("quota_weight")),
      rate_limit_weight: Number(value("rate_limit_weight")),
      concurrency_weight: Number(value("concurrency_weight")),
      latency_weight: Number(value("latency_weight")),
      failure_weight: Number(value("failure_weight")),
    },
  };
}

function ConfirmDialog({ title, message, busy, onCancel, onConfirm }: { title: string; message: string; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  return (
    <div className="dialog-backdrop">
      <section className="confirm-dialog" role="alertdialog" aria-modal="true">
        <h2>{title}</h2>
        <p>{message}</p>
        <div className="form-actions">
          <button onClick={onCancel}>Cancel</button>
          <button className="danger-button" disabled={busy} onClick={onConfirm}>
            {busy ? "Working..." : "Confirm"}
          </button>
        </div>
      </section>
    </div>
  );
}
function OneTimeKey({ value, onClose }: { value: { key: string; prefix: string; client: string }; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="dialog-backdrop">
      <section className="secret-dialog" role="dialog" aria-modal="true">
        <KeyRound size={22} />
        <h2>Gateway key issued</h2>
        <p>This plaintext is shown once. Store it before dismissing this window.</p>
        <dl>
          <div>
            <dt>Client</dt>
            <dd>{value.client}</dd>
          </div>
          <div>
            <dt>Prefix</dt>
            <dd>{value.prefix}</dd>
          </div>
        </dl>
        <code>{value.key}</code>
        <div className="form-actions">
          <button
            onClick={async () => {
              await navigator.clipboard.writeText(value.key);
              setCopied(true);
            }}
          >
            <Clipboard size={15} />
            {copied ? "Copied" : "Copy key"}
          </button>
          <button className="primary" onClick={onClose}>
            I stored this key
          </button>
        </div>
      </section>
    </div>
  );
}
function KeyManager({ value, busy, onClose, onRevoke, onRotate }: { value: { client: Row; rows: Row[] }; busy: string; onClose: () => void; onRevoke: (row: Row) => Promise<void>; onRotate: (row: Row) => Promise<void> }) {
  return (
    <div className="dialog-backdrop">
      <section className="editor key-manager" role="dialog" aria-modal="true">
        <div className="editor-head">
          <div>
            <h2>{String(value.client.name)} keys</h2>
            <p>Keys are immutable. Rotate active keys or revoke access permanently.</p>
          </div>
          <button className="icon-button" onClick={onClose}>
            <X size={18} />
          </button>
        </div>
        <DataTable
          rows={value.rows}
          columns={["key_prefix", "label", "expires_at", "enabled", "last_used_at", "created_at", "revoked_at", "revoke_reason"]}
          actions={(row) =>
            row.enabled ? (
              <>
                <button disabled={!!busy} onClick={() => void onRotate(row)}>
                  Rotate
                </button>
                <button className="danger-text" disabled={!!busy} onClick={() => void onRevoke(row)}>
                  Revoke
                </button>
              </>
            ) : (
              <span>Revoked</span>
            )
          }
        />
      </section>
    </div>
  );
}


// Every reason the routing engine can give for skipping a candidate, said plainly.
// Two are prefixes because the health state is appended to them.
const EXCLUSION_REASONS: Record<string, string> = {
  route_excluded_this_request: "Already tried and failed earlier in this same request",
  provider_missing_from_snapshot: "The provider is not in the published configuration",
  provider_disabled: "The provider is turned off",
  provider_circuit_open: "Paused after repeated failures, and not yet retried",
  credential_excluded_this_request: "Already tried and failed earlier in this same request",
  credential_disabled: "The credential is turned off",
  credential_other_provider: "Belongs to a different provider",
  credential_in_cooldown: "Cooling down after a recent failure",
  credential_quota_exhausted: "Out of quota",
  credential_rpm_exhausted: "At its requests-per-minute limit",
  credential_tpm_exhausted: "At its tokens-per-minute limit",
  credential_concurrency_exhausted: "At its concurrent-request limit",
  credential_not_permitted_for_route: "Not permitted to serve this model",
  credential_not_in_route_pool: "Not a member of the credential pool this route restricts to",
  credential_not_in_policy_allow_list: "Not on the routing policy's allow list",
  latency_above_policy_limit: "Slower than the routing policy allows",
  quota_headroom_below_policy_minimum: "Less quota headroom than the policy requires",
  rpm_headroom_below_policy_minimum: "Less request headroom than the policy requires",
  tpm_headroom_below_policy_minimum: "Less token headroom than the policy requires",
};

const HEALTH_REASONS: Record<string, string> = {
  rate_limited: "rate limited by the provider",
  auth_failed: "rejected by the provider",
  quota_exhausted: "out of quota",
  unavailable: "unreachable",
  cooldown: "cooling down",
  disabled: "turned off",
};

function explainExclusion(reason: unknown): string {
  const key = String(reason ?? "");
  if (!key) return "Eligible";
  const known = EXCLUSION_REASONS[key];
  if (known) return known;
  for (const prefix of ["credential_health_", "provider_health_"]) {
    if (key.startsWith(prefix)) {
      const state = key.slice(prefix.length);
      const subject = prefix.startsWith("credential") ? "credential" : "provider";
      return `The ${subject} is ${HEALTH_REASONS[state] ?? state.replaceAll("_", " ")}`;
    }
  }
  return key.replaceAll("_", " ");
}

/** Why this request went where it went, one block per attempt. */
function RoutingDecision({ attempts }: { attempts: Row[] }) {
  if (!attempts.length) {
    return <p className="muted">No routing decision was recorded for this request.</p>;
  }
  return (
    <div className="routing-decision">
      {attempts.map((attempt, index) => {
        const considered = (attempt.considered as Row[] | undefined) ?? [];
        const selected = attempt.selected as Row | undefined;
        const eligible = considered.filter((row) => row.eligible === true);
        const excluded = considered.filter((row) => row.eligible !== true);
        return (
          <section key={index} className="routing-attempt">
            <header>
              <strong>Attempt {String(attempt.attempt_number ?? index + 1)}</strong>
              {attempt.is_fallback === true ? (
                <span className="badge">Fallback after {explainError(attempt.fallback_reason)}</span>
              ) : null}
            </header>
            {selected ? (
              <p className="routing-selected">
                Chose <strong>{readable(selected.provider)}</strong> using credential{" "}
                <strong>{readable(selected.credential_name)}</strong>
                {selected.score !== undefined ? ` (score ${Number(selected.score).toFixed(2)})` : ""}
                {eligible.length > 1 ? `, the best of ${eligible.length} eligible` : ""}.
              </p>
            ) : (
              <p className="routing-selected">
                Nothing was eligible, so no provider was contacted.
              </p>
            )}
            {excluded.length ? (
              <table className="routing-excluded">
                <caption>{excluded.length} candidate{excluded.length === 1 ? "" : "s"} skipped</caption>
                <thead>
                  <tr><th>Provider</th><th>Credential</th><th>Why it was skipped</th></tr>
                </thead>
                <tbody>
                  {excluded.map((row, position) => (
                    <tr key={position}>
                      <td>{readable(row.provider)}</td>
                      <td>{row.credential_name ? readable(row.credential_name) : "Whole route"}</td>
                      <td>{explainExclusion(row.reason)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}

function RequestDetail({ value, onClose }: { value: Row; onClose: () => void }) {
  const request = (value.request as Row | undefined) ?? {};
  const attempts = (value.attempts as Row[] | undefined) ?? [];
  const usage = (value.usage as Row[] | undefined) ?? [];
  const routing = (value.routing as Row[] | undefined) ?? [];
  const totals = usage.reduce<{ input: number; output: number; cached: number }>(
    (result, row) => ({
      input: result.input + Number(row.input_tokens ?? 0),
      output: result.output + Number(row.output_tokens ?? 0),
      cached: result.cached + Number(row.cached_tokens ?? 0),
    }),
    { input: 0, output: 0, cached: 0 },
  );
  return (
    <div className="dialog-backdrop">
      <section className="editor request-detail" role="dialog" aria-modal="true" aria-labelledby="request-detail-title">
        <div className="editor-head">
          <div>
            <h2 id="request-detail-title">Request trace</h2>
            <p>{String(request.resolved_model ?? request.requested_model ?? "Unknown model")} · {display(request.status, "status")}</p>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close request trace"><X size={18} /></button>
        </div>
        <div className="trace-summary">
          <div><span>Total latency</span><strong>{display(request.latency_ms, "latency_ms")}</strong></div>
          <div><span>Retries</span><strong>{String(request.retry_count ?? 0)}</strong></div>
          <div><span>Fallbacks used</span><strong>{String(request.fallback_count ?? 0)}</strong></div>
          <div><span>Final result</span><strong>{request.status === "succeeded" && Number(request.fallback_count ?? 0) > 0 ? "Succeeded through fallback" : display(request.status, "status")}</strong></div>
        </div>
        <h3>Routing attempts</h3>
        {attempts.length ? <ol className="attempt-list">{attempts.map((attempt, index) => <li key={String(attempt.id ?? index)}>
          <div><strong>{String(attempt.provider_name ?? `Provider attempt ${index + 1}`)}</strong><span>{display(attempt.status, "status")}</span></div>
          <p>{attempt.credential_name ? `Credential: ${String(attempt.credential_name)}` : "Credential name unavailable"} · {display(attempt.latency_ms, "latency_ms")}</p>
          {Boolean(attempt.upstream_status) && <p>Upstream response: {String(attempt.upstream_status)}</p>}
          {Boolean(attempt.error_category) && <p className="trace-error">{explainError(attempt.error_category)}</p>}
          {Boolean(attempt.response_committed) && <p>The response had already started, so the Gateway could not safely retry.</p>}
        </li>)}</ol> : <div className="empty-state"><strong>No provider attempts recorded</strong><span>The request ended before an upstream attempt was persisted.</span></div>}
        <h3>Why this routing was chosen</h3>
        <RoutingDecision attempts={routing} />
        <h3>Token usage</h3>
        <div className="trace-summary">
          <div><span>Input tokens</span><strong>{totals.input.toLocaleString()}</strong></div>
          <div><span>Output tokens</span><strong>{totals.output.toLocaleString()}</strong></div>
          <div><span>Cached tokens</span><strong>{totals.cached.toLocaleString()}</strong></div>
          <div><span>Cost</span><strong>{formatCurrencyTotals(usage)}</strong></div>
        </div>
      </section>
    </div>
  );
}
