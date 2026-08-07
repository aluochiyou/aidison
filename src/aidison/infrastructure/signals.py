from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

RUNTIME_SIGNAL_CHANNEL = "aidison_runtime_events"


class SignalUnavailableError(RuntimeError):
    """The optional low-latency notification path could not be established."""


class DriverConnection(Protocol):
    async def add_listener(self, channel: str, callback: Any) -> None: ...

    async def remove_listener(self, channel: str, callback: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class DatabaseSignal:
    """Low-latency hint for state that is already durable in PostgreSQL."""

    project_id: UUID
    project_sequence: int
    event_type: str


class PostgresSignalSubscription:
    """One checked-out PostgreSQL connection listening for one project's hints."""

    def __init__(self, *, project_id: UUID) -> None:
        self._project_id = project_id
        self._queue: asyncio.Queue[DatabaseSignal] = asyncio.Queue(maxsize=1)

    def receive(
        self,
        _connection: Any,
        _process_id: int,
        _channel: str,
        payload: str,
    ) -> None:
        try:
            decoded = json.loads(payload)
            signal = DatabaseSignal(
                project_id=UUID(decoded["project_id"]),
                project_sequence=int(decoded["project_sequence"]),
                event_type=str(decoded["event_type"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        if signal.project_id != self._project_id or self._queue.full():
            return
        self._queue.put_nowait(signal)

    async def wait(self, *, timeout_seconds: float) -> DatabaseSignal | None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout_seconds)
        except TimeoutError:
            return None


class PostgresSignalBus:
    """LISTEN/NOTIFY acceleration; durable rows and runtime state remain authoritative."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def subscribe(self, *, project_id: UUID) -> AsyncIterator[PostgresSignalSubscription]:
        subscription = PostgresSignalSubscription(project_id=project_id)
        connection = None
        driver = None
        try:
            connection = await self._engine.connect()
            raw_proxy = await connection.get_raw_connection()
            driver = cast(DriverConnection, raw_proxy.driver_connection)
            await driver.add_listener(RUNTIME_SIGNAL_CHANNEL, subscription.receive)
        except Exception as exc:
            if connection is not None:
                with suppress(Exception):
                    await connection.invalidate()
                with suppress(Exception):
                    await connection.close()
            raise SignalUnavailableError("PostgreSQL signal subscription is unavailable") from exc
        try:
            yield subscription
        finally:
            try:
                await driver.remove_listener(RUNTIME_SIGNAL_CHANNEL, subscription.receive)
            except Exception:
                # Never return a physical connection with a live listener to the pool.
                with suppress(Exception):
                    await connection.invalidate()
            with suppress(Exception):
                await connection.close()


async def publish_domain_event_signal(
    session: AsyncSession,
    *,
    project_id: UUID,
    project_sequence: int,
    event_type: str,
) -> None:
    """Publish in the caller's transaction so PostgreSQL notifies only after commit."""

    payload = json.dumps(
        {
            "project_id": str(project_id),
            "project_sequence": project_sequence,
            "event_type": event_type,
        },
        separators=(",", ":"),
    )
    await session.execute(select(func.pg_notify(RUNTIME_SIGNAL_CHANNEL, payload)))


def engine_from_session_factory(factory: Any) -> AsyncEngine:
    """Resolve the engine frozen into SQLAlchemy's async_sessionmaker."""

    engine = factory.kw.get("bind")
    if not isinstance(engine, AsyncEngine):
        raise TypeError("session factory must be bound to an AsyncEngine")
    return engine
