from collections.abc import Iterable
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

FORWARDED_HEADERS = {"anthropic-beta", "anthropic-version"}


class AnthropicCompatibleAdapter(ProviderAdapter):
    def __init__(
        self,
        config: ProviderConfig,
        *,
        default_headers: dict[str, str] | None = None,
        required_betas: Iterable[str] = (),
        auth_scheme: str = "default",
        endpoint_query: dict[str, str] | None = None,
    ) -> None:
        if config.protocol is not ClientProtocol.ANTHROPIC_MESSAGES:
            raise ValueError("Anthropic adapter requires the Anthropic Messages protocol")
        self.config = config
        self.default_headers = {"user-agent": DEFAULT_USER_AGENT, **(default_headers or {})}
        self.required_betas = frozenset(required_betas)
        self.auth_scheme = auth_scheme
        self.endpoint_query = dict(endpoint_query or {})

    def validate_request(self, request: NormalizedRequest) -> None:
        if request.protocol is not ClientProtocol.ANTHROPIC_MESSAGES:
            raise ValueError("Provider only accepts Anthropic Messages requests")
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
        source_headers = {key.lower(): value for key, value in (incoming_headers or {}).items()}
        headers = {
            "accept": "text/event-stream" if request.stream else "application/json",
            "content-type": "application/json",
            "anthropic-version": source_headers.get("anthropic-version", "2023-06-01"),
            **self.default_headers,
        }
        for name in ("authorization", "cookie", "proxy-authorization", "x-api-key"):
            headers.pop(name, None)
        if self.auth_scheme in {"default", "x-api-key", "both"}:
            headers["x-api-key"] = credential.secret
        if self.auth_scheme in {"bearer", "both"}:
            headers["authorization"] = f"Bearer {credential.secret}"
        for name in FORWARDED_HEADERS:
            if value := source_headers.get(name):
                headers[name] = value
        if self.required_betas:
            headers["anthropic-beta"] = self._merge_betas(headers.get("anthropic-beta"))

        url = f"{str(self.config.base_url).rstrip('/')}/v1/messages"
        if self.endpoint_query:
            url = f"{url}?{urlencode(self.endpoint_query)}"
        return UpstreamRequest(
            url=url,
            headers=headers,
            json_body=request.payload,
            timeout=httpx.Timeout(self.config.timeout_seconds),
        )

    def normalize_error(self, response: httpx.Response) -> ProviderError:
        message, error_type = self._extract_error(response)
        searchable = f"{error_type} {message}".lower()
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
        if not model:
            raise ValueError("Anthropic health probes require an upstream model")
        headers = {
            "accept": "application/json",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            **self.default_headers,
        }
        for name in ("authorization", "cookie", "proxy-authorization", "x-api-key"):
            headers.pop(name, None)
        if self.auth_scheme in {"default", "x-api-key", "both"}:
            headers["x-api-key"] = credential.secret
        if self.auth_scheme in {"bearer", "both"}:
            headers["authorization"] = f"Bearer {credential.secret}"
        if self.required_betas:
            headers["anthropic-beta"] = self._merge_betas(None)
        url = f"{str(self.config.base_url).rstrip('/')}/v1/messages"
        if self.endpoint_query:
            url = f"{url}?{urlencode(self.endpoint_query)}"
        return UpstreamRequest(
            method="POST",
            url=url,
            headers=headers,
            json_body={
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=httpx.Timeout(min(self.config.timeout_seconds, 10)),
        )

    @staticmethod
    def _is_waf_rejection(response: httpx.Response) -> bool:
        try:
            payload = response.json()
        except ValueError:
            return True
        return not (isinstance(payload, dict) and isinstance(payload.get("error"), dict))

    def _merge_betas(self, source: str | None) -> str:
        values = set(self.required_betas)
        values.update(value.strip() for value in (source or "").split(",") if value.strip())
        return ",".join(sorted(values))

    @staticmethod
    def _extract_error(response: httpx.Response) -> tuple[str, str]:
        try:
            payload: Any = response.json()
        except ValueError:
            return "Upstream provider request failed.", ""
        if not isinstance(payload, dict):
            return "Upstream provider request failed.", ""
        error = payload.get("error", payload)
        if not isinstance(error, dict):
            return "Upstream provider request failed.", ""
        message = error.get("message")
        error_type = error.get("type")
        safe_message = message if isinstance(message, str) else "Upstream provider request failed."
        safe_type = error_type if isinstance(error_type, str) else ""
        return safe_message[:500], safe_type[:100]

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
