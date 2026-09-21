"use client";

/**
 * One workflow job, end to end: where it is now, what it is waiting on, and the
 * ordered events that got it there.
 *
 * This is the read-only companion to the operator workflow list. It never drives a
 * transition - a stuck job is diagnosed here and acted on by the worker or the
 * client that owns it, not from this dialog.
 */

import { X } from "lucide-react";
import { Row, display, readable, stateOf } from "./gateway-format";

/** The job's status in the shared state vocabulary. */
function Status({ value }: { value: unknown }) {
  const state = stateOf(value, "status");
  return <span className={state ? `state ${state}` : "state plain"}>{display(value, "status")}</span>;
}

// What a checkpoint is telling us, in an operator's words. The worker records the
// step it reached and, when it paused, why - a pending tool call it needs a result
// for, or a failed/uncertain inference it parked for approval.
function CheckpointSummary({ checkpoint, status }: { checkpoint: Row; status: unknown }) {
  const step = checkpoint.step;
  const tool = checkpoint.tool as Row | undefined;
  const pending = Array.isArray(checkpoint.pending_tools) ? (checkpoint.pending_tools as unknown[]).length : 0;
  return (
    <div className="trace-summary">
      <div><span>Step reached</span><strong>{step === undefined ? "Not started" : String(step)}</strong></div>
      {status === "waiting_for_tool" && (
        <div className="warn">
          <span>Waiting for tool result</span>
          <strong>{tool && (tool.name || tool.id) ? readable(tool.name ?? tool.id) : "External tool"}</strong>
        </div>
      )}
      {pending > 0 && <div><span>Pending tool calls</span><strong>{pending}</strong></div>}
      {status === "waiting_for_approval" && (
        <div className="warn">
          <span>Paused for approval</span>
          <strong>{checkpoint.reason ? readable(checkpoint.reason) : "Uncertain outcome"}</strong>
        </div>
      )}
      {checkpoint.http_status !== undefined && <div><span>Last upstream status</span><strong>{String(checkpoint.http_status)}</strong></div>}
      {checkpoint.error_type !== undefined && <div className="bad"><span>Error</span><strong>{readable(checkpoint.error_type)}</strong></div>}
    </div>
  );
}

export function WorkflowDetail({ value, onClose }: { value: Row; onClose: () => void }) {
  const checkpoint = (value.checkpoint as Row | undefined) ?? {};
  const input = (value.input as Row | undefined) ?? {};
  const events = (value.events as Row[] | undefined) ?? [];
  return (
    <div className="dialog-backdrop">
      <section className="editor request-detail" role="dialog" aria-modal="true" aria-labelledby="workflow-detail-title">
        <div className="editor-head">
          <div>
            <h2 id="workflow-detail-title">Workflow job</h2>
            <p>{String(value.client_name ?? value.client_id ?? "Unknown client")} · {String(input.protocol ?? "unknown protocol")}</p>
            <Status value={value.status} />
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close workflow detail"><X size={18} /></button>
        </div>
        <div className="trace-summary">
          <div><span>Attempts</span><strong>{String(value.attempts ?? 0)} / {String(value.max_attempts ?? 0)}</strong></div>
          <div><span>Version</span><strong>{String(value.version ?? 0)}</strong></div>
          <div><span>Deadline</span><strong>{display(value.deadline, "deadline")}</strong></div>
          <div><span>Updated</span><strong>{display(value.updated_at, "updated_at")}</strong></div>
        </div>
        <h3>Where it is now</h3>
        <CheckpointSummary checkpoint={checkpoint} status={value.status} />
        <h3>Events</h3>
        {events.length ? (
          <ol className="attempt-list">
            {events.map((event, index) => (
              <li key={String(event.sequence ?? index)}>
                <div><strong>{String(event.kind ?? "event").replaceAll("_", " ")}</strong><span className="muted">#{String(event.sequence ?? index)}</span></div>
                {Boolean(event.created_at) && <p>{display(event.created_at, "created_at")}</p>}
              </li>
            ))}
          </ol>
        ) : (
          <div className="empty-state"><strong>No events recorded</strong><span>This job has not emitted any events yet.</span></div>
        )}
      </section>
    </div>
  );
}
