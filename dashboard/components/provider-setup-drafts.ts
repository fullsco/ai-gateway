/**
 * The shapes the guided provider form edits, and the rules a draft must satisfy
 * before it is sent.
 *
 * Everything the operator types is held as a string, because a half-typed number is
 * not a number and clearing a field has to mean "no value" rather than zero. The
 * conversion happens once, on save, after `validateSetup` has had its say.
 */

import { GatewayRow } from "./gateway-api";
import { PROTOCOLS } from "./gateway-protocols";

export type CredentialDraft = {
  existing: boolean; name: string; secret: string; rotate_secret: boolean; enabled: boolean;
  requests_per_minute: string; tokens_per_minute: string; quota_limit: string;
  quota_threshold: string; priority: string;
};
export type MappingDraft = {
  model_id: string; display_name: string; aliases: string; context_window: string; model_enabled: boolean; model_capabilities: string[];
  upstream_model_id: string; protocol: string; serves_protocols: string[]; capabilities: string[]; enabled: boolean;
  max_concurrency: string; priority: string; weight: string; settings_json: string;
  input_price: string; output_price: string; cached_price: string; currency: string;
  pricing_metadata: GatewayRow; route_present: boolean; route_enabled: boolean;
  route_priority: string; allow_model_fallback: boolean;
};

// The select value that means "an id the catalogue does not hold yet". It is not a
// legal model id, so it cannot collide with a real one.
export const NEW_MODEL = "__new__";

export const capabilities = [
  ["streaming", "Streaming"], ["tool_calling", "Tool calling"], ["reasoning", "Reasoning"],
  ["vision", "Vision"], ["structured_output", "Structured output"], ["computer_use", "Computer use"],
] as const;
export const protocols = PROTOCOLS;

export const emptyCredential = (): CredentialDraft => ({
  existing: false, name: "", secret: "", rotate_secret: true, enabled: true,
  requests_per_minute: "", tokens_per_minute: "", quota_limit: "", quota_threshold: "0.95", priority: "100",
});
export const emptyMapping = (): MappingDraft => ({
  model_id: "", display_name: "", aliases: "", context_window: "", model_enabled: true, model_capabilities: ["streaming"],
  upstream_model_id: "", protocol: "anthropic_messages", serves_protocols: ["anthropic_messages"], capabilities: ["streaming"], enabled: true,
  max_concurrency: "8", priority: "100", weight: "1", settings_json: "{}", input_price: "", output_price: "",
  cached_price: "", currency: "USD", pricing_metadata: {}, route_present: true, route_enabled: true,
  route_priority: "100", allow_model_fallback: false,
});
export const text = (value: unknown, fallback = "") => String(value ?? fallback);
export const list = (value: unknown) => Array.isArray(value) ? value.map(String) : [];
export const object = (value: unknown): GatewayRow => value && typeof value === "object" && !Array.isArray(value) ? { ...(value as GatewayRow) } : {};

export function parsePairs(value: unknown): { name: string; value: string }[] {
  const source = object(value);
  return Object.entries(source).map(([name, entry]) => ({ name, value: typeof entry === "string" ? entry : JSON.stringify(entry) }));
}

export function parseParameterValue(value: string): unknown {
  try {
    const parsed = JSON.parse(value);
    return typeof parsed === "object" && parsed !== null ? value : parsed;
  } catch {
    return value;
  }
}

function numberValue(value: string, label: string, options: { optional?: boolean; integer?: boolean; min?: number; max?: number } = {}) {
  if (!value.trim() && options.optional) return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || (options.integer && !Number.isInteger(parsed)) || (options.min !== undefined && parsed < options.min) || (options.max !== undefined && parsed > options.max))
    throw new Error(`${label} must be ${options.integer ? "a whole " : "a valid "}number${options.min !== undefined ? ` of at least ${options.min}` : ""}.`);
  return parsed;
}

/**
 * Throw the first thing wrong with the draft, named well enough to fix.
 *
 * Every message identifies the field and the card it belongs to, because the form
 * shows many identical cards and "priority must be a whole number" applies to any
 * of them.
 */
