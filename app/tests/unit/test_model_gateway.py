from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aidison.providers.model_gateway import (
    FallbackPolicy,
    ModelBudgetContext,
    ModelGateway,
    ModelInvocationRequest,
    ModelTarget,
    ProviderFailure,
    ProviderFailureClass,
    RetryPolicy,
)
from aidison.runtime.agent_runs import AgentRunClaim


class FakeAdapter:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def invoke(self, *, request, target):
        self.calls.append(target.key)
        next_outcome = self.outcomes.pop(0)
        if isinstance(next_outcome, Exception):
            raise next_outcome
        return next_outcome


class FakePermit:
    def __init__(self, events):
        self.events = events

    async def release(self) -> None:
        self.events.append("release")


class FakeQuota:
    def __init__(self):
        self.events = []

    async def acquire(self, *, bucket_key: str, deadline: datetime):
        self.events.append(f"acquire:{bucket_key}")
        return FakePermit(self.events)


class RejectingQuota:
    async def acquire(self, *, bucket_key: str, deadline: datetime):
        raise ProviderFailure(ProviderFailureClass.QUOTA_UNAVAILABLE)


class FakeCircuit:
    def __init__(self, *, allow=True):
        self.allow = allow
        self.successes = []
        self.failures = []

    async def allow_request(self, *, key: str, deadline: datetime) -> bool:
        return self.allow

    async def record_success(self, *, key: str) -> None:
        self.successes.append(key)

    async def record_failure(self, *, key: str, failure: ProviderFailureClass) -> None:
        self.failures.append((key, failure))


class RecordingBudgetPort:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, object]] = []

    async def reserve_attempt(self, *, context, target, ordinal):
        self.events.append(("reserve", ordinal, target.key))
        return f"operation-{ordinal}"

    async def mark_dispatched(self, *, operation_id, context):
        self.events.append(("dispatch", int(operation_id.rsplit("-", 1)[1]), operation_id))

    async def settle(self, *, operation_id, usage_tokens, provider_request_id, response_ref):
        self.events.append(("settle", usage_tokens, operation_id))

    async def release_before_dispatch(self, *, operation_id, context, normalized_error):
        self.events.append(("release", int(operation_id.rsplit("-", 1)[1]), normalized_error))

    async def mark_ambiguous(self, *, operation_id, normalized_error):
        self.events.append(("ambiguous", int(operation_id.rsplit("-", 1)[1]), normalized_error))


class RecordingInvocationObserver:
    def __init__(self) -> None:
        self.records = []

    def observe(self, observation) -> None:
        self.records.append(observation)


class FailingInvocationObserver:
    def observe(self, observation) -> None:
        del observation
        raise RuntimeError("telemetry offline")


class FakeStreamAdapter:
    def __init__(self, events: list[str]):
        self.events = events

    async def stream(self, *, request, target) -> AsyncIterator[dict[str, str]]:
        self.events.append("provider:start")
        yield {"delta": "first"}
        self.events.append("provider:between")
        yield {"delta": "second"}
        self.events.append("provider:end")


