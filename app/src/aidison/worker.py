from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import Field, SecretStr
from sqlalchemy import text

from aidison.application.agent_run_application import (
    DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING,
    DEFAULT_RESEARCH_RUNTIME_BINDING,
    DEFAULT_SOLUTION_RUNTIME_BINDING,
)
from aidison.application.impact_proposal_run_execution import (
    ImpactProposalGraphRunExecutor,
    ImpactProposalLangGraphWorker,
)
from aidison.application.langgraph_worker import LangGraphOrchestrationWorker
from aidison.application.research_run_execution import ResearchLangGraphWorker, ResearchRunExecutor
from aidison.application.solution_run_execution import SolutionLangGraphWorker, SolutionRunExecutor
from aidison.config import AidisonSettings
from aidison.impact.analyst import JsonModeImpactAnalyst
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.model_budget_port import PostgresModelAttemptBudgetPort
from aidison.observability import (
    RuntimeTracingSettings,
    build_runtime_tracer,
)
from aidison.providers.gateway import ProviderSettings, build_chat_model, build_model_target
from aidison.providers.model_gateway import (
    ModelGateway,
    ProviderCircuit,
    ProviderFailure,
    ProviderFailureClass,
    ProviderQuota,
    QuotaPermit,
)
from aidison.research.researcher import (
    GatewayJsonModeSingleTaskResearcher,
    JsonModeResearchProviderAdapter,
)
from aidison.research.source_collection import (
    NoopResearchSourceCollector,
    ResearchSourceCollector,
    TavilySearchResearchSourceCollector,
)
from aidison.runtime.checkpointing import CheckpointRuntime, CheckpointSettings
from aidison.runtime.graphs import GraphRegistry, RegisteredGraph
from aidison.solution.composer import JsonModeSolutionComposer
from aidison.solution.verifier import JsonModeInterfaceVerifier

logger = logging.getLogger(__name__)


class WorkerSettings(AidisonSettings):
    """Runtime settings for the stateless research worker process."""

    yaml_section = "runtime"

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
    durable_recheck_seconds: float = Field(
        default=30,
        validation_alias="AIDISON_DURABLE_RECHECK_SECONDS",
        ge=1,
        le=300,
    )
    tavily_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="TAVILY_API_KEY",
    )
    tavily_timeout_seconds: float = Field(
        default=15.0,
        validation_alias="TAVILY_TIMEOUT_SECONDS",
        gt=0,
        le=60,
    )
    tavily_max_results: int | None = Field(
        default=None,
        validation_alias="TAVILY_MAX_RESULTS",
        ge=1,
    )
    tavily_max_queries: int | None = Field(
        default=None,
        validation_alias="TAVILY_MAX_QUERIES",
        ge=1,
    )
    model_max_concurrency: int = Field(
        default=2,
        validation_alias="AIDISON_MODEL_MAX_CONCURRENCY",
        ge=1,
        le=32,
    )
    research_model_reserved_tokens: int = Field(
        default=8_000,
        validation_alias="AIDISON_RESEARCH_MODEL_RESERVED_TOKENS",
        ge=256,
        le=100_000,
    )
    research_model_timeout_seconds: int = Field(
        default=120,
        validation_alias="AIDISON_RESEARCH_MODEL_TIMEOUT_SECONDS",
        ge=10,
        le=600,
    )


class _LocalQuotaPermit(QuotaPermit):
    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self._released = False

    async def release(self) -> None:
        if not self._released:
            self._released = True
            self._semaphore.release()


class _LocalProviderQuota(ProviderQuota):
    """One worker-process concurrency gate; durable cost truth stays in PostgreSQL."""

    def __init__(self, *, max_concurrency: int) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def acquire(self, *, bucket_key: str, deadline: datetime) -> QuotaPermit:
        del bucket_key
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise ProviderFailure(ProviderFailureClass.QUOTA_UNAVAILABLE)
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=remaining)
        except TimeoutError as error:
            raise ProviderFailure(ProviderFailureClass.QUOTA_UNAVAILABLE) from error
        return _LocalQuotaPermit(self._semaphore)


