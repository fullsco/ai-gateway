/**
 * The protocol surfaces: which client APIs can reach a model, and the vocabulary and
 * payloads that decide it.
 *
 * These cover the case the whole translation change exists for: a mapping whose upstream
 * protocol is OpenAI Chat Completions but which also answers `/v1/messages`, because
 * Claude Code calls nothing else. Before that, such a model returned 404 to every client
 * and nothing in the dashboard said why - so "unreachable" being visible here, from the
 * order the operator is editing rather than the one already saved, is the point.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ModelRouting from "../components/model-routing";
import { buildPayload } from "../components/resource-payload";
import { PROTOCOLS, PROTOCOL_ENDPOINTS, protocolLabel, servedProtocols, translates } from "../components/gateway-protocols";

const response = (data: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } }));

type Row = Record<string, unknown>;

const model = { id: "glm-5.3", display_name: "GLM 5.3", capabilities: ["streaming"] };

/** A routing row, defaulted to a routed, enabled, native OpenAI mapping. */
const route = (overrides: Row = {}): Row => ({
  provider: "AgentRouter",
  provider_id: "prov-1",
  provider_model_id: "map-1",
  upstream_model_id: "glm-4.6",
  protocol: "openai_chat_completions",
  serves_protocols: ["openai_chat_completions"],
  route_enabled: true,
  route_active: true,
  priority: 0,
  provider_enabled: true,
  mapping_enabled: true,
  ...overrides,
});

function fetchForRouting(rows: Row[], onSave: (payload: Row) => void = () => undefined) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.includes("/routing")) {
      if (init?.method === "PUT") {
        onSave(JSON.parse(String(init.body)));
        return response({ model_id: model.id, provider_count: rows.length, strategy: "priority" });
      }
      return response({ model, data: rows });
    }
    if (path.endsWith("/models")) return response({ data: [model] });
    return response({ data: [] });
  });
}

/** The reachability row for one client endpoint, found the way an operator reads it. */
function reachRow(endpoint: string): HTMLElement {
  const row = screen.getByText(endpoint, { selector: "code" }).closest("li");
  if (!row) throw new Error(`No reachability row rendered for ${endpoint}`);
  return row as HTMLElement;
}

