/**
 * The client protocols the gateway speaks, and which of them a given route can answer.
 *
 * A route always answers its own upstream protocol by relaying bytes; anything else it
 * serves is translated on the way through. That distinction is the whole point of the
 * served-protocols control, so it lives here rather than being re-derived at each of the
 * four places that needs it.
 *
 * `translates` mirrors the gateway's own translation registry. Every pair is currently
 * supported, but the shape is a lookup rather than a blanket `true` because the server
 * validates the same question and the UI must not offer a combination it would refuse.
 */

export const PROTOCOLS: [string, string][] = [
  ["anthropic_messages", "Anthropic Messages"],
  ["openai_chat_completions", "OpenAI Chat Completions"],
  ["openai_responses", "OpenAI Responses"],
];

// What a client actually calls. An operator debugging a 404 has the request path in
// front of them, not the protocol's internal name.
export const PROTOCOL_ENDPOINTS: Record<string, string> = {
  anthropic_messages: "/v1/messages",
  openai_chat_completions: "/v1/chat/completions",
  openai_responses: "/v1/responses",
};

export function protocolLabel(value: unknown): string {
  const key = String(value ?? "");
  return PROTOCOLS.find(([id]) => id === key)?.[1] ?? key.replaceAll("_", " ");
}

/** Whether a route speaking `upstream` can answer `client` at all. */
export function translates(client: string, upstream: string): boolean {
  if (!client || !upstream) return false;
  return client === upstream || (PROTOCOLS.some(([id]) => id === client) && PROTOCOLS.some(([id]) => id === upstream));
}

/**
 * The client protocols a mapping answers, always including its own upstream one.
 *
 * Serving the upstream protocol is not a setting: an empty stored list means "no
 * translation configured", not "unreachable". The server normalizes the same way, so
 * the form shows what will actually be saved rather than what was typed.
 */
export function servedProtocols(upstream: string, served: string[]): string[] {
  return PROTOCOLS.map(([id]) => id).filter((id) => id === upstream || served.includes(id));
}
