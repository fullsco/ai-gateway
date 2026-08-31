/**
 * How a raw gateway value is rendered for an operator.
 *
 * Every dictionary here exists because a bare column value misled somebody: a health
 * state that says nothing about whether the router will use a credential, a quota
 * figure with no limit behind it, a six-day-old reading that looks identical to a
 * fresh one. The rule the module follows is that an absent value is described, never
 * coerced, and a measured value is never confused with an assumed one.
 */

export type Row = Record<string, unknown>;

export function readable(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not recorded";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.length ? value.map(readable).join(", ") : "None configured";
  try {
    return JSON.stringify(value);
  } catch {
    return "Unavailable";
  }
}

export function display(value: unknown, key: string, row?: Row) {
  if (key === "quota_source" || key === "balance_source") return observationSource(value);
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
    // A reading nobody has ever taken is not a setting nobody filled in. Saying
    // "Not configured" invited an operator to look for the field they had missed.
    if (OBSERVATION_KEYS.has(key)) return "Not observed";
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
  // cooldown_until is as much a timestamp as anything ending in _at, but the suffix
  // test excluded it, so every cooling-down credential displayed a raw ISO string
  // with microseconds and a UTC offset.
  if (key.endsWith("_at") || key.endsWith("_until")) {
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

export type State = "ok" | "warn" | "bad" | "idle" | "";

// The three-state reading of every column that reports a condition. The dictionaries
// below already turn these values into sentences; this turns the same values into the
// one visual vocabulary, so amber means the same thing in a table cell and on a badge.
//
// Membership is deliberate rather than derived from the display string. "on trial" and
// "cooling down" both recover by themselves, so both are amber; "needs attention" never
// retries, so it is red. And an absence is not a failure: a credential with no quota
// signal is `idle`, because drawing it red would put a fault beside a figure nobody
// has ever measured.
const STATES: Record<string, State> = {
  // status / health / attempt results
  healthy: "ok", succeeded: "ok", success: "ok", serving: "ok", published: "ok", resolved: "ok",
  degraded: "warn", cooldown: "warn", rate_limited: "warn", pending: "warn", draft: "warn", warning: "warn",
  failed: "bad", auth_failed: "bad", unavailable: "bad", quota_exhausted: "bad", error: "bad", critical: "bad", firing: "bad",
  disabled: "idle", unknown: "idle", info: "idle",
  // routing_state, which says what the router will do rather than what last happened
  "in service": "ok", "on trial": "warn", "cooling down": "warn", "needs attention": "bad",
  // quota_confidence
  known: "ok", estimated: "warn",
  // live_access. A client an operator has disabled that is still serving is the
  // dangerous one - they believe access is revoked and it is not.
  "STILL SERVING until you publish": "bad",
  "not serving until you publish": "warn",
  "not serving yet, publish to activate": "warn",
  "not serving": "idle",
};

/**
 * How a value should read: healthy, worth watching, broken, or simply not measured.
 *
 * Returns `""` for a column that carries no condition at all, which is most of them -
 * a provider name has no state and drawing one on it is noise.
 */
export function stateOf(value: unknown, key: string): State {
  // A reason is only ever recorded when something went wrong.
  if (key === "error_category") return value === null || value === undefined || value === "" ? "" : "bad";
  if (key === "cooldown_until") return value === null || value === undefined || value === "" ? "" : "warn";
  // The age of a reading, not the reading itself. Past the staleness horizon the number
  // may describe a state of the world that no longer exists, which is worth flagging
  // even though nothing about it has failed.
  if (OBSERVATION_KEYS.has(key)) {
    if (value === null || value === undefined || value === "") return "idle";
    const observed = new Date(String(value)).getTime();
    if (Number.isNaN(observed)) return "idle";
    return (Date.now() - observed) / 3_600_000 >= STALE_AFTER_HOURS ? "warn" : "ok";
  }
  if (key === "enabled" || key === "route_active" || key === "runtime_ready" || key === "response_committed") {
    if (value === null || value === undefined || value === "") return "";
    return value === true || value === "true" ? "ok" : "idle";
  }
  if (!STATE_KEYS.has(key)) return "";
  // A count is a state only in relation to zero: no routable credential is a fault,
  // and a fault the aggregate columns were reporting as a plain number.
  if (key === "routable_credentials" || key === "healthy_credentials" || key === "routable_members" || key === "active_keys") {
    return Number(value) > 0 ? "ok" : "bad";
  }
  return STATES[String(value ?? "").trim()] ?? "";
}

/** Figures compared down a column: right-aligned, one digit width. */
export function isNumeric(key: string): boolean {
  if (key.endsWith("_ms") || key.endsWith("_count") || key.endsWith("_tokens") || key.endsWith("_seconds")) return true;
  return NUMERIC_KEYS.has(key);
}

// Columns that report a condition. Everything else gets no state treatment at all.
const STATE_KEYS = new Set([
  "status", "health", "routing_state", "routing_eligibility", "quota_confidence", "live_access",
  "severity", "attempt_status_snapshot", "enforcement", "condition_kind",
  "routable_credentials", "healthy_credentials", "routable_members", "active_keys",
]);

const NUMERIC_KEYS = new Set([
  "requests", "succeeded", "failed", "cancelled", "attempts", "priority", "weight", "used",
  "limit_amount", "quota_used", "quota_limit", "quota_threshold", "balance_amount",
  "context_window", "estimated_cost", "max_concurrency", "schema_version",
  "requests_per_minute", "tokens_per_minute", "spending_limit", "usage_records",
  "priced_records", "latency_ms", "retries", "fallbacks",
]);

export function humanizeEvent(value: unknown): string {
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

export function columnLabel(column: string): string {
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

// Where a figure came from. The gateway can measure cumulative spend, because the relay
// answers /v1/dashboard/billing/usage. It cannot measure a balance: the same endpoint
// family reports an identical placeholder ceiling for every credential and rejects API
// keys on the account endpoint, so a balance is an operator's reading of a dashboard and
// nothing refreshes it. Saying so is what stops a stale balance being read as a live one.
const OBSERVATION_SOURCES: Record<string, string> = {
  upstream_usage: "Read from the provider by the gateway",
  operator: "Entered by an operator - not refreshed automatically",
  unknown: "Never observed",
};

function observationSource(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Never observed";
  return OBSERVATION_SOURCES[String(value)] ?? readable(value);
}

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

export function formatCurrencyTotals(value: unknown): string {
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
