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
    await userEvent.selectOptions(screen.getByLabelText(/^Catalogue model/), "__new__");
    fireEvent.change(screen.getByLabelText(/^New model ID/), { target: { value: "model-new" } });
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
    // Closing must not write anything. The form does read the canonical model
    // catalogue on open, so that a model can be selected rather than retyped, so the
    // assertion is on writes rather than on there being no traffic at all.
    const writes = fetchMock.mock.calls.filter(([, init]) => {
      const method = (init as RequestInit | undefined)?.method ?? "GET";
      return method !== "GET";
    });
    expect(writes).toHaveLength(0);
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

describe("model selection", () => {
  it("offers existing catalogue models as a real select", async () => {
    // The picker was an <input list> backed by a <datalist>. A datalist is a typeahead
    // hint, not a control: it renders no affordance that a list exists, and on mobile
    // browsers the suggestions frequently never appear at all, which is indistinguishable
    // from the field being broken. A native select is the same choice made visible, and
    // works in every browser.
    vi.stubGlobal("fetch", fetchForWorkspace());
    render(<ProviderSetup onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);

    const field = await waitFor(() => {
      const node = screen.getByLabelText(/^Catalogue model/);
      expect(node.tagName).toBe("SELECT");
      return node as HTMLSelectElement;
    });
    // Real options, discoverable by role, unlike datalist entries.
    await waitFor(() => {
      const values = Array.from(field.querySelectorAll("option")).map((node) => node.getAttribute("value"));
      expect(values).toContain("model-1");
    });
    expect(screen.getByRole("option", { name: /Model One/ })).toBeInTheDocument();
  });

  it("adds a brand new model through an explicit choice rather than free typing", async () => {
    // A select cannot express "an id that does not exist yet", so the new-model path is
    // an option of its own that reveals a text field. Without it, switching to a select
    // would remove the ability to onboard a model the gateway has never seen.
    let payload: GatewayRow = {};
    vi.stubGlobal("fetch", fetchForWorkspace((value) => { payload = value; }));
    render(<ProviderSetup onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);
    await waitFor(() => expect(screen.getByLabelText(/^Catalogue model/).querySelectorAll("option").length).toBeGreaterThan(1));

    fireEvent.change(screen.getByLabelText("Provider name"), { target: { value: "New Provider" } });
    fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "https://new.example" } });
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "primary" } });
    fireEvent.change(screen.getByLabelText("Secret"), { target: { value: "secret-value" } });

    await userEvent.selectOptions(screen.getByLabelText(/^Catalogue model/), "__new__");
    fireEvent.change(screen.getByLabelText(/^New model ID/), { target: { value: "model-new" } });
    fireEvent.change(screen.getByLabelText("Model display name"), { target: { value: "Model New" } });
    fireEvent.change(screen.getByLabelText(/^Provider model ID/), { target: { value: "upstream-new" } });
    await userEvent.click(screen.getByRole("button", { name: "Create provider workspace" }));

    await waitFor(() => expect(payload.models).toBeDefined());
    expect(payload.models).toMatchObject([{ id: "model-new", display_name: "Model New" }]);
  });

  it("adopts the stored metadata of a selected model so the shared guard cannot fire", async () => {
    // The form used to prefill display_name from the id and leave context_window
    // blank, which differs from what the catalogue holds and is refused as a shared
    // model metadata change. Selecting the model copies its real values instead.
    let payload: GatewayRow = {};
    vi.stubGlobal("fetch", fetchForWorkspace((value) => { payload = value; }));
    render(<ProviderSetup onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);
    await waitFor(() => expect(screen.getByLabelText(/^Catalogue model/).querySelectorAll("option").length).toBeGreaterThan(1));

    fireEvent.change(screen.getByLabelText("Provider name"), { target: { value: "New Provider" } });
    fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "https://new.example" } });
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "primary" } });
    fireEvent.change(screen.getByLabelText("Secret"), { target: { value: "secret-value" } });
    await userEvent.selectOptions(screen.getByLabelText(/^Catalogue model/), "model-1");
    await userEvent.click(screen.getByRole("button", { name: "Create provider workspace" }));

    await waitFor(() => expect(payload.models).toBeDefined());
    expect(payload.models).toMatchObject([
      {
        id: "model-1",
        display_name: "Model One",
        context_window: 128000,
        enabled: false,
        aliases: ["latest", "fast"],
      },
    ]);
  });

  it("explains a shared model conflict by naming the field and both values", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response({
      error: "shared_model_metadata_conflict",
      model_id: "model-1",
      field: "context_window",
      current: 128000,
      requested: null,
      shared_with: ["Other Provider"],
    }, 409)));

    await expect(gatewayApi("providers/reconcile", { method: "PUT", body: "{}" }))
      .rejects.toThrow(/context window/);
    await expect(gatewayApi("providers/reconcile", { method: "PUT", body: "{}" }))
      .rejects.toThrow(/128000/);
    await expect(gatewayApi("providers/reconcile", { method: "PUT", body: "{}" }))
      .rejects.toThrow(/Other Provider/);
  });

  it("no longer blames a slash for every not found", async () => {
    // The slash advice was printed on any 404 and told operators to rename ids that
    // are primary keys referenced by routes, aliases and usage history. Slashes are
    // now addressable, so the guess is gone.
    vi.stubGlobal("fetch", vi.fn(() => response({ detail: "Not Found" }, 404)));
    await expect(gatewayApi("models/does-not-exist/routing")).rejects.toThrow(/Not Found/);
    await expect(gatewayApi("models/does-not-exist/routing")).rejects.not.toThrow(/rename it without a slash/);
  });
});