class _AllowAllProviderCircuit(ProviderCircuit):
    """A deliberately local availability seam; it is not a distributed truth source."""

    async def allow_request(self, *, key: str, deadline: datetime) -> bool:
        del key, deadline
        return True

    async def record_success(self, *, key: str) -> None:
        del key

    async def record_failure(self, *, key: str, failure: ProviderFailureClass) -> None:
        del key, failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aidison durable runtime worker")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="claim and execute at most one job")
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify PostgreSQL and artifact storage dependencies, then exit",
    )
    parser.add_argument(
        "--runtime",
        choices=("research", "solution", "impact"),
        default="research",
        help="exact modern LangGraph runtime family to consume",
    )
    parser.add_argument(
        "--run-id",
        type=UUID,
        help="target exactly one Research AgentRun; requires --runtime research --once",
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


async def check_solution_dependencies(settings: WorkerSettings) -> None:
    """Fail closed when the dedicated LangGraph checkpoint store is unavailable."""

    await check_dependencies(settings)
    database = DatabaseSettings()
    checkpoints = CheckpointRuntime(_checkpoint_settings(database))
    try:
        await checkpoints.start()
    finally:
        await checkpoints.close()


async def run_solution_worker(settings: WorkerSettings, *, once: bool = False) -> None:
    """Consume only the exact SolutionGraph binding during the runtime cutover."""

    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    database = DatabaseSettings()
    engine = create_engine(database)
    checkpoints = CheckpointRuntime(_checkpoint_settings(database))
    runtime_tracer = build_runtime_tracer(RuntimeTracingSettings())
    try:
        executor = SolutionRunExecutor(
            session_factory=create_session_factory(engine),
            artifact_root=settings.artifact_root,
            checkpointer=await checkpoints.start(),
            composer=JsonModeSolutionComposer(build_chat_model()),
            verifier=JsonModeInterfaceVerifier(build_chat_model()),
            runtime_tracer=runtime_tracer,
        )
        worker = SolutionLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=create_session_factory(engine),
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=DEFAULT_SOLUTION_RUNTIME_BINDING,
                            compiled_graph=executor,
                        ),
                    )
                ),
                worker_id=settings.worker_id,
                lease_seconds=settings.lease_seconds,
            )
        )
        if once:
            await worker.run_once()
            return
        while True:
            try:
                outcome = await worker.run_once()
            except Exception:
                logger.exception("Solution AgentRun execution failed")
                await asyncio.sleep(settings.poll_seconds)
                continue
            if outcome is None:
                await asyncio.sleep(settings.poll_seconds)
    finally:
        runtime_tracer.flush()
        await checkpoints.close()
        await engine.dispose()


