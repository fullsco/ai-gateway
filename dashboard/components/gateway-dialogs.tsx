"use client";

/**
 * The modal dialogs shared across resource views: a destructive-action confirmation,
 * the one-and-only-time a plaintext gateway key is shown, and the key list behind it.
 */

import { useState } from "react";
import { Clipboard, KeyRound, X } from "lucide-react";
import { Row } from "./gateway-format";
import { DataTable } from "./data-table";

export function ConfirmDialog({ title, message, busy, onCancel, onConfirm }: { title: string; message: string; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  return (
    <div className="dialog-backdrop">
      <section className="confirm-dialog" role="alertdialog" aria-modal="true">
        <h2>{title}</h2>
        <p>{message}</p>
        <div className="form-actions">
          <button onClick={onCancel}>Cancel</button>
          <button className="danger-button" disabled={busy} onClick={onConfirm}>
            {busy ? "Working..." : "Confirm"}
          </button>
        </div>
      </section>
    </div>
  );
}

export function OneTimeKey({ value, onClose }: { value: { key: string; prefix: string; client: string }; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="dialog-backdrop">
      <section className="secret-dialog" role="dialog" aria-modal="true">
        <KeyRound size={22} />
        <h2>Gateway key issued</h2>
        <p>This plaintext is shown once. Store it before dismissing this window.</p>
        <dl>
          <div>
            <dt>Client</dt>
            <dd>{value.client}</dd>
          </div>
          <div>
            <dt>Prefix</dt>
            <dd>{value.prefix}</dd>
          </div>
        </dl>
        <code>{value.key}</code>
        <div className="form-actions">
          <button
            onClick={async () => {
              await navigator.clipboard.writeText(value.key);
              setCopied(true);
            }}
          >
            <Clipboard size={15} />
            {copied ? "Copied" : "Copy key"}
          </button>
          <button className="primary" onClick={onClose}>
            I stored this key
          </button>
        </div>
      </section>
    </div>
  );
}

export function KeyManager({ value, busy, onClose, onRevoke, onRotate }: { value: { client: Row; rows: Row[] }; busy: string; onClose: () => void; onRevoke: (row: Row) => Promise<void>; onRotate: (row: Row) => Promise<void> }) {
  return (
    <div className="dialog-backdrop">
      <section className="editor key-manager" role="dialog" aria-modal="true">
        <div className="editor-head">
          <div>
            <h2>{String(value.client.name)} keys</h2>
            <p>Keys are immutable. Rotate active keys or revoke access permanently.</p>
          </div>
          <button className="icon-button" onClick={onClose}>
            <X size={18} />
          </button>
        </div>
        <DataTable
          rows={value.rows}
          columns={["key_prefix", "label", "expires_at", "enabled", "last_used_at", "created_at", "revoked_at", "revoke_reason"]}
          actions={(row) =>
            row.enabled ? (
              <>
                <button disabled={!!busy} onClick={() => void onRotate(row)}>
                  Rotate
                </button>
                <button className="danger-text" disabled={!!busy} onClick={() => void onRevoke(row)}>
                  Revoke
                </button>
              </>
            ) : (
              <span>Revoked</span>
            )
          }
        />
      </section>
    </div>
  );
}
