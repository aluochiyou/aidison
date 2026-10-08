from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Literal
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from aidison.api.app import create_app
from aidison.observability import TelemetryCorrelation
from aidison.observability.runtime_tracing import (
    DisabledRuntimeTracer,
    LangfuseRuntimeTracer,
    LangGraphRuntimeCallback,
    RuntimeTracer,
    RuntimeTracingSettings,
    build_runtime_tracer,
)

RUN_A = UUID("00000000-0000-0000-0000-0000000000a1")
RUN_B = UUID("00000000-0000-0000-0000-0000000000b2")


class _FakeScope(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None


class _FakeLangfuse:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.flush_calls = 0

    def start_as_current_observation(self, **kwargs: object) -> _FakeScope:
        self.calls.append(kwargs)
        return _FakeScope()

    def flush(self) -> None:
        self.flush_calls += 1


def _tracer(client: _FakeLangfuse) -> LangfuseRuntimeTracer:
    return LangfuseRuntimeTracer(
        project="aidison-runtime",
        public_key="pk-test",
        secret_key="sk-test",
        base_url="https://langfuse.test",
        environment="test",
        release="2026.10.8",
        client_factory=lambda **_: client,
    )


def test_runtime_tracing_is_disabled_without_explicit_complete_configuration() -> None:
    assert isinstance(build_runtime_tracer(RuntimeTracingSettings()), DisabledRuntimeTracer)
    assert isinstance(
        build_runtime_tracer(
            RuntimeTracingSettings(enabled=True, public_key=SecretStr("pk-only"))
        ),
        DisabledRuntimeTracer,
    )


def test_runtime_trace_uses_stable_run_trace_and_only_safe_metadata() -> None:
    client = _FakeLangfuse()
    tracer = _tracer(client)
    correlation = TelemetryCorrelation(
        request_id="req-1",
        project_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=RUN_A,
        task_id="power.research",
    )

    with tracer.span(
        name="aidison.research.graph",
        correlation=correlation,
        kind="agent",
        attributes={"aidison.graph_name": "research", "aidison.outcome": "waiting"},
    ):
        pass

    assert client.calls[0]["trace_context"] == {"trace_id": RUN_A.hex}
    assert "input" not in client.calls[0]
    assert "output" not in client.calls[0]
    metadata = client.calls[0]["metadata"]
    assert metadata == {
        "aidison.project": "runtime",
        "aidison.component": "runtime",
        "aidison.service": "aidison-runtime",
        "aidison.request_id": "req-1",
        "aidison.project_id": "00000000-0000-0000-0000-000000000001",
        "aidison.run_id": str(RUN_A),
        "aidison.task_id": "power.research",
        "aidison.graph_name": "research",
        "aidison.outcome": "waiting",
    }


def test_runtime_trace_rejects_prompt_like_attributes_without_calling_exporter() -> None:
    client = _FakeLangfuse()
    tracer = _tracer(client)

    with tracer.span(
        name="aidison.research.graph",
        correlation=TelemetryCorrelation(run_id=RUN_A),
        attributes={"raw_prompt": "private text"},
    ):
        pass

    assert client.calls == []


def test_langgraph_callback_ignores_graph_inputs_outputs_and_uses_agent_run_trace() -> None:
    client = _FakeLangfuse()
    tracer = _tracer(client)
    callback = LangGraphRuntimeCallback(
        tracer=tracer,
        correlation=TelemetryCorrelation(run_id=RUN_A, task_id="power.research"),
        graph_name="admitted_ready_set",
        graph_revision="research/v1",
    )
    callback_run_id = UUID("00000000-0000-0000-0000-0000000000c3")

    callback.on_chain_start(
        {"name": "private-node"},
        {"raw_prompt": "must never leave the process"},
        run_id=callback_run_id,
    )
    callback.on_chain_end(
        {"raw_response": "must never leave the process"},
        run_id=callback_run_id,
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["trace_context"] == {"trace_id": RUN_A.hex}
    assert "input" not in call and "output" not in call
    assert call["metadata"] == {
        "aidison.project": "runtime",
        "aidison.component": "runtime",
        "aidison.service": "aidison-runtime",
        "aidison.run_id": str(RUN_A),
        "aidison.task_id": "power.research",
        "aidison.invocation_id": str(callback_run_id),
        "aidison.event": "chain_started",
        "aidison.graph_name": "admitted_ready_set",
        "aidison.graph_revision": "research/v1",
    }


def test_runtime_trace_isolates_parallel_run_trace_ids() -> None:
    client = _FakeLangfuse()
    tracer = _tracer(client)

    async def emit(run_id: UUID) -> None:
        with tracer.span(
            name="aidison.research.task",
            correlation=TelemetryCorrelation(run_id=run_id),
            attributes={"aidison.event": "task_started"},
        ):
            await asyncio.sleep(0)

    async def emit_all() -> None:
        await asyncio.gather(emit(RUN_A), emit(RUN_B))

    asyncio.run(emit_all())
    assert {item["trace_context"]["trace_id"] for item in client.calls} == {
        RUN_A.hex,
        RUN_B.hex,
    }


def test_resumed_graph_execution_stays_on_the_original_agent_run_trace() -> None:
    client = _FakeLangfuse()
    tracer = _tracer(client)

    for graph_name, callback_run_id in (
        ("single_task_research", UUID("00000000-0000-0000-0000-0000000000d4")),
        ("admitted_ready_set", UUID("00000000-0000-0000-0000-0000000000e5")),
    ):
        callback = LangGraphRuntimeCallback(
            tracer=tracer,
            correlation=TelemetryCorrelation(project_id=UUID(int=1), run_id=RUN_A),
            graph_name=graph_name,
            graph_revision="research/v1",
        )
        callback.on_chain_start({}, {}, run_id=callback_run_id)
        callback.on_chain_end({}, run_id=callback_run_id)
        callback.close()

    assert len(client.calls) == 2
    assert {item["trace_context"]["trace_id"] for item in client.calls} == {RUN_A.hex}
    assert {item["metadata"]["aidison.graph_name"] for item in client.calls} == {
        "single_task_research",
        "admitted_ready_set",
    }


def test_runtime_exporter_failure_never_changes_product_execution() -> None:
    tracer = LangfuseRuntimeTracer(
        project="aidison-runtime",
        public_key="pk-test",
        secret_key="sk-test",
        base_url="https://langfuse.test",
        environment="test",
        release=None,
        client_factory=lambda **_: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    with tracer.span(
        name="aidison.api.request",
        correlation=TelemetryCorrelation(request_id="req-1"),
        attributes={"http.method": "GET"},
    ):
        observed = "still-runs"
    tracer.flush()
    assert observed == "still-runs"


class _RecordingRuntimeTracer(RuntimeTracer):
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, TelemetryCorrelation, dict[str, object] | None]] = []
        self.flushed = False

    def span(
        self,
        *,
        name: str,
        correlation: TelemetryCorrelation,
        kind: Literal["span", "agent", "tool", "chain", "retriever"] = "span",
        attributes: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> AbstractContextManager[None]:
        del kind
        self.calls.append((name, correlation, dict(attributes) if attributes is not None else None))
        return _FakeScope()

    def flush(self) -> None:
        self.flushed = True


def test_api_binds_request_correlation_to_one_safe_runtime_span_and_flushes_on_shutdown() -> None:
    tracer = _RecordingRuntimeTracer()
    with TestClient(create_app(runtime_tracer=tracer)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert len(tracer.calls) == 1
    name, correlation, attributes = tracer.calls[0]
    assert name == "aidison.api.request"
    assert correlation.request_id == response.headers["x-request-id"]
    assert attributes == {"http.method": "GET"}
    assert tracer.flushed is True
