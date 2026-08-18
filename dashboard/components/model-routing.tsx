"use client";

import { ArrowDown, ArrowUp, Check, Save, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { gatewayApi } from "./provider-setup";

type Row = Record<string, unknown>;
type RouteDraft = { provider: string; priority: number };
type ProviderOption = { provider: string; enabled: boolean; routed: boolean; priority: number };

const text = (value: unknown, fallback = "") => String(value ?? fallback);

function optionsFromRouting(rows: Row[]): ProviderOption[] {
  const byProvider = new Map<string, ProviderOption>();
  rows.forEach((row) => {
    const provider = text(row.provider);
    if (!provider) return;
    const enabled = row.provider_enabled !== false && row.mapping_enabled !== false;
    const routed = row.route_enabled === true;
    const priority = Number(row.priority ?? 100);
    const existing = byProvider.get(provider);
    if (!existing) {
      byProvider.set(provider, { provider, enabled, routed, priority });
      return;
    }
    existing.enabled = existing.enabled || enabled;
    existing.routed = existing.routed || routed;
    existing.priority = Math.min(existing.priority, priority);
  });
  return [...byProvider.values()];
}

export default function ModelRouting({ onNotice }: { onNotice: (message: string, kind?: "success" | "error") => void }) {
  const [models, setModels] = useState<Row[]>([]);
  const [modelId, setModelId] = useState("");
  const [options, setOptions] = useState<ProviderOption[]>([]);
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
      const derived = optionsFromRouting((payload.data as Row[]) ?? []);
      setOptions(derived);
      setRoutes(
        derived
          .filter((option) => option.routed)
          .sort((a, b) => a.priority - b.priority)
          .map((option, index) => ({ provider: option.provider, priority: index * 10 })),
      );
    } catch (reason) {
      onNotice(reason instanceof Error ? reason.message : "Unable to load model routing", "error");
      setOptions([]);
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
    setRoutes((current) => [...current, { provider, priority: current.length * 10 }]);
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
          providers: routes.map((route, index) => ({ provider: route.provider, priority: route.priority, fallback: index > 0 })),
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
    <div className="routing-review">
      <strong>What will happen</strong>
      <span>{routes.length ? `${text(selected?.display_name, modelId)} will try ${routes[0]?.provider} first${routes.length > 1 ? `, then fall back to ${routes.slice(1).map((route) => route.provider).join(", ")}` : ""}.` : "Add at least one provider to create a route."}</span>
      <small>Changes remain unpublished until you review and publish the working configuration.</small>
    </div>
  </section>;
}
