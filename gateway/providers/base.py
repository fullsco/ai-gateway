from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

import httpx
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from gateway.protocols import Capability, ClientProtocol, NormalizedRequest

# How this gateway identifies itself on every outbound request, whether that is
# provider traffic, a health probe or a billing poll.
#
# Letting httpx supply its own gives "python-httpx/x.y", which is on Cloudflare's
# generic-library blocklist; measured against gorouter.app and tabitoken.com the
# httpx default, python-requests and curl are all refused with an HTML challenge
# page, and this value is accepted. It lives here, in the one module every caller
# already imports, because it was previously copied into each adapter and the
# copies are what allowed a third call path - the credential usage poll - to omit
# it entirely and go unnoticed: fifteen credentials served traffic normally while
# reporting no spend figure at all.
DEFAULT_USER_AGENT = "ai-gateway/0.1"

# Wordings that decide what a 401 or 403 actually means. They live here, for the same
# reason DEFAULT_USER_AGENT does: both adapters classify the same providers -
# AgentRouter answers the same credentials over /v1/messages and /v1/chat/completions -
# so a rule that holds on one protocol holds on the other. They were previously two
# diverging copies. The Anthropic tuple knew "insufficient balance" and "billing
# limit", the OpenAI one knew "insufficient_quota" and "billing", and neither knew the
# other's, so one reseller's quota 403 read as exhausted quota on one protocol and as
# a rejected credential on the other. The shorter substrings below subsume both sets.
QUOTA_MARKERS = ("quota", "credit balance", "insufficient balance", "billing")

# A provider that gates on client identity answers 401/403 with this wording for
# *every* key presented, working ones included. Measured on AgentRouter while probing
# eight credentials parked as auth_failed: with a minimal header set all eight returned
# 403 "unauthorized client detected" - the credential then serving all production
# traffic among them - while with the mapping's real headers four returned 200, two
# were out of quota, one was outside its token's IP allow list and one lacked model
# entitlement (deploy/act_on_credential_probe.py). Reading this wording as a credential
# rejection therefore condemns every healthy key on the provider in turn, and the
# remedy is the request's headers, not key rotation.
CLIENT_GATING_MARKERS = ("unauthorized client",)


class ErrorCategory(StrEnum):
    AUTHENTICATION_ERROR = "authentication_error"
    # The key is real, but its client is not allowed this protocol or model. A
    # configuration decision the operator can see and change, not a bad secret.
    AUTHORIZATION_ERROR = "authorization_error"
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
    # The client is not permitted this protocol or model. No retry can grant it.
    ErrorCategory.AUTHORIZATION_ERROR: (False, RetryScope.NONE, False),
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


def classify_upstream_status(
    status_code: int,
    searchable: str,
    *,
    waf_rejection: bool = False,
) -> ErrorCategory:
    """Read an upstream failure's status and wording into a category.

    Shared by every adapter. The classification is deliberately protocol-independent:
    the providers behind it are resellers reached over whichever protocol a mapping
    happens to declare, and the same host answers the same credentials on both. When
    this logic lived once per adapter the copies drifted, and a quota 403 was read as
    a rejected credential on one protocol while reading correctly on the other.

    `searchable` is the error type, code and message lowercased into one string, so a
    marker matches wherever the provider chose to put the wording. `waf_rejection`
    says the 403 body was not an API error object at all.
    """
    if status_code == 403 and waf_rejection:
        # An edge or bot-protection layer answering 403 with a challenge page is not a
        # credential rejection. Without this a Cloudflare 403 parked every working key
        # on the provider in turn.
        return ErrorCategory.UPSTREAM_WAF_REJECTION
    if status_code == 403 and _matches(searchable, QUOTA_MARKERS):
        # Some resellers answer 403 rather than 402 or 429 when a key is out of quota.
        # Reading that as a rejected credential parked working keys as auth_failed,
        # which is both wrong and unrecoverable: quota comes back, a bad secret does
        # not. Two AgentRouter credentials sat like that while the provider was
        # plainly saying "user quota is not enough".
        return ErrorCategory.QUOTA_EXHAUSTED
    if status_code in {401, 403} and _matches(searchable, CLIENT_GATING_MARKERS):
        # The provider refused the *client's* identity, not this credential, and says
        # so for every key including the ones that work. Condemning the credential
        # burns the whole pool one key at a time and points the operator at rotation
        # when the fix is the request's headers. Provider-scoped and not the
        # credential's fault, so a sibling key is not tried for the same refusal.
        return ErrorCategory.UPSTREAM_WAF_REJECTION
    if status_code in {401, 403}:
        # The upstream rejected *this credential*, not the client's gateway key. This
        # also covers a key outside its token's IP allow list or without entitlement
        # to the model: both are specific to the credential, so a sibling may work.
        return ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR
    if status_code in {402, 429} and _matches(searchable, QUOTA_MARKERS):
        return ErrorCategory.QUOTA_EXHAUSTED
    if status_code == 402:
        # Payment Required is a billing condition even when the wording matches no
        # marker. Classifying it as an invalid request made it non-retryable and
        # returned 400 to the client, the opposite of the intended failover to a
        # credential that still has balance.
        return ErrorCategory.QUOTA_EXHAUSTED
    if status_code == 429:
        return ErrorCategory.RATE_LIMIT
    if status_code in {408, 504}:
        return ErrorCategory.TIMEOUT
    if status_code >= 500:
        return ErrorCategory.PROVIDER_UNAVAILABLE
    if status_code == 404 and "model" in searchable:
        return ErrorCategory.MODEL_UNAVAILABLE
    if 400 <= status_code < 500:
        return ErrorCategory.INVALID_REQUEST
    return ErrorCategory.INTERNAL_ERROR


def _matches(searchable: str, markers: tuple[str, ...]) -> bool:
    return any(marker in searchable for marker in markers)


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
