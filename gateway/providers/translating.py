"""An adapter that serves a client protocol the upstream provider does not speak.

Route selection used to require the client and upstream protocols to be identical, so a
model reachable only over Chat Completions answered 404 to Claude Code, which speaks
only Anthropic Messages. This wrapper closes that gap without touching either concrete
adapter: it restates the request in the upstream's protocol and then hands it to the
real adapter, so auth scheme, default headers, endpoint query, user agent and the
streamed-usage opt-in all still apply exactly as they do on a native route.

Only the request direction lives here. The response direction is applied by the
executor, which has to keep feeding *upstream* bytes to usage extraction while sending
translated bytes to the client -- a split this adapter cannot see.
"""

import httpx

from gateway.protocols import ClientProtocol, NormalizedRequest
from gateway.protocols.translate import ProtocolTranslator
from gateway.providers.base import (
    Credential,
    ProviderAdapter,
    ProviderError,
    UpstreamRequest,
)


class TranslatingAdapter(ProviderAdapter):
    """Serves `translator.client_protocol` from an upstream speaking its other side."""

    def __init__(self, inner: ProviderAdapter, translator: ProtocolTranslator) -> None:
        # Caught here rather than at the first request so a mapping configured to serve
        # a protocol its provider cannot reach fails when the snapshot is built.
        upstream = getattr(getattr(inner, "config", None), "protocol", None)
        if upstream is not None and upstream is not translator.upstream_protocol:
            raise ValueError(
                "Translator expects an upstream speaking "
                f"{translator.upstream_protocol.value}, but the provider speaks "
                f"{upstream.value}"
            )
        self.inner = inner
        self.translator = translator

    @property
    def client_protocol(self) -> ClientProtocol:
        return self.translator.client_protocol

    @property
    def upstream_protocol(self) -> ClientProtocol:
        return self.translator.upstream_protocol

    def validate_request(self, request: NormalizedRequest) -> None:
        self._to_upstream(request)

    def create_request(
        self,
        request: NormalizedRequest,
        credential: Credential,
        incoming_headers: dict[str, str] | None = None,
    ) -> UpstreamRequest:
        return self.inner.create_request(
            self._to_upstream(request),
            credential,
            incoming_headers,
        )

    def normalize_error(self, response: httpx.Response) -> ProviderError:
        # Upstream errors already leave the gateway in one shape via api/errors.py, so
        # the wrapped adapter's classification is the whole answer.
        return self.inner.normalize_error(response)

    def create_probe_request(
        self,
        credential: Credential,
        *,
        model: str | None = None,
    ) -> UpstreamRequest:
        # A probe tests whether the upstream and this credential are reachable, which
        # is a question about the upstream itself; it is asked in the upstream's own
        # protocol and never translated.
        return self.inner.create_probe_request(credential, model=model)

    def _to_upstream(self, request: NormalizedRequest) -> NormalizedRequest:
        """The same request restated in the upstream's protocol.

        `required_capabilities` carries over untouched rather than being re-derived
        from the translated payload: a capability is a property of what the client
        asked for, and the translated wire form no longer shows it -- an Anthropic
        `thinking` block becomes `reasoning_effort`, which no derivation rule reads.
        The wrapped adapter then applies exactly the check it applies natively.
        """
        if request.protocol is not self.client_protocol:
            raise ValueError(
                f"Route serves {self.client_protocol.value} requests, "
                f"not {request.protocol.value}"
            )
        upstream = request.model_copy(
            update={
                "protocol": self.upstream_protocol,
                "payload": self.translator.translate_request(request.payload),
            }
        )
        self.inner.validate_request(upstream)
        return upstream
