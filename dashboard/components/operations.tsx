"use client";

/**
 * The operational health view: what the running fleet is doing right now, read from
 * the gateway's own /operations/health probe rather than the working tables.
 *
 * Everything here is a live reading with an explicit absence when a signal was not
 * collected, and the scaling recommendation is shown as advisory only - the gateway
 * never authorizes a scale action from this endpoint, and neither does this screen.
 */

import { Row, State } from "./gateway-format";

/** A signal that should normally be zero; non-zero is worth an operator's eye. */
const unwanted = (value: unknown): State => (Number(value ?? 0) > 0 ? "warn" : "ok");

function seconds(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not measured";
  const total = Number(value);
  if (!Number.isFinite(total) || total < 0) return "Not measured";
  if (total < 90) return `${Math.round(total)} s`;
  if (total < 5400) return `${Math.round(total / 60)} min`;
  return `${Math.round(total / 3600)} h`;
}

function percent(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not measured";
  const ratio = Number(value);
  if (!Number.isFinite(ratio) || ratio < 0) return "Not measured";
  return `${(ratio * 100).toFixed(1)}%`;
}

// The warnings the probe can raise, in an operator's words. An unknown code is shown
// verbatim rather than hidden, so a new backend warning is never silently dropped.
const WARNINGS: Record<string, string> = {
  pool_exhausted: "Database connection pool is fully in use",
  database_unavailable: "Database could not be reached for this probe",
  health_events_dropped: "Passive health events were dropped before they could be written",
  workflow_queue_age: "A queued workflow has been waiting longer than a minute",
  unknown_usage: "Some requests have an unresolved usage outcome",
  settlement_backlog: "Settlements are waiting to be applied",
};

const DATABASE: Record<string, [string, State]> = {
  available: ["Available", "ok"],
  unavailable: ["Unavailable", "bad"],
  unconfigured: ["Not configured", "idle"],
};

// The scaling verdict the operating-envelope evaluator returned. It is advice, not an
// action: the gateway does not scale from this endpoint and marks it advisory-only.
const RECOMMENDATION: Record<string, [string, State]> = {
  hold: ["Hold - no change advised", "ok"],
  scale_out: ["Scale out advised (advisory only)", "warn"],
  scale_in: ["Scale in advised (advisory only)", "warn"],
  drain_review: ["Review for drain - utilization very low", "warn"],
  investigate: ["Investigate - a required signal is missing", "bad"],
};

