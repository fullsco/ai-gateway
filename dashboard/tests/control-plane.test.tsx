import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    expect(screen.getByText(/edge or bot-protection layer at the provider blocked/i)).toBeVisible();
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
          { id: "attempt-1", provider_name: "AgentRouter", credential_name: "Primary", status: "failed", upstream_status: 429, error_category: "rate_limit", latency_ms: 320 },
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

  it("explains every failure the gateway can report", async () => {
    // Two former keys ("rate_limited", "authentication_failed") are health states,
    // not error categories, so the two most important failures rendered as a raw
    // de-underscored string while the test passed against the wrong fixture.
    const expected: [string, RegExp][] = [
      ["authentication_error", /gateway API key sent by the client/i],
      ["upstream_authentication_error", /provider rejected this credential/i],
      ["upstream_waf_rejection", /edge or bot-protection layer/i],
      ["rate_limit", /rate limited this credential/i],
      ["quota_exhausted", /out of quota or balance/i],
      ["model_unavailable", /does not serve the requested model/i],
      ["no_eligible_route", /every candidate was unhealthy/i],
      ["provider_unavailable", /could not be reached/i],
      ["timeout", /did not respond before the configured timeout/i],
      ["invalid_request", /rejected as malformed/i],
      ["internal_error", /gateway failed to complete/i],
    ];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("health")) return response({ data: expected.map(([category], index) => ({
        id: index + 1, provider_name: "AgentRouter", credential_name: `k${index}`,
        status: "degraded", error_category: category,
      })) });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Health" }));
    await screen.findByText(/edge or bot-protection layer/i);
    expected.forEach(([category, phrase]) => {
      expect(screen.getByText(phrase), `no explanation for ${category}`).toBeVisible();
    });
  });

  it("presents an alert as what happened, why, and what to do", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("alerts")) return response({ data: [{
        id: 1, severity: "critical", status: "open",
        title: "Provider is failing most requests: GoRouter",
        summary: "More than half of the attempts sent to this provider failed.",
        recommended_action: "Check the provider status, then Health for affected credentials.",
        observed: { attempts: 20, failures: 18, failure_rate: 0.9 },
        occurrence_count: 4, last_seen_at: "2026-08-20T00:00:00Z", resolved_reason: null,
      }] });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Alerts" }));
    expect(await screen.findByText(/More than half of the attempts/i)).toBeVisible();
    expect(screen.getByText(/Check the provider status/i)).toBeVisible();
    // The measured values read as a sentence, not raw JSON.
    expect(screen.getByText(/failure rate: 0\.9/i)).toBeVisible();
    expect(document.body.textContent).not.toContain("[object Object]");
  });

  it("describes an alert rule without showing raw JSON", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("alert-rules")) return response({ data: [{
        id: "r1", name: "Provider is failing most requests", severity: "critical",
        enabled: true, condition_kind: "provider_failure_rate",
        condition: { window_minutes: 15, at_least: 0.5, min_requests: 10 },
        description: "More than half of the attempts failed.",
        cooldown_seconds: 900, updated_at: "2026-08-20T00:00:00Z",
      }] });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Alert rules" }));
    expect(await screen.findByText("Provider failing a share of attempts")).toBeVisible();
    expect(screen.getByText(/at or above 0\.5, measured over 15 minutes/i)).toBeVisible();
    expect(document.body.textContent).not.toContain("window_minutes");
  });

  it("says whether a quota figure is a measurement", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("credentials")) return response({ data: [
        { id: "c1", name: "measured", provider_name: "P", enabled: true, health: "healthy",
          quota_used: 5, quota_limit: 10, quota_confidence: "known", balance_amount: null },
        { id: "c2", name: "no-limit", provider_name: "P", enabled: true, health: "healthy",
          quota_used: 5, quota_limit: null, quota_confidence: "estimated", balance_amount: 2.5 },
        { id: "c3", name: "nothing", provider_name: "P", enabled: true, health: "healthy",
          quota_used: null, quota_limit: null, quota_confidence: "unknown", balance_amount: null },
      ] });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: /Credentials/ }));
    expect(await screen.findByText(/Measured - a limit and a usage figure/i)).toBeVisible();
    expect(screen.getByText(/headroom unknown/i)).toBeVisible();
    expect(screen.getByText(/do not read this as remaining capacity/i)).toBeVisible();
    // An unobserved balance must not read as zero.
    expect(screen.getAllByText("Not observed").length).toBeGreaterThan(0);
  });

  it("explains why a request was routed the way it was", async () => {
    // The routing trace records every candidate and why each was skipped, but it
    // was recorded and never shown, so the operator could not answer "why did this
    // go there" without reading the database.
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("requests/request-1")) return response({
        request: { id: "request-1", requested_model: "claude-opus-5", status: "succeeded", latency_ms: 900, retry_count: 1, fallback_count: 1 },
        attempts: [{ id: "a1", attempt_number: 1, provider_name: "GoRouter", credential_name: "k1", status: "failed", error_category: "provider_unavailable" }],
        usage: [],
        routing: [
          {
            attempt_number: 1, is_fallback: false, strategy: "priority",
            selected: { provider: "AgentRouter", credential_name: "primary", score: 7.83 },
            considered: [
              { provider: "AgentRouter", credential_name: "primary", eligible: true, score: 7.83 },
              { provider: "AgentRouter", credential_name: "spare", eligible: true, score: 6.33 },
              { provider: "GoRouter", credential_name: "gr-1", eligible: false, reason: "credential_health_auth_failed" },
              { provider: "TabiAi", credential_name: "t-1", eligible: false, reason: "provider_disabled" },
              { provider: "AgentRouter", credential_name: "cooling", eligible: false, reason: "credential_in_cooldown" },
              { provider: "AgentRouter", credential_name: "spent", eligible: false, reason: "credential_quota_exhausted" },
            ],
          },
        ],
      });
      if (path.includes("requests")) return response({ data: [{ id: "request-1", requested_model: "claude-opus-5", status: "succeeded", latency_ms: 900, retry_count: 1, fallback_count: 1 }] });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Requests" }));
    await userEvent.click(await screen.findByRole("button", { name: "Trace request" }));
    expect(await screen.findByRole("dialog", { name: "Request trace" })).toBeVisible();

    // What was chosen, and that it beat the other eligible candidate.
    expect(screen.getByText(/Chose/)).toBeVisible();
    expect(screen.getByText(/the best of 2 eligible/)).toBeVisible();
    // Each skip reason reads as a sentence, never as a raw enum value.
    expect(screen.getByText("The credential is rejected by the provider")).toBeVisible();
    expect(screen.getByText("The provider is turned off")).toBeVisible();
    expect(screen.getByText("Cooling down after a recent failure")).toBeVisible();
    expect(screen.getByText("Out of quota")).toBeVisible();
    expect(screen.getByText(/4 candidates skipped/)).toBeVisible();
    expect(document.body.textContent).not.toContain("credential_health_auth_failed");
    expect(document.body.textContent).not.toContain("provider_disabled");
  });

  it("says so when nothing was eligible", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("requests/request-1")) return response({
        request: { id: "request-1", requested_model: "glm-5.2", status: "failed", error_category: "no_eligible_route" },
        attempts: [], usage: [],
        routing: [{ attempt_number: 1, selected: null, considered: [
          { provider: "hcnsec", credential_name: "h1", eligible: false, reason: "credential_health_unavailable" },
        ] }],
      });
      if (path.includes("requests")) return response({ data: [{ id: "request-1", requested_model: "glm-5.2", status: "failed" }] });
      return response(overview);
    }));
    render(<ControlPlane />);
    await userEvent.click(screen.getByRole("button", { name: "Requests" }));
    await userEvent.click(await screen.findByRole("button", { name: "Trace request" }));
    expect(await screen.findByText(/Nothing was eligible, so no provider was contacted/)).toBeVisible();
    expect(screen.getByText("The credential is unreachable")).toBeVisible();
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

