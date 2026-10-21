"""LangGraph checkpoint configuration and PostgreSQL resource lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from aidison.config import AidisonSettings

CheckpointConnection = AsyncConnection[dict[str, Any]]


def normalize_psycopg_url(database_url: str) -> str:
    """Convert the application's asyncpg SQLAlchemy URL for psycopg consumers."""

    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if database_url.startswith(("postgresql://", "postgres://")):
        return database_url
    raise ValueError("checkpoint database URL must use PostgreSQL")


class CheckpointSettings(AidisonSettings):
    """Dedicated LangGraph checkpoint connection and schema settings."""

    yaml_section = None

    database_url: str = Field(
        default="postgresql://aidison:aidison_dev@localhost:5432/aidison",
        validation_alias="AIDISON_CHECKPOINT_DATABASE_URL",
    )
    schema_name: str = Field(
        default="aidison_checkpoints",
        validation_alias="AIDISON_CHECKPOINT_SCHEMA",
        pattern=r"^[a-z_][a-z0-9_]{0,62}$",
    )
    min_pool_size: int = Field(default=1, ge=1, le=16)
    max_pool_size: int = Field(default=4, ge=1, le=32)
    setup_on_start: bool = True

    @property
    def psycopg_url(self) -> str:
        return normalize_psycopg_url(self.database_url)


class CheckpointRuntimeError(RuntimeError):
    """Base error for the dedicated LangGraph checkpoint lifecycle."""


class CheckpointRuntimeUnavailable(CheckpointRuntimeError):
    """Raised when the required checkpoint store cannot be prepared."""


class CheckpointRuntimeNotStarted(CheckpointRuntimeError):
    """Raised when code tries to use a checkpoint store outside its lifecycle."""


class CheckpointRuntime:
    """Own the dedicated pool and saver without exposing Saver tables to Alembic.

    The application Domain schema and LangGraph Saver schema have separate owners.
    This object creates the validated schema once, configures every pooled connection
    with that schema as its ``search_path``, and fails closed if initialization fails.
    """

    def __init__(self, settings: CheckpointSettings | None = None) -> None:
        self._settings = settings or CheckpointSettings()
        self._pool: AsyncConnectionPool[CheckpointConnection] | None = None
        self._saver: AsyncPostgresSaver | None = None
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> CheckpointSettings:
        return self._settings

    @property
    def saver(self) -> AsyncPostgresSaver:
        if self._saver is None:
            raise CheckpointRuntimeNotStarted("LangGraph checkpoint runtime is not started")
        return self._saver

    async def start(self) -> AsyncPostgresSaver:
        """Create the schema, open the pool, and initialize Saver-owned tables."""

        async with self._lock:
            if self._saver is not None:
                return self._saver

            pool: AsyncConnectionPool[CheckpointConnection] | None = None
            try:
                await self._ensure_schema()
                pool = cast(
                    AsyncConnectionPool[CheckpointConnection],
                    AsyncConnectionPool(
                        self._settings.psycopg_url,
                        min_size=self._settings.min_pool_size,
                        max_size=self._settings.max_pool_size,
                        open=False,
                        kwargs={
                            "autocommit": True,
                            "prepare_threshold": 0,
                            "row_factory": dict_row,
                            "options": f"-c search_path={self._settings.schema_name}",
                        },
                    ),
                )
                await pool.open(wait=True)
                saver = AsyncPostgresSaver(pool)
                if self._settings.setup_on_start:
                    await saver.setup()
            except BaseException as exc:
                if pool is not None:
                    await pool.close()
                raise CheckpointRuntimeUnavailable(
                    "LangGraph checkpoint runtime could not be initialized"
                ) from exc

            self._pool = pool
            self._saver = saver
            return saver

    async def close(self) -> None:
        """Release pool resources and make future use require an explicit restart."""

        async with self._lock:
            pool = self._pool
            self._pool = None
            self._saver = None
            if pool is not None:
                await pool.close()

    async def _ensure_schema(self) -> None:
        async with await AsyncConnection.connect(
            self._settings.psycopg_url,
            autocommit=True,
        ) as connection:
            await connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self._settings.schema_name)
                )
            )
