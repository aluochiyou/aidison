from __future__ import annotations

import argparse
import asyncio
import os
import socket
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text

from aidison.application.research import ResearchWorker
from aidison.infrastructure.database import create_engine, create_session_factory


class WorkerSettings(BaseSettings):
    """Runtime settings for the stateless research worker process."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    artifact_root: Path = Field(
        default=Path("artifacts/data"),
        validation_alias="AIDISON_ARTIFACT_ROOT",
    )
    worker_id: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}",
        validation_alias="AIDISON_WORKER_ID",
        min_length=1,
        max_length=160,
    )
    concurrency: int = Field(
        default=3,
        validation_alias="AIDISON_WORKER_CONCURRENCY",
        ge=3,
        le=32,
    )
    lease_seconds: int = Field(
        default=60,
        validation_alias="AIDISON_WORKER_LEASE_SECONDS",
        ge=15,
        le=600,
    )
    poll_seconds: float = Field(
        default=0.5,
        validation_alias="AIDISON_WORKER_POLL_SECONDS",
        ge=0.05,
        le=10,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aidison durable research worker")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="claim and execute at most one job")
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify PostgreSQL and artifact storage dependencies, then exit",
    )
    return parser


async def check_dependencies(settings: WorkerSettings) -> None:
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    if not os.access(settings.artifact_root, os.R_OK | os.W_OK):
        raise RuntimeError(f"artifact root is not readable and writable: {settings.artifact_root}")

    engine = create_engine()
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def run_worker(settings: WorkerSettings, *, once: bool = False) -> None:
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    engine = create_engine()
    try:
        worker = ResearchWorker(
            session_factory=create_session_factory(engine),
            artifact_root=settings.artifact_root,
            lease_seconds=settings.lease_seconds,
            poll_seconds=settings.poll_seconds,
        )
        if once:
            await worker.run_once(worker_id=settings.worker_id)
            return
        await worker.run_forever(
            worker_id=settings.worker_id,
            concurrency=settings.concurrency,
        )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = WorkerSettings()
    if args.check:
        asyncio.run(check_dependencies(settings))
    else:
        asyncio.run(run_worker(settings, once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
