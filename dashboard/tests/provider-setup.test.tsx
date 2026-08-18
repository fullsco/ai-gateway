import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ProviderSetup, { gatewayApi, GatewayRow } from "../components/provider-setup";

const response = (data: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } }));
const provider = { id: "provider-1", name: "Provider", base_url: "https://provider.example", enabled: false, priority: 7, timeout_seconds: 42, settings: { region: "east" } };
const workspace: Record<string, GatewayRow[]> = {
  credentials: [{ id: "credential-1", provider_id: "provider-1", name: "primary", enabled: false, priority: 9, quota_limit: 100, quota_threshold: 0.8, requests_per_minute: 12, tokens_per_minute: 345 }],
  models: [{ id: "model-1", display_name: "Model One", aliases: ["latest", "fast"], capabilities: ["streaming", "vision"], context_window: 128000, enabled: false }],
  "provider-models": [{ id: "mapping-1", provider_id: "provider-1", model_id: "model-1", upstream_model_id: "upstream-1", protocol: "anthropic_messages", capabilities: ["streaming", "vision"], enabled: false, priority: 11, weight: 2.5, max_concurrency: 4, settings: { auth_scheme: "bearer", default_headers: { "x-test": "yes" }, nested: { preserve: true } }, pricing: { input_per_million: 1.25, output_per_million: 2.5, cached_input_per_million: 0.5, currency: "EUR", version: "2026-01", effective_at: "2026-01-01" } }],
  routes: [{ id: "route-1", provider_id: "provider-1", provider_model_id: "mapping-1", model_id: "model-1", priority: 13, enabled: false, allow_model_fallback: true, pool_id: "pool-1" }],
  "provider-pools": [{ id: "pool-1", name: "Provider Pool", enabled: false, strategy: "weighted", settings: { health_aware: false, quota_aware: true } }],
};

function fetchForWorkspace(onReconcile: (payload: GatewayRow) => void = () => undefined) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).split("/").pop() ?? "";
    if (init?.method === "PUT") {
      onReconcile(JSON.parse(String(init.body)));
      return response({ provider_id: "provider-1" });
    }
    return response({ data: workspace[path] ?? [] });
  });
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  vi.restoreAllMocks();
});

