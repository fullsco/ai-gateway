/**
 * Form data to admin-API request body, one branch per resource.
 *
 * The field names read here are the same ones `resource-fields.tsx` renders; the two
 * files are a pair. Blank is turned into `null` rather than `0` or `""` wherever the
 * API distinguishes "no limit" from "a limit of nothing", and malformed JSON is
 * rejected here with the field name in the message rather than reaching the server.
 */

import { Row } from "./gateway-format";
import { servedProtocols } from "./gateway-protocols";
import { ResourceView } from "./gateway-resources";

export function buildPayload(view: ResourceView, data: FormData, row?: Row) {
  const value = (name: string) => String(data.get(name) ?? "").trim();
  const list = (name: string) =>
    value(name)
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  const optionalNumber = (name: string) => (value(name) ? Number(value(name)) : null);
  // Checkbox groups submit one entry per ticked box and nothing at all when none are,
  // so `get` would read only the first. An empty result is a real answer here.
  const checked = (name: string) => data.getAll(name).map((entry) => String(entry).trim()).filter(Boolean);
  const bool = (name: string) => value(name) === "true";
  const json = (name: string) => {
    try {
      return value(name) ? JSON.parse(value(name)) : {};
    } catch {
      throw new Error(`${name} must be valid JSON.`);
    }
  };
  if (row?.__rotate) return { secret: value("secret") };
  // observed_at is deliberately not sent. The server stamps it, because a recorded
  // figure going stale unnoticed was the original problem and a client-supplied
  // timestamp can claim to be fresher than it is.
  if (row?.__balance) return { amount: Number(value("amount")), currency: value("currency").toUpperCase() || "USD" };
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
      serves_protocols: servedProtocols(value("protocol"), checked("serves_protocols")),
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