export function validateSetup(draft: { credentials: CredentialDraft[]; mappings: MappingDraft[]; providerPriority: string; timeout: string }) {
  const { credentials, mappings, providerPriority, timeout } = draft;
  const labels = credentials.map((item) => item.name.trim().toLocaleLowerCase());
  const repeatedLabel = labels.find((label, index) => label && labels.indexOf(label) !== index);
  if (repeatedLabel) throw new Error(`Two credentials are named "${repeatedLabel}". Rename one of them - credential names must be unique.`);
  const keys = mappings.map((item) => ({ key: `${item.model_id.trim()}\u0000${item.upstream_model_id.trim()}\u0000${item.protocol}`.toLocaleLowerCase(), item }));
  const repeatedMapping = keys.find((entry, index) => entry.key && keys.findIndex((other) => other.key === entry.key) !== index);
  if (repeatedMapping) {
    // Naming the exact combination turns a refusal into a fix: the operator
    // knows which card pair to collapse instead of diffing every card.
    const { item } = repeatedMapping;
    throw new Error(
      `Two mappings describe the same model "${item.model_id.trim()}" with upstream ` +
        `"${item.upstream_model_id.trim()}" over ${item.protocol.replaceAll("_", " ")}. ` +
        `Remove one of the two cards - only one can be saved.`,
    );
  }
  const aliases = Array.from(new Map(mappings.map((item) => [item.model_id.trim(), item.aliases])).values())
    .flatMap((value) => value.split(",").map((alias) => alias.trim().toLocaleLowerCase()).filter(Boolean));
  if (aliases.some((alias, index) => aliases.indexOf(alias) !== index)) throw new Error("Canonical model aliases must be unique.");
  credentials.forEach((item) => {
    numberValue(item.priority, `Priority for ${item.name}`, { integer: true, min: 0 });
    numberValue(item.quota_threshold, `Quota threshold for ${item.name}`, { min: Number.MIN_VALUE, max: 1 });
    numberValue(item.quota_limit, `Quota limit for ${item.name}`, { optional: true, min: 0 });
    numberValue(item.requests_per_minute, `Requests per minute for ${item.name}`, { optional: true, integer: true, min: 1 });
    numberValue(item.tokens_per_minute, `Tokens per minute for ${item.name}`, { optional: true, integer: true, min: 1 });
    if ((!item.existing || item.rotate_secret) && !item.secret) throw new Error(`Enter a secret for credential ${item.name}.`);
  });
  mappings.forEach((item) => {
    numberValue(item.context_window, `Context window for ${item.model_id}`, { optional: true, integer: true, min: 1 });
    numberValue(item.max_concurrency, `Maximum concurrency for ${item.upstream_model_id}`, { integer: true, min: 1 });
    numberValue(item.priority, `Mapping priority for ${item.upstream_model_id}`, { integer: true, min: 0 });
    numberValue(item.weight, `Mapping weight for ${item.upstream_model_id}`, { min: Number.MIN_VALUE });
    numberValue(item.route_priority, `Route priority for ${item.upstream_model_id}`, { integer: true, min: 0 });
    const hasPricing = Boolean(item.input_price.trim() || item.output_price.trim() || item.cached_price.trim());
    if (hasPricing && (!item.input_price.trim() || !item.output_price.trim() || !item.currency.trim())) throw new Error(`Pricing for ${item.upstream_model_id} requires input, output, and currency.`);
    if (hasPricing && !/^[A-Za-z]{3}$/.test(item.currency.trim())) throw new Error(`Pricing currency for ${item.upstream_model_id} must be a three-letter code.`);
    [item.input_price, item.output_price, item.cached_price].filter(Boolean).forEach((value) => numberValue(value, `Pricing for ${item.upstream_model_id}`, { min: 0 }));
    try {
      const settings = JSON.parse(item.settings_json || "{}");
      if (!settings || typeof settings !== "object" || Array.isArray(settings)) throw new Error();
    } catch {
      throw new Error(`Transport settings for ${item.upstream_model_id} must be a JSON object.`);
    }
  });
  numberValue(providerPriority, "Provider priority", { integer: true, min: 0 });
  numberValue(timeout, "Provider timeout", { min: Number.MIN_VALUE });
}
