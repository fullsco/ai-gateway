"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { Plus, Trash2, X } from "lucide-react";

export type GatewayRow = Record<string, unknown>;

function readable(value: unknown): string {
  if (typeof value === "string") return value.replaceAll("_", " ");
  if (value && typeof value === "object" && "message" in value) return String(value.message);
  try { return JSON.stringify(value); } catch { return "Request failed"; }
}

// What each reconcile guard reason means, and what to do about it. These are refusals
// to overwrite something deliberate, so each one names the thing that is in the way.
const RECONCILE_REASONS: Record<string, string> = {
  selective_credential_access:
    "some credentials on this provider are restricted to particular models, and saving here would give every credential access to every model. Clear the per-credential model restrictions first, or edit this provider outside the setup form.",
  selective_pool_membership:
    "this provider's pool has hand-picked members, and saving here would rebuild it with every credential. Clear the pool membership first, or edit outside the setup form.",
  custom_route_pool:
    "a route for this provider uses a pool this form does not manage. Point the route at the provider's own pool, or edit outside the setup form.",
  custom_pool_configuration:
    "this provider's pool carries settings this form does not manage, such as being bound to a single model. Reset those settings, or edit outside the setup form.",
  member_operational_state:
    "a pool member is disabled or draining, and saving here would re-enable it. Finish or undo the drain first.",
};

// A shared model's metadata belongs to the catalogue, not to one provider, so the
// refusal names the field and both values rather than leaving the operator to guess.
function sharedModelConflict(data: GatewayRow): string {
  const field = String(data.field ?? "").replaceAll("_", " ");
  const shared = Array.isArray(data.shared_with) ? data.shared_with.join(", ") : "";
  const show = (value: unknown) =>
    value === null || value === undefined || value === ""
      ? "empty"
      : Array.isArray(value) ? value.join(", ") : String(value);
  const where = shared ? ` It is also served by ${shared}.` : "";
  return (
    `"${data.model_id}" already exists in the shared model catalogue and its ${field} ` +
    `does not match: it is currently ${show(data.current)} and this form would set it ` +
    `to ${show(data.requested)}.${where} Either keep the existing value, or change it ` +
    `for every provider from the Models and routing page.`
  );
}

