"use client";

/**
 * The landing view: today's traffic, then what the published runtime is actually
 * serving. The distinction is the point - the working tables can say one thing while
 * the runtime serves another, so this panel reads from the runtime alone.
 */

import { Row, State } from "./gateway-format";

/** A metric: label, figure, and how the figure reads. */
export type Metric = [string, string, State?];

/** A count that must be above zero for the runtime to serve anything at all. */
const needed = (value: unknown): State => (Number(value ?? 0) > 0 ? "ok" : "bad");
/** A count that is normally zero, and worth an operator's eye when it is not. */
const unwanted = (value: unknown): State => (Number(value ?? 0) > 0 ? "warn" : "");

export function Overview({ runtime, metrics, loading }: { runtime: Row; metrics: Metric[]; loading: boolean }) {
  if (loading && !Object.keys(runtime).length)
    return (
      <div className="loading-state" role="status">
        Loading runtime status...
      </div>
    );
  return (
    <>
      <div className="strip">
        {metrics.map(([label, value, state]) => (
          <div key={label} className={state ?? undefined}>
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
          <span className={runtime.runtime_ready ? "state ok" : "state bad"}>{runtime.runtime_ready ? "Ready" : "Not ready"}</span>
        </div>
        <div className="exposure">
          <div>
            <span>Active snapshot</span>
            <strong>{String(runtime.config_version ?? "None")}</strong>
          </div>
          {/* A runtime with no healthy provider and no active credential serves nothing,
              which the figures reported as two calm zeroes among four. */}
          <div className={needed(runtime.healthy_providers)}>
            <span>Healthy providers</span>
            <strong>{String(runtime.healthy_providers ?? 0)}</strong>
          </div>
          <div className={needed(runtime.active_keys)}>
            <span>Active credentials</span>
            <strong>{String(runtime.active_keys ?? 0)}</strong>
          </div>
          <div className={unwanted(runtime.keys_in_cooldown)}>
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