describe("draft versus published state", () => {
  it("does not claim an empty draft is reviewable", async () => {
    // The gateway guarantees a claimed change can be named, so this is the fallback
    // path. It previously read "Changes affect: the initial configuration." for any
    // empty list, which is only true before the first publish and otherwise told the
    // operator a published snapshot did not exist.
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path.includes("config/status")) {
        return response({
          active_version: 7,
          active_checksum: "a".repeat(64),
          working_checksum: "b".repeat(64),
          has_unpublished_changes: true,
          changed_sections: [],
          changes: [],
          change_count: 0,
          serving_version: 7,
          serving_published_version: true,
        });
      }
      return response({ data: [] });
    }));

    render(<ControlPlane />);
    await userEvent.click(await screen.findByRole("button", { name: /Configuration/i }));

    expect(await screen.findByText(/could not be itemised/i)).toBeInTheDocument();
    expect(screen.queryByText(/the initial configuration/i)).not.toBeInTheDocument();
  });
});

describe("observation freshness", () => {
  it("says how old a balance reading is and when it is too old to trust", async () => {
    // A hand-entered balance six days old rendered exactly like one taken a minute
    // ago, so a figure that could not possibly be current was read as current. The
    // poller that would refresh the quota figure is off by default, so the same is
    // true of quota_observed_at.
    const stale = new Date(Date.now() - 6 * 24 * 3600 * 1000).toISOString();
    const fresh = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("credentials")) {
        return response({ data: [
          { id: "c1", name: "stale-one", provider_name: "GoRouter", enabled: true,
            health: "healthy", balance_amount: "0.061184", balance_observed_at: stale },
          { id: "c2", name: "fresh-one", provider_name: "GoRouter", enabled: true,
            health: "healthy", balance_amount: "12.00", balance_observed_at: fresh },
        ] });
      }
      return response({ data: [] });
    }));

    render(<ControlPlane />);
    await userEvent.click(await screen.findByRole("button", { name: /Credentials/i }));

    expect(await screen.findByText(/6 days ago - stale, may not reflect reality/)).toBeInTheDocument();
    expect(screen.getByText(/10 min ago/)).toBeInTheDocument();
    // The fresh one must not be labelled stale.
    expect(screen.getByText(/10 min ago/).textContent).not.toMatch(/stale/);
  });

  it("shows how old the quota reading is, because that is the figure that exists", async () => {
    // The age annotation was added to balance_observed_at and quota_observed_at, but
    // only balance_observed_at was a credentials column, and balance is null on forty
    // of forty-two credentials. quota_observed_at is populated on all of them and is
    // days old because the poller is off, so the one age worth reading was the one
    // never displayed.
    const stale = new Date(Date.now() - 6 * 24 * 3600 * 1000).toISOString();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("credentials")) {
        return response({ data: [
          { id: "c1", name: "agent-one", provider_name: "AgentRouter", enabled: true, health: "auth_failed",
            quota_used: "12373.4414", quota_limit: null, quota_confidence: "estimated",
            quota_observed_at: stale, balance_amount: null, balance_observed_at: null },
        ] });
      }
      return response({ data: [] });
    }));

    render(<ControlPlane />);
    await userEvent.click(await screen.findByRole("button", { name: /Credentials/i }));

    expect(await screen.findByText(/6 days ago - stale, may not reflect reality/)).toBeInTheDocument();
  });

  it("does not print a raw machine timestamp for a cooldown", async () => {
    // cooldown_until is a timestamp, but the formatting branch keys off keys ending in
    // _at, so every cooling-down credential showed an unreadable ISO string with
    // microseconds and an offset.
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("credentials")) {
        return response({ data: [
          { id: "c1", name: "cooling", provider_name: "AgentRouter", enabled: true,
            health: "quota_exhausted", cooldown_until: "2026-08-24T17:03:04.617395+00:00" },
        ] });
      }
      return response({ data: [] });
    }));

    render(<ControlPlane />);
    await userEvent.click(await screen.findByRole("button", { name: /Credentials/i }));
    await screen.findByText("cooling");
    expect(document.body.textContent).not.toContain("2026-08-24T17:03:04.617395+00:00");
  });

  it("calls an observation that was never taken unobserved, not unconfigured", async () => {
    // A missing observation is not a missing setting. "Not configured" implies the
    // operator failed to fill something in, when the truth is that nothing has ever
    // recorded a reading.
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("credentials")) {
        return response({ data: [
          { id: "c1", name: "never-read", provider_name: "AgentRouter", enabled: true,
            health: "healthy", balance_amount: null, balance_observed_at: null, quota_observed_at: null },
        ] });
      }
      return response({ data: [] });
    }));

    render(<ControlPlane />);
    await userEvent.click(await screen.findByRole("button", { name: /Credentials/i }));
    const row = (await screen.findByText("never-read")).closest("tr") as HTMLElement;
    // quota_observed_at, balance_amount and balance_observed_at are all readings, and
    // all three are absent. A genuinely unset field such as quota_limit still reads
    // "Not configured", which is correct for a setting.
    expect(row.textContent?.match(/Not observed/g)).toHaveLength(3);
  });

  it("says which figures a machine measured and which a person typed", async () => {
    // Now that the poller runs, quota_observed_at refreshes itself every fifteen
    // minutes while balance_amount is still only ever entered by hand. A fresh quota
    // timestamp sitting beside a hand-typed balance invited reading both as current
    // measurements. quota_source and balance_source were already returned by the API
    // and never shown, so the one field that resolves the ambiguity was invisible.
    const fresh = new Date(Date.now() - 9 * 60 * 1000).toISOString();
    const older = new Date(Date.now() - 5 * 24 * 3600 * 1000).toISOString();
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("credentials")) {
        return response({ data: [
          { id: "c1", name: "polled", provider_name: "AgentRouter", enabled: true, health: "healthy",
            quota_used: "67500.196", quota_source: "upstream_usage", quota_observed_at: fresh,
            balance_amount: "12.50", balance_source: "operator", balance_observed_at: older },
        ] });
      }
      return response({ data: [] });
    }));

    render(<ControlPlane />);
    await userEvent.click(await screen.findByRole("button", { name: /Credentials/i }));
    const row = (await screen.findByText("polled")).closest("tr") as HTMLElement;

    // The polled figure is named as the gateway's own reading.
    expect(row.textContent).toMatch(/Read from the provider by the gateway/);
    // The balance is named as a human entry that nothing will refresh, which is the
    // whole reason it can be five days old while the quota beside it is nine minutes old.
    expect(row.textContent).toMatch(/Entered by an operator/);
    expect(row.textContent).toMatch(/not refreshed automatically/);
  });

  it("does not describe an unpolled credential as measured", async () => {
    // Sixteen of forty-two credentials sit on relays that do not implement the usage
    // endpoint. The poller writes nothing for them, so their provenance is genuinely
    // absent and must not borrow the vocabulary of a measurement.
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("credentials")) {
        return response({ data: [
          { id: "c1", name: "unpolled", provider_name: "TabiAi", enabled: true, health: "healthy",
            quota_used: null, quota_source: null, quota_observed_at: null,
            balance_amount: null, balance_source: null, balance_observed_at: null },
        ] });
      }
      return response({ data: [] });
    }));

    render(<ControlPlane />);
    await userEvent.click(await screen.findByRole("button", { name: /Credentials/i }));
    const row = (await screen.findByText("unpolled")).closest("tr") as HTMLElement;

    expect(row.textContent).not.toMatch(/Read from the provider/);
    expect(row.textContent).not.toMatch(/Entered by an operator/);
    expect(row.textContent).toMatch(/Never observed/);
  });
});

