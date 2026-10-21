from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.research.collaboration import compile_research_collaboration_scope
from aidison.research.langgraph_contracts import TaskEnvelope


def _task(*, run_id, basis_hash: str, key: str, dependency_task_ids=()) -> TaskEnvelope:
    return TaskEnvelope(
        run_id=run_id,
        task_key=key,
        basis_hash=basis_hash,
        plan_revision=2,
        capability="research",
        input_refs=(f"module://{key}",),
        dependency_task_ids=dependency_task_ids,
        coverage_keys=(f"coverage.{key}",),
        allowed_tool_ids=(),
        budget_ref=f"budget://{run_id}",
        idempotency_key=f"task:{run_id}:{key}",
    )


def test_collaboration_scope_is_order_independent_and_contract_sensitive() -> None:
    run_id = uuid4()
    basis_hash = sha256(b"basis").hexdigest()
    first = _task(run_id=run_id, basis_hash=basis_hash, key="power")
    second = _task(
        run_id=run_id,
        basis_hash=basis_hash,
        key="motor",
        dependency_task_ids=(first.id,),
    )

    scope = compile_research_collaboration_scope((first, second))

    assert scope == compile_research_collaboration_scope((second, first))
    assert scope != compile_research_collaboration_scope(
        (first, second.model_copy(update={"capability": "research_verifier"}))
    )


def test_collaboration_scope_rejects_mixed_run_or_plan_revision() -> None:
    basis_hash = sha256(b"basis").hexdigest()
    first = _task(run_id=uuid4(), basis_hash=basis_hash, key="power")
    another_run = _task(run_id=uuid4(), basis_hash=basis_hash, key="motor")

    with pytest.raises(ValueError, match="share run"):
        compile_research_collaboration_scope((first, another_run))
    with pytest.raises(ValueError, match="share run"):
        compile_research_collaboration_scope(
            (first, first.model_copy(update={"id": uuid4(), "plan_revision": 3}))
        )
