from datetime import UTC, datetime, timedelta

from gateway.auth import (
    AuthDenial,
    ClientPermissions,
    GatewayClient,
    InMemoryGatewayKeyStore,
    authenticate_request,
)
from gateway.protocols import ClientProtocol
from gateway.security.gateway_keys import GatewayKey, GatewayKeyHasher


def make_authenticated_store():
    hasher = GatewayKeyHasher(b"p" * 32)
    issued = hasher.issue(key_id="key-1", client_id="client-1")
    client = GatewayClient(
        id="client-1",
        name="Claude Code",
        permissions=ClientPermissions(
            protocols=frozenset({ClientProtocol.ANTHROPIC_MESSAGES}),
            allowed_models=frozenset({"claude-example"}),
        ),
    )
    return hasher, issued, InMemoryGatewayKeyStore([issued.record], [client])


def test_authenticates_anthropic_and_openai_header_styles() -> None:
    hasher, issued, store = make_authenticated_store()

    by_api_key = authenticate_request(
        {"x-api-key": issued.plaintext},
        ClientProtocol.ANTHROPIC_MESSAGES,
        "claude-example",
        store=store,
        hasher=hasher,
    )
    by_bearer = authenticate_request(
        {"Authorization": f"Bearer {issued.plaintext}"},
        ClientProtocol.ANTHROPIC_MESSAGES,
        "claude-example",
        store=store,
        hasher=hasher,
    )

    assert by_api_key is not None
    assert by_api_key.key_id == "key-1"
    assert by_bearer is not None


def test_authentication_rejects_tampered_key_and_disallowed_request() -> None:
    hasher, issued, store = make_authenticated_store()

    tampered = authenticate_request(
        {"x-api-key": f"{issued.plaintext}x"},
        ClientProtocol.ANTHROPIC_MESSAGES,
        "claude-example",
        store=store,
        hasher=hasher,
    )
    wrong_protocol = authenticate_request(
        {"x-api-key": issued.plaintext},
        ClientProtocol.OPENAI_CHAT_COMPLETIONS,
        "claude-example",
        store=store,
        hasher=hasher,
    )
    wrong_model = authenticate_request(
        {"x-api-key": issued.plaintext},
        ClientProtocol.ANTHROPIC_MESSAGES,
        "other-model",
        store=store,
        hasher=hasher,
    )

    # The reason matters: a tampered secret and a permission gap are different
    # problems with different fixes, and reporting both as an invalid key sent
    # operators hunting for a bad secret when the client simply lacked a protocol.
    assert tampered is AuthDenial.INVALID_KEY
    assert wrong_protocol is AuthDenial.PROTOCOL_NOT_PERMITTED
    assert wrong_model is AuthDenial.MODEL_NOT_PERMITTED


def test_key_plaintext_is_not_exposed_by_its_record() -> None:
    hasher, issued, _ = make_authenticated_store()

    assert issued.plaintext not in repr(issued.record)
    assert issued.plaintext not in repr(hasher)


def test_authentication_rejects_expired_gateway_key() -> None:
    hasher, issued, _ = make_authenticated_store()
    expired = GatewayKey(
        id=issued.record.id,
        client_id=issued.record.client_id,
        key_prefix=issued.record.key_prefix,
        digest=issued.record.digest,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    client = GatewayClient(
        id="client-1",
        name="Claude Code",
        permissions=ClientPermissions(
            protocols=frozenset({ClientProtocol.ANTHROPIC_MESSAGES})
        ),
    )
    store = InMemoryGatewayKeyStore([expired], [client])

    authenticated = authenticate_request(
        {"x-api-key": issued.plaintext},
        ClientProtocol.ANTHROPIC_MESSAGES,
        "claude-example",
        store=store,
        hasher=hasher,
    )

    assert authenticated is AuthDenial.KEY_EXPIRED
