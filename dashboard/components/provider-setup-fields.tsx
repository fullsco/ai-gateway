"use client";

/**
 * The repeated editors inside the guided provider form: a name/value pair list, and
 * one credential card.
 *
 * Both are rendered many times over on the same screen, so every control that can be
 * removed carries a label naming which one it removes - an unlabelled delete button in
 * a stack of eight identical cards is a guess.
 */

import { Plus, Trash2 } from "lucide-react";
import { CredentialDraft } from "./provider-setup-drafts";

type Pair = { name: string; value: string };

export function PairEditor({ label, items, onChange, empty }: { label: string; items: Pair[]; onChange: (items: Pair[]) => void; empty: string }) {
  const update = (index: number, patch: Partial<Pair>) =>
    onChange(items.map((entry, position) => (position === index ? { ...entry, ...patch } : entry)));
  return (
    <div className="pair-editor full">
      <div className="subsection-head">
        <div>
          <strong>{label}</strong>
          <p>{items.length ? `${items.length} configured` : empty}</p>
        </div>
        <button type="button" onClick={() => onChange([...items, { name: "", value: "" }])}>
          <Plus size={14} />
          Add
        </button>
      </div>
      {items.map((item, index) => (
        <div className="pair-row" key={index}>
          <label className="field">
            <span>Name</span>
            <input value={item.name} onChange={(event) => update(index, { name: event.target.value })} />
          </label>
          <label className="field">
            <span>Value</span>
            <input value={item.value} onChange={(event) => update(index, { value: event.target.value })} />
          </label>
          <button
            type="button"
            className="icon-button danger"
            onClick={() => onChange(items.filter((_, position) => position !== index))}
            aria-label={`Remove ${label.toLowerCase()} row ${index + 1}`}
          >
            <Trash2 size={15} />
          </button>
        </div>
      ))}
    </div>
  );
}

export function CredentialCard({ item, index, update, remove }: { item: CredentialDraft; index: number; update: (patch: Partial<CredentialDraft>) => void; remove: () => void }) {
  return (
    <div className="repeater">
      <label className="field">
        <span>Label</span>
        <input value={item.name} onChange={(event) => update({ name: event.target.value })} required readOnly={item.existing} />
      </label>
      <label className="check-control">
        <input type="checkbox" checked={item.enabled} onChange={(event) => update({ enabled: event.target.checked })} />
        Credential enabled
      </label>
      {item.existing && (
        <label className="check-control">
          <input type="checkbox" checked={item.rotate_secret} onChange={(event) => update({ rotate_secret: event.target.checked, secret: "" })} />
          Rotate secret
        </label>
      )}
      <label className="field">
        <span>{item.existing ? "New secret" : "Secret"}</span>
        <input
          type="password"
          value={item.secret}
          disabled={item.existing && !item.rotate_secret}
          onChange={(event) => update({ secret: event.target.value })}
          required={!item.existing || item.rotate_secret}
        />
      </label>
      <label className="field">
        <span>Routing priority</span>
        <input type="number" min="0" step="1" value={item.priority} onChange={(event) => update({ priority: event.target.value })} />
        <small>Lower values are preferred when credentials are otherwise equally healthy.</small>
      </label>
      <label className="field">
        <span>Requests per minute</span>
        <input type="number" min="1" step="1" value={item.requests_per_minute} onChange={(event) => update({ requests_per_minute: event.target.value })} />
        <small>Leave blank for provider-managed limits.</small>
      </label>
      <label className="field">
        <span>Tokens per minute</span>
        <input type="number" min="1" step="1" value={item.tokens_per_minute} onChange={(event) => update({ tokens_per_minute: event.target.value })} />
        <small>Maximum token throughput for this credential.</small>
      </label>
      <label className="field">
        <span>Quota limit</span>
        <input type="number" min="0" step="any" value={item.quota_limit} onChange={(event) => update({ quota_limit: event.target.value })} />
        <small>Optional provider quota amount.</small>
      </label>
      <label className="field">
        <span>Quota warning threshold</span>
        <input type="number" min="0.01" max="1" step="0.01" value={item.quota_threshold} onChange={(event) => update({ quota_threshold: event.target.value })} />
        <small>Temporarily stop using it at this share of quota.</small>
      </label>
      <button type="button" className="icon-button danger remove-control" onClick={remove} aria-label={`Remove credential ${item.name || index + 1}`}>
        <Trash2 size={15} />
      </button>
    </div>
  );
}
