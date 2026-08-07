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

    def add_termination_listener(self, callback: Any) -> None: ...

    def remove_termination_listener(self, callback: Any) -> None: ...

    async def execute(self, query: str, *args: Any) -> str: ...

    def get_server_pid(self) -> int: ...


@dataclass(frozen=True, slots=True)
class DatabaseSignal:
    """Low-latency hint for state that is already durable in PostgreSQL."""

    project_id: UUID
    project_sequence: int
    event_type: str


class PostgresSignalSubscription:
    """One checked-out PostgreSQL connection listening for one project's hints."""

    def __init__(self, *, project_id: UUID, backend_pid: int) -> None:
        self._project_id = project_id
        self._queue: asyncio.Queue[DatabaseSignal] = asyncio.Queue(maxsize=1)
        self._terminated = asyncio.Event()
        self.backend_pid = backend_pid

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

    def connection_lost(self, _connection: Any) -> None:
        self._terminated.set()

    async def wait(self, *, timeout_seconds: float) -> DatabaseSignal | None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if self._terminated.is_set():
            raise SignalUnavailableError("PostgreSQL signal connection terminated")
        notification = asyncio.create_task(self._queue.get())
        terminated = asyncio.create_task(self._terminated.wait())
        try:
            done, _ = await asyncio.wait(
                {notification, terminated},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                return None
            if terminated in done:
                raise SignalUnavailableError("PostgreSQL signal connection terminated")
            return notification.result()
        finally:
            for task in (notification, terminated):
                if not task.done():
                    task.cancel()
            await asyncio.gather(notification, terminated, return_exceptions=True)


def signal_listener_name(project_id: UUID) -> str:
    return f"aidison-signal:{project_id}"


class PostgresSignalBus:
    """LISTEN/NOTIFY acceleration; durable rows and runtime state remain authoritative."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def subscribe(self, *, project_id: UUID) -> AsyncIterator[PostgresSignalSubscription]:
        connection = None
        driver = None
        subscription = None
        try:
            connection = await self._engine.connect()
            raw_proxy = await connection.get_raw_connection()
            driver = cast(DriverConnection, raw_proxy.driver_connection)
            await driver.execute(
                "SELECT set_config('application_name', $1, false)",
                signal_listener_name(project_id),
            )
            subscription = PostgresSignalSubscription(
                project_id=project_id,
                backend_pid=driver.get_server_pid(),
            )
            driver.add_termination_listener(subscription.connection_lost)
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
            cleanup_failed = False
            try:
                await driver.remove_listener(RUNTIME_SIGNAL_CHANNEL, subscription.receive)
            except Exception:
                cleanup_failed = True
            try:
                driver.remove_termination_listener(subscription.connection_lost)
                await driver.execute("SELECT set_config('application_name', '', false)")
            except Exception:
                cleanup_failed = True
            if cleanup_failed:
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
