from typing import Any
from urllib.parse import urlencode

import httpx

from gateway.protocols import ClientProtocol, NormalizedRequest
from gateway.providers.base import (
    DEFAULT_USER_AGENT,
    Credential,
    ProviderAdapter,
    ProviderConfig,
    ProviderError,
    UpstreamRequest,
    build_provider_error,
    classify_upstream_status,
)

ENDPOINTS = {
    ClientProtocol.OPENAI_CHAT_COMPLETIONS: "/v1/chat/completions",
    ClientProtocol.OPENAI_RESPONSES: "/v1/responses",
}


class OpenAICompatibleAdapter(ProviderAdapter):
    def __init__(
        self,
        config: ProviderConfig,
        *,
        default_headers: dict[str, str] | None = None,
        endpoint_query: dict[str, str] | None = None,
    ) -> None:
        if config.protocol not in ENDPOINTS:
            raise ValueError("OpenAI adapter requires Chat Completions or Responses protocol")
        self.config = config
        # A provider or mapping may still override it; only the absence is fixed.
        self.default_headers = {"user-agent": DEFAULT_USER_AGENT, **(default_headers or {})}
        self.endpoint_query = dict(endpoint_query or {})

    def validate_request(self, request: NormalizedRequest) -> None:
        if request.protocol is not self.config.protocol:
            raise ValueError("Provider does not support the requested OpenAI protocol")
        missing = request.required_capabilities - self.config.capabilities
        if missing:
            names = ", ".join(sorted(capability.value for capability in missing))
            raise ValueError(f"Provider does not support required capabilities: {names}")

    def create_request(
        self,
        request: NormalizedRequest,
        credential: Credential,
        incoming_headers: dict[str, str] | None = None,
    ) -> UpstreamRequest:
        self.validate_request(request)
        headers = {
            "accept": "text/event-stream" if request.stream else "application/json",
            "content-type": "application/json",
            **self.default_headers,
            "authorization": f"Bearer {credential.secret}",
        }
        url = f"{str(self.config.base_url).rstrip('/')}{ENDPOINTS[request.protocol]}"
        if self.endpoint_query:
            url = f"{url}?{urlencode(self.endpoint_query)}"
        return UpstreamRequest(
            url=url,
            headers=headers,
            json_body=self._upstream_payload(request),
            timeout=httpx.Timeout(self.config.timeout_seconds),
        )

    @staticmethod
    def _upstream_payload(request: NormalizedRequest) -> dict[str, Any]:
        """Ask for usage on streamed Chat Completions.

        Chat Completions emits no usage over SSE unless the caller opts in, so
        every streamed request produced no usage record and therefore no cost at
        all. A caller that set stream_options itself is left alone.
        """
        if (
            not request.stream
            or request.protocol is not ClientProtocol.OPENAI_CHAT_COMPLETIONS
            or "stream_options" in request.payload
        ):
            return request.payload
        return {**request.payload, "stream_options": {"include_usage": True}}

    def normalize_error(self, response: httpx.Response) -> ProviderError:
        message, error_type, error_code = self._extract_error(response)
        # The code is folded in here but not in the Anthropic adapter for the one
        # protocol-specific reason there is: OpenAI error objects carry a `code` field
        # and Anthropic's do not. Everything downstream of `searchable` is shared.
        searchable = f"{error_type} {error_code} {message}".lower()
        return build_provider_error(
            classify_upstream_status(
                response.status_code,
                searchable,
                waf_rejection=self._is_waf_rejection(response),
            ),
            message,
            upstream_status=response.status_code,
            retry_after_seconds=self._retry_after(response.headers.get("retry-after")),
        )

    def create_probe_request(
        self,
        credential: Credential,
        *,
        model: str | None = None,
    ) -> UpstreamRequest:
        return UpstreamRequest(
            # GET rather than HEAD: relays commonly route only GET on /v1/models
            # and answer HEAD with 404, which made every probe look like a
            # failure. GET is still free and still authenticates the credential.
            method="GET",
            url=f"{str(self.config.base_url).rstrip('/')}/v1/models",
            headers={
                "accept": "application/json",
                **self.default_headers,
                "authorization": f"Bearer {credential.secret}",
            },
            timeout=httpx.Timeout(min(self.config.timeout_seconds, 10)),
        )

    @staticmethod
    def _is_waf_rejection(response: httpx.Response) -> bool:
        """True when the body is not a provider error envelope.

        A challenge or interstitial page is HTML, or JSON without an error object.
        """
        try:
            payload = response.json()
        except ValueError:
            return True
        return not (isinstance(payload, dict) and isinstance(payload.get("error"), dict))

    @staticmethod
    def _extract_error(response: httpx.Response) -> tuple[str, str, str]:
        try:
            payload: Any = response.json()
        except ValueError:
            return "Upstream provider request failed.", "", ""
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if not isinstance(error, dict):
            return "Upstream provider request failed.", "", ""
        message = error.get("message")
        error_type = error.get("type")
        error_code = error.get("code")
        return (
            message[:500] if isinstance(message, str) else "Upstream provider request failed.",
            error_type[:100] if isinstance(error_type, str) else "",
            error_code[:100] if isinstance(error_code, str) else "",
        )

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        try:
            parsed = float(value) if value is not None else None
        except ValueError:
            return None
        return parsed if parsed is not None and parsed >= 0 else None
