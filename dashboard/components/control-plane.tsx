"use client";

import {
  Activity, Cable, Clipboard, Coins, KeyRound, Menu, Network,
  Pencil, Plus, RefreshCw, Route, ScrollText, Settings2, ShieldCheck,
  Trash2, Users, X,
} from "lucide-react";
import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";

type Row = Record<string, unknown>;
type View = "overview" | "providers" | "credentials" | "clients" | "models" |
  "provider-models" | "routing" | "routing-policies" | "requests" | "health" |
  "usage" | "audit" | "configuration";
type ResourceView = Exclude<View, "overview">;
type Notice = { kind: "success" | "error"; message: string } | null;

const navigation: [View, typeof Activity, string][] = [
  ["overview", Activity, "Overview"], ["providers", Cable, "Providers"],
  ["credentials", KeyRound, "Credentials"], ["clients", Users, "Clients"],
  ["models", Network, "Models"], ["provider-models", Network, "Mappings"],
  ["routing", Route, "Routes"], ["routing-policies", Settings2, "Policies"],
  ["requests", ScrollText, "Requests"], ["health", ShieldCheck, "Health"],
  ["usage", Coins, "Usage"], ["audit", Activity, "Audit"],
  ["configuration", Settings2, "Configuration"],
];

const resources: Record<ResourceView, { endpoint: string; title: string; mutable: boolean; columns: string[] }> = {
  providers: { endpoint: "providers", title: "Providers", mutable: true, columns: ["name", "enabled", "health", "priority", "credential_count"] },
  credentials: { endpoint: "credentials", title: "Credentials", mutable: true, columns: ["name", "provider_name", "masked_hint", "enabled", "health", "priority"] },
  clients: { endpoint: "clients", title: "Gateway clients", mutable: true, columns: ["name", "allowed_protocols", "allowed_models", "enabled", "active_keys"] },
  models: { endpoint: "models", title: "Canonical models", mutable: true, columns: ["id", "display_name", "aliases", "capabilities", "enabled", "provider_route_count"] },
  "provider-models": { endpoint: "provider-models", title: "Provider mappings", mutable: true, columns: ["model_id", "provider_name", "upstream_model_id", "protocol", "enabled", "priority", "max_concurrency"] },
  routing: { endpoint: "routes", title: "Routes", mutable: true, columns: ["model_id", "provider_name", "upstream_model_id", "priority", "enabled", "policy_name"] },
  "routing-policies": { endpoint: "routing-policies", title: "Routing policies", mutable: true, columns: ["name", "enabled", "policy", "updated_at"] },
  requests: { endpoint: "requests", title: "Requests", mutable: false, columns: ["id", "protocol", "requested_model", "status", "latency_ms", "retry_count", "fallback_count"] },
  health: { endpoint: "health", title: "Health checks", mutable: false, columns: ["provider_id", "credential_id", "status", "latency_ms", "error_category", "checked_at"] },
  usage: { endpoint: "usage", title: "Usage", mutable: false, columns: ["request_id", "input_tokens", "output_tokens", "cached_tokens", "estimated_cost", "recorded_at"] },
  audit: { endpoint: "audit", title: "Audit trail", mutable: false, columns: ["action", "resource_type", "resource_id", "actor_id", "created_at"] },
  configuration: { endpoint: "config/versions", title: "Configuration versions", mutable: false, columns: ["id", "status", "schema_version", "checksum", "created_at", "published_at"] },
};

async function api(path: string, init?: RequestInit) {
  const response = await fetch(`/api/gateway/${path}`, {
    ...init, cache: "no-store", headers: { "content-type": "application/json", ...init?.headers },
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) { window.location.assign("/login"); throw new Error("Session expired"); }
  if (!response.ok) {
    const detail = Array.isArray(data.details) ? data.details.map((item: Row) => `${item.location}: ${item.message}`).join(". ") : "";
    const validation = Array.isArray(data.detail) ? data.detail.map((item: Row) => `${(item.loc as unknown[]).slice(-1)[0]}: ${item.msg}`).join(". ") : "";
    throw new Error(detail || validation || data.error || "Control-plane request failed");
  }
  return data;
}

function display(value: unknown, key: string) {
  if (value === null || value === undefined || value === "") return "--";
  if (typeof value === "boolean") return value ? "Enabled" : "Disabled";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "Any";
  if (typeof value === "object") return JSON.stringify(value);
  if (key.endsWith("_at")) return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(String(value)));
  if (key.includes("cost")) return `$${Number(value).toFixed(4)}`;
  if (key.includes("latency") && value !== "--") return `${Number(value).toFixed(0)} ms`;
  return String(value);
}

