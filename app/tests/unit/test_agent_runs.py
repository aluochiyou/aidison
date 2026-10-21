from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily


def _binding() -> RuntimeBinding:
    return RuntimeBinding(
        runtime_family=RuntimeFamily.LANGGRAPH_V1,
        runtime_revision="runtime-v1",
        graph_key="research",
        graph_revision="research-v1",
        state_schema_version="research-state-v1",
        profile_binding_ref="profile://research/default@1",
        policy_binding_ref="policy://research/default@1",
    )


def test_terminal_agent_run_requires_completion_timestamp() -> None:
    values = {
        "project_id": "00000000-0000-0000-0000-000000000001",
        "kind": AgentRunKind.RESEARCH,
        "idempotency_key": "research-run-1",
        "basis_hash": "a" * 64,
        "basis_project_revision": 1,
        "runtime_binding": _binding(),
        "thread_id": "run-1",
        "status": AgentRunStatus.SUCCEEDED,
    }

    with pytest.raises(ValidationError, match="completed_at"):
        AgentRun.model_validate(values)

    assert AgentRun.model_validate(
        values | {"completed_at": datetime.now(UTC)}
    ).status is AgentRunStatus.SUCCEEDED
