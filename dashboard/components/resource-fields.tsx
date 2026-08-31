"use client";

/**
 * The form controls behind the generic record editor.
 *
 * `Fields` is one branch per resource rather than a schema-driven renderer, because
 * every branch differs in ways a schema would have to encode anyway: which fields
 * appear only when creating, which are read-only once an id exists, and what a sensible
 * default is. The field names are the wire contract with `buildPayload`.
 */

import { Row } from "./gateway-format";
import { PROTOCOLS, servedProtocols } from "./gateway-protocols";
import { ResourceView } from "./gateway-resources";

export function Field({ name, label, defaultValue, type = "text", required = false, min, max, step, readOnly = false }: { name: string; label: string; defaultValue?: unknown; type?: string; required?: boolean; min?: number; max?: number; step?: string; readOnly?: boolean }) {
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

/**
 * A group of checkboxes sharing one field name, read back with `FormData.getAll`.
 *
 * Uncontrolled like every other field here, which means it cannot react to a change in
 * the `protocol` select beside it. It does not need to: the server always adds the
 * upstream protocol back to the served set, so the invariant holds without live JS.
 */
function CheckboxGroup({ name, label, values, options, hint }: { name: string; label: string; values: string[]; options: [string, string][]; hint?: string }) {
  return (
    <fieldset className="field checkbox-group">
      <legend>{label}</legend>
      <div className="capability-grid">
        {options.map(([id, text]) => (
          <label key={id}>
            <input type="checkbox" name={name} value={id} defaultChecked={values.includes(id)} />
            {text}
          </label>
        ))}
      </div>
      {hint && <small>{hint}</small>}
    </fieldset>
  );
}

export function Fields({ view, row = {}, references }: { view: ResourceView; row?: Row; references: Record<string, Row[]> }) {
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
        <SelectField name="protocol" label="How requests are sent" value={row.protocol} options={PROTOCOLS} />
        <CheckboxGroup
          name="serves_protocols"
          label="Client APIs answered"
          values={servedProtocols(String(row.protocol ?? ""), ((row.serves_protocols as string[]) ?? []).map(String))}
          options={PROTOCOLS}
          hint="The protocol above is always answered natively; the rest are translated. A client calling an unchecked API gets a 404 for this model even when the route is healthy."
        />
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