async function renderRouting(rows: Row[], onSave?: (payload: Row) => void) {
  const fetchMock = fetchForRouting(rows, onSave);
  vi.stubGlobal("fetch", fetchMock);
  render(<ModelRouting onNotice={vi.fn()} />);
  // The reachability panel renders while the routing request is still in flight, with
  // no routes and so every endpoint unreachable. Waiting on the heading alone made an
  // assertion that a row reads "unreachable" pass against the loading state instead of
  // against the configuration, so wait for the drafted order to exist.
  await screen.findByText("Primary provider");
  return fetchMock;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("served protocol vocabulary", () => {
  it("always serves the upstream protocol, so an empty list is not unreachable", () => {
    // The invariant the whole control rests on. Relaying bytes to the protocol a route
    // already speaks is free, so a stored empty list means "no translation configured"
    // and never "answers nothing". Reading it the other way would draw every mapping
    // written before the column existed as unreachable.
    expect(servedProtocols("openai_chat_completions", [])).toEqual(["openai_chat_completions"]);
    expect(servedProtocols("anthropic_messages", [])).toEqual(["anthropic_messages"]);
  });

  it("does not repeat the upstream protocol when it is also stored", () => {
    expect(servedProtocols("anthropic_messages", ["anthropic_messages"])).toEqual(["anthropic_messages"]);
  });

  it("normalizes to the declared order and drops values the gateway does not know", () => {
    // The list arrives from an API and from checkbox order, neither of which is sorted.
    // Rendering it verbatim made the same configuration read differently between the
    // table and the form.
    expect(servedProtocols("openai_responses", ["anthropic_messages", "openai_chat_completions"])).toEqual([
      "anthropic_messages",
      "openai_chat_completions",
      "openai_responses",
    ]);
    expect(servedProtocols("anthropic_messages", ["grpc", ""])).toEqual(["anthropic_messages"]);
  });

  it("treats a route as answering its own protocol and refuses unknown ones", () => {
    expect(translates("anthropic_messages", "anthropic_messages")).toBe(true);
    expect(translates("anthropic_messages", "openai_chat_completions")).toBe(true);
    expect(translates("anthropic_messages", "grpc")).toBe(false);
    expect(translates("", "anthropic_messages")).toBe(false);
  });

  it("gives every protocol a label and a request path", () => {
    // The reachability view prints the endpoint as the answer to "why did my client get
    // a 404", so a missing entry would render the word "undefined" as a URL.
    PROTOCOLS.forEach(([id, label]) => {
      expect(PROTOCOL_ENDPOINTS[id]).toMatch(/^\/v1\//);
      expect(protocolLabel(id)).toBe(label);
    });
    expect(protocolLabel("openai_responses")).toBe("OpenAI Responses");
    expect(protocolLabel(undefined)).toBe("");
  });
});

describe("client reachability", () => {
  it("reports an Anthropic client reaching an OpenAI upstream by translation", async () => {
    // The case Claude Code needs: /v1/messages answered by a route that speaks OpenAI
    // Chat Completions upstream. This is a 404 before the translation matrix exists.
    await renderRouting([route({ serves_protocols: ["openai_chat_completions", "anthropic_messages"] })]);

    const messages = reachRow("/v1/messages");
    expect(within(messages).getByText("translated")).toBeVisible();
    expect(within(messages).getByText(/Served by AgentRouter \/ glm-4\.6 \(translated\)/)).toBeVisible();

    // Its own protocol is still relayed rather than translated.
    expect(within(reachRow("/v1/chat/completions")).getByText("native")).toBeVisible();
  });

  it("names the endpoint nothing answers instead of leaving it to a client's 404", async () => {
    await renderRouting([route()]);

    const responses = reachRow("/v1/responses");
    expect(within(responses).getByText("unreachable")).toBeVisible();
    expect(within(responses).getByText(/No route in this order answers \/v1\/responses/)).toBeVisible();
    expect(within(responses).getByText(/tick it on the mapping in guided setup/)).toBeVisible();
  });

  it("reads a gateway older than the served-protocols column as native only", async () => {
    // A snapshot published before migration 025 reports no serves_protocols at all. It
    // answered exactly its upstream protocol then and must read that way now, or every
    // pre-migration mapping would be drawn as newly reachable from everywhere.
    await renderRouting([route({ serves_protocols: undefined })]);

    expect(within(reachRow("/v1/chat/completions")).getByText("native")).toBeVisible();
    expect(within(reachRow("/v1/messages")).getByText("unreachable")).toBeVisible();
  });

  it("distinguishes an endpoint served both natively and by translation", async () => {
    await renderRouting([
      route({ provider: "AgentRouter", protocol: "anthropic_messages", serves_protocols: ["anthropic_messages"], upstream_model_id: "claude-native" }),
      route({ provider: "GoRouter", provider_id: "prov-2", provider_model_id: "map-2", priority: 100, serves_protocols: ["openai_chat_completions", "anthropic_messages"] }),
    ]);

    const messages = reachRow("/v1/messages");
    expect(within(messages).getByText("native + translated")).toBeVisible();
    // Named in the order traffic is tried, so the primary is the one read first.
    expect(within(messages).getByText(/AgentRouter \/ claude-native \(native\).*GoRouter \/ glm-4\.6 \(translated\)/)).toBeVisible();
  });

  it("answers for the order being edited, not the one already saved", async () => {
    // Reachability sits under the provider order the operator is rearranging, so
    // removing the last route that answers an endpoint has to read as unreachable
    // immediately - before a save, and before a client discovers it as a 404.
    let saved: Row | null = null;
    const fetchMock = await renderRouting(
      [
        route({ provider: "AgentRouter", serves_protocols: ["openai_chat_completions", "anthropic_messages"] }),
        route({ provider: "GoRouter", provider_id: "prov-2", provider_model_id: "map-2", priority: 100 }),
      ],
      (payload) => { saved = payload; },
    );
    expect(within(reachRow("/v1/messages")).getByText("translated")).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Remove AgentRouter" }));

    await waitFor(() => expect(within(reachRow("/v1/messages")).getByText("unreachable")).toBeVisible());
    // Still only the two reads; the warning did not require saving the mistake first.
    expect(fetchMock.mock.calls.every(([, init]) => ((init as RequestInit | undefined)?.method ?? "GET") === "GET")).toBe(true);
    expect(saved).toBeNull();
  });

  it("keeps a translated route in the save payload as an explicit mapping", async () => {
    // A provider can own one mapping per protocol. Sending the provider name alone let
    // the gateway re-expand the selection onto mappings the operator never chose.
    let saved: Row = {};
    await renderRouting(
      [
        route({ provider_model_id: "map-openai", serves_protocols: ["openai_chat_completions", "anthropic_messages"] }),
        route({ provider_model_id: "map-responses", protocol: "openai_responses", route_enabled: false, route_active: false, priority: 100 }),
      ],
      (payload) => { saved = payload; },
    );

    await userEvent.click(screen.getByRole("button", { name: /Save working changes/i }));

    await waitFor(() => expect(saved.providers).toBeDefined());
    expect(saved.providers).toMatchObject([{ provider_id: "prov-1", provider_model_ids: ["map-openai"], fallback: false }]);
  });
});

describe("provider-models editor payload", () => {
  /** The mapping form's fields, with only the ones this behaviour depends on filled in. */
  function mappingForm(protocol: string, ticked: string[]) {
    const data = new FormData();
    data.set("provider_id", "prov-1");
    data.set("model_id", "glm-5.3");
    data.set("upstream_model_id", "glm-4.6");
    data.set("protocol", protocol);
    // A checkbox group submits one entry per ticked box under the same name.
    ticked.forEach((entry) => data.append("serves_protocols", entry));
    data.set("pricing", "{}");
    data.set("settings", "{}");
    data.set("priority", "100");
    data.set("weight", "1");
    data.set("max_concurrency", "8");
    data.set("enabled", "true");
    return data;
  }

  it("carries the ticked client APIs through with the upstream protocol included", () => {
    const payload = buildPayload("provider-models", mappingForm("openai_chat_completions", ["anthropic_messages"]));
    expect(payload).toMatchObject({
      protocol: "openai_chat_completions",
      serves_protocols: ["anthropic_messages", "openai_chat_completions"],
    });
  });

  it("still serves the upstream protocol when every box is unticked", () => {
    // An empty checkbox group submits nothing at all, which is a real answer rather than
    // a missing field. Reading it as "answers no client API" would save a mapping the
    // gateway can never route, from the very protocol it speaks.
    expect(buildPayload("provider-models", mappingForm("anthropic_messages", []))).toMatchObject({
      serves_protocols: ["anthropic_messages"],
    });
  });

  it("normalizes whatever order the boxes were ticked in", () => {
    const payload = buildPayload("provider-models", mappingForm("anthropic_messages", ["openai_responses", "openai_chat_completions"]));
    expect(payload).toMatchObject({
      serves_protocols: ["anthropic_messages", "openai_chat_completions", "openai_responses"],
    });
  });
});
