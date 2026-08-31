export type GatewayRow = Record<string, unknown>;

function readable(value: unknown): string {
  if (typeof value === "string") return value.replaceAll("_", " ");
  if (value && typeof value === "object" && "message" in value) return String(value.message);
  try { return JSON.stringify(value); } catch { return "Request failed"; }
}

// What each reconcile guard reason means, and what to do about it. These are refusals
// to overwrite something deliberate, so each one names the thing that is in the way.
const RECONCILE_REASONS: Record<string, string> = {
  selective_credential_access:
    "some credentials on this provider are restricted to particular models, and saving here would give every credential access to every model. Clear the per-credential model restrictions first, or edit this provider outside the setup form.",
  selective_pool_membership:
    "this provider's pool has hand-picked members, and saving here would rebuild it with every credential. Clear the pool membership first, or edit outside the setup form.",
  custom_route_pool:
    "a route for this provider uses a pool this form does not manage. Point the route at the provider's own pool, or edit outside the setup form.",
  custom_pool_configuration:
    "this provider's pool carries settings this form does not manage, such as being bound to a single model. Reset those settings, or edit outside the setup form.",
  member_operational_state:
    "a pool member is disabled or draining, and saving here would re-enable it. Finish or undo the drain first.",
};

// A shared model's metadata belongs to the catalogue, not to one provider, so the
// refusal names the field and both values rather than leaving the operator to guess.
function sharedModelConflict(data: GatewayRow): string {
  const field = String(data.field ?? "").replaceAll("_", " ");
  const shared = Array.isArray(data.shared_with) ? data.shared_with.join(", ") : "";
  const show = (value: unknown) =>
    value === null || value === undefined || value === ""
      ? "empty"
      : Array.isArray(value) ? value.join(", ") : String(value);
  const where = shared ? ` It is also served by ${shared}.` : "";
  return (
    `"${data.model_id}" already exists in the shared model catalogue and its ${field} ` +
    `does not match: it is currently ${show(data.current)} and this form would set it ` +
    `to ${show(data.requested)}.${where} Either keep the existing value, or change it ` +
    `for every provider from the Models and routing page.`
  );
}

export async function gatewayApi(path: string, init?: RequestInit) {
  const response = await fetch(`/api/gateway/${path}`, {
    ...init,
    cache: "no-store",
    headers: { "content-type": "application/json", ...init?.headers },
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Session expired");
  }
  if (!response.ok) {
    const details = Array.isArray(data.details) ? data.details.map((item: GatewayRow) => `${item.location}: ${item.message}`).join(". ") : "";
    const validation = Array.isArray(data.detail) ? data.detail.map((item: GatewayRow) => `${(item.loc as unknown[]).slice(-1)[0]}: ${item.msg}`).join(". ") : "";
    // FastAPI returns a plain string detail for routing failures, most often
    // {"detail":"Not Found"}. That matched none of the shapes above, so a real cause
    // was replaced by the generic fallback and the operator was told nothing at all.
    const plain = typeof data.detail === "string" ? data.detail : "";
    const status = response.status === 404 ? `${plain || "Not found"} (${path})` : plain;
    // Several guards answer with an error code plus a "reason" naming which condition
    // fired. Dropping the reason turned an actionable refusal into a dead end:
    // "provider topology not supported" says nothing about what to change, and the
    // reason says exactly which part of the existing setup is in the way.
    const reason = typeof data.reason === "string" ? RECONCILE_REASONS[data.reason] ?? data.reason.replaceAll("_", " ") : "";
    const primary = readable(data.error);
    const explained = primary && reason ? `${primary}: ${reason}` : primary || reason;
    if (data.error === "publish_would_strand_models") {
      // The guard names exactly which models would be left with no route. An
      // operator who only reads "would strand models" still has to hunt for the
      // list, so repeat it here with what to do about each one.
      const names = Array.isArray(data.models) ? data.models.map((model: unknown) => `"${String(model)}"`).join(", ") : "";
      throw new Error(
        `Publish refused: ${data.message ?? "some enabled models would have no provider route"}` +
          (names ? ` - stranded: ${names}.` : ".") +
          ` Open Models and either disable or delete ${names ? "them" : "the listed models"}, then publish again.`,
      );
    }
    if (data.error === "shared_model_metadata_conflict") {
      throw new Error(sharedModelConflict(data));
    }
    // The guards write a full sentence in "message"; the error code alone is a
    // dead end, so the sentence always wins when both exist.
    const message = typeof data.message === "string" ? data.message : "";
    throw new Error(details || validation || message || explained || status || `Request failed with status ${response.status}`);
  }
  return data;
}
