"""Translation between the client protocol a request arrives on and a route's upstream one.

The gateway matched routes protocol-natively, so a model reachable only over Chat
Completions returned 404 to a client that speaks Anthropic Messages -- no env var or
model name could bridge it. This package supplies the missing mapping in both
directions for every pair of protocols the gateway knows.

Lookup returns None for an identity pair, which is how the executor keeps its verbatim
byte-relay path for routes that need no translation.
"""

from gateway.protocols.models import ClientProtocol
from gateway.protocols.translate.anthropic_from_chat import AnthropicFromChatCompletions
from gateway.protocols.translate.base import (
    ChainedTranslator,
    ProtocolTranslator,
    SSEDecoder,
    SSEEvent,
    StreamTranslator,
    TranslationContext,
    format_sse,
)
from gateway.protocols.translate.chat_from_anthropic import ChatCompletionsFromAnthropic
from gateway.protocols.translate.chat_from_responses import ChatCompletionsFromResponses
from gateway.protocols.translate.responses_from_chat import ResponsesFromChatCompletions

_ANTHROPIC = ClientProtocol.ANTHROPIC_MESSAGES
_CHAT = ClientProtocol.OPENAI_CHAT_COMPLETIONS
_RESPONSES = ClientProtocol.OPENAI_RESPONSES


def _build_registry() -> dict[tuple[ClientProtocol, ClientProtocol], ProtocolTranslator]:
    anthropic_from_chat = AnthropicFromChatCompletions()
    chat_from_anthropic = ChatCompletionsFromAnthropic()
    responses_from_chat = ResponsesFromChatCompletions()
    chat_from_responses = ChatCompletionsFromResponses()
    registry: dict[tuple[ClientProtocol, ClientProtocol], ProtocolTranslator] = {
        (_ANTHROPIC, _CHAT): anthropic_from_chat,
        (_CHAT, _ANTHROPIC): chat_from_anthropic,
        (_RESPONSES, _CHAT): responses_from_chat,
        (_CHAT, _RESPONSES): chat_from_responses,
    }
    # The Anthropic <-> Responses pairs are composed through Chat Completions rather
    # than mapped again by hand, so there is one mapping table per pair of shapes and
    # a correction to the Anthropic rules cannot drift between two copies.
    registry[(_ANTHROPIC, _RESPONSES)] = ChainedTranslator(anthropic_from_chat, chat_from_responses)
    registry[(_RESPONSES, _ANTHROPIC)] = ChainedTranslator(responses_from_chat, chat_from_anthropic)
    return registry


_TRANSLATORS = _build_registry()


def get_translator(
    client_protocol: ClientProtocol,
    upstream_protocol: ClientProtocol,
) -> ProtocolTranslator | None:
    """The translator for one direction, or None when the bytes pass through unchanged."""
    if client_protocol is upstream_protocol:
        return None
    return _TRANSLATORS.get((client_protocol, upstream_protocol))


def can_translate(
    client_protocol: ClientProtocol,
    upstream_protocol: ClientProtocol,
) -> bool:
    """Whether a route speaking `upstream_protocol` can answer `client_protocol` at all."""
    return (
        client_protocol is upstream_protocol
        or (client_protocol, upstream_protocol) in _TRANSLATORS
    )


def translatable_pairs() -> tuple[tuple[ClientProtocol, ClientProtocol], ...]:
    return tuple(sorted(_TRANSLATORS, key=lambda pair: (pair[0].value, pair[1].value)))


__all__ = [
    "ChainedTranslator",
    "ProtocolTranslator",
    "SSEDecoder",
    "SSEEvent",
    "StreamTranslator",
    "TranslationContext",
    "can_translate",
    "format_sse",
    "get_translator",
    "translatable_pairs",
]
