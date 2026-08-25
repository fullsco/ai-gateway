import asyncio

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import ClientPermissions, GatewayClient, InMemoryGatewayKeyStore
from gateway.config import Settings
from gateway.models import CanonicalModel, ModelRegistry, ProviderModel
from gateway.protocols import Capability, ClientProtocol
from gateway.providers import Credential, ProviderConfig
from gateway.providers.anthropic import AnthropicCompatibleAdapter
from gateway.routing import CredentialState, ProviderState, RoutingEngine
from gateway.runtime import GatewayRuntime
from gateway.security.gateway_keys import GatewayKeyHasher


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class DelayedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(1)
        yield b"never"

    async def aclose(self) -> None:
        return None


def make_runtime(handler, *, allow_model_fallback: bool = False, alternate_route: bool = False):
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key", client_id="client")
    client = GatewayClient(
        id="client",
        name="Claude Code",
        permissions=ClientPermissions(frozenset({ClientProtocol.ANTHROPIC_MESSAGES})),
    )
    capabilities = frozenset({Capability.STREAMING})
    provider_models = [
        ProviderModel(
            "provider-model",
            "model-x",
            "provider-a",
            "upstream-x",
            ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities,
            priority=10,
            allow_model_fallback=allow_model_fallback,
        )
    ]
    if alternate_route:
        provider_models.append(
            ProviderModel(
                "provider-model-b",
                "model-x",
                "provider-b",
                "upstream-b",
                ClientProtocol.ANTHROPIC_MESSAGES,
                capabilities,
                priority=20,
            )
        )
    registry = ModelRegistry(
        [CanonicalModel("model-x", frozenset(), capabilities)],
        provider_models,
    )
    adapter = AnthropicCompatibleAdapter(
        ProviderConfig(
            id="provider-a",
            name="Provider A",
            base_url="https://upstream.example",
            protocol=ClientProtocol.ANTHROPIC_MESSAGES,
            capabilities=capabilities,
        )
    )
    adapters = {"provider-model": adapter}
    if alternate_route:
        adapters["provider-model-b"] = AnthropicCompatibleAdapter(
            ProviderConfig(
                id="provider-b",
                name="Provider B",
                base_url="https://fallback.example",
                protocol=ClientProtocol.ANTHROPIC_MESSAGES,
                capabilities=capabilities,
            )
        )
    runtime = GatewayRuntime(
        key_store=InMemoryGatewayKeyStore([issued.record], [client]),
        key_hasher=hasher,
        model_registry=registry,
        routing_engine=RoutingEngine(registry),
        provider_states=(ProviderState("provider-a"), ProviderState("provider-b")),
        credential_states=(
            CredentialState("credential-a", "provider-a", priority=10),
            CredentialState("credential-b", "provider-a", priority=20),
            CredentialState("credential-c", "provider-b", priority=10),
        ),
        provider_model_adapters=adapters,
        credentials={
            "credential-a": Credential(id="credential-a", secret="provider-key-a"),
            "credential-b": Credential(id="credential-b", secret="provider-key-b"),
            "credential-c": Credential(id="credential-c", secret="provider-key-c"),
        },
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return runtime, issued.plaintext


def test_rate_limit_rotates_to_next_credential() -> None:
    used_keys = []

    def handler(request: httpx.Request):
        used_keys.append(request.headers["x-api-key"])
        if request.headers["x-api-key"] == "provider-key-a":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"type": "message", "id": "success"})

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "messages": []},
        )

    assert response.status_code == 200
    assert response.json()["id"] == "success"
    assert used_keys == ["provider-key-a", "provider-key-b"]


def test_empty_stream_fails_over_before_committing_response() -> None:
    used_keys = []

    def handler(request: httpx.Request):
        used_keys.append(request.headers["x-api-key"])
        chunks = [] if request.headers["x-api-key"] == "provider-key-a" else [b"data: ok\n\n"]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BytesStream(chunks),
        )

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    assert response.content == b"data: ok\n\n"
    assert used_keys == ["provider-key-a", "provider-key-b"]


def test_first_event_timeout_releases_route_and_fails_over() -> None:
    used_keys = []

    def handler(request: httpx.Request):
        used_keys.append(request.headers["x-api-key"])
        stream = (
            DelayedStream()
            if request.headers["x-api-key"] == "provider-key-a"
            else BytesStream([b"data: ok\n\n"])
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    runtime, key = make_runtime(handler)
    app = create_app(
        Settings(environment="test", first_event_timeout_seconds=0.01, _env_file=None),
        runtime,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    assert response.content == b"data: ok\n\n"
    assert used_keys == ["provider-key-a", "provider-key-b"]


def test_failed_route_does_not_fallback_to_another_mapping_without_permission() -> None:
    used_hosts = []

    def handler(request: httpx.Request):
        used_hosts.append(request.url.host)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    runtime, key = make_runtime(handler, alternate_route=True)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "messages": []},
        )

    assert response.status_code == 503
    assert used_hosts == ["upstream.example", "upstream.example"]


def test_failed_route_falls_back_to_another_mapping_when_permitted() -> None:
    used_hosts = []

    def handler(request: httpx.Request):
        used_hosts.append(request.url.host)
        if request.url.host == "upstream.example":
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"id": "fallback-success"})

    runtime, key = make_runtime(
        handler, allow_model_fallback=True, alternate_route=True
    )
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "messages": []},
        )

    assert response.status_code == 200
    # 503 is PROVIDER_UNAVAILABLE, which _RETRY_SEMANTICS scopes to the provider.
    # A second key against the same dead host cannot help, so the attempt is spent
    # on the fallback provider instead. This assertion previously expected
    # upstream twice, which encoded the executor ignoring retry_scope entirely.
    assert used_hosts == ["upstream.example", "fallback.example"]


