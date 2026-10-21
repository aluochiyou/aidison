"""Process-local sliding-window circuit breaker for model providers."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aidison.providers.model_gateway import ProviderFailureClass

_COUNTED_FAILURES = frozenset(
    {
        ProviderFailureClass.RATE_LIMITED,
        ProviderFailureClass.TRANSIENT_UPSTREAM,
        ProviderFailureClass.CONNECTION_PRE_DISPATCH,
        ProviderFailureClass.TIMEOUT_AFTER_DISPATCH,
        ProviderFailureClass.MALFORMED_RESPONSE,
    }
)


@dataclass
class _CircuitBucket:
    failure_times: deque[float] = field(default_factory=deque)
    open_until: float | None = None
    half_open_probe_in_flight: bool = False


class SlidingWindowProviderCircuit:
    """Bound repeated provider availability failures inside one worker process.

    The circuit is intentionally not durable or distributed.  Aidison currently
    runs one Python worker process, so PostgreSQL remains the authority for Run
    state and cost while this object only suppresses locally repeated calls to
    an unhealthy provider target.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        failure_window_seconds: float,
        open_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("circuit failure threshold must be positive")
        if failure_window_seconds <= 0:
            raise ValueError("circuit failure window must be positive")
        if open_seconds <= 0:
            raise ValueError("circuit open duration must be positive")
        self._failure_threshold = failure_threshold
        self._failure_window_seconds = failure_window_seconds
        self._open_seconds = open_seconds
        self._clock = clock
        self._buckets: dict[str, _CircuitBucket] = {}
        self._lock = asyncio.Lock()

    async def allow_request(self, *, key: str, deadline: datetime) -> bool:
        if deadline <= datetime.now(UTC):
            return False
        now = self._clock()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return True
            self._prune(bucket, now=now)
            if bucket.open_until is None:
                if not bucket.failure_times:
                    self._buckets.pop(key, None)
                return True
            if now < bucket.open_until:
                return False
            if bucket.half_open_probe_in_flight:
                return False
            bucket.half_open_probe_in_flight = True
            return True

    async def record_success(self, *, key: str) -> None:
        async with self._lock:
            self._buckets.pop(key, None)

    async def record_failure(self, *, key: str, failure: ProviderFailureClass) -> None:
        now = self._clock()
        async with self._lock:
            bucket = self._buckets.get(key)
            if failure not in _COUNTED_FAILURES:
                if (
                    bucket is not None
                    and bucket.half_open_probe_in_flight
                    and failure is ProviderFailureClass.QUOTA_UNAVAILABLE
                ):
                    # The local semaphore rejected the probe before any provider
                    # request. Release the single-probe slot without claiming
                    # that the upstream recovered.
                    bucket.half_open_probe_in_flight = False
                    return
                # A non-availability response from a half-open probe proves the
                # provider is reachable. The request itself may still be invalid,
                # but that must not leave the availability circuit stuck open.
                if bucket is not None and bucket.half_open_probe_in_flight:
                    self._buckets.pop(key, None)
                return

            if bucket is None:
                bucket = _CircuitBucket()
                self._buckets[key] = bucket
            self._prune(bucket, now=now)
            bucket.failure_times.append(now)
            if bucket.half_open_probe_in_flight or bucket.open_until is not None:
                bucket.half_open_probe_in_flight = False
                bucket.open_until = now + self._open_seconds
                return
            if len(bucket.failure_times) >= self._failure_threshold:
                bucket.open_until = now + self._open_seconds

    def _prune(self, bucket: _CircuitBucket, *, now: float) -> None:
        cutoff = now - self._failure_window_seconds
        while bucket.failure_times and bucket.failure_times[0] < cutoff:
            bucket.failure_times.popleft()


__all__ = ["SlidingWindowProviderCircuit"]