export function Operations({ health, loading }: { health: Row; loading: boolean }) {
  if (loading && !Object.keys(health).length)
    return (
      <div className="loading-state" role="status">
        Loading operational health...
      </div>
    );

  const warnings = Array.isArray(health.warnings) ? (health.warnings as string[]) : [];
  const pool = (health.pool as Row | undefined) ?? undefined;
  const workflows = Array.isArray(health.workflows) ? (health.workflows as Row[]) : [];
  const accounting = (health.accounting as Row | undefined) ?? undefined;
  const capacity = (health.capacity as Row | undefined) ?? undefined;
  const envelope = (health.operating_envelope as Row | undefined) ?? undefined;
  const [databaseLabel, databaseState] = DATABASE[String(health.database)] ?? [String(health.database ?? "Unknown"), "idle" as State];
  const poolInUse = pool ? Number(pool.size ?? 0) - Number(pool.idle ?? 0) : null;
  const readiness: [string, State] = health.draining
    ? ["Draining", "warn"]
    : health.ready
      ? ["Ready", "ok"]
      : ["Not ready", "bad"];
  const recommendationRaw = envelope ? String(envelope.recommendation ?? "") : "";
  const [recommendationLabel, recommendationState] = RECOMMENDATION[recommendationRaw] ?? [recommendationRaw || "Unavailable", "idle" as State];
  const reasons = envelope && Array.isArray(envelope.reasons) ? (envelope.reasons as string[]) : [];

  return (
    <>
      <div className="strip">
        <div className={readiness[1]}>
          <span>Process</span>
          <strong>{readiness[0]}</strong>
        </div>
        <div className={databaseState}>
          <span>Database</span>
          <strong>{databaseLabel}</strong>
        </div>
        <div>
          <span>Active requests</span>
          <strong>{Number(health.active_requests ?? 0).toLocaleString()}</strong>
        </div>
        {pool && (
          <div className={poolInUse !== null && Number(pool.maximum ?? 0) > 0 && poolInUse >= Number(pool.maximum) ? "bad" : ""}>
            <span>DB pool in use</span>
            <strong>{poolInUse ?? 0} / {String(pool.maximum ?? 0)}</strong>
          </div>
        )}
        <div className={warnings.length ? "warn" : "ok"}>
          <span>Warnings</span>
          <strong>{warnings.length}</strong>
        </div>
      </div>

      <section className="ledger">
        <div className="section-head">
          <div>
            <h2>Live warnings</h2>
            <p>Raised by the gateway&apos;s own health probe for this observation. An empty list is the healthy case.</p>
          </div>
        </div>
        {warnings.length ? (
          <ul className="warning-list">
            {warnings.map((code) => (
              <li key={code} className={code === "database_unavailable" ? "state bad" : "state warn"}>
                {WARNINGS[code] ?? code.replaceAll("_", " ")}
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state"><strong>No warnings</strong><span>Nothing was flagged in this probe.</span></div>
        )}
      </section>

      {workflows.length > 0 && (
        <section className="ledger">
          <div className="section-head">
            <h2>Workflows in flight</h2>
          </div>
          <div className="exposure">
            {workflows.map((row) => (
              <div key={String(row.status)} className={row.status === "queued" && Number(row.oldest_seconds ?? 0) > 60 ? "warn" : ""}>
                <span>{String(row.status ?? "").replaceAll("_", " ")}</span>
                <strong>{Number(row.count ?? 0).toLocaleString()}</strong>
                {row.oldest_seconds !== undefined && <small>oldest {seconds(row.oldest_seconds)}</small>}
              </div>
            ))}
          </div>
        </section>
      )}

      {accounting && (
        <section className="ledger">
          <div className="section-head">
            <h2>Accounting backlog</h2>
          </div>
          <div className="exposure">
            <div className={unwanted(accounting.settlement_backlog)}>
              <span>Settlements pending</span>
              <strong>{Number(accounting.settlement_backlog ?? 0).toLocaleString()}</strong>
            </div>
            <div className={unwanted(accounting.unknown_usage)}>
              <span>Unresolved usage</span>
              <strong>{Number(accounting.unknown_usage ?? 0).toLocaleString()}</strong>
            </div>
            <div>
              <span>Oldest settlement</span>
              <strong>{seconds(accounting.oldest_settlement_seconds)}</strong>
            </div>
          </div>
        </section>
      )}

      {capacity && (
        <section className="ledger">
          <div className="section-head">
            <h2>Provider capacity</h2>
          </div>
          <div className="exposure">
            <div>
              <span>Peak provider utilization</span>
              <strong>{percent(capacity.provider_utilization)}</strong>
            </div>
          </div>
        </section>
      )}

      {envelope && (
        <section className="ledger">
          <div className="section-head">
            <div>
              <h2>Scaling recommendation</h2>
              <p>Advisory only. The gateway does not scale from this endpoint; treat this as guidance to confirm, not an authorized action.</p>
            </div>
            <span className={`state ${recommendationState}`}>{recommendationLabel}</span>
          </div>
          {reasons.length > 0 && (
            <ul className="warning-list">
              {reasons.map((reason) => (
                <li key={reason} className="state idle">{reason.replaceAll("_", " ")}</li>
              ))}
            </ul>
          )}
        </section>
      )}
    </>
  );
}