function DataTable({ rows, columns, actions }: { rows: Row[]; columns: string[]; actions?: (row: Row) => ReactNode }) {
  if (!rows.length) return <div className="empty-state"><strong>No records yet</strong><span>Use Add record to create the first entry when this resource is configurable.</span></div>;
  return <div className="table-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column.replaceAll("_", " ")}</th>)}{actions && <th className="action-heading">Actions</th>}</tr></thead><tbody>{rows.map((row, index) => <tr key={String(row.id ?? index)}>{columns.map((column) => <td key={column} data-label={column.replaceAll("_", " ")}><span className={column === "status" || column === "health" || column === "enabled" ? `value status-value ${String(row[column])}` : "value"}>{display(row[column], column)}</span></td>)}{actions && <td data-label="Actions" className="row-actions">{actions(row)}</td>}</tr>)}</tbody></table></div>;
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
      if (target === "overview") setOverview(result); else setRows(result.data);
      setStale(false); setUpdatedAt(new Date());
    } catch (reason) {
      if (request !== sequence.current) return;
      setStale(true); setNotice({ kind: "error", message: reason instanceof Error ? reason.message : "Unable to load control plane" });
    } finally { if (request === sequence.current) setLoading(false); }
  }

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("view") as View | null;
    if (requested === "overview" || (requested && requested in resources)) setView(requested);
    const onPopState = () => {
      const requested = new URLSearchParams(window.location.search).get("view") as View | null;
      setView(
        requested === "overview" || (requested && requested in resources)
          ? requested
          : "overview",
      );
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  useEffect(() => { setNotice(null); void load(view); }, [view]);
  const metrics: [string, string][] = [
    ["Requests today", Number(overview.requests_today ?? 0).toLocaleString()],
    ["Successful", Number(overview.successful ?? 0).toLocaleString()],
    ["Failed", Number(overview.failed ?? 0).toLocaleString()],
    ["Active providers", String(overview.active_providers ?? 0)],
    ["Fallback rate", `${(Number(overview.fallback_rate ?? 0) * 100).toFixed(1)}%`],
    ["Month cost", `$${Number(overview.estimated_cost ?? 0).toFixed(4)}`],
  ];
  return <main className="shell"><a className="skip-link" href="#workspace">Skip to content</a><aside className={navOpen ? "nav-open" : ""}><div className="rail-head"><div className="mark">AG</div><button className="nav-close" onClick={() => setNavOpen(false)} aria-label="Close navigation"><X size={20} /></button></div><nav aria-label="Control plane">{navigation.map(([id, Icon, label]) => <button aria-current={view === id ? "page" : undefined} className={view === id ? "active" : ""} key={id} onClick={() => { setView(id); window.history.pushState(null, "", `/?view=${id}`); setNavOpen(false); }} title={label}><Icon size={17} aria-hidden="true" /><span>{label}</span></button>)}</nav><div className="operator"><span className="lamp" />Authenticated operator</div></aside><section className="workspace" id="workspace"><header><button className="menu-button" onClick={() => setNavOpen(true)} aria-label="Open navigation"><Menu size={20} /></button><div className="heading"><h1>{navigation.find(([id]) => id === view)?.[2]}</h1><p>{stale ? "Showing last loaded data" : updatedAt ? `Updated ${updatedAt.toLocaleTimeString()}` : "Gateway configuration and operational state"}</p></div><button className="refresh" disabled={loading} onClick={() => void load()}><RefreshCw size={15} className={loading ? "spin" : ""} />{loading ? "Refreshing" : "Refresh"}</button></header>{notice && <div className={`notice ${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}><span>{notice.message}</span><button onClick={() => setNotice(null)} aria-label="Dismiss notification"><X size={16} /></button></div>}{view === "overview" ? <Overview runtime={overview} metrics={metrics} loading={loading} /> : <Resource view={view} rows={rows} loading={loading} reload={() => load(view, true)} notify={setNotice} />}</section></main>;
}

function Overview({ runtime, metrics, loading }: { runtime: Row; metrics: [string, string][]; loading: boolean }) {
  if (loading && !Object.keys(runtime).length) return <div className="loading-state" role="status">Loading runtime status...</div>;
  return <><div className="strip">{metrics.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div><section className="ledger"><div className="section-head"><h2>Runtime exposure</h2><span className={runtime.runtime_ready ? "status ok" : "status"}>{runtime.runtime_ready ? "Ready" : "Not ready"}</span></div><div className="exposure"><div><span>Loaded config</span><strong>{String(runtime.config_version ?? "None")}</strong></div><div><span>Healthy providers</span><strong>{String(runtime.healthy_providers ?? 0)}</strong></div><div><span>Active credentials</span><strong>{String(runtime.active_keys ?? 0)}</strong></div><div><span>Keys cooling down</span><strong>{String(runtime.keys_in_cooldown ?? 0)}</strong></div></div></section><section className="ledger"><div className="section-head"><h2>Operator sequence</h2></div><div className="stations"><span>Observe</span><span>Diagnose</span><span>Configure</span><span>Verify</span><span>Publish</span></div></section></>;
}

function Resource({ view, rows, loading, reload, notify }: { view: ResourceView; rows: Row[]; loading: boolean; reload: () => Promise<void>; notify: (notice: Notice) => void }) {
  const config = resources[view];
  const [editor, setEditor] = useState<{ row?: Row } | null>(null);
  const [confirm, setConfirm] = useState<{ row: Row; action: "delete" | "rollback" } | null>(null);
  const [busy, setBusy] = useState("");
  const [issued, setIssued] = useState<{ key: string; prefix: string; client: string } | null>(null);
  const [keys, setKeys] = useState<{ client: Row; rows: Row[] } | null>(null);

  async function mutate(label: string, operation: () => Promise<unknown>) {
    if (busy) return false;
    setBusy(label); notify(null);
    try { await operation(); await reload(); notify({ kind: "success", message: `${label} completed.` }); }
    catch (reason) { notify({ kind: "error", message: reason instanceof Error ? reason.message : `${label} failed.` }); return false; }
    finally { setBusy(""); }
    return true;
  }
  async function remove() {
    if (!confirm) return;
    const endpoint = view === "routing" ? "routes" : config.endpoint;
    const completed = await mutate("Delete", () => api(`${endpoint}/${String(confirm.row.id)}`, { method: "DELETE" }));
    if (completed) setConfirm(null);
  }
  async function issueKey(row: Row) {
    await mutate("Key issue", async () => {
      const result = await api(`clients/${row.id}/keys`, { method: "POST" });
      setIssued({ key: result.key, prefix: result.key_prefix, client: String(row.name) });
    });
  }
  async function showKeys(row: Row) {
    setBusy("Load keys");
    try { setKeys({ client: row, rows: (await api(`clients/${row.id}/keys`)).data }); }
    catch (reason) { notify({ kind: "error", message: reason instanceof Error ? reason.message : "Unable to load keys" }); }
    finally { setBusy(""); }
  }
  async function publish() {
    if (!window.confirm("Publish the current working configuration and refresh the gateway runtime?")) return;
    await mutate("Configuration publish", () => api("config/publish", { method: "POST" }));
  }
  async function rollback() {
    if (!confirm) return;
    const completed = await mutate("Configuration rollback", () => api(`config/versions/${confirm.row.id}/rollback`, { method: "POST" }));
    if (completed) setConfirm(null);
  }
  const actions = (row: Row) => <div className="action-group">{config.mutable && <><button className="icon-button" title="Edit" aria-label={`Edit ${String(row.name ?? row.id)}`} onClick={() => setEditor({ row })}><Pencil size={15} /></button><button className="icon-button danger" title="Delete" aria-label={`Delete ${String(row.name ?? row.id)}`} onClick={() => setConfirm({ row, action: "delete" })}><Trash2 size={15} /></button></>}{view === "credentials" && <button onClick={() => setEditor({ row: { ...row, __rotate: true } })}>Rotate</button>}{view === "clients" && <><button onClick={() => void issueKey(row)} disabled={!!busy}>Issue key</button><button onClick={() => void showKeys(row)}>Keys</button></>}{view === "configuration" && row.status !== "published" && <button onClick={() => setConfirm({ row, action: "rollback" })}>Rollback</button>}{view === "configuration" && <span className="immutable-label">Snapshot immutable</span>}</div>;
  return <section className="ledger"><div className="section-head"><div><h2>{config.title}</h2><p>{config.mutable ? "Working configuration. Publish to activate runtime changes." : view === "configuration" ? "Published snapshots are immutable; rollback changes only active version." : "Operational records are read-only."}</p></div><div className="section-actions">{config.mutable && <button onClick={() => setEditor({})}><Plus size={15} />Add record</button>}{view === "configuration" && <button className="primary" disabled={!!busy} onClick={() => void publish()}>{busy ? "Publishing..." : "Publish current"}</button>}</div></div>{loading ? <div className="loading-state" role="status">Loading {config.title.toLowerCase()}...</div> : <DataTable rows={rows} columns={config.columns} actions={actions} />}{editor && <ResourceEditor view={view} row={editor.row} onClose={() => setEditor(null)} onSaved={async (message) => { setEditor(null); await reload(); notify({ kind: "success", message }); }} />}{confirm && <ConfirmDialog title={confirm.action === "delete" ? "Delete record" : "Rollback configuration"} message={confirm.action === "delete" ? `Delete ${String(confirm.row.name ?? confirm.row.id)}? Related records may also be removed.` : `Make version ${String(confirm.row.id)} the active configuration?`} busy={!!busy} onCancel={() => setConfirm(null)} onConfirm={() => void (confirm.action === "delete" ? remove() : rollback())} />}{issued && <OneTimeKey value={issued} onClose={() => setIssued(null)} />}{keys && <KeyManager value={keys} busy={busy} onClose={() => setKeys(null)} onRevoke={async (key) => { if (!window.confirm(`Revoke key ${String(key.key_prefix)}? This cannot be undone.`)) return; await mutate("Key revocation", () => api(`client-keys/${key.id}/revoke`, { method: "POST" })); const refreshed = await api(`clients/${keys.client.id}/keys`); setKeys({ ...keys, rows: refreshed.data }); }} />}</section>;
}

function ResourceEditor({ view, row, onClose, onSaved }: { view: ResourceView; row?: Row; onClose: () => void; onSaved: (message: string) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [references, setReferences] = useState<Record<string, Row[]>>({});
  const editing = !!row?.id && !row.__rotate;
  useEffect(() => { Promise.all([api("providers"), api("models"), api("provider-models"), api("routing-policies")]).then(([providers, models, mappings, policies]) => setReferences({ providers: providers.data, models: models.data, mappings: mappings.data, policies: policies.data })).catch(() => undefined); }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const form = event.currentTarget; const data = new FormData(form);
    try {
      const body = buildPayload(view, data, row);
      const endpoint = view === "routing" ? "routes" : resources[view].endpoint;
      const path = row?.__rotate ? `credentials/${row.id}/rotate` : editing ? `${endpoint}/${row?.id}` : endpoint;
      await api(path, { method: row?.__rotate ? "POST" : editing ? "PUT" : "POST", body: JSON.stringify(body) });
      await onSaved(row?.__rotate ? "Credential rotated securely." : editing ? "Changes saved." : "Record created.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Save failed"); setBusy(false); }
  }
  return <div className="dialog-backdrop"><section className="editor" role="dialog" aria-modal="true" aria-labelledby="editor-title"><div className="editor-head"><div><h2 id="editor-title">{row?.__rotate ? "Rotate credential" : editing ? `Edit ${resources[view].title}` : `Add ${resources[view].title}`}</h2><p>{row?.__rotate ? "The replacement secret is encrypted before persistence and is never returned." : "Changes affect the working configuration until published."}</p></div><button className="icon-button" onClick={onClose} aria-label="Close editor"><X size={18} /></button></div><form onSubmit={submit}>{row?.__rotate ? <Field name="secret" label="Replacement secret" type="password" required /> : <Fields view={view} row={row} references={references} />}{error && <div className="form-error" role="alert">{error}</div>}<div className="form-actions"><button type="button" onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>{busy ? "Saving..." : editing ? "Save changes" : "Create record"}</button></div></form></section></div>;
}

function Field({ name, label, defaultValue, type = "text", required = false, min, step, readOnly = false }: { name: string; label: string; defaultValue?: unknown; type?: string; required?: boolean; min?: number; step?: string; readOnly?: boolean }) {
  return <label className="field"><span>{label}</span><input name={name} type={type} defaultValue={String(defaultValue ?? "")} required={required} min={min} step={step} readOnly={readOnly} /></label>;
}

function SelectField({ name, label, value, options }: { name: string; label: string; value?: unknown; options: [string, string][] }) {
  return <label className="field"><span>{label}</span><select name={name} defaultValue={String(value ?? options[0]?.[0] ?? "")}>{options.map(([id, text]) => <option key={id} value={id}>{text}</option>)}</select></label>;
}

function Fields({ view, row = {}, references }: { view: ResourceView; row?: Row; references: Record<string, Row[]> }) {
  const protocols: [string, string][] = [["anthropic_messages", "Anthropic Messages"], ["openai_chat_completions", "OpenAI Chat Completions"], ["openai_responses", "OpenAI Responses"]];
  const enabled = <SelectField name="enabled" label="Status" value={String(row.enabled ?? true)} options={[["true", "Enabled"], ["false", "Disabled"]]} />;
  if (view === "providers") return <><Field name="name" label="Provider name" defaultValue={row.name} required /><Field name="base_url" label="Base URL" type="url" defaultValue={row.base_url} required /><Field name="capabilities" label="Shared capabilities (comma separated)" defaultValue={(row.capabilities as string[] ?? []).join(", ")} /><Field name="priority" label="Priority" type="number" defaultValue={row.priority ?? 100} min={0} /> <Field name="timeout_seconds" label="Timeout seconds" type="number" defaultValue={row.timeout_seconds ?? 600} min={1} />{enabled}</>;
  if (view === "credentials") return <><SelectField name="provider_id" label="Provider" value={row.provider_id} options={(references.providers ?? []).map((item) => [String(item.id), String(item.name)])} /><Field name="name" label="Credential label" defaultValue={row.name} required />{!row.id && <Field name="secret" label="Provider secret" type="password" required />}<Field name="priority" label="Priority" type="number" defaultValue={row.priority ?? 100} min={0} /><Field name="quota_limit" label="Quota limit" type="number" defaultValue={row.quota_limit} min={0} step="0.01" /><Field name="quota_threshold" label="Quota threshold" type="number" defaultValue={row.quota_threshold ?? 0.95} min={0} step="0.01" /><Field name="requests_per_minute" label="Requests per minute" type="number" defaultValue={row.requests_per_minute} min={1} /><Field name="tokens_per_minute" label="Tokens per minute" type="number" defaultValue={row.tokens_per_minute} min={1} />{row.id && enabled}</>;
  if (view === "clients") return <><Field name="name" label="Client name" defaultValue={row.name} required /><Field name="allowed_protocols" label="Allowed protocols (comma separated)" defaultValue={(row.allowed_protocols as string[] ?? []).join(", ")} required /><Field name="allowed_models" label="Allowed models (blank means any)" defaultValue={(row.allowed_models as string[] ?? []).join(", ")} /><Field name="requests_per_minute" label="Requests per minute" type="number" defaultValue={row.requests_per_minute} min={1} /><Field name="tokens_per_minute" label="Tokens per minute" type="number" defaultValue={row.tokens_per_minute} min={1} /><Field name="spending_limit" label="Spending limit" type="number" defaultValue={row.spending_limit} min={0} step="0.01" />{enabled}</>;
  if (view === "models") return <><Field name="id" label="Canonical model ID" defaultValue={row.id} required readOnly={!!row.id} /><Field name="display_name" label="Display name" defaultValue={row.display_name} required /><Field name="aliases" label="Aliases (comma separated)" defaultValue={(row.aliases as string[] ?? []).join(", ")} /><Field name="capabilities" label="Capabilities (comma separated)" defaultValue={(row.capabilities as string[] ?? []).join(", ")} /><Field name="context_window" label="Context window" type="number" defaultValue={row.context_window} min={1} />{enabled}</>;
  if (view === "provider-models") return <><SelectField name="provider_id" label="Provider" value={row.provider_id} options={(references.providers ?? []).map((item) => [String(item.id), String(item.name)])} /><SelectField name="model_id" label="Canonical model" value={row.model_id} options={(references.models ?? []).map((item) => [String(item.id), String(item.display_name ?? item.id)])} /><Field name="upstream_model_id" label="Upstream model ID" defaultValue={row.upstream_model_id} required /><SelectField name="protocol" label="Protocol" value={row.protocol} options={protocols} /><Field name="capabilities" label="Capabilities (comma separated)" defaultValue={(row.capabilities as string[] ?? []).join(", ")} /><Field name="priority" label="Priority" type="number" defaultValue={row.priority ?? 100} min={0} /><Field name="weight" label="Weight" type="number" defaultValue={row.weight ?? 1} min={0} step="0.1" /><Field name="max_concurrency" label="Maximum concurrency" type="number" defaultValue={row.max_concurrency ?? 8} min={1} />{enabled}</>;
  if (view === "routing") return <><SelectField name="model_id" label="Canonical model" value={row.model_id} options={(references.models ?? []).map((item) => [String(item.id), String(item.display_name ?? item.id)])} /><SelectField name="provider_model_id" label="Provider mapping" value={row.provider_model_id} options={(references.mappings ?? []).map((item) => [String(item.id), `${String(item.provider_name)} / ${String(item.upstream_model_id)}`])} /><SelectField name="policy_id" label="Routing policy" value={row.policy_id ?? ""} options={[["", "No policy"], ...(references.policies ?? []).map((item) => [String(item.id), String(item.name)] as [string, string])]} /><Field name="priority" label="Priority" type="number" defaultValue={row.priority ?? 100} min={0} /><SelectField name="allow_model_fallback" label="Model fallback" value={String(row.allow_model_fallback ?? false)} options={[["false", "Disabled"], ["true", "Allowed"]]} />{enabled}</>;
  const policy = row.policy as Row | undefined;
  return <><Field name="name" label="Policy name" defaultValue={row.name} required /><Field name="health_weight" label="Health weight" type="number" defaultValue={policy?.health_weight ?? 3} min={0} step="0.1" /><Field name="quota_weight" label="Quota weight" type="number" defaultValue={policy?.quota_weight ?? 2} min={0} step="0.1" /><Field name="rate_limit_weight" label="Rate-limit weight" type="number" defaultValue={policy?.rate_limit_weight ?? 2} min={0} step="0.1" /><Field name="concurrency_weight" label="Concurrency weight" type="number" defaultValue={policy?.concurrency_weight ?? 1} min={0} step="0.1" /><Field name="latency_weight" label="Latency weight" type="number" defaultValue={policy?.latency_weight ?? 1} min={0} step="0.1" /><Field name="failure_weight" label="Failure penalty" type="number" defaultValue={policy?.failure_weight ?? 2} min={0} step="0.1" />{enabled}</>;
}

function buildPayload(view: ResourceView, data: FormData, row?: Row) {
  const value = (name: string) => String(data.get(name) ?? "").trim(); const list = (name: string) => value(name).split(",").map((item) => item.trim()).filter(Boolean); const optionalNumber = (name: string) => value(name) ? Number(value(name)) : null; const bool = (name: string) => value(name) === "true";
  if (row?.__rotate) return { secret: value("secret") };
  if (view === "providers") return { name: value("name"), base_url: value("base_url"), capabilities: list("capabilities"), priority: Number(value("priority")), timeout_seconds: Number(value("timeout_seconds")), enabled: bool("enabled") };
  if (view === "credentials") return row?.id ? { name: value("name"), priority: Number(value("priority")), quota_limit: optionalNumber("quota_limit"), quota_threshold: Number(value("quota_threshold")), requests_per_minute: optionalNumber("requests_per_minute"), tokens_per_minute: optionalNumber("tokens_per_minute"), enabled: bool("enabled") } : { provider_id: value("provider_id"), name: value("name"), secret: value("secret"), priority: Number(value("priority")), quota_limit: optionalNumber("quota_limit"), quota_threshold: Number(value("quota_threshold")) };
  if (view === "clients") return { name: value("name"), allowed_protocols: list("allowed_protocols"), allowed_models: list("allowed_models"), requests_per_minute: optionalNumber("requests_per_minute"), tokens_per_minute: optionalNumber("tokens_per_minute"), spending_limit: optionalNumber("spending_limit"), enabled: bool("enabled") };
  if (view === "models") return { id: value("id"), display_name: value("display_name"), aliases: list("aliases"), capabilities: list("capabilities"), context_window: optionalNumber("context_window"), enabled: bool("enabled") };
  if (view === "provider-models") return { provider_id: value("provider_id"), model_id: value("model_id"), upstream_model_id: value("upstream_model_id"), protocol: value("protocol"), capabilities: list("capabilities"), priority: Number(value("priority")), weight: Number(value("weight")), max_concurrency: Number(value("max_concurrency")), enabled: bool("enabled") };
  if (view === "routing") return { model_id: value("model_id"), provider_model_id: value("provider_model_id"), policy_id: value("policy_id") || null, priority: Number(value("priority")), allow_model_fallback: bool("allow_model_fallback"), enabled: bool("enabled") };
  return { name: value("name"), enabled: bool("enabled"), policy: { health_weight: Number(value("health_weight")), quota_weight: Number(value("quota_weight")), rate_limit_weight: Number(value("rate_limit_weight")), concurrency_weight: Number(value("concurrency_weight")), latency_weight: Number(value("latency_weight")), failure_weight: Number(value("failure_weight")) } };
}

function ConfirmDialog({ title, message, busy, onCancel, onConfirm }: { title: string; message: string; busy: boolean; onCancel: () => void; onConfirm: () => void }) { return <div className="dialog-backdrop"><section className="confirm-dialog" role="alertdialog" aria-modal="true"><h2>{title}</h2><p>{message}</p><div className="form-actions"><button onClick={onCancel}>Cancel</button><button className="danger-button" disabled={busy} onClick={onConfirm}>{busy ? "Working..." : "Confirm"}</button></div></section></div>; }
function OneTimeKey({ value, onClose }: { value: { key: string; prefix: string; client: string }; onClose: () => void }) { const [copied, setCopied] = useState(false); return <div className="dialog-backdrop"><section className="secret-dialog" role="dialog" aria-modal="true"><KeyRound size={22} /><h2>Gateway key issued</h2><p>This plaintext is shown once. Store it before dismissing this window.</p><dl><div><dt>Client</dt><dd>{value.client}</dd></div><div><dt>Prefix</dt><dd>{value.prefix}</dd></div></dl><code>{value.key}</code><div className="form-actions"><button onClick={async () => { await navigator.clipboard.writeText(value.key); setCopied(true); }}><Clipboard size={15} />{copied ? "Copied" : "Copy key"}</button><button className="primary" onClick={onClose}>I stored this key</button></div></section></div>; }
function KeyManager({ value, busy, onClose, onRevoke }: { value: { client: Row; rows: Row[] }; busy: string; onClose: () => void; onRevoke: (row: Row) => Promise<void> }) { return <div className="dialog-backdrop"><section className="editor key-manager" role="dialog" aria-modal="true"><div className="editor-head"><div><h2>{String(value.client.name)} keys</h2><p>Keys are immutable. Revoke access permanently when a key is no longer trusted.</p></div><button className="icon-button" onClick={onClose}><X size={18} /></button></div><DataTable rows={value.rows} columns={["key_prefix", "enabled", "last_used_at", "created_at", "revoked_at"]} actions={(row) => row.enabled ? <button className="danger-text" disabled={!!busy} onClick={() => void onRevoke(row)}>Revoke</button> : <span>Revoked</span>} /></section></div>; }
