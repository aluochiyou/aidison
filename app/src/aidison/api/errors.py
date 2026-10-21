"""Versioned API error envelope and request-correlation helpers.

The error boundary intentionally exposes an actionable code and safe message,
never a Python exception, credential, prompt, or raw provider payload.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from enum import StrEnum
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


class ApiErrorCode(StrEnum):
    INVALID_COMMAND = "INVALID_COMMAND"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    POLICY_DENIED = "POLICY_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    STALE_PROJECT_REVISION = "STALE_PROJECT_REVISION"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DECISION_ALREADY_RESOLVED = "DECISION_ALREADY_RESOLVED"
    ANSWER_SCHEMA_MISMATCH = "ANSWER_SCHEMA_MISMATCH"
    INGRESS_RATE_LIMITED = "INGRESS_RATE_LIMITED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    MODEL_REQUEST_TIMED_OUT = "MODEL_REQUEST_TIMED_OUT"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    MODEL_CREDIT_EXHAUSTED = "MODEL_CREDIT_EXHAUSTED"
    DOMAIN_CONFLICT = "DOMAIN_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ApiErrorCode
    message: str = Field(min_length=1, max_length=2_000)
    correlation_id: str = Field(min_length=1, max_length=200)
    retryable: bool
    current_revision: int | None = Field(default=None, ge=1)
    details_ref: str | None = Field(default=None, min_length=1, max_length=500)
    details: object | None = None


class ApiErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ApiErrorPayload


request_correlation: ContextVar[str | None] = ContextVar(
    "aidison_request_correlation", default=None
)


def set_request_correlation_id(value: str) -> Token[str | None]:
    """Set an internal request ID for the duration of one API request."""
    return request_correlation.set(value)


def current_request_correlation_id() -> str:
    return request_correlation.get() or f"req-{uuid4()}"


def error_response(
    status_code: int,
    code: ApiErrorCode,
    message: str,
    *,
    details: object | None = None,
    current_revision: int | None = None,
    details_ref: str | None = None,
    correlation_id: str | None = None,
) -> JSONResponse:
    """Return one safe, typed error response suitable for commands, queries and streams."""
    request_id = correlation_id or current_request_correlation_id()
    safe_details = details if _is_safe_detail(details) else None
    envelope = ApiErrorEnvelope(
        error=ApiErrorPayload(
            code=code,
            message=message,
            correlation_id=request_id,
            retryable=_retryable(code),
            current_revision=current_revision,
            details_ref=details_ref,
            details=safe_details,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(envelope.model_dump(exclude_none=True)),
        headers={"X-Request-ID": request_id},
    )


def _retryable(code: ApiErrorCode) -> bool:
    return code in {
        ApiErrorCode.DEPENDENCY_UNAVAILABLE,
        ApiErrorCode.INGRESS_RATE_LIMITED,
        ApiErrorCode.MODEL_REQUEST_TIMED_OUT,
        ApiErrorCode.MODEL_OUTPUT_INVALID,
    }


_UNSAFE_DETAIL_KEY_PARTS = (
    "api_key",
    "password",
    "secret",
    "authorization",
    "credential",
    "prompt",
    "response",
    "exception",
    "traceback",
    "stack",
    "reasoning",
)


def _is_safe_detail(value: object | None) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and not any(part in key.lower() for part in _UNSAFE_DETAIL_KEY_PARTS)
            and _is_safe_detail(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_is_safe_detail(item) for item in value)
    return isinstance(value, (str, int, float, bool))


__all__ = [
    "ApiErrorCode",
    "ApiErrorEnvelope",
    "ApiErrorPayload",
    "current_request_correlation_id",
    "error_response",
    "request_correlation",
    "set_request_correlation_id",
]