export async function gatewayApi(path: string, init?: RequestInit) {
  const response = await fetch(`/api/gateway/${path}`, {
    ...init,
    cache: "no-store",
    headers: { "content-type": "application/json", ...init?.headers },
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Session expired");
  }
  if (!response.ok) {
    const details = Array.isArray(data.details) ? data.details.map((item: GatewayRow) => `${item.location}: ${item.message}`).join(". ") : "";
    const validation = Array.isArray(data.detail) ? data.detail.map((item: GatewayRow) => `${(item.loc as unknown[]).slice(-1)[0]}: ${item.msg}`).join(". ") : "";
    // FastAPI returns a plain string detail for routing failures, most often
    // {"detail":"Not Found"}. That matched none of the shapes above, so a real cause
    // was replaced by the generic fallback and the operator was told nothing at all.
    const plain = typeof data.detail === "string" ? data.detail : "";
    const status = response.status === 404 ? `${plain || "Not found"} (${path})` : plain;
    // Several guards answer with an error code plus a "reason" naming which condition
    // fired. Dropping the reason turned an actionable refusal into a dead end:
    // "provider topology not supported" says nothing about what to change, and the
    // reason says exactly which part of the existing setup is in the way.
    const reason = typeof data.reason === "string" ? RECONCILE_REASONS[data.reason] ?? data.reason.replaceAll("_", " ") : "";
    const primary = readable(data.error);
    const explained = primary && reason ? `${primary}: ${reason}` : primary || reason;
    if (data.error === "shared_model_metadata_conflict") {
      throw new Error(sharedModelConflict(data));
    }
    throw new Error(details || validation || explained || status || `Request failed with status ${response.status}`);
  }
  return data;
}

type CredentialDraft = {
  existing: boolean; name: string; secret: string; rotate_secret: boolean; enabled: boolean;
  requests_per_minute: string; tokens_per_minute: string; quota_limit: string;
  quota_threshold: string; priority: string;
};
type MappingDraft = {
  model_id: string; display_name: string; aliases: string; context_window: string; model_enabled: boolean; model_capabilities: string[];
  upstream_model_id: string; protocol: string; capabilities: string[]; enabled: boolean;
  max_concurrency: string; priority: string; weight: string; settings_json: string;
  input_price: string; output_price: string; cached_price: string; currency: string;
  pricing_metadata: GatewayRow; route_present: boolean; route_enabled: boolean;
  route_priority: string; allow_model_fallback: boolean;
};

const capabilities = [
  ["streaming", "Streaming"], ["tool_calling", "Tool calling"], ["reasoning", "Reasoning"],
  ["vision", "Vision"], ["structured_output", "Structured output"], ["computer_use", "Computer use"],
] as const;
const protocols = [
  ["anthropic_messages", "Anthropic Messages"],
  ["openai_chat_completions", "OpenAI Chat Completions"],
  ["openai_responses", "OpenAI Responses"],
] as const;

const emptyCredential = (): CredentialDraft => ({
  existing: false, name: "", secret: "", rotate_secret: true, enabled: true,
  requests_per_minute: "", tokens_per_minute: "", quota_limit: "", quota_threshold: "0.95", priority: "100",
});
const emptyMapping = (): MappingDraft => ({
  model_id: "", display_name: "", aliases: "", context_window: "", model_enabled: true, model_capabilities: ["streaming"],
  upstream_model_id: "", protocol: "anthropic_messages", capabilities: ["streaming"], enabled: true,
  max_concurrency: "8", priority: "100", weight: "1", settings_json: "{}", input_price: "", output_price: "",
  cached_price: "", currency: "USD", pricing_metadata: {}, route_present: true, route_enabled: true,
  route_priority: "100", allow_model_fallback: false,
});
const text = (value: unknown, fallback = "") => String(value ?? fallback);
const list = (value: unknown) => Array.isArray(value) ? value.map(String) : [];
const object = (value: unknown): GatewayRow => value && typeof value === "object" && !Array.isArray(value) ? { ...(value as GatewayRow) } : {};

function parsePairs(value: unknown): { name: string; value: string }[] {
  const source = object(value);
  return Object.entries(source).map(([name, entry]) => ({ name, value: typeof entry === "string" ? entry : JSON.stringify(entry) }));
}

function parseParameterValue(value: string): unknown {
  try {
    const parsed = JSON.parse(value);
    return typeof parsed === "object" && parsed !== null ? value : parsed;
  } catch {
    return value;
  }
}

function numberValue(value: string, label: string, options: { optional?: boolean; integer?: boolean; min?: number; max?: number } = {}) {
  if (!value.trim() && options.optional) return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || (options.integer && !Number.isInteger(parsed)) || (options.min !== undefined && parsed < options.min) || (options.max !== undefined && parsed > options.max))
    throw new Error(`${label} must be ${options.integer ? "a whole " : "a valid "}number${options.min !== undefined ? ` of at least ${options.min}` : ""}.`);
  return parsed;
}

function PairEditor({ label, items, onChange, empty }: { label: string; items: { name: string; value: string }[]; onChange: (items: { name: string; value: string }[]) => void; empty: string }) {
  return <div className="pair-editor full"><div className="subsection-head"><div><strong>{label}</strong><p>{items.length ? `${items.length} configured` : empty}</p></div><button type="button" onClick={() => onChange([...items, { name: "", value: "" }])}><Plus size={14} />Add</button></div>{items.map((item, index) => <div className="pair-row" key={index}><label className="field"><span>Name</span><input value={item.name} onChange={(event) => onChange(items.map((entry, position) => position === index ? { ...entry, name: event.target.value } : entry))} /></label><label className="field"><span>Value</span><input value={item.value} onChange={(event) => onChange(items.map((entry, position) => position === index ? { ...entry, value: event.target.value } : entry))} /></label><button type="button" className="icon-button danger" onClick={() => onChange(items.filter((_, position) => position !== index))} aria-label={`Remove ${label.toLowerCase()} row ${index + 1}`}><Trash2 size={15} /></button></div>)}</div>;
}

