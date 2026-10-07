import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from aidison.worker import WorkerSettings, _parser, _run_worker_pool


def test_worker_settings_use_safe_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AIDISON_ARTIFACT_ROOT",
        "AIDISON_WORKER_ID",
        "AIDISON_WORKER_CONCURRENCY",
        "AIDISON_WORKER_LEASE_SECONDS",
        "AIDISON_WORKER_POLL_SECONDS",
        "AIDISON_DURABLE_RECHECK_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = WorkerSettings(_env_file=None)

    assert settings.artifact_root == Path("artifacts/data")
    assert settings.concurrency == 3
    assert settings.lease_seconds == 60
    assert settings.poll_seconds == 0.5
    assert settings.durable_recheck_seconds == 30
    assert settings.worker_id


def test_worker_allows_one_local_coroutine_lane_and_rejects_zero() -> None:
    assert WorkerSettings(_env_file=None, concurrency=1).concurrency == 1
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        WorkerSettings(_env_file=None, concurrency=0)


def test_worker_pool_starts_the_configured_number_of_local_lanes() -> None:
    async def exercise() -> int:
        started = 0
        all_started = asyncio.Event()
        never = asyncio.Event()

        async def run_once() -> None:
            nonlocal started
            started += 1
            if started == 3:
                all_started.set()
            await never.wait()

        pool = asyncio.create_task(
            _run_worker_pool(
                concurrency=3,
                poll_seconds=0.01,
                runtime_name="fixture",
                run_once=run_once,
            )
        )
        await asyncio.wait_for(all_started.wait(), timeout=1)
        pool.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pool
        return started

    assert asyncio.run(exercise()) == 3


@pytest.mark.parametrize(
    ("arguments", "once", "check"),
    [([], False, False), (["--once"], True, False), (["--check"], False, True)],
)
def test_worker_cli_modes(arguments: list[str], once: bool, check: bool) -> None:
    parsed = _parser().parse_args(arguments)
    assert parsed.once is once
    assert parsed.check is check


def test_worker_cli_parses_targeted_research_run_id() -> None:
    parsed = _parser().parse_args(
        ["--runtime", "research", "--once", "--run-id", "cd07e61a-5d09-4e07-bda5-9a4a2f0a3cf1"]
    )

    assert str(parsed.run_id) == "cd07e61a-5d09-4e07-bda5-9a4a2f0a3cf1"
