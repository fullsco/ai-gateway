/**
 * Why a request failed, and why a candidate route was passed over.
 *
 * The gateway records a category or a reason code. On its own each one is a dead end for
 * whoever is reading this console while something is broken: "no_eligible_route" names
 * the symptom and none of the cause. Every entry here says what happened and, where
 * there is one, what the operator should do about it.
 */

export function explainError(value: unknown): string {
  // Keys must be the values the gateway actually writes. Two of the previous
  // entries ("rate_limited", "authentication_failed") are health states, not
  // error categories, and were never emitted, so the two most operationally
  // important failures fell through to a raw de-underscored string.
  const explanations: Record<string, string> = {
    upstream_waf_rejection:
      "An edge or bot-protection layer at the provider blocked the request, not the credential. Other eligible routes may still be used.",
    rate_limit:
      "The provider rate limited this credential. It is paused briefly and another eligible credential or provider is used.",
    upstream_authentication_error:
      "The provider rejected this credential. Rotate or disable it; other credentials on the same provider are tried first.",
    authentication_error:
      "The gateway API key sent by the client was not recognised. Nothing upstream is wrong.",
    authorization_error:
      "The client's key is valid, but its client is not allowed this protocol or model, or the key is revoked or expired. Fix it in Clients, not at the provider.",
    quota_exhausted:
      "This credential is out of quota or balance. Another credential or provider with headroom is required.",
    timeout: "The provider did not respond before the configured timeout.",
    provider_unavailable:
      "The provider could not be reached or returned a server error. Traffic moves to another eligible provider.",
    model_unavailable:
      "This provider does not serve the requested model, even though it is mapped to it.",
    no_eligible_route:
      "The model is configured, but no route could serve it at that moment: every candidate was unhealthy, cooling down, out of quota, or at its concurrency limit. Open the request trace to see which and why.",
    invalid_request:
      "The request itself was rejected as malformed. Retrying elsewhere would fail the same way, so no failover was attempted.",
    internal_error: "The gateway failed to complete the request.",
  };
  return explanations[String(value)] ?? String(value ?? "No reason recorded").replaceAll("_", " ");
}

// Every reason the routing engine can give for skipping a candidate, said plainly.
// Two are prefixes because the health state is appended to them.
const EXCLUSION_REASONS: Record<string, string> = {
  route_excluded_this_request: "Already tried and failed earlier in this same request",
  provider_missing_from_snapshot: "The provider is not in the published configuration",
  provider_disabled: "The provider is turned off",
  provider_circuit_open: "Paused after repeated failures, and not yet retried",
  credential_excluded_this_request: "Already tried and failed earlier in this same request",
  credential_disabled: "The credential is turned off",
  credential_other_provider: "Belongs to a different provider",
  credential_in_cooldown: "Cooling down after a recent failure",
  credential_quota_exhausted: "Out of quota",
  credential_rpm_exhausted: "At its requests-per-minute limit",
  credential_tpm_exhausted: "At its tokens-per-minute limit",
  credential_concurrency_exhausted: "At its concurrent-request limit",
  credential_not_permitted_for_route: "Not permitted to serve this model",
  credential_not_in_route_pool: "Not a member of the credential pool this route restricts to",
  credential_not_in_policy_allow_list: "Not on the routing policy's allow list",
  latency_above_policy_limit: "Slower than the routing policy allows",
  quota_headroom_below_policy_minimum: "Less quota headroom than the policy requires",
  rpm_headroom_below_policy_minimum: "Less request headroom than the policy requires",
  tpm_headroom_below_policy_minimum: "Less token headroom than the policy requires",
};

const HEALTH_REASONS: Record<string, string> = {
  rate_limited: "rate limited by the provider",
  auth_failed: "rejected by the provider",
  quota_exhausted: "out of quota",
  unavailable: "unreachable",
  cooldown: "cooling down",
  disabled: "turned off",
};

export function explainExclusion(reason: unknown): string {
  const key = String(reason ?? "");
  if (!key) return "Eligible";
  const known = EXCLUSION_REASONS[key];
  if (known) return known;
  for (const prefix of ["credential_health_", "provider_health_"]) {
    if (key.startsWith(prefix)) {
      const state = key.slice(prefix.length);
      const subject = prefix.startsWith("credential") ? "credential" : "provider";
      return `The ${subject} is ${HEALTH_REASONS[state] ?? state.replaceAll("_", " ")}`;
    }
  }
  return key.replaceAll("_", " ");
}
