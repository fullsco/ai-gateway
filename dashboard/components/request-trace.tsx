"use client";

/**
 * One request, end to end: what was attempted, what was skipped, and what it cost.
 *
 * This is the view an operator opens when a request failed and the summary row does
 * not explain it. It answers the two questions the row cannot: which candidates the
 * router considered, and why each one it rejected was rejected.
 */

import { X } from "lucide-react";
import { Row, display, formatCurrencyTotals, readable, stateOf } from "./gateway-format";
import { explainError, explainExclusion } from "./gateway-explain";

/** An attempt's or a request's outcome, drawn in the shared state vocabulary. */
function Outcome({ value }: { value: unknown }) {
  const state = stateOf(value, "status");
  return <span className={state ? `state ${state}` : "state plain"}>{display(value, "status")}</span>;
}

/** Why this request went where it went, one block per attempt. */
function RoutingDecision({ attempts }: { attempts: Row[] }) {
  if (!attempts.length) {
    return <p className="muted">No routing decision was recorded for this request.</p>;
  }
  return (
    <div className="routing-decision">
      {attempts.map((attempt, index) => {
        const considered = (attempt.considered as Row[] | undefined) ?? [];
        const selected = attempt.selected as Row | undefined;
        const eligible = considered.filter((row) => row.eligible === true);
        const excluded = considered.filter((row) => row.eligible !== true);
        return (
          <section key={index} className="routing-attempt">
            <header>
              <strong>Attempt {String(attempt.attempt_number ?? index + 1)}</strong>
              {attempt.is_fallback === true ? (
                <span className="state warn">Fallback after {explainError(attempt.fallback_reason)}</span>
              ) : null}
            </header>
            {selected ? (
              <p className="routing-selected">
                Chose <strong>{readable(selected.provider)}</strong> using credential{" "}
                <strong>{readable(selected.credential_name)}</strong>
                {selected.score !== undefined ? ` (score ${Number(selected.score).toFixed(2)})` : ""}
                {eligible.length > 1 ? `, the best of ${eligible.length} eligible` : ""}.
              </p>
            ) : (
              <p className="routing-selected">
                Nothing was eligible, so no provider was contacted.
              </p>
            )}
            {excluded.length ? (
              <table className="routing-excluded">
                <caption>{excluded.length} candidate{excluded.length === 1 ? "" : "s"} skipped</caption>
                <thead>
                  <tr><th>Provider</th><th>Credential</th><th>Why it was skipped</th></tr>
                </thead>
                <tbody>
                  {excluded.map((row, position) => (
                    <tr key={position}>
                      <td>{readable(row.provider)}</td>
                      <td>{row.credential_name ? readable(row.credential_name) : "Whole route"}</td>
                      <td>{explainExclusion(row.reason)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}

export function RequestDetail({ value, onClose }: { value: Row; onClose: () => void }) {
  const request = (value.request as Row | undefined) ?? {};
  const attempts = (value.attempts as Row[] | undefined) ?? [];
  const usage = (value.usage as Row[] | undefined) ?? [];
  const routing = (value.routing as Row[] | undefined) ?? [];
  const totals = usage.reduce<{ input: number; output: number; cached: number }>(
    (result, row) => ({
      input: result.input + Number(row.input_tokens ?? 0),
      output: result.output + Number(row.output_tokens ?? 0),
      cached: result.cached + Number(row.cached_tokens ?? 0),
    }),
    { input: 0, output: 0, cached: 0 },
  );
  return (
    <div className="dialog-backdrop">
      <section className="editor request-detail" role="dialog" aria-modal="true" aria-labelledby="request-detail-title">
        <div className="editor-head">
          <div>
            <h2 id="request-detail-title">Request trace</h2>
            <p>{String(request.resolved_model ?? request.requested_model ?? "Unknown model")}</p>
            <Outcome value={request.status} />
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close request trace"><X size={18} /></button>
        </div>
        <div className="trace-summary">
          <div><span>Total latency</span><strong>{display(request.latency_ms, "latency_ms")}</strong></div>
          <div><span>Retries</span><strong>{String(request.retry_count ?? 0)}</strong></div>
          <div><span>Fallbacks used</span><strong>{String(request.fallback_count ?? 0)}</strong></div>
          {/* A request that only succeeded because a fallback caught it is not the same
              outcome as one that succeeded outright, so it does not read as green. */}
          <div className={request.status === "succeeded" && Number(request.fallback_count ?? 0) > 0 ? "warn" : stateOf(request.status, "status")}>
            <span>Final result</span>
            <strong>{request.status === "succeeded" && Number(request.fallback_count ?? 0) > 0 ? "Succeeded through fallback" : display(request.status, "status")}</strong>
          </div>
        </div>
        <h3>Routing attempts</h3>
        {attempts.length ? <ol className="attempt-list">{attempts.map((attempt, index) => <li key={String(attempt.id ?? index)}>
          <div><strong>{String(attempt.provider_name ?? `Provider attempt ${index + 1}`)}</strong><Outcome value={attempt.status} /></div>
          <p>{attempt.credential_name ? `Credential: ${String(attempt.credential_name)}` : "Credential name unavailable"} · {display(attempt.latency_ms, "latency_ms")}</p>
          {Boolean(attempt.upstream_status) && <p>Upstream response: {String(attempt.upstream_status)}</p>}
          {Boolean(attempt.error_category) && <p className="trace-error">{explainError(attempt.error_category)}</p>}
          {Boolean(attempt.response_committed) && <p>The response had already started, so the Gateway could not safely retry.</p>}
        </li>)}</ol> : <div className="empty-state"><strong>No provider attempts recorded</strong><span>The request ended before an upstream attempt was persisted.</span></div>}
        <h3>Why this routing was chosen</h3>
        <RoutingDecision attempts={routing} />
        <h3>Token usage</h3>
        <div className="trace-summary">
          <div><span>Input tokens</span><strong>{totals.input.toLocaleString()}</strong></div>
          <div><span>Output tokens</span><strong>{totals.output.toLocaleString()}</strong></div>
          <div><span>Cached tokens</span><strong>{totals.cached.toLocaleString()}</strong></div>
          <div><span>Cost</span><strong>{formatCurrencyTotals(usage)}</strong></div>
        </div>
      </section>
    </div>
  );
}
