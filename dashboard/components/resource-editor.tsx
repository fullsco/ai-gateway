"use client";

/**
 * The generic add/edit dialog for any mutable resource, plus the two credential
 * operations that borrow it: rotating a secret, and recording an observed balance.
 *
 * Form options are loaded once for the dialog's lifetime and the submit button stays
 * disabled until they arrive, so a select can never be saved while it is still empty.
 */

import { FormEvent, useEffect, useState } from "react";
import { X } from "lucide-react";
import { gatewayApi as api } from "./gateway-api";
import { Row } from "./gateway-format";
import { ResourceView, resources } from "./gateway-resources";
import { Field, Fields } from "./resource-fields";
import { buildPayload } from "./resource-payload";

export function ResourceEditor({ view, row, onClose, onSaved }: { view: ResourceView; row?: Row; onClose: () => void; onSaved: (message: string) => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [references, setReferences] = useState<Record<string, Row[]>>({});
  const [referencesLoading, setReferencesLoading] = useState(true);
  const editing = !!row?.id && !row.__rotate && !row.__balance;
  useEffect(() => {
    let active = true;
    Promise.all([api("providers"), api("models"), api("provider-models"), api("routing-policies")])
      .then(([providers, models, mappings, policies]) => {
        if (active)
          setReferences({
            providers: providers.data ?? [],
            models: models.data ?? [],
            mappings: mappings.data ?? [],
            policies: policies.data ?? [],
          });
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "Unable to load form options.");
      })
      .finally(() => {
        if (active) setReferencesLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const body = buildPayload(view, data, row);
      const endpoint = view === "routing" ? "routes" : resources[view].endpoint;
      const path = row?.__rotate
        ? `credentials/${row.id}/rotate`
        : row?.__balance
          ? `credentials/${row.id}/balance`
          : editing ? `${endpoint}/${row?.id}` : endpoint;
      await api(path, {
        method: row?.__rotate ? "POST" : row?.__balance ? "PUT" : editing ? "PUT" : "POST",
        body: JSON.stringify(body),
      });
      await onSaved(
        row?.__rotate ? "Credential rotated securely."
        : row?.__balance ? "Balance recorded as an operator observation."
        : editing ? "Changes saved." : "Record created.",
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Save failed");
      setBusy(false);
    }
  }
  return (
    <div className="dialog-backdrop">
      <section className="editor" role="dialog" aria-modal="true" aria-labelledby="editor-title">
        <div className="editor-head">
          <div>
            <h2 id="editor-title">{row?.__rotate ? "Rotate credential" : row?.__balance ? "Record credential balance" : editing ? `Edit ${resources[view].title}` : `Add ${resources[view].title}`}</h2>
            <p>{row?.__rotate ? "The replacement secret is encrypted before persistence and is never returned." : row?.__balance ? "The gateway cannot discover a balance: the provider reports an identical placeholder ceiling for every credential. Enter the figure from the provider's own dashboard. It is stored as an operator observation, timestamped on the server, and does not affect routing." : "Changes affect the working configuration until published."}</p>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close editor">
            <X size={18} />
          </button>
        </div>
        <form onSubmit={submit}>
          {referencesLoading && !row?.__rotate && !row?.__balance ? (
            <div className="loading-state" role="status">
              Loading form options...
            </div>
          ) : row?.__rotate ? (
            <Field name="secret" label="Replacement secret" type="password" required />
          ) : row?.__balance ? (
            <>
              <Field name="amount" label="Balance remaining" type="number" min={0} step="any" defaultValue={row.balance_amount ?? ""} required />
              <Field name="currency" label="Currency" defaultValue={String(row.balance_currency ?? "USD")} required />
            </>
          ) : (
            <Fields view={view} row={row} references={references} />
          )}
          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}
          <div className="form-actions">
            <button type="button" onClick={onClose}>
              Cancel
            </button>
            <button className="primary" disabled={busy || (referencesLoading && !row?.__rotate && !row?.__balance)}>
              {busy ? "Saving..." : row?.__balance ? "Record balance" : editing ? "Save changes" : "Create record"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
