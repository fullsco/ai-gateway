from typing import Any
from urllib.parse import urlencode

import httpx

from gateway.protocols import ClientProtocol, NormalizedRequest
from gateway.providers.base import (
    Credential,
    ErrorCategory,
    ProviderAdapter,
    ProviderConfig,
    ProviderError,
    UpstreamRequest,
)

ENDPOINTS = {
    ClientProtocol.OPENAI_CHAT_COMPLETIONS: "/v1/chat/completions",
    ClientProtocol.OPENAI_RESPONSES: "/v1/responses",
}
QUOTA_MARKERS = ("quota", "insufficient_quota", "billing", "credit balance")


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
        self.default_headers = dict(default_headers or {})
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
            json_body=request.payload,
            timeout=httpx.Timeout(self.config.timeout_seconds),
        )

    def normalize_error(self, response: httpx.Response) -> ProviderError:
        message, error_type, error_code = self._extract_error(response)
        searchable = f"{error_type} {error_code} {message}".lower()
        retry_after = self._retry_after(response.headers.get("retry-after"))

        if response.status_code in {401, 403}:
            category, retryable = ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR, False
        elif response.status_code in {402, 429} and any(
            marker in searchable for marker in QUOTA_MARKERS
        ):
            category, retryable = ErrorCategory.QUOTA_EXHAUSTED, False
        elif response.status_code == 429:
            category, retryable = ErrorCategory.RATE_LIMIT, True
        elif response.status_code in {408, 504}:
            category, retryable = ErrorCategory.TIMEOUT, True
        elif response.status_code >= 500:
            category, retryable = ErrorCategory.PROVIDER_UNAVAILABLE, True
        elif response.status_code == 404 and "model" in searchable:
            category, retryable = ErrorCategory.MODEL_UNAVAILABLE, False
        elif 400 <= response.status_code < 500:
            category, retryable = ErrorCategory.INVALID_REQUEST, False
        else:
            category, retryable = ErrorCategory.INTERNAL_ERROR, False

        return ProviderError(
            category=category,
            message=message,
            retryable=retryable,
            upstream_status=response.status_code,
            retry_after_seconds=retry_after,
        )

    def create_probe_request(self, credential: Credential) -> UpstreamRequest:
        return UpstreamRequest(
            method="HEAD",
            url=f"{str(self.config.base_url).rstrip('/')}/v1/models",
            headers={
                "accept": "application/json",
                **self.default_headers,
                "authorization": f"Bearer {credential.secret}",
            },
            timeout=httpx.Timeout(min(self.config.timeout_seconds, 10)),
        )

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