describe("publish refusal clarity", () => {
  it("names stranded models and the way out when publish would strand them", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response({
      error: "publish_would_strand_models",
      message: "some enabled models have no route in this snapshot",
      models: ["deepseek-v4-flash"],
    }, 409)));

    await expect(gatewayApi("config/publish", { method: "POST" }))
      .rejects.toThrow(/stranded: "deepseek-v4-flash"/);
    await expect(gatewayApi("config/publish", { method: "POST" }))
      .rejects.toThrow(/Open Models and either disable or delete/);
  });

  it("prefers the server's sentence over the bare error code", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response({
      error: "provider_has_dependents",
      message: "Deleting this provider would also remove configuration that active models depend on.",
    }, 409)));

    await expect(gatewayApi("providers/p-1", { method: "DELETE" }))
      .rejects.toThrow(/remove configuration that active models depend on/);
    await expect(gatewayApi("providers/p-1", { method: "DELETE" }))
      .rejects.not.toThrow(/provider_has_dependents/);
  });
});

describe("duplicate mapping guard", () => {
  it("blocks the save and names the duplicate instead of a round-trip refusal", async () => {
    const fetchMock = fetchForWorkspace();
    vi.stubGlobal("fetch", fetchMock);
    render(<ProviderSetup provider={provider} onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);

    // Hydrate, then add a second card describing the exact same combination.
    expect(await screen.findByDisplayValue("latest, fast")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Model" }));
    // Only the new card renders a "New model ID" input; card 1 uses the select.
    const newIds = screen.getAllByLabelText(/^New model ID/);
    const names = screen.getAllByLabelText("Model display name");
    const upstreams = screen.getAllByLabelText(/^Provider model ID/);
    fireEvent.change(newIds[newIds.length - 1], { target: { value: "model-1" } });
    fireEvent.change(names[names.length - 1], { target: { value: "Model One" } });
    fireEvent.change(upstreams[upstreams.length - 1], { target: { value: "upstream-1" } });

    fireEvent.click(screen.getByRole("button", { name: "Save provider workspace" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/Two mappings describe the same model "model-1"/);
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining("providers/reconcile"), expect.anything());
  });
});

describe("served protocols control", () => {
  /** The guided create form, filled just far enough that a save passes validation. */
  async function fillNewProvider() {
    fireEvent.change(screen.getByLabelText("Provider name"), { target: { value: "New Provider" } });
    fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "https://new.example" } });
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "primary" } });
    fireEvent.change(screen.getByLabelText("Secret"), { target: { value: "secret-value" } });
    await userEvent.selectOptions(screen.getByLabelText(/^Catalogue model/), "__new__");
    fireEvent.change(screen.getByLabelText(/^New model ID/), { target: { value: "model-new" } });
    fireEvent.change(screen.getByLabelText("Model display name"), { target: { value: "Model New" } });
    fireEvent.change(screen.getByLabelText(/^Provider model ID/), { target: { value: "upstream-new" } });
  }

  const served = (name: RegExp) => screen.getByRole("checkbox", { name });
  /** Whether this API is relayed, converted, or cannot be served at all. */
  const marker = (name: RegExp) => served(name).closest("label")?.querySelector("em")?.textContent;

  it("cannot switch off the protocol the route speaks upstream", async () => {
    // Relaying bytes to its own protocol is not a setting. Offering it as one invited an
    // operator to claim a route is unreachable from the very API it speaks, which the
    // server normalizes away - so the form would show a state it could never save.
    vi.stubGlobal("fetch", fetchForWorkspace());
    render(<ProviderSetup onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);

    const native = served(/Anthropic Messages/);
    expect(native).toBeChecked();
    expect(native).toBeDisabled();
    expect(marker(/Anthropic Messages/)).toBe("native");
    expect(served(/OpenAI Chat Completions/)).toBeEnabled();
    expect(served(/OpenAI Chat Completions/)).not.toBeChecked();
    expect(marker(/OpenAI Chat Completions/)).toBe("translated");
  });

  it("carries a newly ticked client API through to the save payload", async () => {
    let payload: GatewayRow = {};
    vi.stubGlobal("fetch", fetchForWorkspace((value) => { payload = value; }));
    render(<ProviderSetup onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);
    await fillNewProvider();

    await userEvent.click(served(/OpenAI Chat Completions/));
    await userEvent.click(screen.getByRole("button", { name: "Create provider workspace" }));

    await waitFor(() => expect(payload.mappings).toBeDefined());
    // Normalized to the declared order, upstream protocol included, as the server stores it.
    expect((payload.mappings as GatewayRow[])[0].serves_protocols).toEqual([
      "anthropic_messages",
      "openai_chat_completions",
    ]);
  });

  it("keeps the old protocol served when the upstream protocol changes", async () => {
    // Changing how the gateway talks to a provider must not silently 404 the clients that
    // were reaching the model a moment ago. The previous protocol stays served, now by
    // translation, and the checkbox shows the new arrangement so it can be undone.
    let payload: GatewayRow = {};
    vi.stubGlobal("fetch", fetchForWorkspace((value) => { payload = value; }));
    render(<ProviderSetup onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);
    await fillNewProvider();

    await userEvent.selectOptions(screen.getByLabelText(/^Protocol/), "openai_chat_completions");

    // Anthropic is still answered, but now as a translation rather than a relay, and it
    // has become something the operator may switch off.
    expect(served(/Anthropic Messages/)).toBeChecked();
    expect(served(/Anthropic Messages/)).toBeEnabled();
    expect(marker(/Anthropic Messages/)).toBe("translated");
    expect(served(/OpenAI Chat Completions/)).toBeDisabled();
    expect(marker(/OpenAI Chat Completions/)).toBe("native");

    await userEvent.click(screen.getByRole("button", { name: "Create provider workspace" }));
    await waitFor(() => expect(payload.mappings).toBeDefined());
    const mapping = (payload.mappings as GatewayRow[])[0];
    expect(mapping.protocol).toBe("openai_chat_completions");
    expect(mapping.serves_protocols).toEqual(["anthropic_messages", "openai_chat_completions"]);
  });

  it("hydrates a stored translation and round-trips it unchanged", async () => {
    const translating = [{ ...workspace["provider-models"][0], serves_protocols: ["anthropic_messages", "openai_responses"] }];
    let payload: GatewayRow = {};
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input).split("/").pop() ?? "";
      if (init?.method === "PUT") {
        payload = JSON.parse(String(init.body));
        return response({ provider_id: "provider-1" });
      }
      if (path === "provider-models") return response({ data: translating });
      return response({ data: workspace[path] ?? [] });
    }));
    render(<ProviderSetup provider={provider} onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);
    await screen.findByDisplayValue("upstream-1");

    expect(served(/Anthropic Messages/)).toBeChecked();
    expect(served(/OpenAI Responses/)).toBeChecked();
    expect(served(/OpenAI Chat Completions/)).not.toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: "Save provider workspace" }));
    await waitFor(() => expect(payload.mappings).toBeDefined());
    expect((payload.mappings as GatewayRow[])[0].serves_protocols).toEqual([
      "anthropic_messages",
      "openai_responses",
    ]);
  });

  it("reads a mapping stored before the column existed as native only", async () => {
    // A pre-migration row reports no serves_protocols at all. It answered exactly its
    // upstream protocol then, and saving it back must not widen that.
    let payload: GatewayRow = {};
    vi.stubGlobal("fetch", fetchForWorkspace((value) => { payload = value; }));
    render(<ProviderSetup provider={provider} onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);
    await screen.findByDisplayValue("upstream-1");

    expect(served(/Anthropic Messages/)).toBeChecked();
    expect(served(/OpenAI Chat Completions/)).not.toBeChecked();
    expect(served(/OpenAI Responses/)).not.toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: "Save provider workspace" }));
    await waitFor(() => expect(payload.mappings).toBeDefined());
    expect((payload.mappings as GatewayRow[])[0].serves_protocols).toEqual(["anthropic_messages"]);
  });

  it("says what an unticked endpoint costs a client", async () => {
    // The consequence is a 404 on a healthy route, which is the confusing case: the
    // provider is up, the model exists, and the client still gets nothing.
    vi.stubGlobal("fetch", fetchForWorkspace());
    render(<ProviderSetup onClose={vi.fn()} onSaved={vi.fn(async () => undefined)} />);

    expect(screen.getByText(/A client calling an unchecked endpoint gets a 404 for this model/)).toBeVisible();
    expect(screen.getByRole("group", { name: "Client APIs this mapping answers" })).toBeVisible();
  });
});

describe("scratch hydrate copy", () => {
  it("copies the proven hydrate pattern verbatim", async () => {
    vi.stubGlobal("fetch", fetchForWorkspace());
    const onClose = vi.fn();
    render(<ProviderSetup provider={provider} onClose={onClose} onSaved={vi.fn(async () => undefined)} />);
    try {
      expect(await screen.findByDisplayValue("latest, fast")).toBeVisible();
    } catch (error) {
      console.log("HYDRATE FAILED:", (error as Error).message.slice(0, 200));
      throw error;
    }
  });
});