describe("recording a credential balance", () => {
  it("records the balance an operator read from the provider dashboard", async () => {
    // The gateway cannot discover a balance: the relay reports an identical placeholder
    // ceiling for every credential and rejects API keys on its own account endpoint. So
    // the figure is an operator observation, and until now there was no way to enter one.
    // The column read "Not observed" permanently and topping the account up changed
    // nothing on screen.
    const calls: { path: string; method?: string; body?: string }[] = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      calls.push({ path, method: init?.method, body: init?.body ? String(init.body) : undefined });
      if (path.includes("credentials")) {
        return response({ data: [
          { id: "c1", name: "agent-one", provider_name: "AgentRouter", enabled: true,
            health: "healthy", balance_amount: null, balance_observed_at: null },
        ] });
      }
      return response({ data: [] });
    }));

    render(<ControlPlane />);
    await userEvent.click(await screen.findByRole("button", { name: /Credentials/i }));
    await screen.findByText("agent-one");
    await userEvent.click(screen.getByRole("button", { name: /Record balance/i }));

    fireEvent.change(await screen.findByLabelText(/Balance remaining/i), { target: { value: "12.5" } });
    // The row action and the dialog submit share a name, so the click is scoped to the dialog.
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /^Record balance$/i }));

    await waitFor(() => {
      const put = calls.find((call) => call.method === "PUT" && call.path.includes("/balance"));
      expect(put).toBeDefined();
      expect(put?.path).toContain("credentials/c1/balance");
      expect(JSON.parse(put?.body ?? "{}")).toMatchObject({ amount: 12.5 });
    });
  });
});