export default function ProviderSetup({ provider, onClose, onSaved }: { provider?: GatewayRow; onClose: () => void; onSaved: () => Promise<void> }) {
  const editing = Boolean(provider?.id);
  const [name, setName] = useState(text(provider?.name));
  const [baseUrl, setBaseUrl] = useState(text(provider?.base_url));
  const [providerEnabled, setProviderEnabled] = useState(Boolean(provider?.enabled ?? true));
  const [providerPriority, setProviderPriority] = useState(text(provider?.priority, "100"));
  const [timeout, setTimeout] = useState(text(provider?.timeout_seconds, "600"));
  const [providerSettings, setProviderSettings] = useState(JSON.stringify(object(provider?.settings), null, 2));
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [authScheme, setAuthScheme] = useState(text(object(provider?.settings).auth_scheme, "default"));
  const [headers, setHeaders] = useState(() => parsePairs(object(provider?.settings).default_headers));
  const [queryParameters, setQueryParameters] = useState(() => parsePairs(object(provider?.settings).endpoint_query));
  const [credentials, setCredentials] = useState<CredentialDraft[]>([emptyCredential()]);
  const [mappings, setMappings] = useState<MappingDraft[]>([emptyMapping()]);
  // The full canonical catalogue, so an existing model can be chosen instead of
  // retyped. Retyping is how an operator either creates an accidental second model
  // from a typo, or reproduces an existing id exactly and trips the shared-model
  // guard on a field the form itself prefilled.
  const [catalogue, setCatalogue] = useState<GatewayRow[]>([]);
  const [strategy, setStrategy] = useState("priority");
  const [poolEnabled, setPoolEnabled] = useState(true);
  const poolMetadata = useRef({ health_aware: true, quota_aware: true });
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [hydrationFailed, setHydrationFailed] = useState(false);
  const [loading, setLoading] = useState(editing);
  const [error, setError] = useState("");
  const initialFocus = useRef<HTMLInputElement>(null);
  const dialog = useRef<HTMLElement>(null);

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape" && !busyRef.current) onClose(); };
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); previous?.focus(); };
  }, [onClose]);

  useEffect(() => { busyRef.current = busy; }, [busy]);

  useEffect(() => {
    if (!loading) initialFocus.current?.focus();
  }, [loading]);

  // The canonical catalogue is loaded whether or not a provider is being edited.
  // Adding a provider is precisely when an operator needs to attach an existing model,
  // and the provider-scoped effect below returns early with no provider id, so the
  // picker would have been empty in the one flow that most needed it.
  useEffect(() => {
    let active = true;
    gatewayApi("models")
      .then((modelData) => { if (active) setCatalogue((modelData.data as GatewayRow[]) ?? []); })
      .catch(() => undefined);
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!provider?.id) return;
    let active = true;
    Promise.all([gatewayApi("credentials"), gatewayApi("models"), gatewayApi("provider-models"), gatewayApi("routes"), gatewayApi("provider-pools")])
      .then(([credentialData, modelData, mappingData, routeData, poolData]) => {
        if (!active) return;
        const providerCredentials = (credentialData.data as GatewayRow[]).filter((item) => text(item.provider_id) === text(provider.id));
        const modelMap = new Map((modelData.data as GatewayRow[]).map((item) => [text(item.id), item]));
        setCatalogue(modelData.data as GatewayRow[]);
        const providerMappings = (mappingData.data as GatewayRow[]).filter((item) => text(item.provider_id) === text(provider.id));
        const routes = (routeData.data as GatewayRow[]).filter((item) => text(item.provider_id) === text(provider.id));
        const poolIds = new Set(routes.filter((item) => text(item.provider_id) === text(provider.id) && item.pool_id).map((item) => text(item.pool_id)));
        const pool = (poolData.data as GatewayRow[]).find((item) => poolIds.has(text(item.id))) ?? (poolData.data as GatewayRow[]).find((item) => text(item.name) === `${text(provider.name)} Pool`);
          if (pool) {
          setStrategy(text(pool.strategy, "priority"));
          setPoolEnabled(Boolean(pool.enabled));
            const settings = object(pool.settings);
            poolMetadata.current = { health_aware: Boolean(settings.health_aware ?? true), quota_aware: Boolean(settings.quota_aware ?? true) };
        }
        setCredentials(providerCredentials.map((item) => ({
          existing: true, name: text(item.name), secret: "", rotate_secret: false, enabled: Boolean(item.enabled),
          requests_per_minute: text(item.requests_per_minute), tokens_per_minute: text(item.tokens_per_minute),
          quota_limit: text(item.quota_limit), quota_threshold: text(item.quota_threshold, "0.95"), priority: text(item.priority, "100"),
        })));
        setMappings(providerMappings.map((item) => {
          const model = modelMap.get(text(item.model_id)) ?? {};
          const pricing = object(item.pricing);
          const route = routes.find((candidate) => text(candidate.provider_model_id) === text(item.id));
          const { input_per_million, output_per_million, cached_input_per_million, currency, ...pricingMetadata } = pricing;
          return {
            model_id: text(item.model_id), display_name: text(model.display_name, text(item.model_id)), aliases: list(model.aliases).join(", "),
            context_window: text(model.context_window), model_enabled: Boolean(model.enabled ?? true), model_capabilities: list(model.capabilities), upstream_model_id: text(item.upstream_model_id),
            protocol: text(item.protocol), capabilities: list(item.capabilities), enabled: Boolean(item.enabled), max_concurrency: text(item.max_concurrency, "8"),
            priority: text(item.priority, "100"), weight: text(item.weight, "1"), settings_json: JSON.stringify(object(item.settings), null, 2), input_price: text(input_per_million),
            output_price: text(output_per_million), cached_price: text(cached_input_per_million), currency: text(currency, "USD"), pricing_metadata: pricingMetadata,
            route_present: Boolean(route), route_enabled: Boolean(route?.enabled ?? true), route_priority: text(route?.priority, text(item.priority, "100")),
            allow_model_fallback: Boolean(route?.allow_model_fallback),
          };
        }));
      })
      .catch((reason) => {
        if (!active) return;
        setHydrationFailed(true);
        setError(reason instanceof Error ? reason.message : "Unable to load provider configuration");
      })
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [provider]);

  const updateCredential = (index: number, patch: Partial<CredentialDraft>) => setCredentials((items) => items.map((item, position) => position === index ? { ...item, ...patch } : item));
  const updateMapping = (index: number, patch: Partial<MappingDraft>) => setMappings((items) => {
    const sourceModelId = items[index].model_id;
    const canonicalKeys = ["display_name", "aliases", "context_window", "model_enabled", "model_capabilities"] as const;
    const canonicalPatch = Object.fromEntries(
      canonicalKeys.filter((key) => key in patch).map((key) => [key, patch[key]]),
    ) as Partial<MappingDraft>;
    return items.map((item, position) => {
      const shared = item.model_id === sourceModelId ? canonicalPatch : {};
      return position === index ? { ...item, ...shared, ...patch } : { ...item, ...shared };
    });
  });

  // Choosing a model from the catalogue adopts its stored metadata verbatim. The
  // canonical fields then describe what the catalogue already holds, so saving cannot
  // trip the shared-model guard, and the operator is not asked to retype an id that
  // has to match character for character.
  const selectCatalogueModel = (index: number, modelId: string) => {
    const known = catalogue.find((item) => text(item.id) === modelId);
    if (!known) {
      updateMapping(index, { model_id: modelId });
      return;
    }
    updateMapping(index, {
      model_id: modelId,
      display_name: text(known.display_name, modelId),
      aliases: list(known.aliases).join(", "),
      context_window: text(known.context_window),
      model_enabled: Boolean(known.enabled ?? true),
      model_capabilities: list(known.capabilities),
      // The provider's own name for the model is usually the canonical id; it stays
      // editable because relays differ, but prefilling removes the common case.
      upstream_model_id: modelId,
    });
  };

  function validate() {
    const labels = credentials.map((item) => item.name.trim().toLocaleLowerCase());
    if (labels.some((label, index) => labels.indexOf(label) !== index)) throw new Error("Credential labels must be unique.");
    const keys = mappings.map((item) => `${item.model_id.trim()}\u0000${item.upstream_model_id.trim()}\u0000${item.protocol}`.toLocaleLowerCase());
    if (keys.some((key, index) => keys.indexOf(key) !== index)) throw new Error("Model, upstream model, and protocol combinations must be unique.");
    const aliases = Array.from(new Map(mappings.map((item) => [item.model_id.trim(), item.aliases])).values())
      .flatMap((value) => value.split(",").map((alias) => alias.trim().toLocaleLowerCase()).filter(Boolean));
    if (aliases.some((alias, index) => aliases.indexOf(alias) !== index)) throw new Error("Canonical model aliases must be unique.");
    credentials.forEach((item) => {
      numberValue(item.priority, `Priority for ${item.name}`, { integer: true, min: 0 });
      numberValue(item.quota_threshold, `Quota threshold for ${item.name}`, { min: Number.MIN_VALUE, max: 1 });
      numberValue(item.quota_limit, `Quota limit for ${item.name}`, { optional: true, min: 0 });
      numberValue(item.requests_per_minute, `Requests per minute for ${item.name}`, { optional: true, integer: true, min: 1 });
      numberValue(item.tokens_per_minute, `Tokens per minute for ${item.name}`, { optional: true, integer: true, min: 1 });
      if ((!item.existing || item.rotate_secret) && !item.secret) throw new Error(`Enter a secret for credential ${item.name}.`);
    });
    mappings.forEach((item) => {
      numberValue(item.context_window, `Context window for ${item.model_id}`, { optional: true, integer: true, min: 1 });
      numberValue(item.max_concurrency, `Maximum concurrency for ${item.upstream_model_id}`, { integer: true, min: 1 });
      numberValue(item.priority, `Mapping priority for ${item.upstream_model_id}`, { integer: true, min: 0 });
      numberValue(item.weight, `Mapping weight for ${item.upstream_model_id}`, { min: Number.MIN_VALUE });
      numberValue(item.route_priority, `Route priority for ${item.upstream_model_id}`, { integer: true, min: 0 });
      const hasPricing = Boolean(item.input_price.trim() || item.output_price.trim() || item.cached_price.trim());
      if (hasPricing && (!item.input_price.trim() || !item.output_price.trim() || !item.currency.trim())) throw new Error(`Pricing for ${item.upstream_model_id} requires input, output, and currency.`);
      if (hasPricing && !/^[A-Za-z]{3}$/.test(item.currency.trim())) throw new Error(`Pricing currency for ${item.upstream_model_id} must be a three-letter code.`);
      [item.input_price, item.output_price, item.cached_price].filter(Boolean).forEach((value) => numberValue(value, `Pricing for ${item.upstream_model_id}`, { min: 0 }));
      try {
        const settings = JSON.parse(item.settings_json || "{}");
        if (!settings || typeof settings !== "object" || Array.isArray(settings)) throw new Error();
      } catch {
        throw new Error(`Transport settings for ${item.upstream_model_id} must be a JSON object.`);
      }
    });
    numberValue(providerPriority, "Provider priority", { integer: true, min: 0 });
    numberValue(timeout, "Provider timeout", { min: Number.MIN_VALUE });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    try {
      validate();
       const settings = object(providerSettings ? JSON.parse(providerSettings) : {});
       settings.auth_scheme = authScheme;
       settings.default_headers = Object.fromEntries(headers.filter((item) => item.name.trim()).map((item) => [item.name.trim(), item.value]));
       settings.endpoint_query = Object.fromEntries(queryParameters.filter((item) => item.name.trim()).map((item) => [item.name.trim(), parseParameterValue(item.value)]));
      if (!settings || typeof settings !== "object" || Array.isArray(settings)) throw new Error();
      setBusy(true);
      busyRef.current = true;
      const modelDrafts = Array.from(new Map(mappings.map((item) => [item.model_id.trim(), item])).values());
      const payload = {
        name: name.trim(), base_url: baseUrl.trim(), enabled: providerEnabled, priority: Number(providerPriority), timeout_seconds: Number(timeout), settings,
        credentials: credentials.map((item) => ({
          name: item.name.trim(), secret: item.rotate_secret ? item.secret : null, rotate_secret: item.rotate_secret,
          enabled: item.enabled, priority: Number(item.priority), quota_limit: item.quota_limit ? Number(item.quota_limit) : null,
          quota_threshold: Number(item.quota_threshold), requests_per_minute: item.requests_per_minute ? Number(item.requests_per_minute) : null,
          tokens_per_minute: item.tokens_per_minute ? Number(item.tokens_per_minute) : null,
        })),
        models: modelDrafts.map((item) => ({ id: item.model_id.trim(), display_name: item.display_name.trim() || item.model_id.trim(), aliases: item.aliases.split(",").map((alias) => alias.trim()).filter(Boolean), capabilities: Array.from(new Set([...item.model_capabilities, ...mappings.filter((mapping) => mapping.model_id.trim() === item.model_id.trim()).flatMap((mapping) => mapping.capabilities)])), enabled: item.model_enabled, context_window: item.context_window ? Number(item.context_window) : null })),
        mappings: mappings.map((item) => ({
          model_id: item.model_id.trim(), upstream_model_id: item.upstream_model_id.trim(), protocol: item.protocol, capabilities: item.capabilities,
          enabled: item.enabled, max_concurrency: Number(item.max_concurrency), priority: Number(item.priority), weight: Number(item.weight), settings: JSON.parse(item.settings_json || "{}"),
          pricing: item.input_price ? { ...item.pricing_metadata, input_per_million: Number(item.input_price), output_per_million: Number(item.output_price), ...(item.cached_price ? { cached_input_per_million: Number(item.cached_price) } : {}), currency: item.currency.trim().toUpperCase() } : {},
        })),
        routes: mappings.filter((item) => item.route_present).map((item) => ({ model_id: item.model_id.trim(), mapping_upstream_model_id: item.upstream_model_id.trim(), mapping_protocol: item.protocol, priority: Number(item.route_priority), enabled: item.route_enabled, allow_model_fallback: item.allow_model_fallback })),
        pool_strategy: strategy, pool_enabled: poolEnabled, ...poolMetadata.current,
      };
      await gatewayApi("providers/reconcile", { method: "PUT", body: JSON.stringify(payload) });
      await onSaved();
    } catch (reason) {
      setError(reason instanceof SyntaxError ? "Provider settings must be valid JSON." : reason instanceof Error ? reason.message : "Provider setup failed");
      setBusy(false);
      busyRef.current = false;
    }
  }

  return <div className="dialog-backdrop"><section ref={dialog} className="editor provider-setup" role="dialog" aria-modal="true" aria-labelledby="provider-setup-title" aria-busy={loading || busy}>
    <div className="editor-head"><div><h2 id="provider-setup-title">{editing ? `Configure ${text(provider?.name)}` : "Add provider"}</h2><p>Set up one provider workspace: credentials, available models, protocols, and primary or fallback routing.</p></div><button type="button" className="icon-button" onClick={onClose} disabled={busy} aria-label="Close provider setup"><X size={18} /></button></div>
    <form onSubmit={submit}>{loading || (editing && hydrationFailed) ? <div className="loading-state" role={hydrationFailed ? "alert" : "status"}>{hydrationFailed ? <span>{error}. Provider configuration could not be loaded. Close and retry.</span> : "Loading provider configuration..."}</div> : <>
      <fieldset disabled={busy}><legend>Provider</legend><div className="provider-grid">
        <label className="field"><span>Provider name</span><input ref={initialFocus} value={name} onChange={(event) => setName(event.target.value)} required readOnly={editing} /></label>
        <label className="field"><span>Base URL</span><input type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} required /></label>
        <label className="field"><span>Priority</span><input type="number" min="0" step="1" value={providerPriority} onChange={(event) => setProviderPriority(event.target.value)} required /></label>
        <label className="field"><span>Timeout seconds</span><input type="number" min="0.001" step="any" value={timeout} onChange={(event) => setTimeout(event.target.value)} required /></label>
        <label className="check-control"><input type="checkbox" checked={providerEnabled} onChange={(event) => setProviderEnabled(event.target.checked)} />Provider enabled</label>
         <label className="field"><span>Authentication</span><select value={authScheme} onChange={(event) => setAuthScheme(event.target.value)}><option value="default">Provider default</option><option value="bearer">Bearer token</option><option value="api_key">API key</option></select><small>How the Gateway sends the stored credential upstream.</small></label>
         <PairEditor label="Custom headers" items={headers} onChange={setHeaders} empty="No custom headers" />
         <PairEditor label="Endpoint parameters" items={queryParameters} onChange={setQueryParameters} empty="No endpoint parameters" />
         <details className="advanced-config" open={showAdvanced} onToggle={(event) => setShowAdvanced(event.currentTarget.open)}><summary>Advanced configuration</summary><label className="field full"><span>Raw provider settings</span><textarea value={providerSettings} onChange={(event) => setProviderSettings(event.target.value)} rows={4} /><small>Use only for provider-specific options not covered above.</small></label></details>
      </div></fieldset>
       <fieldset disabled={busy}><legend>Credentials</legend><div className="subsection-head"><p>Manage this provider as one credential pool. Health, quota, rate limits, and failover are evaluated together.</p><button type="button" onClick={() => setCredentials((items) => [...items, emptyCredential()])}><Plus size={14} />Credential</button></div>
        <div className="repeater-list">{credentials.map((item, index) => <div className="repeater" key={index}>
          <label className="field"><span>Label</span><input value={item.name} onChange={(event) => updateCredential(index, { name: event.target.value })} required readOnly={item.existing} /></label>
          <label className="check-control"><input type="checkbox" checked={item.enabled} onChange={(event) => updateCredential(index, { enabled: event.target.checked })} />Credential enabled</label>
          {item.existing && <label className="check-control"><input type="checkbox" checked={item.rotate_secret} onChange={(event) => updateCredential(index, { rotate_secret: event.target.checked, secret: "" })} />Rotate secret</label>}
          <label className="field"><span>{item.existing ? "New secret" : "Secret"}</span><input type="password" value={item.secret} disabled={item.existing && !item.rotate_secret} onChange={(event) => updateCredential(index, { secret: event.target.value })} required={!item.existing || item.rotate_secret} /></label>
           <label className="field"><span>Routing priority</span><input type="number" min="0" step="1" value={item.priority} onChange={(event) => updateCredential(index, { priority: event.target.value })} /><small>Lower values are preferred when credentials are otherwise equally healthy.</small></label>
           <label className="field"><span>Requests per minute</span><input type="number" min="1" step="1" value={item.requests_per_minute} onChange={(event) => updateCredential(index, { requests_per_minute: event.target.value })} /><small>Leave blank for provider-managed limits.</small></label>
           <label className="field"><span>Tokens per minute</span><input type="number" min="1" step="1" value={item.tokens_per_minute} onChange={(event) => updateCredential(index, { tokens_per_minute: event.target.value })} /><small>Maximum token throughput for this credential.</small></label>
           <label className="field"><span>Quota limit</span><input type="number" min="0" step="any" value={item.quota_limit} onChange={(event) => updateCredential(index, { quota_limit: event.target.value })} /><small>Optional provider quota amount.</small></label>
           <label className="field"><span>Quota warning threshold</span><input type="number" min="0.01" max="1" step="0.01" value={item.quota_threshold} onChange={(event) => updateCredential(index, { quota_threshold: event.target.value })} /><small>Temporarily stop using it at this share of quota.</small></label>
          <button type="button" className="icon-button danger remove-control" onClick={() => setCredentials((items) => items.filter((_, position) => position !== index))} aria-label={`Remove credential ${item.name || index + 1}`}><Trash2 size={15} /></button>
        </div>)}</div>
      </fieldset>
       <fieldset disabled={busy}><legend>Models and routes</legend><div className="subsection-head"><p>Choose the models this provider serves, then set its primary or fallback position. Internal mappings are generated automatically.</p><button type="button" onClick={() => setMappings((items) => [...items, emptyMapping()])}><Plus size={14} />Model</button></div>
        <div className="mapping-list">{mappings.map((item, index) => <article className="mapping-card" key={index}>
          <div className="mapping-card-head"><strong>{item.model_id || `Mapping ${index + 1}`}</strong><button type="button" className="icon-button danger" onClick={() => setMappings((items) => items.filter((_, position) => position !== index))} aria-label={`Remove mapping ${item.model_id || index + 1}`}><Trash2 size={15} /></button></div>
           <div className="provider-grid"><label className="field"><span>Model ID</span><input value={item.model_id} list={`model-catalogue-${index}`} onChange={(event) => selectCatalogueModel(index, event.target.value)} required /><datalist id={`model-catalogue-${index}`}>{catalogue.map((known) => <option value={text(known.id)} key={text(known.id)}>{text(known.display_name, text(known.id))}</option>)}</datalist><small>{catalogue.some((known) => text(known.id) === item.model_id.trim()) ? "Existing catalogue model. Its shared details are filled in below and are edited for every provider at once." : "Pick an existing model, or type a new id to add one. Clients use this name to request the model."}</small></label><label className="field"><span>Model display name</span><input value={item.display_name} onChange={(event) => updateMapping(index, { display_name: event.target.value })} required /></label>
          <label className="field"><span>Aliases (comma separated)</span><input value={item.aliases} onChange={(event) => updateMapping(index, { aliases: event.target.value })} /></label><label className="field"><span>Context window</span><input type="number" min="1" step="1" value={item.context_window} onChange={(event) => updateMapping(index, { context_window: event.target.value })} /></label>
           <label className="field"><span>Provider model ID</span><input value={item.upstream_model_id} onChange={(event) => updateMapping(index, { upstream_model_id: event.target.value })} required /><small>The provider's name for this model.</small></label><label className="field"><span>Protocol</span><select value={item.protocol} onChange={(event) => updateMapping(index, { protocol: event.target.value })}>{protocols.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select><small>How requests are sent upstream.</small></label>
           <label className="field"><span>Provider priority</span><input type="number" min="0" step="1" value={item.priority} onChange={(event) => updateMapping(index, { priority: event.target.value })} /><small>Lower values are preferred.</small></label><label className="field"><span>Traffic share</span><input type="number" min="0.001" step="any" value={item.weight} onChange={(event) => updateMapping(index, { weight: event.target.value })} /><small>Used when multiple routes are eligible.</small></label><label className="field"><span>Concurrent request limit</span><input type="number" min="1" step="1" value={item.max_concurrency} onChange={(event) => updateMapping(index, { max_concurrency: event.target.value })} /></label>
          <label className="check-control"><input type="checkbox" checked={item.model_enabled} onChange={(event) => updateMapping(index, { model_enabled: event.target.checked })} />Canonical model enabled</label><label className="check-control"><input type="checkbox" checked={item.enabled} onChange={(event) => updateMapping(index, { enabled: event.target.checked })} />Mapping enabled</label></div>
          <fieldset className="capability-fieldset"><legend>Capabilities</legend><div className="capability-grid">{capabilities.map(([value, label]) => <label key={value}><input type="checkbox" checked={item.capabilities.includes(value)} onChange={(event) => updateMapping(index, { capabilities: event.target.checked ? [...item.capabilities, value] : item.capabilities.filter((entry) => entry !== value) })} />{label}</label>)}</div></fieldset>
           <details><summary>Advanced transport settings</summary><label className="field"><span>Raw transport configuration</span><textarea value={item.settings_json} onChange={(event) => updateMapping(index, { settings_json: event.target.value })} rows={4} /><small>Optional provider-specific configuration. Most operators can leave this unchanged.</small></label></details>
           <div className="pricing-grid"><label className="field"><span>Input price per 1M tokens</span><input type="number" min="0" step="any" value={item.input_price} onChange={(event) => updateMapping(index, { input_price: event.target.value })} /></label><label className="field"><span>Output price per 1M tokens</span><input type="number" min="0" step="any" value={item.output_price} onChange={(event) => updateMapping(index, { output_price: event.target.value })} /></label><label className="field"><span>Cached input per 1M tokens</span><input type="number" min="0" step="any" value={item.cached_price} onChange={(event) => updateMapping(index, { cached_price: event.target.value })} /></label><label className="field"><span>Currency</span><input maxLength={3} value={item.currency} onChange={(event) => updateMapping(index, { currency: event.target.value })} /></label></div>
           <div className="route-controls"><label className="check-control"><input type="checkbox" checked={item.route_present} onChange={(event) => updateMapping(index, { route_present: event.target.checked })} />Make model available</label><label className="check-control"><input type="checkbox" checked={item.route_enabled} disabled={!item.route_present} onChange={(event) => updateMapping(index, { route_enabled: event.target.checked })} />Route active</label><label className="check-control"><input type="checkbox" checked={item.allow_model_fallback} disabled={!item.route_present} onChange={(event) => updateMapping(index, { allow_model_fallback: event.target.checked })} />Use as model fallback</label><label className="field"><span>Route priority</span><input type="number" min="0" step="1" disabled={!item.route_present} value={item.route_priority} onChange={(event) => updateMapping(index, { route_priority: event.target.value })} /><small>Lower values are tried first.</small></label></div>
        </article>)}</div>
      </fieldset>
      <fieldset disabled={busy}><legend>Credential pool</legend><div className="pool-controls"><label className="field"><span>Selection strategy</span><select value={strategy} onChange={(event) => setStrategy(event.target.value)}><option value="priority">Priority</option><option value="weighted">Weighted</option><option value="least_loaded">Least loaded</option></select></label><label className="check-control"><input type="checkbox" checked={poolEnabled} onChange={(event) => setPoolEnabled(event.target.checked)} />Pool enabled</label></div><p className="metadata-note">Health and quota awareness are retained as pool metadata; runtime behavior is governed by routing policy and availability.</p></fieldset>
      {error && <div className="form-error" role="alert">{error}</div>}
      <div className="form-actions"><button type="button" onClick={onClose} disabled={busy}>Cancel</button><button className="primary" disabled={busy}>{busy ? "Saving..." : editing ? "Save provider workspace" : "Create provider workspace"}</button></div>
    </>}</form>
  </section></div>;
}
