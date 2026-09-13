"use client";

/**
 * The guided provider form: one provider's credentials, the models it serves, and the
 * routes that make them reachable, saved as a single reconcile call.
 *
 * It is one form rather than five because that is how the objects actually relate - a
 * credential with no mapping serves nothing, and a mapping with no route is invisible to
 * clients. Editing them separately is how a provider ends up half-configured.
 */

import { FormEvent, useEffect, useRef, useState } from "react";
import { Plus, X } from "lucide-react";
import { GatewayRow, gatewayApi } from "./gateway-api";
import { servedProtocols } from "./gateway-protocols";
import {
  CredentialDraft,
  MappingDraft,
  NEW_MODEL,
  emptyCredential,
  emptyMapping,
  list,
  object,
  parsePairs,
  parseParameterValue,
  text,
  validateSetup,
} from "./provider-setup-drafts";
import { CredentialCard, PairEditor } from "./provider-setup-fields";
import { MappingCard } from "./provider-setup-mapping";

export { gatewayApi } from "./gateway-api";
export type { GatewayRow } from "./gateway-api";

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
            protocol: text(item.protocol), serves_protocols: servedProtocols(text(item.protocol), list(item.serves_protocols)), capabilities: list(item.capabilities), enabled: Boolean(item.enabled), max_concurrency: text(item.max_concurrency, "8"),
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
  const catalogueHas = (modelId: string) => catalogue.some((known) => text(known.id) === modelId.trim());

  // A select is the choice made visible, but it cannot express an id that does not
  // exist yet, so adding a model is an option of its own that reveals a text field
  // rather than a free-text box that only sometimes offers suggestions.
  const chooseModel = (index: number, chosen: string) => {
    if (chosen === NEW_MODEL) {
      updateMapping(index, { model_id: "" });
      return;
    }
    selectCatalogueModel(index, chosen);
  };

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

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    try {
      validateSetup({ credentials, mappings, providerPriority, timeout });
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
          model_id: item.model_id.trim(), upstream_model_id: item.upstream_model_id.trim(), protocol: item.protocol, serves_protocols: item.serves_protocols, capabilities: item.capabilities,
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

  return (
    <div className="dialog-backdrop">
      <section ref={dialog} className="editor provider-setup" role="dialog" aria-modal="true" aria-labelledby="provider-setup-title" aria-busy={loading || busy}>
        <div className="editor-head">
          <div>
            <h2 id="provider-setup-title">{editing ? `Configure ${text(provider?.name)}` : "Add provider"}</h2>
            <p>Set up one provider workspace: credentials, available models, protocols, and primary or fallback routing.</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} disabled={busy} aria-label="Close provider setup">
            <X size={18} />
          </button>
        </div>
        <form onSubmit={submit}>
          {loading || (editing && hydrationFailed) ? (
            <div className="loading-state" role={hydrationFailed ? "alert" : "status"}>
              {hydrationFailed ? <span>{error}. Provider configuration could not be loaded. Close and retry.</span> : "Loading provider configuration..."}
            </div>
          ) : (
            <>
              <fieldset disabled={busy}>
                <legend>Provider</legend>
                <div className="provider-grid">
                  <label className="field">
                    <span>Provider name</span>
                    <input ref={initialFocus} value={name} onChange={(event) => setName(event.target.value)} required readOnly={editing} />
                  </label>
                  <label className="field">
                    <span>Base URL</span>
                    <input type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} required />
                  </label>
                  <label className="field">
                    <span>Priority</span>
                    <input type="number" min="0" step="1" value={providerPriority} onChange={(event) => setProviderPriority(event.target.value)} required />
                  </label>
                  <label className="field">
                    <span>Timeout seconds</span>
                    <input type="number" min="0.001" step="any" value={timeout} onChange={(event) => setTimeout(event.target.value)} required />
                  </label>
                  <label className="check-control">
                    <input type="checkbox" checked={providerEnabled} onChange={(event) => setProviderEnabled(event.target.checked)} />
                    Provider enabled
                  </label>
                  <label className="field">
                    <span>Authentication</span>
                    <select value={authScheme} onChange={(event) => setAuthScheme(event.target.value)}>
                      <option value="default">Provider default</option>
                      <option value="bearer">Bearer token</option>
                      <option value="api_key">API key</option>
                    </select>
                    <small>How the Gateway sends the stored credential upstream.</small>
                  </label>
                  <PairEditor label="Custom headers" items={headers} onChange={setHeaders} empty="No custom headers" />
                  <PairEditor label="Endpoint parameters" items={queryParameters} onChange={setQueryParameters} empty="No endpoint parameters" />
                  <details className="advanced-config" open={showAdvanced} onToggle={(event) => setShowAdvanced(event.currentTarget.open)}>
                    <summary>Advanced configuration</summary>
                    <label className="field full">
                      <span>Raw provider settings</span>
                      <textarea value={providerSettings} onChange={(event) => setProviderSettings(event.target.value)} rows={4} />
                      <small>Use only for provider-specific options not covered above.</small>
                    </label>
                  </details>
                </div>
              </fieldset>
              <fieldset disabled={busy}>
                <legend>Credentials</legend>
                <div className="subsection-head">
                  <p>Manage this provider as one credential pool. Health, quota, rate limits, and failover are evaluated together.</p>
                  <button type="button" onClick={() => setCredentials((items) => [...items, emptyCredential()])}>
                    <Plus size={14} />
                    Credential
                  </button>
                </div>
                <div className="repeater-list">
                  {credentials.map((item, index) => (
                    <CredentialCard
                      key={index}
                      item={item}
                      index={index}
                      update={(patch) => updateCredential(index, patch)}
                      remove={() => setCredentials((items) => items.filter((_, position) => position !== index))}
                    />
                  ))}
                </div>
              </fieldset>
              <fieldset disabled={busy}>
                <legend>Models and routes</legend>
                <div className="subsection-head">
                  <p>Choose the models this provider serves, then set its primary or fallback position. Internal mappings are generated automatically.</p>
                  <button type="button" onClick={() => setMappings((items) => [...items, emptyMapping()])}>
                    <Plus size={14} />
                    Model
                  </button>
                </div>
                <div className="mapping-list">
                  {mappings.map((item, index) => (
                    <MappingCard
                      key={index}
                      item={item}
                      index={index}
                      catalogue={catalogue}
                      catalogueHas={catalogueHas}
                      choose={(chosen) => chooseModel(index, chosen)}
                      selectCatalogue={(modelId) => selectCatalogueModel(index, modelId)}
                      update={(patch) => updateMapping(index, patch)}
                      remove={() => setMappings((items) => items.filter((_, position) => position !== index))}
                    />
                  ))}
                </div>
              </fieldset>
              <fieldset disabled={busy}>
                <legend>Credential pool</legend>
                <div className="pool-controls">
                  <label className="field">
                    <span>Selection strategy</span>
                    <select value={strategy} onChange={(event) => setStrategy(event.target.value)}>
                      <option value="priority">Priority</option>
                      <option value="weighted">Weighted</option>
                      <option value="least_loaded">Least loaded</option>
                    </select>
                  </label>
                  <label className="check-control">
                    <input type="checkbox" checked={poolEnabled} onChange={(event) => setPoolEnabled(event.target.checked)} />
                    Pool enabled
                  </label>
                </div>
                <p className="metadata-note">Health and quota awareness are retained as pool metadata; runtime behavior is governed by routing policy and availability.</p>
              </fieldset>
              {error && <div className="form-error" role="alert">{error}</div>}
              <div className="form-actions">
                <button type="button" onClick={onClose} disabled={busy}>Cancel</button>
                <button className="primary" disabled={busy}>{busy ? "Saving..." : editing ? "Save provider workspace" : "Create provider workspace"}</button>
              </div>
            </>
          )}
        </form>
      </section>
    </div>
  );
}
