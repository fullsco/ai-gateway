"use client";

/**
 * One resource view: its table, its search, and every action that can be taken from a
 * row. Which actions exist is decided per view rather than per row type, because the
 * same table serves working configuration an operator may change and operational
 * records that are read-only.
 *
 * Every mutation goes through `mutate`, so a failure always surfaces as a notice with
 * the server's own sentence in it, and a second click cannot fire while one is in
 * flight.
 */

import { useEffect, useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { gatewayApi as api } from "./gateway-api";
import { Row, display } from "./gateway-format";
import { Notice, ResourceView, resources } from "./gateway-resources";
import { DataTable } from "./data-table";
import { ResourceEditor } from "./resource-editor";
import { ConfirmDialog, KeyManager, OneTimeKey } from "./gateway-dialogs";
import { RequestDetail } from "./request-trace";
import ProviderSetup from "./provider-setup";

export function Resource({ view, rows, loading, reload, notify }: { view: ResourceView; rows: Row[]; loading: boolean; reload: () => Promise<void>; notify: (notice: Notice) => void }) {
  const config = resources[view];
  const [search, setSearch] = useState("");
  const [editor, setEditor] = useState<{ row?: Row } | null>(null);
  const [confirm, setConfirm] = useState<{
    row: Row;
    action: "delete" | "rollback";
  } | null>(null);
  const [busy, setBusy] = useState("");
  const [issued, setIssued] = useState<{
    key: string;
    prefix: string;
    client: string;
  } | null>(null);
  const [keys, setKeys] = useState<{ client: Row; rows: Row[] } | null>(null);
  const [publishState, setPublishState] = useState<Row | null>(null);
  const [providerSetup, setProviderSetup] = useState<Row | null | undefined>(null);
  const [requestDetail, setRequestDetail] = useState<Row | null>(null);
  useEffect(() => {
    if (view === "configuration")
      void api("config/status")
        .then(setPublishState)
        .catch(() => setPublishState(null));
  }, [view, rows]);

  async function mutate(label: string, operation: () => Promise<unknown>) {
    if (busy) return false;
    setBusy(label);
    notify(null);
    try {
      await operation();
      await reload();
      notify({ kind: "success", message: `${label} completed.` });
    } catch (reason) {
      notify({
        kind: "error",
        message: reason instanceof Error ? reason.message : `${label} failed.`,
      });
      return false;
    } finally {
      setBusy("");
    }
    return true;
  }
  async function remove() {
    if (!confirm) return;
    const endpoint = view === "routing" ? "routes" : config.endpoint;
    const completed = await mutate("Delete", () => api(`${endpoint}/${String(confirm.row.id)}`, { method: "DELETE" }));
    if (completed) setConfirm(null);
  }
  async function issueKey(row: Row) {
    const label = window.prompt("Key label (optional)")?.trim() || null;
    const expiresAt = window.prompt("Expiry in ISO 8601 format (optional)")?.trim() || null;
    await mutate("Key issue", async () => {
      const result = await api(`clients/${row.id}/keys`, {
        method: "POST",
        body: JSON.stringify({ label, expires_at: expiresAt }),
      });
      setIssued({
        key: result.key,
        prefix: result.key_prefix,
        client: String(row.name),
      });
    });
  }
  async function rotateKey(key: Row) {
    const label = window.prompt("Replacement key label (optional)")?.trim() || null;
    const expiresAt = window.prompt("Replacement expiry in ISO 8601 format (optional)")?.trim() || null;
    await mutate("Key rotation", async () => {
      const result = await api(`client-keys/${key.id}/rotate`, {
        method: "POST",
        body: JSON.stringify({ label, expires_at: expiresAt }),
      });
      setIssued({ key: result.key, prefix: result.key_prefix, client: String(keys?.client.name ?? "Gateway client") });
      if (keys) {
        const refreshed = await api(`clients/${keys.client.id}/keys`);
        setKeys({ ...keys, rows: refreshed.data });
      }
    });
  }
  async function showKeys(row: Row) {
    setBusy("Load keys");
    try {
      setKeys({
        client: row,
        rows: (await api(`clients/${row.id}/keys`)).data,
      });
    } catch (reason) {
      notify({
        kind: "error",
        message: reason instanceof Error ? reason.message : "Unable to load keys",
      });
    } finally {
      setBusy("");
    }
  }
  async function publish() {
    if (!window.confirm("Publish the current working configuration and refresh the gateway runtime?")) return;
    await mutate("Configuration publish", () => api("config/publish", { method: "POST" }));
  }
  async function showRequest(row: Row) {
    setBusy("Request detail");
    try {
      setRequestDetail(await api(`requests/${row.id}`));
    } catch (reason) {
      notify({ kind: "error", message: reason instanceof Error ? reason.message : "Unable to load request detail" });
    } finally {
      setBusy("");
    }
  }
  async function rollback() {
    if (!confirm) return;
    const completed = await mutate("Configuration rollback", () => api(`config/versions/${confirm.row.id}/rollback`, { method: "POST" }));
    if (completed) setConfirm(null);
  }
  const actions = (row: Row) => (
    <div className="action-group">
      {config.mutable && (
        <>
          <button className="icon-button" title="Edit" aria-label={`Edit ${String(row.name ?? row.id)}`} onClick={() => setEditor({ row })}>
            <Pencil size={15} />
          </button>
          <button className="icon-button danger" title="Delete" aria-label={`Delete ${String(row.name ?? row.id)}`} onClick={() => setConfirm({ row, action: "delete" })}>
            <Trash2 size={15} />
          </button>
        </>
      )}
      {view === "credentials" && <button onClick={() => setEditor({ row: { ...row, __rotate: true } })}>Rotate</button>}
      {view === "credentials" && <button onClick={() => setEditor({ row: { ...row, __balance: true } })}>Record balance</button>}
      {view === "providers" && <button onClick={() => setProviderSetup(row)}>Configure</button>}
      {view === "clients" && (
        <>
          <button onClick={() => void issueKey(row)} disabled={!!busy}>
            Issue key
          </button>
          <button onClick={() => void showKeys(row)}>Keys</button>
        </>
      )}
      {view === "alerts" && row.status === "open" && <button onClick={() => void mutate("Alert acknowledge", () => api(`alerts/${row.id}/acknowledge`, { method: "POST" }))}>Acknowledge</button>}
      {view === "alerts" && row.status !== "resolved" && <button onClick={() => void mutate("Alert resolve", () => api(`alerts/${row.id}/resolve`, { method: "POST" }))}>Resolve</button>}
      {view === "configuration" && row.status !== "published" && <button onClick={() => setConfirm({ row, action: "rollback" })}>Rollback</button>}
      {view === "configuration" && <span className="immutable-label">Snapshot immutable</span>}
      {view === "requests" && <button onClick={() => void showRequest(row)} disabled={!!busy}>Trace request</button>}
    </div>
  );
  const filteredRows = search.trim() ? rows.filter((row) => Object.values(row).some((value) => display(value, "").toLowerCase().includes(search.trim().toLowerCase()))) : rows;
  return (
    <section className="ledger">
      {view === "configuration" && publishState && (
        <div className={`publish-state ${publishState.has_unpublished_changes ? "draft" : "published"}`}>
          <strong>{publishState.has_unpublished_changes ? "Unpublished working changes" : "Working configuration matches production"}</strong>
          <span>Active snapshot {String(publishState.active_version ?? "None")}. Publishing creates a new immutable snapshot and activates it after runtime refresh.</span>
          {Boolean(publishState.has_unpublished_changes) && Array.isArray(publishState.changes) && (publishState.changes as Row[]).length > 0 && (
            <div className="change-review">
              <span className="change-review-head">{Number(publishState.change_count ?? (publishState.changes as Row[]).length)} change{Number(publishState.change_count ?? (publishState.changes as Row[]).length) === 1 ? "" : "s"} will become active when you publish</span>
              <ul>
                {(publishState.changes as Row[]).map((entry, index) => (
                  <li key={index} className={`change-${String(entry.change)}`}>
                    <span className="change-mark" aria-hidden="true">{entry.change === "added" ? "+" : entry.change === "removed" ? "-" : "~"}</span>
                    <span className="change-copy"><strong>{String(entry.resource)}</strong>{String(entry.summary)}</span>
                  </li>
                ))}
              </ul>
              {Number(publishState.change_count ?? 0) > (publishState.changes as Row[]).length && (
                <span className="change-more">Showing the first {(publishState.changes as Row[]).length} of {String(publishState.change_count)} changes.</span>
              )}
            </div>
          )}
          {Boolean(publishState.has_unpublished_changes) && (!Array.isArray(publishState.changes) || (publishState.changes as Row[]).length === 0) && (
            // The gateway guarantees a claimed change can be named, so this is a
            // fallback rather than the normal path. It must still be honest: "the
            // initial configuration" is only true before anything has been published,
            // and an unitemised difference has to say so rather than imply an empty
            // draft is reviewable.
            <span>
              {publishState.active_version == null
                ? "Nothing has been published yet. Publishing creates the first snapshot."
                : Array.isArray(publishState.changed_sections) && (publishState.changed_sections as string[]).length > 0
                  ? `Changes affect: ${(publishState.changed_sections as string[]).join(", ")}.`
                  : "The working configuration differs from the published snapshot, but the difference could not be itemised. Review before publishing."}
            </span>
          )}
        </div>
      )}
      <div className="section-head">
        <div>
          <h2>{config.title}</h2>
          <p>{config.mutable ? "Working configuration. Changes remain drafts until explicitly published." : view === "configuration" ? "Published snapshots are immutable; rollback creates a new active version." : "Operational records are read-only."}</p>
        </div>
        <div className="section-actions">
          {rows.length > 0 && (
            <label className="search-field">
              <span className="sr-only">Search {config.title}</span>
              <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={`Search ${config.title.toLowerCase()}`} />
            </label>
          )}
          {config.mutable && (
            <button onClick={() => setEditor({})}>
              <Plus size={15} />
              Add record
            </button>
          )}
          {view === "providers" && <button className="primary" onClick={() => setProviderSetup(undefined)}><Plus size={15} />Guided setup</button>}
          {view === "configuration" && (
            <button className="primary" disabled={!!busy || publishState?.has_unpublished_changes === false} onClick={() => void publish()}>
              {busy ? "Publishing..." : "Review and publish"}
            </button>
          )}
        </div>
      </div>
      {loading ? (
        <div className="loading-state" role="status">
          Loading {config.title.toLowerCase()}...
        </div>
      ) : (
        <DataTable rows={filteredRows} columns={config.columns} actions={actions} />
      )}
      {editor && (
        <ResourceEditor
          view={view}
          row={editor.row}
          onClose={() => setEditor(null)}
          onSaved={async (message) => {
            setEditor(null);
            await reload();
            notify({ kind: "success", message });
          }}
        />
      )}
      {confirm && <ConfirmDialog title={confirm.action === "delete" ? "Delete record" : "Rollback configuration"} message={confirm.action === "delete" ? `Delete ${String(confirm.row.name ?? confirm.row.id)}? Related records may also be removed.` : `Make version ${String(confirm.row.id)} the active configuration?`} busy={!!busy} onCancel={() => setConfirm(null)} onConfirm={() => void (confirm.action === "delete" ? remove() : rollback())} />}
      {issued && <OneTimeKey value={issued} onClose={() => setIssued(null)} />}
      {keys && (
        <KeyManager
          value={keys}
          busy={busy}
          onClose={() => setKeys(null)}
          onRevoke={async (key) => {
            const reason = window.prompt("Revocation reason (optional)");
            if (reason === null) return;
            if (!window.confirm(`Revoke key ${String(key.key_prefix)}? This cannot be undone.`)) return;
            await mutate("Key revocation", () =>
              api(`client-keys/${key.id}/revoke`, {
                method: "POST",
                body: JSON.stringify({ reason: reason.trim() || null }),
              }),
            );
            const refreshed = await api(`clients/${keys.client.id}/keys`);
            setKeys({ ...keys, rows: refreshed.data });
          }}
          onRotate={rotateKey}
        />
      )}
      {requestDetail && <RequestDetail value={requestDetail} onClose={() => setRequestDetail(null)} />}
      {providerSetup !== null && <ProviderSetup provider={providerSetup ?? undefined} onClose={() => setProviderSetup(null)} onSaved={async () => { setProviderSetup(null); await reload(); notify({ kind: "success", message: "Provider configuration reconciled. Review and publish when ready." }); }} />}
    </section>
  );
}
