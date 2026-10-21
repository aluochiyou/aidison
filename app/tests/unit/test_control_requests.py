from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.api.schemas import CreateAgentRunControlRequest
from aidison.runtime.control_requests import AgentRunControlRequest, ControlRequestKind


def test_control_requests_enforce_kind_specific_payloads() -> None:
    basis = sha256(b"basis").hexdigest()
    pause = AgentRunControlRequest(
        agent_run_id=uuid4(),
        kind=ControlRequestKind.PAUSE,
        basis_hash=basis,
        idempotency_key="pause:1",
    )
    steering = AgentRunControlRequest(
        agent_run_id=uuid4(),
        kind=ControlRequestKind.RUNTIME_STEERING,
        basis_hash=basis,
        idempotency_key="steer:1",
        payload={"instruction": "prioritize motor evidence"},
    )
    assert pause.payload == {}
    assert steering.payload["instruction"] == "prioritize motor evidence"
    with pytest.raises(ValidationError):
        AgentRunControlRequest(
            agent_run_id=uuid4(),
            kind=ControlRequestKind.PAUSE,
            basis_hash=basis,
            idempotency_key="pause:2",
            payload={"x": "y"},
        )
    with pytest.raises(ValidationError):
        AgentRunControlRequest(
            agent_run_id=uuid4(),
            kind=ControlRequestKind.BASIS_STEERING,
            basis_hash=basis,
            idempotency_key="basis:1",
        )


def test_control_request_api_schema_rejects_cross_kind_payloads() -> None:
    basis = sha256(b"basis").hexdigest()
    assert (
        CreateAgentRunControlRequest(
            kind="runtime_steering",
            basis_hash=basis,
            instruction="prioritize motor evidence",
        ).instruction
        == "prioritize motor evidence"
    )
    with pytest.raises(ValidationError, match="requires instruction"):
        CreateAgentRunControlRequest(kind="runtime_steering", basis_hash=basis)
    with pytest.raises(ValidationError, match="must not carry steering payload"):
        CreateAgentRunControlRequest(
            kind="pause",
            basis_hash=basis,
            instruction="stop",
        )
    with pytest.raises(ValidationError, match="requires change_request_ref"):
        CreateAgentRunControlRequest(kind="basis_steering", basis_hash=basis)
