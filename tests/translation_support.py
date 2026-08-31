"""Fixtures shared by the protocol translation tests.

Kept out of the test modules themselves so each stays under the file-size limit and so
the unit tests and the integration tests decode SSE the same way -- a translator that
passed only because its own test parsed frames loosely would be worth nothing.
"""

import base64
import json

import httpx

from gateway.configuration import RuntimeBuilder
from gateway.protocols import ClientProtocol
from gateway.protocols.translate import TranslationContext, get_translator
from gateway.security import CredentialCipher, GatewayKeyHasher
from tests.test_observability import TelemetryPool

ANTHROPIC = ClientProtocol.ANTHROPIC_MESSAGES
CHAT = ClientProtocol.OPENAI_CHAT_COMPLETIONS
RESPONSES = ClientProtocol.OPENAI_RESPONSES

CONTEXT = TranslationContext(requested_model="glm-5.3")
THINKING_CONTEXT = TranslationContext(requested_model="glm-5.3", reasoning_requested=True)

ENCRYPTION_KEY = base64.b64encode(b"e" * 32).decode()
PEPPER = base64.b64encode(b"p" * 32).decode()

PRICING = {"input_per_million": 1, "output_per_million": 2, "currency": "USD"}


def translator(client: ClientProtocol, upstream: ClientProtocol):
    found = get_translator(client, upstream)
    assert found is not None, f"no translator for {client.value} <- {upstream.value}"
    return found


def parse_sse(data: bytes | str) -> list[tuple[str | None, object]]:
    """SSE bytes as (event name, decoded data) pairs, in order."""
    text = data.decode() if isinstance(data, bytes) else data
    events: list[tuple[str | None, object]] = []
    for raw in text.split("\n\n"):
        if not raw.strip():
            continue
        name: str | None = None
        payloads: list[str] = []
        for line in raw.splitlines():
            field, _, value = line.partition(":")
            body = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = body
            elif field == "data":
                payloads.append(body)
        joined = "\n".join(payloads)
        events.append((name, joined if joined == "[DONE]" else json.loads(joined)))
    return events


def frame_names(data: bytes) -> list[str]:
    return [
        name or (payload if isinstance(payload, str) else "data")
        for name, payload in parse_sse(data)
    ]


class PricedPool(TelemetryPool):
    """A telemetry pool that answers the spend reservation with "within limit".

    TelemetryPool returns the next attempt id for any fetchval it does not recognise,
    which `reserve_client_spending` reads as a limit already reached. The routes here
    carry a rate card on purpose -- cost landing in the usage record is half of what
    the streaming test asserts -- so the reservation has to answer properly rather
    than be sidestepped by dropping the pricing.
    """

    async def fetchval(self, query: str, *args: object) -> object:
        if "reserve_client_spending" in query:
            self.execute_calls.append((query, args))
            return None
        return await super().fetchval(query, *args)


def _provider(identifier: str, host: str, protocol: str) -> dict:
    return {
        "id": identifier,
        "name": identifier,
        "base_url": f"https://{host}",
        "protocol": protocol,
        "capabilities": ["streaming", "tool_calling"],
    }


def _mapping(
    identifier: str,
    provider_id: str,
    protocol: str,
    serves: list[str],
    priority: int,
) -> dict:
    return {
        "id": identifier,
        "canonical_model_id": "glm-5.3",
        "provider_id": provider_id,
        "upstream_model_id": f"upstream-{identifier}",
        "protocol": protocol,
        "serves_protocols": serves,
        "capabilities": ["streaming", "tool_calling"],
        "priority": priority,
        "pricing": PRICING,
    }


def build_translating_runtime(handler, *, native_route: bool = False):
    """A canonical model whose only route speaks Chat Completions upstream.

    Before served protocols existed this configuration answered `/v1/messages` with
    404 `model_unavailable`, which is exactly the wall Claude Code hit on every
    OpenAI-protocol model in production. With `native_route`, a higher-priority
    Anthropic-native mapping is added for the same model so route ordering and
    failover between a native and a translated route can be observed.
    """
    cipher = CredentialCipher.from_base64(ENCRYPTION_KEY)
    hasher = GatewayKeyHasher.from_base64(PEPPER)
    issued = hasher.issue(key_id="key", client_id="client")

    providers = [_provider("chat-provider", "chat.example", "openai_chat_completions")]
    mappings = [
        _mapping(
            "chat-mapping",
            "chat-provider",
            "openai_chat_completions",
            ["openai_chat_completions", "anthropic_messages"],
            20,
        )
    ]
    if native_route:
        providers.append(_provider("anthropic-provider", "anthropic.example", "anthropic_messages"))
        mappings.append(
            _mapping(
                "native-mapping",
                "anthropic-provider",
                "anthropic_messages",
                ["anthropic_messages"],
                10,
            )
        )

    credentials = []
    for provider_id in ("chat-provider", "anthropic-provider"):
        envelope = cipher.encrypt(
            f"key-for-{provider_id}", context=f"provider-credential:{provider_id}-credential"
        )
        credentials.append(
            {
                "id": f"{provider_id}-credential",
                "provider_id": provider_id,
                "secret_nonce": envelope.nonce,
                "secret_ciphertext": envelope.ciphertext,
                "priority": 10,
            }
        )

    payload = {
        "clients": [
            {
                "id": "client",
                "name": "Claude Code",
                "allowed_protocols": ["anthropic_messages", "openai_chat_completions"],
            }
        ],
        "gateway_keys": [
            {
                "id": "key",
                "client_id": "client",
                "key_prefix": issued.record.key_prefix,
                "key_digest": issued.record.digest,
            }
        ],
        "providers": providers,
        "credentials": credentials,
        "models": [{"id": "glm-5.3", "capabilities": ["streaming", "tool_calling"]}],
        "provider_models": mappings,
    }
    runtime = RuntimeBuilder(encryption_key=ENCRYPTION_KEY, key_pepper=PEPPER).build(payload)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return runtime.__class__(**{**runtime.__dict__, "http_client": client}), issued.plaintext
