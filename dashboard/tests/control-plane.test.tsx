import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ControlPlane from "../components/control-plane";

const overview = { requests_today: 12, successful: 10, failed: 2, active_providers: 2, fallback_rate: 0.1, estimated_cost: null, runtime_ready: true, config_version: 31, healthy_providers: 1, active_keys: 2, keys_in_cooldown: 1 };
const response = (data: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } }));

beforeEach(() => window.history.replaceState(null, "", "/"));

describe("control plane", () => {
  it("shows overview statistics and active snapshot", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(overview)));
    render(<ControlPlane />);
    expect(await screen.findByText("Requests today")).toBeVisible();
    expect(screen.getByText("Active snapshot")).toBeVisible();
    expect(screen.getByText("31")).toBeVisible();
  });

  it("searches providers and never renders object coercion", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => String(input).includes("providers") ? response({ data: [{ id: "one", name: "AgentRouter", enabled: true, health: "healthy", settings: { region: "primary" } }, { id: "two", name: "GoRouter", enabled: true, health: "degraded", settings: { region: "backup" } }] }) : response(overview)));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Providers" }));
    expect(await screen.findByText("AgentRouter")).toBeVisible();
    await userEvent.type(screen.getByPlaceholderText("Search providers"), "GoRouter");
    expect(screen.getByText("GoRouter")).toBeVisible();
    expect(screen.queryByText("AgentRouter")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("[object Object]");
  });

  it("shows degraded health and activity with readable metadata", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => { const path = String(input); if (path.includes("health")) return response({ data: [{ id: 1, provider_name: "GoRouter", credential_name: "Primary", status: "degraded", error_category: "upstream_waf_rejection" }] }); if (path.includes("events")) return response({ data: [{ id: 1, event_type: "credential_rotated", provider_name: "AgentRouter", metadata: { reason: "operator" } }] }); return response(overview); }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Health" }));
    expect(await screen.findByText("Degraded")).toBeVisible();
    expect(screen.getByText(/upstream rejected the request with a WAF response/i)).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Activity" }));
    expect(await screen.findByText("Credential secret rotated")).toBeVisible();
    expect(document.body.textContent).not.toContain("[object Object]");
  });

  it("shows human-readable audit resource names instead of raw ids", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => String(input).includes("audit")
      ? response({ data: [{ id: 1, action: "model_routing_updated", resource_type: "model", resource_id: "50e4c0d2-1a2b", resource_name: "Claude Opus 5", created_at: "2026-08-18T00:00:00Z" }] })
      : response(overview)));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Audit" }));
    expect(await screen.findByText("Claude Opus 5")).toBeVisible();
    expect(screen.getByText("Model routing updated")).toBeVisible();
    expect(document.body.textContent).not.toContain("50e4c0d2-1a2b");
  });

  it("opens a human-readable request trace with fallback and token usage", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("requests/request-1")) return response({
        request: { id: "request-1", requested_model: "claude-opus-5", resolved_model: "claude-opus-5", status: "succeeded", latency_ms: 1800, retry_count: 0, fallback_count: 1 },
        attempts: [
          { id: "attempt-1", provider_name: "AgentRouter", credential_name: "Primary", status: "failed", upstream_status: 429, error_category: "rate_limited", latency_ms: 320 },
          { id: "attempt-2", provider_name: "GoRouter", credential_name: "Fallback", status: "succeeded", upstream_status: 200, latency_ms: 1480 },
        ],
        usage: [{ input_tokens: 100, output_tokens: 40, cached_tokens: 10, estimated_cost: null }],
      });
      if (path.includes("requests")) return response({ data: [{ id: "request-1", requested_model: "claude-opus-5", status: "succeeded", latency_ms: 1800, retry_count: 0, fallback_count: 1 }] });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Requests" }));
    await userEvent.click(await screen.findByRole("button", { name: "Trace request" }));
    expect(await screen.findByRole("dialog", { name: "Request trace" })).toBeVisible();
    expect(screen.getByText("Succeeded through fallback")).toBeVisible();
    expect(screen.getByText("AgentRouter")).toBeVisible();
    expect(screen.getByText("GoRouter")).toBeVisible();
    expect(screen.getByText("Pricing unavailable")).toBeVisible();
  });

  it("opens the high-level model routing workspace", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("/models/model-1/routing")) return response({ model: { id: "model-1", display_name: "Model One", capabilities: ["streaming"] }, data: [
        { provider: "AgentRouter", route_enabled: true, priority: 0, provider_enabled: true, mapping_enabled: true },
        { provider: "GoRouter", route_enabled: false, priority: 100, provider_enabled: true, mapping_enabled: true },
      ] });
      if (path.endsWith("/models")) return response({ data: [{ id: "model-1", display_name: "Model One", capabilities: ["streaming"] }] });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Models" }));
    expect(await screen.findByText("Models and routing")).toBeVisible();
    expect(screen.getByText("Primary provider")).toBeVisible();
    expect(screen.getByText(/will try AgentRouter first/i)).toBeVisible();
    // GoRouter exposes the model but is not yet routed, so it is offered to add.
    expect(screen.getByRole("option", { name: "GoRouter" })).toBeInTheDocument();
  });

  it("saves routing by provider identity and explicit mappings", async () => {
    // One provider exposing the model under two protocols owns two mappings.
    // Only the anthropic mapping is routed, so only that one may be sent back:
    // sending the provider name alone let the gateway enable both.
    const routing = {
      model: { id: "model-1", display_name: "Model One", capabilities: ["streaming"] },
      data: [
        { provider: "AgentRouter", provider_id: "prov-1", provider_model_id: "map-anthropic", protocol: "anthropic_messages", route_enabled: true, route_active: true, priority: 0, provider_enabled: true, mapping_enabled: true },
        { provider: "AgentRouter", provider_id: "prov-1", provider_model_id: "map-openai", protocol: "openai_chat_completions", route_enabled: false, route_active: false, priority: 100, provider_enabled: true, mapping_enabled: true },
      ],
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/models/model-1/routing")) {
        if (init?.method === "PUT") return response({ model_id: "model-1", provider_count: 1, strategy: "priority" });
        return response(routing);
      }
      if (path.endsWith("/models")) return response({ data: [{ id: "model-1", display_name: "Model One", capabilities: ["streaming"] }] });
      return response(overview);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Models" }));
    await userEvent.click(await screen.findByRole("button", { name: /Save working changes/i }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "PUT")).toBe(true));
    const put = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const sent = JSON.parse(String((put?.[1] as RequestInit).body));
    expect(sent.providers).toHaveLength(1);
    expect(sent.providers[0].provider_id).toBe("prov-1");
    expect(sent.providers[0].provider_model_ids).toEqual(["map-anthropic"]);
    expect(sent.providers[0].provider).toBeUndefined();
  });

  it("does not present a route through a disabled provider as active", async () => {
    // A route row can stay enabled while its provider is disabled. Reporting it
    // as routed produced a payload the gateway rejected with 422, which blocked
    // every save for the model.
    const routing = {
      model: { id: "model-1", display_name: "Model One", capabilities: [] },
      data: [
        { provider: "AgentRouter", provider_id: "prov-1", provider_model_id: "map-1", route_enabled: true, route_active: true, priority: 0, provider_enabled: true, mapping_enabled: true },
        { provider: "TabiAi", provider_id: "prov-2", provider_model_id: "map-2", route_enabled: true, route_active: false, priority: 100, provider_enabled: false, mapping_enabled: true },
      ],
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/models/model-1/routing")) {
        if (init?.method === "PUT") return response({ model_id: "model-1", provider_count: 1, strategy: "priority" });
        return response(routing);
      }
      if (path.endsWith("/models")) return response({ data: [{ id: "model-1", display_name: "Model One", capabilities: [] }] });
      return response(overview);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Models" }));
    expect(await screen.findByText(/will try AgentRouter first/i)).toBeVisible();
    // The disabled provider is neither routed nor offered for selection.
    expect(screen.queryByText("TabiAi")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Save working changes/i }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "PUT")).toBe(true));
    const put = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    const sent = JSON.parse(String((put?.[1] as RequestInit).body));
    expect(sent.providers.map((entry: { provider_id: string }) => entry.provider_id)).toEqual(["prov-1"]);
  });

  it("never renders an unpriced attempt as a measured zero cost", async () => {
    // A request that failed over from an unpriced route to a priced one used to
    // render "0.0000 null" for the unpriced attempt, which reads as measured.
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("requests/request-1")) return response({
        request: { id: "request-1", requested_model: "claude-opus-5", status: "succeeded", latency_ms: 1200, retry_count: 1, fallback_count: 0 },
        attempts: [
          { id: "a1", attempt_number: 1, provider_name: "GoRouter", credential_name: "k1", status: "failed", error_category: "provider_unavailable" },
          { id: "a2", attempt_number: 2, provider_name: "AgentRouter", credential_name: "k2", status: "succeeded", upstream_status: 200 },
        ],
        usage: [
          { input_tokens: 10, output_tokens: 0, cached_tokens: 0, estimated_cost: null, currency: null },
          { input_tokens: 24, output_tokens: 30, cached_tokens: 0, estimated_cost: 0.000348, currency: "USD" },
        ],
      });
      if (path.includes("requests")) return response({ data: [{ id: "request-1", requested_model: "claude-opus-5", status: "succeeded", latency_ms: 1200, retry_count: 1, fallback_count: 0 }] });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Requests" }));
    await userEvent.click(await screen.findByRole("button", { name: "Trace request" }));
    expect(await screen.findByRole("dialog", { name: "Request trace" })).toBeVisible();
    expect(screen.queryByText(/0\.0000 null/)).not.toBeInTheDocument();
    // The priced attempt is shown, and the unpriced one is declared, not hidden.
    expect(screen.getByText(/0\.0003 USD \(1 unpriced\)/)).toBeVisible();
  });

  it("shows a human-readable publish review before publishing", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("config/status")) return response({
        active_version: 68,
        has_unpublished_changes: true,
        changed_sections: ["Mappings and routes"],
        change_count: 2,
        changes: [
          { change: "added", resource: "Model routing", summary: "Added route: claude-opus-5 via GoRouter" },
          { change: "updated", resource: "Model routing", summary: "Changed route claude-opus-5 via AgentRouter: priority 100 to 10" },
        ],
      });
      if (path.includes("config/versions")) return response({ data: [{ id: 68, status: "published", schema_version: 1 }] });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Configuration" }));
    expect(await screen.findByText("2 changes will become active when you publish")).toBeVisible();
    expect(screen.getByText("Added route: claude-opus-5 via GoRouter")).toBeVisible();
    expect(screen.getByText("Changed route claude-opus-5 via AgentRouter: priority 100 to 10")).toBeVisible();
  });

  it("creates a provider and reports success", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => init?.method === "POST" ? response({ id: "new", name: "New provider" }, 201) : String(input).includes("providers") ? response({ data: [] }) : response(overview));
    vi.stubGlobal("fetch", fetchMock);
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Providers" }));
    await userEvent.click(screen.getByRole("button", { name: /Add record/i }));
    await userEvent.type(screen.getByLabelText("Provider name"), "New provider");
    await userEvent.type(screen.getByLabelText("Base URL"), "https://provider.example");
    await userEvent.click(screen.getByRole("button", { name: "Create record" }));
    await waitFor(() => expect(screen.getByText("Record created.")).toBeVisible());
    expect(fetchMock).toHaveBeenCalledWith("/api/gateway/providers", expect.objectContaining({ method: "POST" }));
  });

  it("requires confirmation before deleting a provider", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => init?.method === "DELETE" ? response({ deleted: true }) : String(input).includes("providers") ? response({ data: [{ id: "one", name: "AgentRouter", enabled: true, health: "healthy" }] }) : response(overview));
    vi.stubGlobal("fetch", fetchMock);
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Providers" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete AgentRouter" }));
    expect(screen.getByRole("alertdialog")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining("/providers/one"), expect.objectContaining({ method: "DELETE" }));
  });

  it("does not expose object API errors as [object Object]", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response({ error: { code: "provider_conflict", message: "Provider already exists" } }, 409)));
    render(<ControlPlane />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeVisible());
    expect(screen.getByRole("alert")).not.toHaveTextContent("[object Object]");
  });

  it("issues a labeled expiring gateway key", async () => {
    vi.spyOn(window, "prompt")
      .mockReturnValueOnce("automation")
      .mockReturnValueOnce("2030-01-01T00:00:00Z");
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("clients/client-1/keys") && init?.method === "POST") {
        return response({ key: "gw_plaintext", key_prefix: "gw_abcd" }, 201);
      }
      if (path.includes("clients")) return response({ data: [{ id: "client-1", name: "Automation", enabled: true }] });
      return response(overview);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ControlPlane />);

    await userEvent.click(screen.getByRole("button", { name: "Clients" }));
    await userEvent.click(await screen.findByRole("button", { name: "Issue key" }));

    await waitFor(() => expect(screen.getByText("Gateway key issued")).toBeVisible());
    const call = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes("clients/client-1/keys") && init?.method === "POST"
    );
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      label: "automation",
      expires_at: "2030-01-01T00:00:00Z",
    });
  });
});