def _request(*, fallback=True) -> ModelInvocationRequest:
    primary = ModelTarget(
        provider="primary",
        model="primary-v1",
        revision="2026-09",
        credential_pool_id="pool-a",
        quota_group="chat",
        capabilities=("structured_output",),
    )
    fallback_target = ModelTarget(
        provider="fallback",
        model="fallback-v1",
        revision="2026-09",
        credential_pool_id="pool-b",
        quota_group="chat",
        capabilities=("structured_output",),
    )
    return ModelInvocationRequest(
        logical_invocation_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        basis_hash="a" * 64,
        prompt_ref="artifact+sha256://" + "b" * 64 + "/" + str(uuid4()),
        required_capabilities=("structured_output",),
        targets=(primary, fallback_target) if fallback else (primary,),
        retry_policy=RetryPolicy(max_attempts_per_target=2, base_backoff_seconds=0),
        fallback_policy=FallbackPolicy.AVAILABILITY_ONLY,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def _budget_context() -> ModelBudgetContext:
    now = datetime.now(UTC)
    return ModelBudgetContext(
        account_id=uuid4(),
        claim=AgentRunClaim(
            run_id=uuid4(),
            worker_id="test-worker",
            generation=1,
            lease_token=uuid4(),
            lease_expires_at=now + timedelta(minutes=1),
        ),
        logical_step="research.model",
        idempotency_prefix="test-model-invocation",
        request_hash="c" * 64,
        reserved_tokens=40,
    )


@pytest.mark.asyncio
async def test_rate_limit_retries_as_distinct_physical_attempts_with_new_quota_permit() -> None:
    adapter = FakeAdapter(
        [
            ProviderFailure(ProviderFailureClass.RATE_LIMITED, retry_after_seconds=0),
            {"response_ref": "artifact://response-1", "provider_request_id": "req-1"},
        ]
    )
    quota = FakeQuota()
    circuit = FakeCircuit()
    result = await ModelGateway(adapter=adapter, quota=quota, circuit=circuit).invoke(_request())

    assert result.status == "succeeded"
    assert len(result.attempts) == 2
    assert quota.events == [
        "acquire:primary:primary-v1:pool-a:chat",
        "release",
        "acquire:primary:primary-v1:pool-a:chat",
        "release",
    ]
    assert adapter.calls == ["primary:primary-v1:pool-a:chat"] * 2


@pytest.mark.asyncio
async def test_availability_failure_uses_frozen_fallback_with_visible_origin() -> None:
    adapter = FakeAdapter(
        [
            ProviderFailure(ProviderFailureClass.TRANSIENT_UPSTREAM),
            ProviderFailure(ProviderFailureClass.TRANSIENT_UPSTREAM),
            {"response_ref": "artifact://response-2", "provider_request_id": "req-2"},
        ]
    )
    result = await ModelGateway(adapter=adapter, quota=FakeQuota(), circuit=FakeCircuit()).invoke(
        _request()
    )

    assert result.status == "succeeded"
    assert result.actual_target.provider == "fallback"
    assert result.fallback_from is not None
    assert len(result.attempts) == 3


@pytest.mark.asyncio
async def test_open_circuit_fails_closed_without_provider_or_quota_call() -> None:
    adapter = FakeAdapter([])
    quota = FakeQuota()
    result = await ModelGateway(
        adapter=adapter, quota=quota, circuit=FakeCircuit(allow=False)
    ).invoke(_request(fallback=False))

    assert result.status == "failed"
    assert result.failure is ProviderFailureClass.CIRCUIT_OPEN
    assert adapter.calls == []
    assert quota.events == []


@pytest.mark.asyncio
async def test_non_availability_failure_never_uses_availability_only_fallback() -> None:
    adapter = FakeAdapter([ProviderFailure(ProviderFailureClass.INVALID_REQUEST)])
    result = await ModelGateway(adapter=adapter, quota=FakeQuota(), circuit=FakeCircuit()).invoke(
        _request()
    )

    assert result.status == "failed"
    assert result.failure is ProviderFailureClass.INVALID_REQUEST
    assert adapter.calls == ["primary:primary-v1:pool-a:chat"]


@pytest.mark.asyncio
async def test_quota_rejection_is_typed_and_fails_closed_before_provider_call() -> None:
    adapter = FakeAdapter([])
    result = await ModelGateway(
        adapter=adapter, quota=RejectingQuota(), circuit=FakeCircuit()
    ).invoke(_request())

    assert result.status == "failed"
    assert result.failure is ProviderFailureClass.QUOTA_UNAVAILABLE
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_malformed_provider_response_is_not_reported_as_success() -> None:
    result = await ModelGateway(
        adapter=FakeAdapter([{"provider_request_id": "missing-response-ref"}]),
        quota=FakeQuota(),
        circuit=FakeCircuit(),
    ).invoke(_request(fallback=False))

    assert result.status == "failed"
    assert result.failure is ProviderFailureClass.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_budget_port_records_each_physical_attempt_and_settles_known_usage() -> None:
    adapter = FakeAdapter(
        [
            ProviderFailure(ProviderFailureClass.TRANSIENT_UPSTREAM),
            {
                "response_ref": "artifact://response-budgeted",
                "provider_request_id": "req-budgeted",
                "usage_tokens": 31,
            },
        ]
    )
    budget = RecordingBudgetPort()
    request = _request().model_copy(update={"budget_context": _budget_context()})
    observer = RecordingInvocationObserver()
    result = await ModelGateway(
        adapter=adapter,
        quota=FakeQuota(),
        circuit=FakeCircuit(),
        budget=budget,
        observer=observer,
    ).invoke(request)

    assert result.status == "succeeded"
    assert result.usage_tokens == 31
    assert len(observer.records) == 1
    assert observer.records[0].logical_invocation_id == request.logical_invocation_id
    assert observer.records[0].usage_tokens == 31
    assert not hasattr(observer.records[0], "provider_payload")
    assert budget.events == [
        ("reserve", 1, "primary:primary-v1:pool-a:chat"),
        ("dispatch", 1, "operation-1"),
        ("ambiguous", 1, "transient_upstream"),
        ("reserve", 2, "primary:primary-v1:pool-a:chat"),
        ("dispatch", 2, "operation-2"),
        ("settle", 31, "operation-2"),
    ]


@pytest.mark.asyncio
async def test_observer_failure_cannot_change_a_settled_model_result() -> None:
    result = await ModelGateway(
        adapter=FakeAdapter(
            [
                {
                    "response_ref": "artifact://response-observed",
                    "provider_request_id": "req-observed",
                    "usage_tokens": 12,
                }
            ]
        ),
        quota=FakeQuota(),
        circuit=FakeCircuit(),
        observer=FailingInvocationObserver(),
    ).invoke(_request(fallback=False))

    assert result.status == "succeeded"
    assert result.usage_tokens == 12


@pytest.mark.asyncio
async def test_budget_port_releases_reservation_when_quota_rejects_before_provider_call() -> None:
    budget = RecordingBudgetPort()
    request = _request(fallback=False).model_copy(update={"budget_context": _budget_context()})
    result = await ModelGateway(
        adapter=FakeAdapter([]),
        quota=RejectingQuota(),
        circuit=FakeCircuit(),
        budget=budget,
    ).invoke(request)

    assert result.failure is ProviderFailureClass.QUOTA_UNAVAILABLE
    assert budget.events == [
        ("reserve", 1, "primary:primary-v1:pool-a:chat"),
        ("release", 1, "quota_unavailable"),
    ]


@pytest.mark.asyncio
async def test_budgeted_request_with_missing_usage_fails_closed_after_ambiguity() -> None:
    budget = RecordingBudgetPort()
    request = _request(fallback=False).model_copy(update={"budget_context": _budget_context()})
    result = await ModelGateway(
        adapter=FakeAdapter(
            [{"response_ref": "artifact://response-without-usage", "provider_request_id": "req-1"}]
        ),
        quota=FakeQuota(),
        circuit=FakeCircuit(),
        budget=budget,
    ).invoke(request)

    assert result.status == "failed"
    assert result.failure is ProviderFailureClass.UNKNOWN_USAGE_OR_EFFECT
    assert budget.events == [
        ("reserve", 1, "primary:primary-v1:pool-a:chat"),
        ("dispatch", 1, "operation-1"),
        ("ambiguous", 1, "missing_or_unbounded_provider_usage"),
    ]


@pytest.mark.asyncio
async def test_stream_holds_quota_permit_until_provider_stream_is_exhausted() -> None:
    events: list[str] = []
    quota = FakeQuota()
    adapter = FakeStreamAdapter(events)
    gateway = ModelGateway(adapter=FakeAdapter([]), quota=quota, circuit=FakeCircuit())

    received = [
        item
        async for item in gateway.stream(
            _request(fallback=False),
            stream_adapter=adapter,
        )
    ]

    assert received == [{"delta": "first"}, {"delta": "second"}]
    assert quota.events == [
        "acquire:primary:primary-v1:pool-a:chat",
        "release",
    ]
    assert events == ["provider:start", "provider:between", "provider:end"]


@pytest.mark.asyncio
async def test_stream_releases_quota_when_consumer_explicitly_closes_early() -> None:
    events: list[str] = []
    quota = FakeQuota()
    stream = ModelGateway(adapter=FakeAdapter([]), quota=quota, circuit=FakeCircuit()).stream(
        _request(fallback=False), stream_adapter=FakeStreamAdapter(events)
    )

    assert await anext(stream) == {"delta": "first"}
    assert quota.events == ["acquire:primary:primary-v1:pool-a:chat"]
    await stream.aclose()

    assert quota.events == [
        "acquire:primary:primary-v1:pool-a:chat",
        "release",
    ]