def test_an_upstream_that_truncates_a_stream_is_blamed_for_it() -> None:
    """A stream cut short by the provider must not read as a client cancellation.

    The response is already committed with 200 when this happens, so there is no
    status code left to tell the truth with. The exception used to escape the
    streaming generator and reach uvicorn as an unhandled ASGI error: it logged a
    traceback that implied a fault in the gateway, recorded the attempt as merely
    "cancelled", and submitted nothing to health, so a provider that habitually cuts
    streams short was never scored for it and looked exactly like a user pressing
    Ctrl-C. Eleven of these were sitting in the production log.
    """

    class TruncatedStream(httpx.AsyncByteStream):
        """Sends a first event, so the response commits, then dies mid-body."""

        async def __aiter__(self):
            yield b"event: message_start\ndata: {}\n\n"
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=TruncatedStream()
        )

    runtime, key = make_runtime(handler)
    app = create_app(Settings(environment="test", _env_file=None), runtime)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "stream": True, "messages": []},
        )

    # The committed 200 stands, but the client is told the stream did not finish
    # rather than being left to treat a truncated answer as a complete one.
    assert response.status_code == 200
    assert b"upstream_truncated" in response.content
    assert b"message_start" in response.content


def test_stream_keepalive_holds_connection_open_during_upstream_silence():
    """A model that thinks longer than the edge read timeout must not be cut off.

    Cloudflare measures proxy_read_timeout between bytes delivered to the client, and
    on this Free zone it is 125s and not editable. An upstream that goes quiet for
    longer than that has its client connection killed by the edge while the gateway
    and the provider are both still healthy and still working. The client sees a 524,
    or worse a stream that simply stops and is indistinguishable from a complete one.
    Production ran 9.2% of attempts past 120s and paid $21 in one day for answers it
    could not deliver.

    A no-op event keeps the byte clock alive without adding anything the client has to
    understand: Anthropic's own API sends `ping`, so its clients already ignore it.
    """

    class SilentThenFinishes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: message_start\ndata: {}\n\n"
            await asyncio.sleep(0.25)
            yield b"event: message_stop\ndata: {}\n\n"

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SilentThenFinishes(),
        )

    runtime, key = make_runtime(handler)
    app = create_app(
        Settings(environment="test", stream_keepalive_seconds=0.05, _env_file=None),
        runtime,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    # The connection was kept warm across the silence.
    assert b"event: ping" in response.content
    # Real events are still relayed, in order, and the stream still completes.
    assert b"message_start" in response.content
    assert b"message_stop" in response.content
    assert response.content.index(b"message_start") < response.content.index(b"event: ping")
    assert response.content.index(b"event: ping") < response.content.index(b"message_stop")


def test_stream_keepalive_never_splices_into_a_partial_event():
    """A stall part-way through an event must not have a keepalive spliced into it.

    The upstream body arrives as raw network chunks and an event can straddle two of
    them. Injecting during that gap would produce `data: {"usa` + `event: ping`, which
    no client can parse, so a liveness fix would become a corruption bug. The keepalive
    is therefore only ever emitted while the client sits on an event boundary.
    """

    class StallsMidEvent(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: message_start\ndata: {}\n\n"
            yield b'event: message_delta\ndata: {"par'   # deliberately unterminated
            await asyncio.sleep(0.25)
            yield b'tial": true}\n\nevent: message_stop\ndata: {}\n\n'

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=StallsMidEvent()
        )

    runtime, key = make_runtime(handler)
    app = create_app(
        Settings(environment="test", stream_keepalive_seconds=0.05, _env_file=None),
        runtime,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-api-key": key},
            json={"model": "model-x", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    # The split event is reassembled exactly as sent, with nothing injected into it.
    assert b'event: message_delta\ndata: {"partial": true}\n\n' in response.content
    assert b'{"par' + b"event: ping" not in response.content
    assert response.content.endswith(b"event: message_stop\ndata: {}\n\n")


def test_stream_keepalive_does_not_alter_recorded_usage():
    """Keepalives must not reach the usage parser or the bill changes.

    Cost is derived by feeding relayed bytes to a streaming SSE usage extractor. If
    synthetic frames were fed in alongside real ones they would shift the parser's
    buffer and could drop the `message_delta` carrying the token counts, which is
    silent: the request records as having cost nothing.
    """
    usage_event = (
        b"event: message_delta\n"
        b'data: {"usage": {"input_tokens": 11, "output_tokens": 7}}\n\n'
    )

    class SilentBeforeUsage(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: message_start\ndata: {}\n\n"
            await asyncio.sleep(0.25)          # forces several keepalives
            yield usage_event
            yield b"event: message_stop\ndata: {}\n\n"

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SilentBeforeUsage()
        )

    def run(keepalive: float) -> bytes:
        runtime, key = make_runtime(handler)
        app = create_app(
            Settings(environment="test", stream_keepalive_seconds=keepalive, _env_file=None),
            runtime,
        )
        with TestClient(app) as client:
            return client.post(
                "/v1/messages",
                headers={"x-api-key": key},
                json={"model": "model-x", "stream": True, "messages": []},
            ).content

    with_keepalive = run(0.05)
    without_keepalive = run(0)

    assert b"event: ping" in with_keepalive
    assert b"event: ping" not in without_keepalive
    # Stripping the synthetic frames reproduces the untouched byte stream exactly, so
    # everything the usage parser is fed is identical with the feature on and off.
    assert with_keepalive.replace(b"event: ping\ndata: {}\n\n", b"") == without_keepalive
    assert usage_event in with_keepalive
