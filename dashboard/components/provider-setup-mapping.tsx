"use client";

/**
 * One model this provider serves: which canonical model it is, how the gateway talks
 * to it upstream, what it costs, and whether a route makes it reachable.
 *
 * The canonical model is a select rather than a text field. A free-text id is how an
 * operator either creates an accidental second model from a typo, or reproduces an
 * existing id exactly and trips the shared-model guard on a field this form itself
 * prefilled - so adding a model is a deliberate option that reveals a text field.
 */

import { Trash2 } from "lucide-react";
import { GatewayRow } from "./gateway-api";
import { PROTOCOLS, PROTOCOL_ENDPOINTS, servedProtocols, translates } from "./gateway-protocols";
import { MappingDraft, NEW_MODEL, capabilities, protocols, text } from "./provider-setup-drafts";

export function MappingCard({
  item,
  index,
  catalogue,
  catalogueHas,
  choose,
  selectCatalogue,
  update,
  remove,
}: {
  item: MappingDraft;
  index: number;
  catalogue: GatewayRow[];
  catalogueHas: (modelId: string) => boolean;
  choose: (chosen: string) => void;
  selectCatalogue: (modelId: string) => void;
  update: (patch: Partial<MappingDraft>) => void;
  remove: () => void;
}) {
  const known = catalogueHas(item.model_id);
  return (
    <article className="mapping-card">
      <div className="mapping-card-head">
        <strong>{item.model_id || `Mapping ${index + 1}`}</strong>
        <button type="button" className="icon-button danger" onClick={remove} aria-label={`Remove mapping ${item.model_id || index + 1}`}>
          <Trash2 size={15} />
        </button>
      </div>
      <div className="provider-grid">
        <label className="field">
          <span>Catalogue model</span>
          <select value={known ? item.model_id.trim() : NEW_MODEL} onChange={(event) => choose(event.target.value)}>
            {catalogue.map((entry) => (
              <option value={text(entry.id)} key={text(entry.id)}>
                {text(entry.display_name, text(entry.id))}
              </option>
            ))}
            <option value={NEW_MODEL}>Add a new model...</option>
          </select>
          <small>
            {known
              ? "Existing catalogue model. Its shared details are filled in below and are edited for every provider at once."
              : "Choose a model this provider already serves, or add a new one. Clients use this name to request the model."}
          </small>
        </label>
        {!known && (
          <label className="field">
            <span>New model ID</span>
            <input value={item.model_id} onChange={(event) => selectCatalogue(event.target.value)} required />
            <small>The name clients use to request the model. It must not already exist in the catalogue.</small>
          </label>
        )}
        <label className="field">
          <span>Model display name</span>
          <input value={item.display_name} onChange={(event) => update({ display_name: event.target.value })} required />
        </label>
        <label className="field">
          <span>Aliases (comma separated)</span>
          <input value={item.aliases} onChange={(event) => update({ aliases: event.target.value })} />
        </label>
        <label className="field">
          <span>Context window</span>
          <input type="number" min="1" step="1" value={item.context_window} onChange={(event) => update({ context_window: event.target.value })} />
        </label>
        <label className="field">
          <span>Provider model ID</span>
          <input value={item.upstream_model_id} onChange={(event) => update({ upstream_model_id: event.target.value })} required />
          <small>The provider&apos;s name for this model.</small>
        </label>
        <label className="field">
          <span>Protocol</span>
          <select
            value={item.protocol}
            onChange={(event) =>
              update({
                protocol: event.target.value,
                // The old upstream protocol stays served, now by translation. Changing
                // how the gateway talks to a provider should not silently 404 the
                // clients that were reaching this model a moment ago; the checkboxes
                // below show the new arrangement, so it can be undone deliberately.
                serves_protocols: servedProtocols(event.target.value, item.serves_protocols),
              })
            }
          >
            {protocols.map(([value, label]) => (
              <option value={value} key={value}>
                {label}
              </option>
            ))}
          </select>
          <small>How requests are sent upstream.</small>
        </label>
        <label className="field">
          <span>Provider priority</span>
          <input type="number" min="0" step="1" value={item.priority} onChange={(event) => update({ priority: event.target.value })} />
          <small>Lower values are preferred.</small>
        </label>
        <label className="field">
          <span>Traffic share</span>
          <input type="number" min="0.001" step="any" value={item.weight} onChange={(event) => update({ weight: event.target.value })} />
          <small>Used when multiple routes are eligible.</small>
        </label>
        <label className="field">
          <span>Concurrent request limit</span>
          <input type="number" min="1" step="1" value={item.max_concurrency} onChange={(event) => update({ max_concurrency: event.target.value })} />
        </label>
        <label className="check-control">
          <input type="checkbox" checked={item.model_enabled} onChange={(event) => update({ model_enabled: event.target.checked })} />
          Canonical model enabled
        </label>
        <label className="check-control">
          <input type="checkbox" checked={item.enabled} onChange={(event) => update({ enabled: event.target.checked })} />
          Mapping enabled
        </label>
      </div>
      <fieldset className="capability-fieldset">
        <legend>Capabilities</legend>
        <div className="capability-grid">
          {capabilities.map(([value, label]) => (
            <label key={value}>
              <input
                type="checkbox"
                checked={item.capabilities.includes(value)}
                onChange={(event) =>
                  update({
                    capabilities: event.target.checked
                      ? [...item.capabilities, value]
                      : item.capabilities.filter((entry) => entry !== value),
                  })
                }
              />
              {label}
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset className="capability-fieldset">
        <legend>Client APIs this mapping answers</legend>
        <div className="capability-grid">
          {PROTOCOLS.map(([value, label]) => {
            const native = value === item.protocol;
            const served = native || item.serves_protocols.includes(value);
            return (
              <label key={value} className={`served-protocol${served ? " on" : ""}${native ? " native" : ""}`}>
                <input
                  type="checkbox"
                  checked={served}
                  // The upstream protocol is answered by relaying bytes, so it cannot be
                  // switched off - unchecking it would claim a route is unreachable from
                  // the very API it speaks. Change the protocol above to move it.
                  disabled={native || !translates(value, item.protocol)}
                  onChange={(event) =>
                    update({
                      serves_protocols: servedProtocols(
                        item.protocol,
                        event.target.checked
                          ? [...item.serves_protocols, value]
                          : item.serves_protocols.filter((entry) => entry !== value),
                      ),
                    })
                  }
                />
                <span>
                  {label} <code>{PROTOCOL_ENDPOINTS[value]}</code>
                </span>
                <em>{native ? "native" : translates(value, item.protocol) ? "translated" : "no translation available"}</em>
              </label>
            );
          })}
        </div>
        <small>
          A client calling an unchecked endpoint gets a 404 for this model even when the route is
          healthy. Translated APIs are converted on the way through, in both directions.
        </small>
      </fieldset>
      <details>
        <summary>Advanced transport settings</summary>
        <label className="field">
          <span>Raw transport configuration</span>
          <textarea value={item.settings_json} onChange={(event) => update({ settings_json: event.target.value })} rows={4} />
          <small>Optional provider-specific configuration. Most operators can leave this unchanged.</small>
        </label>
      </details>
      <div className="pricing-grid">
        <label className="field">
          <span>Input price per 1M tokens</span>
          <input type="number" min="0" step="any" value={item.input_price} onChange={(event) => update({ input_price: event.target.value })} />
        </label>
        <label className="field">
          <span>Output price per 1M tokens</span>
          <input type="number" min="0" step="any" value={item.output_price} onChange={(event) => update({ output_price: event.target.value })} />
        </label>
        <label className="field">
          <span>Cached input per 1M tokens</span>
          <input type="number" min="0" step="any" value={item.cached_price} onChange={(event) => update({ cached_price: event.target.value })} />
        </label>
        <label className="field">
          <span>Currency</span>
          <input maxLength={3} value={item.currency} onChange={(event) => update({ currency: event.target.value })} />
        </label>
      </div>
      <div className="route-controls">
        <label className="check-control">
          <input type="checkbox" checked={item.route_present} onChange={(event) => update({ route_present: event.target.checked })} />
          Make model available
        </label>
        <label className="check-control">
          <input type="checkbox" checked={item.route_enabled} disabled={!item.route_present} onChange={(event) => update({ route_enabled: event.target.checked })} />
          Route active
        </label>
        <label className="check-control">
          <input type="checkbox" checked={item.allow_model_fallback} disabled={!item.route_present} onChange={(event) => update({ allow_model_fallback: event.target.checked })} />
          Use as model fallback
        </label>
        <label className="field">
          <span>Route priority</span>
          <input type="number" min="0" step="1" disabled={!item.route_present} value={item.route_priority} onChange={(event) => update({ route_priority: event.target.value })} />
          <small>Lower values are tried first.</small>
        </label>
      </div>
    </article>
  );
}
