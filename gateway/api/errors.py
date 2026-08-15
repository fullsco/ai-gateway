from fastapi.responses import JSONResponse

from gateway.context import get_request_id
from gateway.providers import ErrorCategory, ProviderError

ERROR_STATUS = {
    ErrorCategory.AUTHENTICATION_ERROR: 401,
    ErrorCategory.UPSTREAM_AUTHENTICATION_ERROR: 502,
    ErrorCategory.RATE_LIMIT: 429,
    ErrorCategory.QUOTA_EXHAUSTED: 429,
    ErrorCategory.MODEL_UNAVAILABLE: 404,
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
