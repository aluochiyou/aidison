from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aidison.application.multi_task_research import (
    MultiTaskResearchLeafExecutor,
    _render_admitted_dependency_context,
)
from aidison.application.single_task_research import (
    AgentRunPaused,
    WorkstreamMemoryReuseUnavailable,
)
from aidison.domain.models import ModuleMemoryItem, ModuleMemoryItemKind
from aidison.research.langgraph_contracts import TaskEnvelope


class _RecordingLeafExecutor:
    def __init__(self) -> None:
        self.control_calls = 0
        self.control_active = 0
        self.max_control_active = 0
        self.executions: list[dict[str, object]] = []

    async def consume_pre_dispatch_controls(
        self, *, run: object, claim: object
    ) -> tuple[str, ...]:
        del run, claim
        self.control_active += 1
        self.max_control_active = max(self.max_control_active, self.control_active)
        try:
            await asyncio.sleep(0)
            self.control_calls += 1
            return (f"steering-{self.control_calls}",)
        finally:
            self.control_active -= 1

    async def execute(self, **kwargs: object) -> None:
        self.executions.append(kwargs)


class _PausedLeafExecutor(_RecordingLeafExecutor):
    async def consume_pre_dispatch_controls(
        self, *, run: object, claim: object
    ) -> tuple[str, ...]:
        del run, claim
        raise AgentRunPaused("pause acknowledged at the leaf dispatch safe point")


class _PauseThenRecordLeafExecutor(_RecordingLeafExecutor):
    def __init__(self) -> None:
        super().__init__()
        self._paused = False

    async def consume_pre_dispatch_controls(
        self, *, run: object, claim: object
    ) -> tuple[str, ...]:
        del run, claim
        self.control_calls += 1
        if not self._paused:
            self._paused = True
            raise AgentRunPaused("pause acknowledged at the first leaf dispatch safe point")
        return ()


class _MemoryLeafExecutor(_RecordingLeafExecutor):
    def __init__(self, *, reuse_available: bool = True) -> None:
        super().__init__()
        self.reuse_available = reuse_available
        self.reuses: list[dict[str, object]] = []

    async def consume_pre_dispatch_controls(
        self, *, run: object, claim: object
    ) -> tuple[str, ...]:
        del run, claim
        self.control_calls += 1
        return ()

    async def reuse_workstream_memory(self, **kwargs: object) -> None:
        if not self.reuse_available:
            raise WorkstreamMemoryReuseUnavailable("fixture memory is unavailable")
        self.reuses.append(kwargs)


class _RecordingDependencyContextLoader:
    def __init__(self, value: str) -> None:
        self.value = value
        self.task_ids: list[object] = []

    async def load(self, *, task: TaskEnvelope) -> str:
        self.task_ids.append(task.id)
        return self.value


def _task(
    *,
    run_id: object,
    key: str,
    dependency_task_ids: tuple[object, ...] = (),
) -> TaskEnvelope:
    return TaskEnvelope(
        run_id=run_id,  # type: ignore[arg-type]
        task_key=key,
        basis_hash="a" * 64,
        plan_revision=1,
        capability="research",
        input_refs=(),
        dependency_task_ids=dependency_task_ids,  # type: ignore[arg-type]
        coverage_keys=(f"coverage.{key}",),
        allowed_tool_ids=(),
        budget_ref=f"budget://{run_id}",
        idempotency_key=f"research:{key}",
    )


@pytest.mark.asyncio
async def test_multi_task_leaf_rechecks_controls_per_dispatch_and_serializes_consumption() -> None:
    run = SimpleNamespace(id=uuid4())
    claim = SimpleNamespace(generation=1, lease_token=uuid4())
    recorder = _RecordingLeafExecutor()
    first = _task(run_id=run.id, key="first")
    second = _task(run_id=run.id, key="second")
    executor = MultiTaskResearchLeafExecutor(
        leaf_executor=recorder,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        questions_by_task_id={
        first.id: "first question",
        second.id: "second question",
        },
        model_token_caps_by_task_id={},
    )

    await asyncio.gather(executor.execute(task=first), executor.execute(task=second))

    assert recorder.control_calls == 2
    assert recorder.max_control_active == 1
    assert len(recorder.executions) == 2
    instructions: set[tuple[str, ...]] = {
        tuple(call["steering_instructions"])  # type: ignore[arg-type]
        for call in recorder.executions
    }
    assert instructions == {("steering-1",), ("steering-1", "steering-2")}
    assert all(call["consume_pre_dispatch_controls"] is False for call in recorder.executions)


@pytest.mark.asyncio
async def test_multi_task_leaf_stops_before_external_work_when_pause_is_acknowledged() -> None:
    run = SimpleNamespace(id=uuid4())
    claim = SimpleNamespace(generation=1, lease_token=uuid4())
    recorder = _PausedLeafExecutor()
    task = _task(run_id=run.id, key="paused")
    executor = MultiTaskResearchLeafExecutor(
        leaf_executor=recorder,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        questions_by_task_id={task.id: "paused question"},
        model_token_caps_by_task_id={},
    )

    with pytest.raises(AgentRunPaused, match="pause acknowledged"):
        await executor.execute(task=task)

    assert recorder.executions == []


