"use client";

/**
 * The table every view renders through, and the analytics stack that is really a
 * dozen of them.
 *
 * `DataTable` and `AnalyticsTables` stay in one module because they call each other:
 * the analytics endpoint answers with a single aggregate row rather than a list, and
 * that row is drawn as strips plus eight nested tables. Splitting them would buy a
 * circular import and nothing else.
 */

import { ReactNode } from "react";
import { Row, columnLabel, display, formatCurrencyTotals, isNumeric, stateOf } from "./gateway-format";
import { explainError } from "./gateway-explain";

/**
 * One cell.
 *
 * Split out because the alternative was a single 300-character line that called
 * `display()` twice - once for the title attribute and once for the text - and derived
 * its class from a hard-coded list of three column names. State now comes from
 * `stateOf`, so every column that reports a condition is drawn like every other one.
 */
function Cell({ row, column }: { row: Row; column: string }) {
  const shown = column === "error_category" ? explainError(row[column]) : display(row[column], column, row);
  const state = stateOf(row[column], column);
  return (
    <td data-label={columnLabel(column)} className={isNumeric(column) ? "numeric" : undefined}>
      <span title={shown} className={state ? `value status-value ${state}` : "value"}>
        {shown}
      </span>
    </td>
  );
}

export function DataTable({ rows, columns, actions }: { rows: Row[]; columns: string[]; actions?: (row: Row) => ReactNode }) {
  if (rows.length === 1 && rows[0].usage_by_model) return <AnalyticsTables payload={rows[0]} />;
  if (!rows.length)
    return (
      <div className="empty-state">
        <strong>No matching records</strong>
        <span>Try a different search, or add a record when this resource is configurable.</span>
      </div>
    );
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} className={isNumeric(column) ? "numeric" : undefined}>
                {columnLabel(column)}
              </th>
            ))}
            {actions && <th className="action-heading">Actions</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={String(row.id ?? index)}>
              {columns.map((column) => (
                <Cell key={column} row={row} column={column} />
              ))}
              {actions && (
                <td data-label="Actions" className="row-actions">
                  {actions(row)}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AnalyticsTables({ payload }: { payload: Row }) {
  const usage = payload.usage as Row | undefined;
  const total = Number(usage?.usage_records ?? 0);
  const coverage = total ? `${((Number(usage?.priced_records ?? 0) / total) * 100).toFixed(1)}%` : "No usage";
  const failover = payload.failover as Row | undefined;
  return (
    <div className="analytics-stack">
      <div className="strip">
        <div>
          <span>Input tokens</span>
          <strong>{Number(usage?.input_tokens ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Output tokens</span>
          <strong>{Number(usage?.output_tokens ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Pricing coverage</span>
          <strong>{coverage}</strong>
        </div>
        <div>
          <span>Estimated cost</span>
          <strong>{formatCurrencyTotals(payload.costs_by_currency)}</strong>
        </div>
      </div>
      <div className="strip">
        <div>
          <span>Upstream retries</span>
          <strong>{Number(failover?.retries ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Provider fallbacks</span>
          <strong>{Number(failover?.fallbacks ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Requests retried</span>
          <strong>{Number(failover?.requests_with_retries ?? 0).toLocaleString()}</strong>
        </div>
        <div>
          <span>Requests with fallback</span>
          <strong>{Number(failover?.requests_with_fallbacks ?? 0).toLocaleString()}</strong>
        </div>
      </div>
      <h3>Request activity by model</h3>
      <DataTable rows={(payload.by_model as Row[]) ?? []} columns={["model", "requests", "succeeded", "failed", "average_latency_ms"]} />
      <h3>Provider attempts</h3>
      <DataTable rows={(payload.attempts_by_provider as Row[]) ?? []} columns={["provider", "attempts", "succeeded", "failed", "cancelled", "average_latency_ms", "p95_latency_ms"]} />
      <h3>Model attempts</h3>
      <DataTable rows={(payload.attempts_by_model as Row[]) ?? []} columns={["model", "attempts", "succeeded", "failed", "cancelled", "average_latency_ms", "p95_latency_ms"]} />
      <h3>Route attempts</h3>
      <DataTable rows={(payload.attempts_by_route as Row[]) ?? []} columns={["route", "attempts", "succeeded", "failed", "cancelled", "average_latency_ms", "p95_latency_ms"]} />
      <h3>Usage by provider</h3>
      <DataTable rows={(payload.usage_by_provider as Row[]) ?? []} columns={["provider", "usage_records", "input_tokens", "output_tokens", "cached_tokens", "priced_records"]} />
      <h3>Usage by model</h3>
      <DataTable rows={(payload.usage_by_model as Row[]) ?? []} columns={["model", "usage_records", "input_tokens", "output_tokens", "cached_tokens", "priced_records"]} />
      <h3>Usage by route</h3>
      <DataTable rows={(payload.usage_by_route as Row[]) ?? []} columns={["route", "usage_records", "input_tokens", "output_tokens", "cached_tokens", "priced_records"]} />
      <h3>Daily activity</h3>
      <DataTable rows={(payload.daily as Row[]) ?? []} columns={["day", "requests", "succeeded", "failed", "input_tokens", "output_tokens", "priced_records"]} />
    </div>
  );
}
