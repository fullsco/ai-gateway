from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

import httpx
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from gateway.protocols import Capability, ClientProtocol, NormalizedRequest


class ErrorCategory(StrEnum):
    AUTHENTICATION_ERROR = "authentication_error"
    UPSTREAM_AUTHENTICATION_ERROR = "upstream_authentication_error"
    RATE_LIMIT = "rate_limit"
    QUOTA_EXHAUSTED = "quota_exhausted"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    INTERNAL_ERROR = "internal_error"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    base_url: AnyHttpUrl
    protocol: ClientProtocol
    capabilities: frozenset[Capability]
    timeout_seconds: float = Field(default=600, gt=0)


class Credential(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    secret: str = Field(min_length=1, repr=False)


class UpstreamRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    method: str = "POST"
    url: str
    headers: dict[str, str]
    json_body: dict[str, Any] | None = None
    timeout: httpx.Timeout


class ProviderError(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: ErrorCategory
    message: str
    retryable: bool
    upstream_status: int | None = None
    retry_after_seconds: float | None = None


class ProviderAdapter(ABC):
    @abstractmethod
    def validate_request(self, request: NormalizedRequest) -> None:
        """Raise ValueError when the provider cannot serve the request."""

    @abstractmethod
    def create_request(
        self,
        request: NormalizedRequest,
        credential: Credential,
        incoming_headers: dict[str, str] | None = None,
    ) -> UpstreamRequest:
        """Translate a normalized request to an upstream request."""

    @abstractmethod
    def normalize_error(self, response: httpx.Response) -> ProviderError:
        """Translate an upstream HTTP error without exposing sensitive data."""

    @abstractmethod
    def create_probe_request(self, credential: Credential) -> UpstreamRequest:
        """Create an authenticated, payload-free reachability probe."""