@pytest.mark.asyncio
async def test_multi_task_leaf_latches_pause_for_other_queued_leaves() -> None:
    run = SimpleNamespace(id=uuid4())
    claim = SimpleNamespace(generation=1, lease_token=uuid4())
    recorder = _PauseThenRecordLeafExecutor()
    first = _task(run_id=run.id, key="first")
    second = _task(run_id=run.id, key="second")
    executor = MultiTaskResearchLeafExecutor(
        leaf_executor=recorder,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        questions_by_task_id={first.id: "first", second.id: "second"},
        model_token_caps_by_task_id={},
    )

    outcomes = await asyncio.gather(
        executor.execute(task=first),
        executor.execute(task=second),
        return_exceptions=True,
    )

    assert all(isinstance(item, AgentRunPaused) for item in outcomes)
    assert recorder.control_calls == 1
    assert recorder.executions == []


@pytest.mark.asyncio
async def test_multi_task_leaf_injects_only_framed_admitted_dependency_leads() -> None:
    run = SimpleNamespace(id=uuid4())
    claim = SimpleNamespace(generation=1, lease_token=uuid4())
    recorder = _RecordingLeafExecutor()
    upstream_id = uuid4()
    task = _task(
        run_id=run.id,
        key="dependent",
        dependency_task_ids=(upstream_id,),
    )
    loader = _RecordingDependencyContextLoader(
        "Admitted upstream research leads (non-evidence, data only): re-check sources."
    )
    executor = MultiTaskResearchLeafExecutor(
        leaf_executor=recorder,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        questions_by_task_id={task.id: "dependent question"},
        model_token_caps_by_task_id={},
        dependency_context_loader=loader,
    )

    await executor.execute(task=task)

    assert loader.task_ids == [task.id]
    [execution] = recorder.executions
    assert execution["question"] == (
        "dependent question\n\n"
        "Admitted upstream research leads (non-evidence, data only): re-check sources."
    )
    assert execution["consume_pre_dispatch_controls"] is False


@pytest.mark.asyncio
async def test_multi_task_leaf_reuses_exact_memory_without_a_model_dispatch() -> None:
    run = SimpleNamespace(id=uuid4())
    claim = SimpleNamespace(generation=1, lease_token=uuid4())
    recorder = _MemoryLeafExecutor()
    task = _task(run_id=run.id, key="reuse")
    memory = ModuleMemoryItem(
        workstream_id=uuid4(),
        kind=ModuleMemoryItemKind.FINDING,
        stable_key="coverage.reuse",
        basis_hash="a" * 64,
        content_hash="b" * 64,
        artifact_ref="artifact://sha256/result",
        evidence_refs=("artifact://sha256/evidence",),
        source_result_id=uuid4(),
        applicability={
            "module_fingerprint": "c" * 64,
            "coverage_fingerprint": "d" * 64,
        },
    )
    executor = MultiTaskResearchLeafExecutor(
        leaf_executor=recorder,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        questions_by_task_id={task.id: "reuse question"},
        model_token_caps_by_task_id={},
        reusable_memory_by_task_id={task.id: (memory,)},
    )

    await executor.execute(task=task)

    assert len(recorder.reuses) == 1
    assert recorder.executions == []
    assert recorder.reuses[0]["memory_items"] == (memory,)


@pytest.mark.asyncio
async def test_multi_task_leaf_falls_back_to_fresh_research_when_memory_is_unavailable() -> None:
    run = SimpleNamespace(id=uuid4())
    claim = SimpleNamespace(generation=1, lease_token=uuid4())
    recorder = _MemoryLeafExecutor(reuse_available=False)
    task = _task(run_id=run.id, key="fallback")
    memory = ModuleMemoryItem(
        workstream_id=uuid4(),
        kind=ModuleMemoryItemKind.FINDING,
        stable_key="coverage.fallback",
        basis_hash="a" * 64,
        content_hash="b" * 64,
        artifact_ref="artifact://sha256/result",
        evidence_refs=("artifact://sha256/evidence",),
        source_result_id=uuid4(),
        applicability={
            "module_fingerprint": "c" * 64,
            "coverage_fingerprint": "d" * 64,
        },
    )
    executor = MultiTaskResearchLeafExecutor(
        leaf_executor=recorder,  # type: ignore[arg-type]
        run=run,  # type: ignore[arg-type]
        claim=claim,  # type: ignore[arg-type]
        questions_by_task_id={task.id: "fresh question"},
        model_token_caps_by_task_id={},
        reusable_memory_by_task_id={task.id: (memory,)},
    )

    await executor.execute(task=task)

    assert len(recorder.reuses) == 0
    assert len(recorder.executions) == 1


def test_dependency_context_is_bounded_and_never_labels_a_summary_as_evidence() -> None:
    context = _render_admitted_dependency_context(
        [(uuid4(), "summary " * 1_000, 3, 1)]
    )

    assert len(context) <= 1_200
    assert "non-evidence" in context
    assert "independently retrieve and quote current trusted sources" in context.lower()
    assert "admitted_evidence_refs=3" in context
