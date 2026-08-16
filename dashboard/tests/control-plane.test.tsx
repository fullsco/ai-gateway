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
    await userEvent.click(screen.getByRole("button", { name: "Activity" }));
    expect(await screen.findByText("credential_rotated")).toBeVisible();
    expect(document.body.textContent).not.toContain("[object Object]");
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
