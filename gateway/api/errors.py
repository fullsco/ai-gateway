from fastapi.responses import JSONResponse

from gateway.auth import AuthDenial
from gateway.context import get_request_id
from gateway.providers import ErrorCategory, ProviderError

ERROR_STATUS = {
    ErrorCategory.AUTHENTICATION_ERROR: 401,
    ErrorCategory.AUTHORIZATION_ERROR: 403,
    ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR: 502,
    ErrorCategory.UPSTREAM_WAF_REJECTION: 502,
    ErrorCategory.RATE_LIMIT: 429,
    ErrorCategory.QUOTA_EXHAUSTED: 429,
    ErrorCategory.MODEL_UNAVAILABLE: 404,
    ErrorCategory.NO_ELIGIBLE_ROUTE: 503,
    ErrorCategory.PROVIDER_UNAVAILABLE: 503,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.INVALID_REQUEST: 400,
    ErrorCategory.INTERNAL_ERROR: 500,
}


def gateway_error(error: ProviderError) -> JSONResponse:
    return JSONResponse(
        status_code=ERROR_STATUS[error.category],
        content={
            "type": "error",
            "error": {
                "type": error.category.value,
                "message": error.message,
                "request_id": get_request_id(),
            },
        },
    )


def client_error(category: ErrorCategory, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": category.value,
                "message": message,
                "request_id": get_request_id(),
            },
        },
    )


# A denial is reported with the permission that is actually missing. Every entry
# point shares this table so the same cause cannot read differently depending on
# which API was called.
_DENIAL_RESPONSES: dict[AuthDenial, tuple[ErrorCategory, str]] = {
    AuthDenial.MISSING_KEY: (
        ErrorCategory.AUTHENTICATION_ERROR,
        "No gateway key was supplied. Send it as x-api-key or as a bearer token.",
    ),
    AuthDenial.INVALID_KEY: (
        ErrorCategory.AUTHENTICATION_ERROR,
        "Invalid gateway key.",
    ),
    AuthDenial.KEY_REVOKED: (
        ErrorCategory.AUTHORIZATION_ERROR,
        "This gateway key has been revoked. Issue a new key for the client.",
    ),
    AuthDenial.KEY_EXPIRED: (
        ErrorCategory.AUTHORIZATION_ERROR,
        "This gateway key has expired. Issue a new key, or extend this one's expiry.",
    ),
    AuthDenial.CLIENT_DISABLED: (
        ErrorCategory.AUTHORIZATION_ERROR,
        "This gateway key belongs to a client that is disabled.",
    ),
    AuthDenial.PROTOCOL_NOT_PERMITTED: (
        ErrorCategory.AUTHORIZATION_ERROR,
        "This gateway key is valid, but its client is not permitted to use this API. "
        "Add the protocol to the client's allowed protocols, or use a key from a "
        "client that already has it.",
    ),
    AuthDenial.MODEL_NOT_PERMITTED: (
        ErrorCategory.AUTHORIZATION_ERROR,
        "This gateway key is valid, but its client is not permitted to use this "
        "model. Add the model to the client's allowed models, or use a key from a "
        "client that already has it.",
    ),
}


def denial_error(denial: AuthDenial) -> JSONResponse:
    category, message = _DENIAL_RESPONSES[denial]
    return client_error(category, message, ERROR_STATUS[category])
