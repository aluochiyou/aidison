from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Sequence
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
from aidison.application.agent_run_health import AgentRunProgressWatchdog
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
    RuntimeModelInvocationObserver,
    RuntimeTracingSettings,
    build_runtime_tracer,
)
from aidison.providers.circuit_breaker import SlidingWindowProviderCircuit
from aidison.providers.contract_probe import (
    ProviderContractProbeResult,
    probe_chat_json_contract,
)
from aidison.providers.gateway import ProviderSettings, build_chat_model, build_model_target
from aidison.providers.model_gateway import (
    ModelGateway,
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
    CompositeResearchSourceCollector,
    GitHubRepositorySourceCollector,
    LocalFileResearchSourceCollector,
    NoopResearchSourceCollector,
    ProjectDocumentResearchSourceCollector,
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
        ge=1,
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
    local_source_root: Path | None = Field(
        default=None,
        validation_alias="AIDISON_LOCAL_SOURCE_ROOT",
    )
    local_source_targets: str = Field(
        default="",
        validation_alias="AIDISON_LOCAL_SOURCE_TARGETS",
    )
    github_api_token: SecretStr | None = Field(
        default=None,
        validation_alias="AIDISON_GITHUB_API_TOKEN",
    )
    github_source_targets: str = Field(
        default="",
        validation_alias="AIDISON_GITHUB_SOURCE_TARGETS",
    )
    github_timeout_seconds: float = Field(
        default=15.0,
        validation_alias="GITHUB_TIMEOUT_SECONDS",
        gt=0,
        le=60,
    )
    model_max_concurrency: int = Field(
        default=2,
        validation_alias="AIDISON_MODEL_MAX_CONCURRENCY",
        ge=1,
        le=32,
    )
    model_circuit_failure_threshold: int = Field(
        default=5,
        validation_alias="AIDISON_MODEL_CIRCUIT_FAILURE_THRESHOLD",
        ge=1,
        le=50,
    )
    model_circuit_window_seconds: float = Field(
        default=60.0,
        validation_alias="AIDISON_MODEL_CIRCUIT_WINDOW_SECONDS",
        gt=0,
        le=3_600,
    )
    model_circuit_open_seconds: float = Field(
        default=30.0,
        validation_alias="AIDISON_MODEL_CIRCUIT_OPEN_SECONDS",
        gt=0,
        le=600,
    )
    model_contract_probe_timeout_seconds: float = Field(
        default=30.0,
        validation_alias="AIDISON_MODEL_CONTRACT_PROBE_TIMEOUT_SECONDS",
        gt=0,
        le=120,
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
    parser.add_argument(
        "--probe-provider-contract",
        action="store_true",
        help="with --check, send one live JSON-mode model probe",
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


async def check_research_dependencies(settings: WorkerSettings) -> None:
    """Validate all research runtime dependencies without contacting a source provider."""

    await check_solution_dependencies(settings)
    async with httpx.AsyncClient(timeout=_source_client_timeout(settings)) as source_client:
        # Collector construction validates static authority only: configured
        # local paths and token/allowlist pairs. It must not send a search or
        # source-read request during a dependency check.
        _research_source_collector(settings=settings, client=source_client)


async def check_provider_contract(
    settings: WorkerSettings,
) -> ProviderContractProbeResult:
    """Verify the live JSON-mode boundary only when an operator opts in."""

    provider_settings = ProviderSettings()
    model = build_chat_model(
        provider_settings,
        thinking="disabled",
        max_tokens=64,
    )
    return await probe_chat_json_contract(
        model,
        timeout_seconds=settings.model_contract_probe_timeout_seconds,
    )


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
        await _run_worker_pool(
            concurrency=settings.concurrency,
            poll_seconds=settings.poll_seconds,
            runtime_name="solution",
            run_once=worker.run_once,
        )
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
        async with httpx.AsyncClient(timeout=_source_client_timeout(settings)) as source_client:
            source_collector = _research_source_collector(
                settings=settings,
                client=source_client,
                project_document_collector=ProjectDocumentResearchSourceCollector(
                    session_factory=session_factory,
                    artifact_root=settings.artifact_root,
                ),
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
                circuit=SlidingWindowProviderCircuit(
                    failure_threshold=settings.model_circuit_failure_threshold,
                    failure_window_seconds=settings.model_circuit_window_seconds,
                    open_seconds=settings.model_circuit_open_seconds,
                ),
                budget=PostgresModelAttemptBudgetPort(session_factory=session_factory),
                observer=RuntimeModelInvocationObserver(runtime_tracer),
                dispatch_gate=AgentRunProgressWatchdog(session_factory=session_factory),
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
            await _run_worker_pool(
                concurrency=settings.concurrency,
                poll_seconds=settings.poll_seconds,
                runtime_name="research",
                run_once=worker.run_once,
            )
    finally:
        runtime_tracer.flush()
        await checkpoints.close()
        await engine.dispose()


def _research_source_collector(
    *,
    settings: WorkerSettings,
    client: httpx.AsyncClient,
    project_document_collector: ResearchSourceCollector | None = None,
) -> ResearchSourceCollector:
    """Build only explicitly configured trusted source readers.

    A GitHub source target is an operator-controlled read allowlist, never a
    model-generated URL. Supplying only a token or only targets is rejected at
    startup so deployments cannot mistake a partial configuration for authority.
    """

    collectors: list[ResearchSourceCollector] = []
    if project_document_collector is not None:
        collectors.append(project_document_collector)
    local_targets = _local_source_targets(settings.local_source_targets)
    if settings.local_source_root is None and local_targets:
        raise ValueError("AIDISON_LOCAL_SOURCE_TARGETS requires AIDISON_LOCAL_SOURCE_ROOT")
    if settings.local_source_root is not None and not local_targets:
        raise ValueError("AIDISON_LOCAL_SOURCE_ROOT requires AIDISON_LOCAL_SOURCE_TARGETS")
    if settings.local_source_root is not None:
        collectors.append(
            LocalFileResearchSourceCollector(
                source_root=settings.local_source_root,
                source_targets=local_targets,
            )
        )
    github_targets = _github_source_targets(settings.github_source_targets)
    if settings.github_api_token is None and github_targets:
        raise ValueError("AIDISON_GITHUB_SOURCE_TARGETS requires AIDISON_GITHUB_API_TOKEN")
    if settings.github_api_token is not None and not github_targets:
        raise ValueError("AIDISON_GITHUB_API_TOKEN requires AIDISON_GITHUB_SOURCE_TARGETS")
    if settings.github_api_token is not None:
        collectors.append(
            GitHubRepositorySourceCollector(
                api_token=settings.github_api_token.get_secret_value(),
                source_targets=github_targets,
                client=client,
            )
        )
    if settings.tavily_api_key is not None:
        collectors.append(
            TavilySearchResearchSourceCollector(
                api_key=settings.tavily_api_key.get_secret_value(),
                client=client,
                max_results=settings.tavily_max_results,
                max_queries=settings.tavily_max_queries,
            )
        )
    if not collectors:
        return NoopResearchSourceCollector()
    # The composite owns the Run-frozen total document budget. Returning a
    # single configured adapter directly would let multiple configured local or
    # GitHub files bypass ``max_documents_total``.
    return CompositeResearchSourceCollector(collectors)


def _github_source_targets(value: str) -> tuple[str, ...]:
    """Parse one compact config value without accepting paths from a task prompt."""

    targets = tuple(
        target.strip()
        for item in value.splitlines()
        for target in item.split(",")
        if target.strip()
    )
    if len(set(targets)) != len(targets):
        raise ValueError("AIDISON_GITHUB_SOURCE_TARGETS must not repeat a target")
    return targets


def _local_source_targets(value: str) -> tuple[str, ...]:
    """Parse configured local source paths; path safety is enforced by the collector."""

    targets = tuple(
        target.strip()
        for item in value.splitlines()
        for target in item.split(",")
        if target.strip()
    )
    if len(set(targets)) != len(targets):
        raise ValueError("AIDISON_LOCAL_SOURCE_TARGETS must not repeat a target")
    return targets


def _source_client_timeout(settings: WorkerSettings) -> float:
    """One reusable client with the least restrictive configured source timeout."""

    return max(settings.tavily_timeout_seconds, settings.github_timeout_seconds)


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
        await _run_worker_pool(
            concurrency=settings.concurrency,
            poll_seconds=settings.poll_seconds,
            runtime_name="impact",
            run_once=worker.run_once,
        )
    finally:
        runtime_tracer.flush()
        await checkpoints.close()
        await engine.dispose()


def _checkpoint_settings(database: DatabaseSettings) -> CheckpointSettings:
    """Prefer the explicit checkpoint URL; otherwise share the application database endpoint."""

    return CheckpointSettings(
        database_url=os.getenv("AIDISON_CHECKPOINT_DATABASE_URL", database.database_url)
    )


async def _run_worker_pool(
    *,
    concurrency: int,
    poll_seconds: float,
    runtime_name: str,
    run_once: Callable[[], Awaitable[object | None]],
) -> None:
    """Run bounded, local coroutine lanes without introducing another worker process.

    Each lane obtains its own durable lease through ``run_once``.  A no-work
    result backs off before the next claim; a failed Run only delays its own
    lane, allowing the remaining local capacity to keep processing independent
    projects.  ``asyncio`` cancellation is deliberately propagated so service
    shutdown does not leave background tasks detached.
    """

    async def lane() -> None:
        while True:
            try:
                outcome = await run_once()
            except Exception:
                logger.exception("%s AgentRun execution failed", runtime_name)
                await asyncio.sleep(poll_seconds)
                continue
            if outcome is None:
                await asyncio.sleep(poll_seconds)

    tasks = tuple(asyncio.create_task(lane()) for _ in range(concurrency))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = WorkerSettings()
    if args.run_id is not None and (args.runtime != "research" or not args.once):
        _parser().error("--run-id requires --runtime research --once")
    if args.probe_provider_contract and not args.check:
        _parser().error("--probe-provider-contract requires --check")
    if args.check:
        check = (
            check_research_dependencies
            if args.runtime == "research"
            else check_solution_dependencies
        )
        asyncio.run(check(settings))
        if args.probe_provider_contract:
            asyncio.run(check_provider_contract(settings))
    elif args.runtime == "research":
        asyncio.run(run_research_worker(settings, once=args.once, run_id=args.run_id))
    elif args.runtime == "solution":
        asyncio.run(run_solution_worker(settings, once=args.once))
    else:
        asyncio.run(run_impact_worker(settings, once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
