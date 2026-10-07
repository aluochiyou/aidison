from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from aidison.observability.contracts import (
    DurableProductEvent,
    DurableProductEventType,
    EphemeralLiveEvent,
    EphemeralLiveEventType,
    MetricPoint,
    StructuredLogRecord,
    TelemetryCorrelation,
    append_durable_product_event,
)


def _correlation() -> TelemetryCorrelation:
    return TelemetryCorrelation(
        request_id="req-20260904-001",
        project_id=UUID("00000000-0000-0000-0000-000000000001"),
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        task_id="power/motor-research",
        invocation_id=UUID("00000000-0000-0000-0000-000000000003"),
        artifact_ref="artifact+sha256://" + "a" * 64 + "/result",
        decision_request_id=UUID("00000000-0000-0000-0000-000000000004"),
    )


def test_product_and_ephemeral_events_share_correlation_but_have_distinct_delivery_semantics() -> (
    None
):
    durable = DurableProductEvent(
        project_id=_correlation().project_id,
        event_type=DurableProductEventType.RESULT_ADMITTED,
        correlation=_correlation(),
        payload={"result_ref": "artifact+sha256://" + "b" * 64 + "/proposal"},
    )
    live = EphemeralLiveEvent(
        event_type=EphemeralLiveEventType.TOKEN_DELTA,
        correlation=_correlation(),
        payload={"delta_length": 12},
    )
    assert durable.event_type.value == "result.admitted"
    assert live.event_type.value == "token.delta"
    assert durable.correlation.run_id == live.correlation.run_id


def test_observability_contract_rejects_secrets_and_hidden_reasoning_from_any_payload() -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        DurableProductEvent(
            project_id=_correlation().project_id,
            event_type=DurableProductEventType.RUN_STARTED,
            payload={"api_key": "do-not-log"},
        )
    with pytest.raises(ValidationError, match="sensitive"):
        StructuredLogRecord(
            timestamp=datetime(2026, 9, 4, tzinfo=UTC),
            level="error",
            service="worker",
            event_code="provider.failed",
            attributes={"raw_prompt": "private prompt"},
        )


def test_structured_log_and_metric_remain_json_safe_and_correlation_aware() -> None:
    record = StructuredLogRecord(
        timestamp=datetime(2026, 9, 4, tzinfo=UTC),
        level="warning",
        service="research-worker",
        event_code="provider.fallback",
        correlation=_correlation(),
        graph_revision="research-graph/r4",
        failure_class="transient",
        duration_ms=120,
        attributes={"provider": "openai", "attempt": 2},
    )
    metric = MetricPoint(
        name="aidison_provider_fallback_total",
        value=1,
        correlation=_correlation(),
        dimensions={"provider": "openai", "failure_class": "transient"},
    )
    assert record.model_dump(mode="json")["correlation"]["run_id"]
    assert metric.model_dump(mode="json")["dimensions"]["provider"] == "openai"


def test_durable_adapter_writes_one_typed_event_to_existing_project_event_append_port() -> None:
    class FakeAppender:
        def __init__(self) -> None:
            self.calls: list[tuple[UUID, str, dict[str, object]]] = []

        async def append_event(
            self, project_id: UUID, event_type: str, payload: dict[str, object]
        ) -> int:
            self.calls.append((project_id, event_type, payload))
            return 19

    appender = FakeAppender()
    event = DurableProductEvent(
        project_id=_correlation().project_id,
        event_type=DurableProductEventType.DECISION_REQUESTED,
        correlation=_correlation(),
        payload={"decision_kind": "module_selection"},
    )
    sequence = asyncio.run(append_durable_product_event(appender, event))
    assert sequence == 19
    assert appender.calls == [
        (
            event.project_id,
            "decision.requested",
            {
                "decision_kind": "module_selection",
                "telemetry": event.correlation.model_dump(mode="json", exclude_none=True),
            },
        )
    ]
