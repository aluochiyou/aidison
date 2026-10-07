from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import monotonic

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import Request, Response
from openai import APIConnectionError, APIStatusError, APITimeoutError

from aidison.api.app import _await_planning_operation, create_app
from aidison.api.errors import (
    ApiErrorCode,
    error_response,
    request_correlation,
    set_request_correlation_id,
)


def test_error_response_has_a_stable_typed_envelope_and_request_correlation() -> None:
    token = set_request_correlation_id("req-r5-01")
    try:
        response = error_response(
            409,
            ApiErrorCode.STALE_PROJECT_REVISION,
            "project changed; refresh before applying this command",
            current_revision=13,
        )
    finally:
        request_correlation.reset(token)
    assert response.status_code == 409
    assert response.headers["x-request-id"] == "req-r5-01"
    assert response.body == (
        b'{"error":{"code":"STALE_PROJECT_REVISION",'
        b'"message":"project changed; refresh before applying this command",'
        b'"correlation_id":"req-r5-01","retryable":false,"current_revision":13}}'
    )


def test_dependency_error_is_retryable_and_internal_details_are_optional() -> None:
    response = error_response(
        503,
        ApiErrorCode.DEPENDENCY_UNAVAILABLE,
        "model provider is temporarily unavailable",
        details={"provider": "openai"},
    )
    assert response.headers["x-request-id"]
    assert b'"retryable":true' in response.body
    assert b'"details":{"provider":"openai"}' in response.body


def test_model_request_timeout_is_retryable_without_provider_detail() -> None:
    response = error_response(
        504,
        ApiErrorCode.MODEL_REQUEST_TIMED_OUT,
        "model planning timed out; retry or narrow the requested scope",
    )

    payload = json.loads(response.body)["error"]
    assert payload["code"] == "MODEL_REQUEST_TIMED_OUT"
    assert payload["retryable"] is True
    assert "provider" not in payload


def test_invalid_model_output_is_retryable_without_raw_response() -> None:
    response = error_response(
        502,
        ApiErrorCode.MODEL_OUTPUT_INVALID,
        (
            "model output did not match the required planning contract; "
            "retry or adjust the planning brief"
        ),
    )

    payload = json.loads(response.body)["error"]
    assert payload["code"] == "MODEL_OUTPUT_INVALID"
    assert payload["retryable"] is True
    assert "response" not in payload


def test_error_response_does_not_emit_a_python_exception_or_secret_field() -> None:
    response = error_response(
        500,
        ApiErrorCode.INTERNAL_ERROR,
        "internal server error",
        details={"api_key": "should-not-appear", "exception": "ValueError: internal"},
    )
    assert b"api_key" not in response.body
    assert b"ValueError" not in response.body


def test_api_middleware_assigns_one_correlation_id_to_a_standardized_validation_error() -> None:
    client = TestClient(create_app())
    response = client.post("/api/projects", json={"name": "only-name"})
    assert response.status_code == 422
    payload = response.json()["error"]
    assert payload["code"] == "INVALID_COMMAND"
    assert payload["correlation_id"] == response.headers["x-request-id"]
    assert payload["retryable"] is False
    assert "input" not in str(payload.get("details", ""))


def test_api_maps_safe_model_timeout_to_retryable_error_code() -> None:
    app = create_app()

    @app.get("/_test/model-timeout")
    async def model_timeout() -> None:
        raise HTTPException(status_code=504, detail="provider raw error must not leak")

    response = TestClient(app).get("/_test/model-timeout")

    assert response.status_code == 504
    payload = response.json()["error"]
    assert payload["code"] == "MODEL_REQUEST_TIMED_OUT"
    assert payload["retryable"] is True
    assert payload["message"] == "model planning timed out; retry or narrow the requested scope"
    assert "provider raw error" not in response.text


def test_api_maps_provider_credit_exhaustion_to_a_safe_non_retryable_error() -> None:
    app = create_app()

    @app.get("/_test/model-credit-exhausted", response_model=None)
    async def model_credit_exhausted() -> None:
        raise APIStatusError(
            "raw provider billing body must not leak",
            response=Response(402, request=Request("POST", "https://provider.invalid/v1/chat")),
            body={"error": {"message": "Insufficient Balance"}},
        )

    response = TestClient(app).get("/_test/model-credit-exhausted")

    assert response.status_code == 402
    payload = response.json()["error"]
    assert payload["code"] == "MODEL_CREDIT_EXHAUSTED"
    assert payload["retryable"] is False
    assert "credit" in payload["message"]
    assert "Insufficient Balance" not in response.text
    assert "billing body" not in response.text


@pytest.mark.parametrize(
    ("path", "error", "expected_status", "expected_code"),
    (
        (
            "/_test/model-connection-error",
            APIConnectionError(
                message="provider network detail must not leak",
                request=Request("POST", "https://provider.invalid/v1/chat"),
            ),
            503,
            "DEPENDENCY_UNAVAILABLE",
        ),
        (
            "/_test/model-timeout-error",
            APITimeoutError(Request("POST", "https://provider.invalid/v1/chat")),
            504,
            "MODEL_REQUEST_TIMED_OUT",
        ),
    ),
)
def test_api_maps_provider_transport_errors_to_safe_retryable_envelopes(
    path: str,
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    app = create_app()

    @app.get(path, response_model=None)
    async def model_transport_error() -> None:
        raise error

    response = TestClient(app).get(path)

    assert response.status_code == expected_status
    payload = response.json()["error"]
    assert payload["code"] == expected_code
    assert payload["retryable"] is True
    assert "provider network detail" not in response.text


@pytest.mark.asyncio
async def test_planning_deadline_returns_without_waiting_for_a_cancel_suppressing_sdk() -> None:
    late_completion = asyncio.Event()

    async def cancel_suppressing_operation() -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            late_completion.set()
            return "late provider value"

    started = monotonic()
    with pytest.raises(TimeoutError, match="API deadline"):
        await _await_planning_operation(cancel_suppressing_operation(), timeout_seconds=0.02)
    assert monotonic() - started < 0.05
    await asyncio.wait_for(late_completion.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_client_disconnect_cancels_the_detached_planning_operation() -> None:
    planner_cancelled = asyncio.Event()

    async def pending_operation() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            planner_cancelled.set()
            raise

    handler = asyncio.create_task(
        _await_planning_operation(pending_operation(), timeout_seconds=600)
    )
    await asyncio.sleep(0)
    handler.cancel()

    with pytest.raises(asyncio.CancelledError):
        await handler
    await asyncio.wait_for(planner_cancelled.wait(), timeout=0.2)


def test_api_uses_deployment_artifact_root_when_no_test_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIDISON_ARTIFACT_ROOT", "/data/artifacts")

    app = create_app()

    assert app.state.artifact_root == Path("/data/artifacts")
