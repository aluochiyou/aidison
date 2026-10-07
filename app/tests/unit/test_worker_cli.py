import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from aidison.research.source_collection import (
    CompositeResearchSourceCollector,
    LocalFileResearchSourceCollector,
    NoopResearchSourceCollector,
)
from aidison.worker import (
    WorkerSettings,
    _github_source_targets,
    _local_source_targets,
    _parser,
    _research_source_collector,
    _run_worker_pool,
)


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


def test_research_source_collector_is_noop_without_explicit_source_authority() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            collector = _research_source_collector(
                settings=WorkerSettings(_env_file=None),
                client=client,
            )
            assert isinstance(collector, NoopResearchSourceCollector)

    asyncio.run(exercise())


def test_research_source_collector_requires_complete_github_authority_config() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="requires AIDISON_GITHUB_API_TOKEN"):
                _research_source_collector(
                    settings=WorkerSettings(
                        _env_file=None,
                        github_source_targets="aidison-lab/flight-docs@main:spec.md",
                    ),
                    client=client,
                )
            with pytest.raises(ValueError, match="requires AIDISON_GITHUB_SOURCE_TARGETS"):
                _research_source_collector(
                    settings=WorkerSettings(
                        _env_file=None,
                        github_api_token="github-test-token",
                    ),
                    client=client,
                )

    asyncio.run(exercise())


def test_research_source_collector_requires_complete_local_source_config(tmp_path: Path) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="requires AIDISON_LOCAL_SOURCE_ROOT"):
                _research_source_collector(
                    settings=WorkerSettings(
                        _env_file=None,
                        local_source_targets="specs/flight-control.md",
                    ),
                    client=client,
                )
            with pytest.raises(ValueError, match="requires AIDISON_LOCAL_SOURCE_TARGETS"):
                _research_source_collector(
                    settings=WorkerSettings(_env_file=None, local_source_root=tmp_path),
                    client=client,
                )

            (tmp_path / "spec.md").write_text("# Spec", encoding="utf-8")
            collector = _research_source_collector(
                settings=WorkerSettings(
                    _env_file=None,
                    local_source_root=tmp_path,
                    local_source_targets="spec.md",
                ),
                client=client,
            )
            assert isinstance(collector, LocalFileResearchSourceCollector)

    asyncio.run(exercise())


def test_research_source_collector_combines_authorized_tavily_and_github_readers() -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            collector = _research_source_collector(
                settings=WorkerSettings(
                    _env_file=None,
                    tavily_api_key="tavily-test-token",
                    github_api_token="github-test-token",
                    github_source_targets="aidison-lab/flight-docs@main:spec.md",
                ),
                client=client,
            )
            assert isinstance(collector, CompositeResearchSourceCollector)

    asyncio.run(exercise())


def test_github_source_targets_accept_comma_or_line_delimited_allowlists() -> None:
    assert _github_source_targets("lab/a@main:one.md, lab/b@stable:two.md\nlab/c@v1:three.md") == (
        "lab/a@main:one.md",
        "lab/b@stable:two.md",
        "lab/c@v1:three.md",
    )
    with pytest.raises(ValueError, match="must not repeat"):
        _github_source_targets("lab/a@main:one.md,lab/a@main:one.md")


def test_local_source_targets_accept_comma_or_line_delimited_allowlists() -> None:
    assert _local_source_targets("specs/a.md, specs/b.md\nmanuals/c.txt") == (
        "specs/a.md",
        "specs/b.md",
        "manuals/c.txt",
    )
    with pytest.raises(ValueError, match="must not repeat"):
        _local_source_targets("specs/a.md,specs/a.md")