async def run_research_worker(
    settings: WorkerSettings,
    *,
    once: bool = False,
    run_id: UUID | None = None,
) -> None:
    """Consume only the direct LangGraph ResearchGraph binding."""

    if run_id is not None and not once:
        raise ValueError("targeted Research AgentRun execution requires once=True")
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    database = DatabaseSettings()
    engine = create_engine(database)
    session_factory = create_session_factory(engine)
    checkpoints = CheckpointRuntime(_checkpoint_settings(database))
    runtime_tracer = build_runtime_tracer(RuntimeTracingSettings())
    try:
        async with httpx.AsyncClient(timeout=settings.tavily_timeout_seconds) as source_client:
            source_collector = _research_source_collector(
                settings=settings,
                client=source_client,
            )
            provider_settings = ProviderSettings()
            model = build_chat_model(
                provider_settings,
                thinking="enabled",
            )
            gateway = ModelGateway(
                adapter=JsonModeResearchProviderAdapter(
                    model=model,
                    session_factory=session_factory,
                    artifact_root=settings.artifact_root,
                ),
                quota=_LocalProviderQuota(max_concurrency=settings.model_max_concurrency),
                circuit=_AllowAllProviderCircuit(),
                budget=PostgresModelAttemptBudgetPort(session_factory=session_factory),
            )
            executor = ResearchRunExecutor(
                session_factory=session_factory,
                artifact_root=settings.artifact_root,
                checkpointer=await checkpoints.start(),
                researcher=GatewayJsonModeSingleTaskResearcher(
                    gateway=gateway,
                    session_factory=session_factory,
                    artifact_root=settings.artifact_root,
                    target=build_model_target(provider_settings, quota_group="research"),
                    reserved_tokens_per_call=settings.research_model_reserved_tokens,
                    max_output_tokens=min(
                        1_600, max(256, settings.research_model_reserved_tokens // 2)
                    ),
                    timeout_seconds=settings.research_model_timeout_seconds,
                ),
                source_collector=source_collector,
                runtime_tracer=runtime_tracer,
            )
            worker = ResearchLangGraphWorker(
                orchestration_worker=LangGraphOrchestrationWorker(
                    session_factory=create_session_factory(engine),
                    registry=GraphRegistry(
                        (
                            RegisteredGraph(
                                binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
                                compiled_graph=executor,
                            ),
                        )
                    ),
                    worker_id=settings.worker_id,
                    lease_seconds=settings.lease_seconds,
                )
            )
            if once:
                await worker.run_once(run_id=run_id)
                return
            while True:
                try:
                    outcome = await worker.run_once()
                except Exception:
                    logger.exception("Research AgentRun execution failed")
                    await asyncio.sleep(settings.poll_seconds)
                    continue
                if outcome is None:
                    await asyncio.sleep(settings.poll_seconds)
    finally:
        runtime_tracer.flush()
        await checkpoints.close()
        await engine.dispose()


def _research_source_collector(
    *,
    settings: WorkerSettings,
    client: httpx.AsyncClient,
) -> ResearchSourceCollector:
    """Select the only production source collector from explicit deployment config."""

    if settings.tavily_api_key is None:
        return NoopResearchSourceCollector()
    return TavilySearchResearchSourceCollector(
        api_key=settings.tavily_api_key.get_secret_value(),
        client=client,
        max_results=settings.tavily_max_results,
        max_queries=settings.tavily_max_queries,
    )


async def run_impact_worker(settings: WorkerSettings, *, once: bool = False) -> None:
    """Consume only the exact reviewable ImpactGraph v2 binding."""

    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    database = DatabaseSettings()
    engine = create_engine(database)
    checkpoints = CheckpointRuntime(_checkpoint_settings(database))
    runtime_tracer = build_runtime_tracer(RuntimeTracingSettings())
    try:
        executor = ImpactProposalGraphRunExecutor(
            session_factory=create_session_factory(engine),
            artifact_root=settings.artifact_root,
            checkpointer=await checkpoints.start(),
            analyst=JsonModeImpactAnalyst(build_chat_model()),
            runtime_tracer=runtime_tracer,
        )
        worker = ImpactProposalLangGraphWorker(
            orchestration_worker=LangGraphOrchestrationWorker(
                session_factory=create_session_factory(engine),
                registry=GraphRegistry(
                    (
                        RegisteredGraph(
                            binding=DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING,
                            compiled_graph=executor,
                        ),
                    )
                ),
                worker_id=settings.worker_id,
                lease_seconds=settings.lease_seconds,
            )
        )
        if once:
            await worker.run_once()
            return
        while True:
            try:
                outcome = await worker.run_once()
            except Exception:
                logger.exception("Impact AgentRun execution failed")
                await asyncio.sleep(settings.poll_seconds)
                continue
            if outcome is None:
                await asyncio.sleep(settings.poll_seconds)
    finally:
        runtime_tracer.flush()
        await checkpoints.close()
        await engine.dispose()


def _checkpoint_settings(database: DatabaseSettings) -> CheckpointSettings:
    """Prefer the explicit checkpoint URL; otherwise share the application database endpoint."""

    return CheckpointSettings(
        database_url=os.getenv("AIDISON_CHECKPOINT_DATABASE_URL", database.database_url)
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = WorkerSettings()
    if args.run_id is not None and (args.runtime != "research" or not args.once):
        _parser().error("--run-id requires --runtime research --once")
    if args.check:
        check = (
            check_solution_dependencies
            if args.runtime in {"research", "solution", "impact"}
            else check_dependencies
        )
        asyncio.run(check(settings))
    elif args.runtime == "research":
        asyncio.run(run_research_worker(settings, once=args.once, run_id=args.run_id))
    elif args.runtime == "solution":
        asyncio.run(run_solution_worker(settings, once=args.once))
    else:
        asyncio.run(run_impact_worker(settings, once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
