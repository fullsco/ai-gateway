"use client";

import { ArrowDown, ArrowUp, Check, Save, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { gatewayApi } from "./gateway-api";
import { PROTOCOLS, PROTOCOL_ENDPOINTS, servedProtocols } from "./gateway-protocols";

type Row = Record<string, unknown>;
type RouteDraft = {
  provider: string;
  providerId: string;
  providerModelIds: string[];
  priority: number;
};
type ProviderOption = {
  provider: string;
  providerId: string;
  enabled: boolean;
  routed: boolean;
  priority: number;
  /** Every enabled mapping this provider has for the model. */
  mappingIds: string[];
  /** The mappings that are currently carrying traffic. */
  activeMappingIds: string[];
};
type MappingFacts = {
  provider: string;
  upstreamModelId: string;
  /** The protocol the gateway speaks to this provider. */
  protocol: string;
  /** Every client protocol this mapping answers, its native one included. */
  serves: string[];
};
type Reach = {
  protocol: string;
  label: string;
  endpoint: string;
  /** The drafted routes that answer this protocol, in the order they are tried. */
  via: { provider: string; upstreamModelId: string; native: boolean }[];
};

const text = (value: unknown, fallback = "") => String(value ?? fallback);

/**
 * A provider may expose the same model under several protocols, so one provider
 * can own several provider/model mappings. Those mapping ids are carried through
 * to the save payload; collapsing them and letting the server re-expand by name
 * silently enabled routes the operator never selected.
 */
function optionsFromRouting(rows: Row[]): ProviderOption[] {
  const byProvider = new Map<string, ProviderOption>();
  rows.forEach((row) => {
    const provider = text(row.provider);
    if (!provider) return;
    const enabled = row.provider_enabled !== false && row.mapping_enabled !== false;
    // `route_active` already accounts for a disabled provider or mapping. Fall
    // back to the raw flag for gateways that predate it.
    const routed = row.route_active === undefined ? row.route_enabled === true : row.route_active === true;
    const priority = Number(row.priority ?? 100);
    const mappingId = text(row.provider_model_id);
    const existing = byProvider.get(provider);
    if (!existing) {
      byProvider.set(provider, {
        provider,
        providerId: text(row.provider_id),
        enabled,
        routed,
        priority,
        mappingIds: enabled && mappingId ? [mappingId] : [],
        activeMappingIds: routed && mappingId ? [mappingId] : [],
      });
      return;
    }
    existing.enabled = existing.enabled || enabled;
    existing.routed = existing.routed || routed;
    existing.priority = Math.min(existing.priority, priority);
    if (!existing.providerId) existing.providerId = text(row.provider_id);
    if (enabled && mappingId && !existing.mappingIds.includes(mappingId)) {
      existing.mappingIds.push(mappingId);
    }
    if (routed && mappingId && !existing.activeMappingIds.includes(mappingId)) {
      existing.activeMappingIds.push(mappingId);
    }
  });
  return [...byProvider.values()];
}

/**
 * Per-mapping protocol facts, kept beside the provider aggregate.
 *
 * `optionsFromRouting` collapses a provider's mappings into one row, which is the right
 * shape for ordering providers but loses the question a client actually asks: can *this*
 * endpoint reach the model. That is a property of an individual mapping, so it is indexed
 * separately rather than smeared across the aggregate.
 */
function mappingFacts(rows: Row[]): Map<string, MappingFacts> {
  const facts = new Map<string, MappingFacts>();
  rows.forEach((row) => {
    const mappingId = text(row.provider_model_id);
    if (!mappingId || facts.has(mappingId)) return;
    const protocol = text(row.protocol);
    facts.set(mappingId, {
      provider: text(row.provider),
      upstreamModelId: text(row.upstream_model_id),
      protocol,
      // A gateway older than the served-protocols column reports nothing here and
      // answered only its upstream protocol - which is what the shared helper returns
      // for an empty list, so old and new snapshots read the same way.
      serves: servedProtocols(protocol, Array.isArray(row.serves_protocols) ? row.serves_protocols.map(String) : []),
    });
  });
  return facts;
}

/**
 * Which client APIs reach this model once the drafted routes are saved.
 *
 * Drafted rather than active on purpose: this sits under the provider order the operator
 * is editing, so it has to answer for the arrangement in front of them. Removing the last
 * provider that answers `/v1/messages` should read as unreachable here, before a client
 * discovers it as a 404.
 */
function reachability(routes: RouteDraft[], facts: Map<string, MappingFacts>): Reach[] {
  return PROTOCOLS.map(([protocol, label]) => ({
    protocol,
    label,
    endpoint: PROTOCOL_ENDPOINTS[protocol],
    via: routes.flatMap((route) =>
      route.providerModelIds.flatMap((mappingId) => {
        const fact = facts.get(mappingId);
        if (!fact || !fact.serves.includes(protocol)) return [];
        return [{ provider: route.provider, upstreamModelId: fact.upstreamModelId, native: fact.protocol === protocol }];
      }),
    ),
  }));
}

function draftFromOption(option: ProviderOption, priority: number): RouteDraft {
  return {
    provider: option.provider,
    providerId: option.providerId,
    // Keep serving exactly what is already routed; a newly added provider takes
    // every mapping it has for the model.
    providerModelIds: option.activeMappingIds.length ? option.activeMappingIds : option.mappingIds,
    priority,
  };
}

export default function ModelRouting({ onNotice }: { onNotice: (message: string, kind?: "success" | "error") => void }) {
  const [models, setModels] = useState<Row[]>([]);
  const [modelId, setModelId] = useState("");
  const [options, setOptions] = useState<ProviderOption[]>([]);
  const [facts, setFacts] = useState<Map<string, MappingFacts>>(new Map());
  const [routes, setRoutes] = useState<RouteDraft[]>([]);
  const [strategy, setStrategy] = useState("priority");
  const [healthAware, setHealthAware] = useState(true);
  const [quotaAware, setQuotaAware] = useState(true);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    gatewayApi("models")
      .then((modelData) => {
        setModels(modelData.data ?? []);
        if (modelData.data?.[0]?.id) setModelId(String(modelData.data[0].id));
        else setLoading(false);
      })
      .catch((reason) => {
        onNotice(reason instanceof Error ? reason.message : "Unable to load models", "error");
        setLoading(false);
      });
  }, [onNotice]);

  const loadRouting = useCallback(async (model: string) => {
    setLoading(true);
    try {
      const payload = await gatewayApi(`models/${encodeURIComponent(model)}/routing`);
      const rows = (payload.data as Row[]) ?? [];
      const derived = optionsFromRouting(rows);
      setOptions(derived);
      setFacts(mappingFacts(rows));
      setRoutes(
        derived
          .filter((option) => option.routed)
          .sort((a, b) => a.priority - b.priority)
          .map((option, index) => draftFromOption(option, index * 10)),
      );
    } catch (reason) {
      onNotice(reason instanceof Error ? reason.message : "Unable to load model routing", "error");
      setOptions([]);
      setFacts(new Map());
      setRoutes([]);
    } finally {
      setLoading(false);
    }
  }, [onNotice]);

  useEffect(() => {
    if (modelId) void loadRouting(modelId);
  }, [modelId, loadRouting]);

  function addProvider(provider: string) {
    if (!provider || routes.some((route) => route.provider === provider)) return;
    const option = options.find((candidate) => candidate.provider === provider);
    if (!option) return;
    setRoutes((current) => [...current, draftFromOption(option, current.length * 10)]);
  }

  function reindex(next: RouteDraft[]): RouteDraft[] {
    return next.map((route, position) => ({ ...route, priority: position * 10 }));
  }

  function move(index: number, direction: -1 | 1) {
    const target = index + direction;
    if (target < 0 || target >= routes.length) return;
    const next = [...routes];
    [next[index], next[target]] = [next[target], next[index]];
    setRoutes(reindex(next));
  }

  async function save() {
    if (!modelId || routes.length === 0) return;
    setSaving(true);
    try {
      await gatewayApi(`models/${encodeURIComponent(modelId)}/routing`, {
        method: "PUT",
        body: JSON.stringify({
          providers: routes.map((route, index) => ({
            // Prefer the stable identifier; provider names are free text and
            // were previously matched case-sensitively in SQL, which silently
            // disabled the very route being selected.
            ...(route.providerId ? { provider_id: route.providerId } : { provider: route.provider }),
            // Name the exact mappings so the server never expands a provider
            // selection onto mappings the operator did not choose.
            ...(route.providerModelIds.length
              ? { provider_model_ids: route.providerModelIds }
              : {}),
            priority: route.priority,
            fallback: index > 0,
          })),
          strategy,
          health_aware: healthAware,
          quota_aware: quotaAware,
        }),
      });
      onNotice("Routing changes saved to the working configuration. Review and publish when ready.");
      await loadRouting(modelId);
    } catch (reason) {
      onNotice(reason instanceof Error ? reason.message : "Unable to save routing", "error");
    } finally {
      setSaving(false);
    }
  }

  const selected = models.find((model) => text(model.id) === modelId);
  const capabilities = (selected?.capabilities as string[] | undefined) ?? [];
  const available = options.filter((option) => option.enabled && !routes.some((route) => route.provider === option.provider));
  const exposesModel = options.length > 0;
  const reach = reachability(routes, facts);

  if (loading && !models.length) return <div className="loading-state" role="status">Loading models...</div>;
  if (!models.length) return <div className="empty-state"><strong>No models yet</strong><span>Add a provider through guided setup to make a model available.</span></div>;

  return <section className="routing-workspace">
    <div className="routing-intro">
      <div><h2>Models and routing</h2><p>Choose where each model is served. The Gateway generates the underlying routes and policy records for you.</p></div>
      <button className="primary" disabled={saving || loading || !routes.length} onClick={() => void save()}><Save size={15} />{saving ? "Saving..." : "Save working changes"}</button>
    </div>
    <div className="routing-grid">
      <label className="field"><span>Model</span><select value={modelId} onChange={(event) => setModelId(event.target.value)}>{models.map((model) => <option key={text(model.id)} value={text(model.id)}>{text(model.display_name, text(model.id))}</option>)}</select><small>{capabilities.length ? `Capabilities: ${capabilities.join(", ")}` : "No additional capabilities listed for this model."}</small></label>
      <label className="field"><span>Routing strategy</span><select value={strategy} onChange={(event) => setStrategy(event.target.value)}><option value="priority">Primary first, then fallback</option><option value="least_loaded">Prefer the least loaded eligible provider</option><option value="weighted">Distribute traffic by weight</option></select></label>
    </div>
    <div className="routing-switches">
      <label className="check-control"><input type="checkbox" checked={healthAware} onChange={(event) => setHealthAware(event.target.checked)} />Health-aware routing<small>Skip providers and credentials that are not currently eligible.</small></label>
      <label className="check-control"><input type="checkbox" checked={quotaAware} onChange={(event) => setQuotaAware(event.target.checked)} />Quota-aware routing<small>Prefer credentials with available quota and rate-limit headroom.</small></label>
    </div>
    <div className="route-builder">
      <div className="section-head">
        <div><h3>Provider order</h3><p>The first provider is primary. Later providers are fallbacks.</p></div>
        <select aria-label="Add provider" value="" disabled={loading || !available.length} onChange={(event) => addProvider(event.target.value)}>
          <option value="">{available.length ? "Add provider" : "No more providers expose this model"}</option>
          {available.map((option) => <option key={option.provider} value={option.provider}>{option.provider}</option>)}
        </select>
      </div>
      {loading ? <div className="loading-state" role="status">Loading routing...</div> : routes.length ? (
        <ol className="route-order">{routes.map((route, index) => <li key={route.provider}>
          <div className="route-rank">{index === 0 ? <Check size={16} /> : index + 1}</div>
          <div className="route-copy"><strong>{route.provider}</strong><span>{index === 0 ? "Primary provider" : "Fallback provider"}</span></div>
          <div className="route-actions">
            <button className="icon-button" disabled={index === 0} onClick={() => move(index, -1)} aria-label={`Move ${route.provider} earlier`}><ArrowUp size={15} /></button>
            <button className="icon-button" disabled={index === routes.length - 1} onClick={() => move(index, 1)} aria-label={`Move ${route.provider} later`}><ArrowDown size={15} /></button>
            <button className="icon-button danger" onClick={() => setRoutes((current) => reindex(current.filter((entry) => entry.provider !== route.provider)))} aria-label={`Remove ${route.provider}`}><X size={15} /></button>
          </div>
        </li>)}</ol>
      ) : (
        <div className="empty-state">
          <strong>{exposesModel ? "No active provider routes" : "No providers expose this model yet"}</strong>
          <span>{exposesModel ? "Add a provider to make this model available." : "Add this model to a provider through guided setup, then route it here."}</span>
        </div>
      )}
    </div>
    <div className="reach-panel">
      <div className="section-head">
        <div>
          <h3>Client reachability</h3>
          <p>Which client APIs can reach this model with the order above. An unanswered API returns 404 to its callers even while the provider is healthy.</p>
        </div>
      </div>
      <ul className="reach-list">
        {reach.map((entry) => {
          const translated = entry.via.filter((step) => !step.native).length;
          return (
            <li key={entry.protocol} className={entry.via.length ? "reach" : "reach unreachable"}>
              <div className="reach-copy">
                <strong>{entry.label}</strong>
                <code>{entry.endpoint}</code>
                <span>
                  {entry.via.length
                    ? `Served by ${entry.via.map((step) => `${step.provider} / ${step.upstreamModelId} (${step.native ? "native" : "translated"})`).join(", ")}.`
                    : `No route in this order answers ${entry.endpoint}. Add a provider whose mapping serves it, or tick it on the mapping in guided setup.`}
                </span>
              </div>
              <span className={entry.via.length ? "state ok" : "state bad"}>
                {entry.via.length === 0 ? "unreachable" : translated === 0 ? "native" : translated === entry.via.length ? "translated" : "native + translated"}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
    <div className="routing-review">
      <strong>What will happen</strong>
      <span>{routes.length ? `${text(selected?.display_name, modelId)} will try ${routes[0]?.provider} first${routes.length > 1 ? `, then fall back to ${routes.slice(1).map((route) => route.provider).join(", ")}` : ""}.` : "Add at least one provider to create a route."}</span>
      <small>Changes remain unpublished until you review and publish the working configuration.</small>
    </div>
  </section>;
}
