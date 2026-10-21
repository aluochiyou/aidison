from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import Field
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aidison.config import AidisonSettings


class DatabaseSettings(AidisonSettings):
    """PostgreSQL configuration loaded without making secrets part of Domain state."""

    yaml_section = "database"

    database_url: str = Field(
        default="postgresql+asyncpg://aidison:aidison_dev@localhost:5432/aidison",
        validation_alias="DATABASE_URL",
    )
    echo_sql: bool = Field(default=False, validation_alias="AIDISON_ECHO_SQL")


def alembic_config_url(database_url: str) -> str:
    """Escape URL percent signs only while passing through ConfigParser.

    Alembic stores ``sqlalchemy.url`` in a ConfigParser whose interpolation
    syntax also uses percent signs. A URL-encoded database password must be
    preserved when Alembic reads the option back.
    """

    return database_url.replace("%", "%%")


def create_engine(settings: DatabaseSettings | None = None) -> AsyncEngine:
    resolved = settings or DatabaseSettings()
    return create_async_engine(
        resolved.database_url,
        echo=resolved.echo_sql,
        pool_pre_ping=True,
    )


def create_session_factory(
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine or create_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=True,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    session = factory()
    try:
        yield session
        if session.in_transaction():
            await session.commit()
    except BaseException:
        if session.in_transaction():
            await session.rollback()
        raise
    finally:
        await session.close()
