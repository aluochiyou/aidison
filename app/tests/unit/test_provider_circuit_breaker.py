from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aidison.providers.circuit_breaker import SlidingWindowProviderCircuit
from aidison.providers.model_gateway import ProviderFailureClass


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _deadline() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_circuit_opens_only_after_threshold_inside_sliding_window() -> None:
    clock = _Clock()
    circuit = SlidingWindowProviderCircuit(
        failure_threshold=3,
        failure_window_seconds=10,
        open_seconds=5,
        clock=clock,
    )

    for _ in range(2):
        await circuit.record_failure(
            key="provider:model:pool:quota",
            failure=ProviderFailureClass.TRANSIENT_UPSTREAM,
        )
        assert await circuit.allow_request(
            key="provider:model:pool:quota",
            deadline=_deadline(),
        )
        clock.advance(1)

    await circuit.record_failure(
        key="provider:model:pool:quota",
        failure=ProviderFailureClass.TRANSIENT_UPSTREAM,
    )
    assert not await circuit.allow_request(
        key="provider:model:pool:quota",
        deadline=_deadline(),
    )


@pytest.mark.asyncio
async def test_circuit_ignores_caller_and_local_quota_failures() -> None:
    circuit = SlidingWindowProviderCircuit(
        failure_threshold=1,
        failure_window_seconds=10,
        open_seconds=5,
    )
    key = "provider:model:pool:quota"

    for failure in (
        ProviderFailureClass.INVALID_REQUEST,
        ProviderFailureClass.PERMISSION_DENIED,
        ProviderFailureClass.CONTEXT_OVERFLOW,
        ProviderFailureClass.QUOTA_UNAVAILABLE,
        ProviderFailureClass.CANCELLED,
    ):
        await circuit.record_failure(key=key, failure=failure)
        assert await circuit.allow_request(key=key, deadline=_deadline())


@pytest.mark.asyncio
async def test_half_open_allows_one_probe_and_success_closes_circuit() -> None:
    clock = _Clock()
    circuit = SlidingWindowProviderCircuit(
        failure_threshold=1,
        failure_window_seconds=10,
        open_seconds=5,
        clock=clock,
    )
    key = "provider:model:pool:quota"
    await circuit.record_failure(key=key, failure=ProviderFailureClass.RATE_LIMITED)
    assert not await circuit.allow_request(key=key, deadline=_deadline())

    clock.advance(5)
    assert await circuit.allow_request(key=key, deadline=_deadline())
    assert not await circuit.allow_request(key=key, deadline=_deadline())

    await circuit.record_success(key=key)
    assert await circuit.allow_request(key=key, deadline=_deadline())


@pytest.mark.asyncio
async def test_failed_half_open_probe_reopens_for_full_cooldown() -> None:
    clock = _Clock()
    circuit = SlidingWindowProviderCircuit(
        failure_threshold=1,
        failure_window_seconds=10,
        open_seconds=5,
        clock=clock,
    )
    key = "provider:model:pool:quota"
    await circuit.record_failure(key=key, failure=ProviderFailureClass.MALFORMED_RESPONSE)
    clock.advance(5)
    assert await circuit.allow_request(key=key, deadline=_deadline())

    await circuit.record_failure(key=key, failure=ProviderFailureClass.TIMEOUT_AFTER_DISPATCH)
    clock.advance(4.9)
    assert not await circuit.allow_request(key=key, deadline=_deadline())
    clock.advance(0.1)
    assert await circuit.allow_request(key=key, deadline=_deadline())


@pytest.mark.asyncio
async def test_failures_outside_window_do_not_accumulate() -> None:
    clock = _Clock()
    circuit = SlidingWindowProviderCircuit(
        failure_threshold=2,
        failure_window_seconds=10,
        open_seconds=5,
        clock=clock,
    )
    key = "provider:model:pool:quota"
    await circuit.record_failure(key=key, failure=ProviderFailureClass.TRANSIENT_UPSTREAM)
    clock.advance(11)
    await circuit.record_failure(key=key, failure=ProviderFailureClass.TRANSIENT_UPSTREAM)

    assert await circuit.allow_request(key=key, deadline=_deadline())


@pytest.mark.asyncio
async def test_nonavailability_half_open_result_closes_availability_circuit() -> None:
    clock = _Clock()
    circuit = SlidingWindowProviderCircuit(
        failure_threshold=1,
        failure_window_seconds=10,
        open_seconds=5,
        clock=clock,
    )
    key = "provider:model:pool:quota"
    await circuit.record_failure(key=key, failure=ProviderFailureClass.RATE_LIMITED)
    clock.advance(5)
    assert await circuit.allow_request(key=key, deadline=_deadline())

    await circuit.record_failure(key=key, failure=ProviderFailureClass.INVALID_REQUEST)
    assert await circuit.allow_request(key=key, deadline=_deadline())


@pytest.mark.asyncio
async def test_local_quota_rejection_releases_probe_without_claiming_recovery() -> None:
    clock = _Clock()
    circuit = SlidingWindowProviderCircuit(
        failure_threshold=1,
        failure_window_seconds=10,
        open_seconds=5,
        clock=clock,
    )
    key = "provider:model:pool:quota"
    await circuit.record_failure(key=key, failure=ProviderFailureClass.RATE_LIMITED)
    clock.advance(5)
    assert await circuit.allow_request(key=key, deadline=_deadline())

    await circuit.record_failure(key=key, failure=ProviderFailureClass.QUOTA_UNAVAILABLE)
    assert await circuit.allow_request(key=key, deadline=_deadline())
    assert not await circuit.allow_request(key=key, deadline=_deadline())