describe("provider setup", () => {
  it("serializes a guided create with runtime controls", async () => {
    let payload: GatewayRow = {};
    vi.stubGlobal("fetch", fetchForWorkspace((value) => { payload = value; }));
    const onSaved = vi.fn(async () => undefined);
    render(<ProviderSetup onClose={vi.fn()} onSaved={onSaved} />);

    fireEvent.change(screen.getByLabelText("Provider name"), { target: { value: "New Provider" } });
    fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "https://new.example" } });
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "primary" } });
    fireEvent.change(screen.getByLabelText("Secret"), { target: { value: "secret-value" } });
    fireEvent.change(screen.getByLabelText(/^Model ID/), { target: { value: "model-new" } });
    fireEvent.change(screen.getByLabelText("Model display name"), { target: { value: "Model New" } });
    fireEvent.change(screen.getByLabelText(/^Provider model ID/), { target: { value: "upstream-new" } });
    await userEvent.selectOptions(screen.getByLabelText("Selection strategy"), "least_loaded");
    await userEvent.click(screen.getByLabelText("Use as model fallback"));
    await userEvent.click(screen.getByRole("button", { name: "Create provider workspace" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(payload).toMatchObject({
      name: "New Provider", enabled: true, priority: 100, timeout_seconds: 600,
      pool_strategy: "least_loaded", pool_enabled: true, health_aware: true, quota_aware: true,
      credentials: [{ name: "primary", secret: "secret-value", rotate_secret: true, enabled: true }],
      models: [{ id: "model-new", display_name: "Model New", aliases: [], enabled: true, context_window: null }],
      mappings: [{ model_id: "model-new", upstream_model_id: "upstream-new", enabled: true, settings: {}, pricing: {} }],
      routes: [{ model_id: "model-new", enabled: true, allow_model_fallback: true }],
    });
  });

  it("hydrates an edit and round-trips preserved state and metadata unchanged", async () => {
    let payload: GatewayRow = {};
    vi.stubGlobal("fetch", fetchForWorkspace((value) => { payload = value; }));
    render(<ProviderSetup provider={provider} onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);

    expect(await screen.findByDisplayValue("latest, fast")).toBeVisible();
    expect(screen.getByLabelText("Label")).toHaveAttribute("readonly");
    expect(screen.getByLabelText("Selection strategy")).toHaveValue("weighted");
    expect(screen.getByLabelText("Provider enabled")).not.toBeChecked();
    expect(screen.getByLabelText("Canonical model enabled")).not.toBeChecked();
    expect(screen.getByLabelText("Mapping enabled")).not.toBeChecked();
    expect(screen.getByLabelText("Route active")).not.toBeChecked();
    expect(screen.getByLabelText("Use as model fallback")).toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: "Save provider workspace" }));
    await waitFor(() => expect(payload.name).toBe("Provider"));
    expect(payload).toMatchObject({ enabled: false, priority: 7, timeout_seconds: 42, settings: { region: "east" }, pool_strategy: "weighted", pool_enabled: false, health_aware: false, quota_aware: true });
    expect((payload.credentials as GatewayRow[])[0]).toMatchObject({ name: "primary", secret: null, rotate_secret: false, enabled: false, priority: 9 });
    expect((payload.models as GatewayRow[])[0]).toEqual({ id: "model-1", display_name: "Model One", aliases: ["latest", "fast"], capabilities: ["streaming", "vision"], enabled: false, context_window: 128000 });
    expect((payload.mappings as GatewayRow[])[0]).toMatchObject({ enabled: false, settings: workspace["provider-models"][0].settings, pricing: workspace["provider-models"][0].pricing });
    expect((payload.routes as GatewayRow[])[0]).toMatchObject({ priority: 13, enabled: false, allow_model_fallback: true });
  });

  it("only sends a replacement secret after explicit rotation", async () => {
    let payload: GatewayRow = {};
    vi.stubGlobal("fetch", fetchForWorkspace((value) => { payload = value; }));
    render(<ProviderSetup provider={provider} onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);
    await screen.findByDisplayValue("primary");

    expect(screen.getByLabelText("New secret")).toBeDisabled();
    await userEvent.click(screen.getByLabelText("Rotate secret"));
    fireEvent.change(screen.getByLabelText("New secret"), { target: { value: "replacement" } });
    await userEvent.click(screen.getByRole("button", { name: "Save provider workspace" }));

    await waitFor(() => expect((payload.credentials as GatewayRow[])[0]).toMatchObject({ secret: "replacement", rotate_secret: true }));
  });

  it("round-trips two mappings that share one aliased canonical model", async () => {
    let payload: GatewayRow = {};
    const shared = {
      ...workspace["provider-models"][0],
      id: "mapping-2",
      upstream_model_id: "upstream-2",
      protocol: "openai_responses",
    };
    workspace["provider-models"].push(shared);
    workspace.routes.push({
      ...workspace.routes[0],
      id: "route-2",
      provider_model_id: "mapping-2",
    });
    vi.stubGlobal("fetch", fetchForWorkspace((value) => { payload = value; }));
    render(<ProviderSetup provider={provider} onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);

    expect(await screen.findAllByDisplayValue("latest, fast")).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "Save provider workspace" }));

    await waitFor(() => expect((payload.mappings as GatewayRow[])).toHaveLength(2));
    expect((payload.models as GatewayRow[])).toHaveLength(1);
    workspace["provider-models"].pop();
    workspace.routes.pop();
  });

  it("does not expose a destructive edit form after hydration fails", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response({ error: "unavailable" }, 503)));
    render(<ProviderSetup provider={provider} onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Provider configuration could not be loaded. Close and retry.");
    expect(screen.queryByRole("button", { name: "Save provider workspace" })).not.toBeInTheDocument();
  });

  it("closes without submitting and supports Escape", async () => {
    const fetchMock = fetchForWorkspace();
    vi.stubGlobal("fetch", fetchMock);
    const onClose = vi.fn();
    const view = render(<ProviderSetup onClose={onClose} onSaved={vi.fn(async () => undefined)} />);
    await userEvent.click(screen.getByRole("button", { name: "Close provider setup" }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(fetchMock).not.toHaveBeenCalled();
    view.unmount();

    render(<ProviderSetup onClose={onClose} onSaved={vi.fn(async () => undefined)} />);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("renders structured API errors and redirects unauthorized requests", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response({ details: [{ location: "credentials.0.name", message: "duplicate label" }] }, 422)));
    render(<ProviderSetup provider={provider} onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("credentials.0.name: duplicate label");

    vi.stubGlobal("fetch", vi.fn(() => response({ error: "not_authenticated" }, 401)));
    await expect(gatewayApi("providers")).rejects.toThrow("Session expired");
  });
});
