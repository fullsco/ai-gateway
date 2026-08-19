from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

import httpx
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from gateway.protocols import Capability, ClientProtocol, NormalizedRequest


class ErrorCategory(StrEnum):
    AUTHENTICATION_ERROR = "authentication_error"
    UPSTREAM_AUTHENTICATION_ERROR = "upstream_authentication_error"
    UPSTREAM_WAF_REJECTION = "upstream_waf_rejection"
    RATE_LIMIT = "rate_limit"
    QUOTA_EXHAUSTED = "quota_exhausted"
    MODEL_UNAVAILABLE = "model_unavailable"
    # The model is configured, but every route or credential that could serve it
    # was ineligible at this moment: unhealthy, cooling down, out of quota, at its
    # concurrency limit, or behind an open circuit. Distinct from
    # MODEL_UNAVAILABLE, which means the provider does not serve the model at all.
    NO_ELIGIBLE_ROUTE = "no_eligible_route"
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


class RetryScope(StrEnum):
    """How far a failure invalidates the target that produced it.

    NONE       - the request itself is at fault; retrying anywhere is pointless.
    CREDENTIAL - this credential is unusable right now; another credential on the
                 same provider may still work (rejected key, rate limit, quota).
    PROVIDER   - the whole provider is suspect; prefer a different provider but
                 another credential is still worth trying if none is available.
    """

    NONE = "none"
    CREDENTIAL = "credential"
    PROVIDER = "provider"


class ProviderError(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: ErrorCategory
    message: str
    retryable: bool
    upstream_status: int | None = None
    retry_after_seconds: float | None = None
    retry_scope: RetryScope = RetryScope.NONE
    # True when the failure says nothing about the credential itself, so the
    # credential must not be marked unhealthy (e.g. malformed client request).
    credential_at_fault: bool = True


# Retry semantics per failure category. This is the single source of truth for
# "may we try again, and does the failure invalidate the credential or the provider".
#   (retryable, retry_scope, credential_at_fault)
_RETRY_SEMANTICS: dict[ErrorCategory, tuple[bool, RetryScope, bool]] = {
    # The *client's* gateway key is bad. Nothing upstream is wrong.
    ErrorCategory.AUTHENTICATION_ERROR: (False, RetryScope.NONE, False),
    # The upstream rejected the credential we sent. Another credential on the same
    # provider may still be accepted, so fail over at credential level.
    ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR: (True, RetryScope.CREDENTIAL, True),
    # An edge/WAF or policy layer blocked the call. Not the credential's fault;
    # rotating keys on the same provider usually will not help.
    ErrorCategory.UPSTREAM_WAF_REJECTION: (True, RetryScope.PROVIDER, False),
    ErrorCategory.RATE_LIMIT: (True, RetryScope.CREDENTIAL, True),
    # This key is out of quota; a sibling key may still have budget.
    ErrorCategory.QUOTA_EXHAUSTED: (True, RetryScope.CREDENTIAL, True),
    # This provider does not serve the model; another provider might.
    ErrorCategory.MODEL_UNAVAILABLE: (True, RetryScope.PROVIDER, False),
    # Nothing was contacted, because nothing was eligible. Retrying inside this
    # request would re-evaluate the same excluded candidates.
    ErrorCategory.NO_ELIGIBLE_ROUTE: (False, RetryScope.NONE, False),
    ErrorCategory.PROVIDER_UNAVAILABLE: (True, RetryScope.PROVIDER, False),
    ErrorCategory.TIMEOUT: (True, RetryScope.PROVIDER, False),
    # The request itself is malformed - retrying anywhere returns the same result.
    ErrorCategory.INVALID_REQUEST: (False, RetryScope.NONE, False),
    ErrorCategory.INTERNAL_ERROR: (False, RetryScope.NONE, False),
}


def build_provider_error(
    category: ErrorCategory,
    message: str,
    *,
    upstream_status: int | None = None,
    retry_after_seconds: float | None = None,
) -> ProviderError:
    """Create a ProviderError with the retry semantics for its category."""
    retryable, scope, at_fault = _RETRY_SEMANTICS[category]
    return ProviderError(
        category=category,
        message=message,
        retryable=retryable,
        upstream_status=upstream_status,
        retry_after_seconds=retry_after_seconds,
        retry_scope=scope,
        credential_at_fault=at_fault,
    )


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
    def create_probe_request(
        self,
        credential: Credential,
        *,
        model: str | None = None,
    ) -> UpstreamRequest:
        """Create an authenticated, low-cost reachability probe."""
