"""Claimed R1 ResearchGraph execution without the legacy Job scheduler."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.application.langgraph_worker import LangGraphOrchestrationWorker
from aidison.application.multi_task_research import (
    AdmittedDependencyContextLoader,
    MultiTaskResearchLeafExecutor,
    MultiTaskResearchProposalFinalizer,
)
from aidison.application.research_consolidation import (
    ResearchConsolidationApplication,
    read_research_evidence_diagnostics,
    read_research_source_collection_diagnostics,
)
from aidison.application.research_decision_bridge import ResearchDecisionBridge
from aidison.application.service import DomainConflictError, DomainNotFoundError, ProjectApplication
from aidison.application.single_task_research import (
    AgentRunCancelled,
    AgentRunPaused,
    ResearchSourcesUnavailable,
    SingleTaskResearcher,
    SingleTaskResearchExecutor,
    SingleTaskResearchPayload,
)
from aidison.application.workstream_memory import ModuleWorkstreamMemoryApplication
from aidison.domain.models import Module, ModuleMemoryItem, ResearchStrategyProposal
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_run_budget import AgentRunBudgetLimitExceededError
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import ArtifactIntegrityError, ContentAddressedArtifactStore
from aidison.infrastructure.database import session_scope
from aidison.infrastructure.store import PostgresDomainStore
from aidison.observability import (
    DisabledRuntimeTracer,
    LangGraphRuntimeCallback,
    RuntimeTracer,
    TelemetryCorrelation,
)
from aidison.providers.model_gateway import ProviderFailureClass
from aidison.research.adaptive_planning import (
    AdaptivePlanAction,
    AdaptivePlanningInput,
    AdaptivePlanningPolicy,
    VerifierRoutingInput,
    build_bounded_gap_patch,
    route_verifier_tasks,
)
from aidison.research.admitted_ready_set_graph import build_admitted_ready_set_graph
from aidison.research.consolidation import SufficiencyOutcome, SufficiencyPolicy
from aidison.research.coverage import (
    CoverageContract,
    ModuleStrategy,
    OrchestrationSignals,
    select_orchestration_shape,
)
from aidison.research.decision_contracts import AgentRunDecision
from aidison.research.defaults import RESEARCH_DEFAULT_MAX_CONCURRENCY
from aidison.research.langgraph_contracts import ExecutionGrant, ProposalManifest, TaskEnvelope
from aidison.research.method_catalog import select_methods
from aidison.research.researcher import (
    JsonModeSingleTaskResearcher,
    ResearchContextBudgetError,
    ResearchModelInvocationError,
)
from aidison.research.single_task_graph import build_single_task_research_graph
from aidison.research.source_collection import NoopResearchSourceCollector, ResearchSourceCollector
from aidison.research.strategy import ResearchCollectionPolicy, ResearchRunContract
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim, AgentRunKind, AgentRunStatus
from aidison.runtime.minimal_graph import execution_thread_config
from aidison.workstreams.memory_routing import MemoryRoute, MemoryRouteDecision


@dataclass(frozen=True, slots=True)
class ResearchRunExecution:
    readiness: str
    agent_decision: AgentRunDecision | None


def _research_failure_summary(failure_code: str, error: Exception | None) -> str:
    """Return a bounded summary suitable for a user-facing workspace projection."""

    known = {
        "no_trusted_research_sources": "没有收集到可保存、可核验的研究来源",
        "tavily_network_failure": "Tavily 检索服务网络连接失败",
        "tavily_authentication_failed": "Tavily 检索凭据无效或无权访问",
        "tavily_quota_exhausted": "Tavily 检索配额已耗尽",
        "tavily_provider_unavailable": "Tavily 检索服务暂时不可用",
        "tavily_request_rejected": "Tavily 拒绝了本次检索请求",
        "tavily_response_invalid": "Tavily 返回了无法解析的检索结果",
        "tavily_response_schema_invalid": "Tavily 返回的检索结构不符合预期",
        "github_network_failure": "GitHub 资料服务网络连接失败",
        "github_authentication_failed": "GitHub 资料访问凭据无效或无权读取白名单文件",
        "github_quota_exhausted": "GitHub 资料访问配额已耗尽",
        "github_provider_unavailable": "GitHub 资料服务暂时不可用",
        "github_source_not_found": "配置的 GitHub 资料文件不存在或当前凭据不可读取",
        "github_document_too_large": "配置的 GitHub 资料文件超过了本次研究允许的正文大小",
        "github_request_rejected": "GitHub 拒绝了本次资料读取请求",
        "github_response_schema_invalid": "GitHub 返回的资料文件无法作为 UTF-8 文本解析",
        "research_graph_missing_proposal": "研究图结束时没有生成可审查提案",
        "research_gap_patch_limit_reached": "关键证据缺口超过了本轮允许的补题上限",
        "research_verifier_route_unavailable": "关键结论需要核验，但没有可执行的核验任务",
        "research_verifier_made_no_progress": "核验任务没有新增可准入的证据",
        "research_sufficiency_blocked": "研究证据不足，且当前策略无法继续扩展",
        "repeated_unresolved_gap_requires_user_steering": (
            "补题后仍出现完全相同的证据缺口，需要你调整研究范围、来源策略或项目约束"
        ),
        "runtime_budget_exhausted": "本轮 AI Token 预算已用完",
        "research_model_usage_unverified": "模型没有返回可核验的 Token 用量，已停止继续计费调用",
        "research_model_timeout": "模型调用超过本轮允许时限，已停止等待",
        "research_run_duration_exceeded": "研究运行超过了已批准的最长时长，已停止继续派发",
        "research_model_provider_failed": "模型服务未能完成本次受控研究调用",
        "research_context_budget_exceeded": "本轮 Token 预算不足以容纳最小研究上下文",
        "research_contract_artifact_invalid": "研究运行合同或覆盖合同的 Artifact 不可用",
        "research_contract_load_failed": "无法加载冻结的研究运行合同",
    }
    summary = known.get(failure_code, "研究执行时发生了受限异常")
    if error is None or failure_code != "research_execution_failed":
        return f"{summary}（{failure_code}）"
    detail = " ".join(str(error).split())[:240]
    return f"{summary}（{failure_code}{': ' + detail if detail else ''}）"


def _model_failure_code(error: ResearchModelInvocationError) -> str:
    """Keep a provider/billing stop observable without exposing raw provider text."""

    if error.result.failure is ProviderFailureClass.UNKNOWN_USAGE_OR_EFFECT:
        return "research_model_usage_unverified"
    if error.result.failure is ProviderFailureClass.TIMEOUT_AFTER_DISPATCH:
        return "research_model_timeout"
    return "research_model_provider_failed"


class ResearchRunExecutor:
    """Read a frozen CoverageContract, execute one task, then await a human decision."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
        checkpointer: BaseCheckpointSaver[Any],
        researcher: SingleTaskResearcher,
        source_collector: ResearchSourceCollector | None = None,
        runtime_tracer: RuntimeTracer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root
        self._checkpointer = checkpointer
        self._researcher = researcher
        self._source_collector = source_collector or NoopResearchSourceCollector()
        self._runtime_tracer = runtime_tracer or DisabledRuntimeTracer()

    def _graph_callback(self, *, run: AgentRun, graph_name: str) -> LangGraphRuntimeCallback:
        """Bind callback spans to one durable AgentRun, never GraphState content."""

        return LangGraphRuntimeCallback(
            tracer=self._runtime_tracer,
            correlation=TelemetryCorrelation(project_id=run.project_id, run_id=run.id),
            graph_name=graph_name,
            graph_revision=run.runtime_binding.graph_revision,
        )

    async def execute_claim(self, *, run: AgentRun, claim: AgentRunClaim) -> ResearchRunExecution:
        if run.kind is not AgentRunKind.RESEARCH:
            raise DomainConflictError("claimed AgentRun is not a Research run")
        try:
            coverage, modules, run_contract = await self._load_research_material(run=run)
        except ArtifactIntegrityError as error:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_contract_artifact_invalid",
                error=error,
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        except Exception as error:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_contract_load_failed",
                error=error,
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        remaining_seconds = self._remaining_run_duration_seconds(
            run=run,
            run_contract=run_contract,
        )
        if remaining_seconds <= 0:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_run_duration_exceeded",
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        try:
            async with asyncio.timeout(remaining_seconds):
                return await self._execute_loaded_claim(
                    run=run,
                    claim=claim,
                    coverage=coverage,
                    modules=modules,
                    run_contract=run_contract,
                )
        except TimeoutError:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_run_duration_exceeded",
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)

    @staticmethod
    def _remaining_run_duration_seconds(
        *,
        run: AgentRun,
        run_contract: ResearchRunContract,
        now: datetime | None = None,
    ) -> float:
        """Calculate one immutable Run duration from its first worker claim.

        ``started_at`` is persisted by Control before graph execution. The
        ``created_at`` fallback only supports rows written before the column
        existed; new queued time never consumes a Run's duration allowance.
        """

        current_time = now or datetime.now(UTC)
        started_at = run.started_at or run.created_at
        deadline_timestamp = started_at.timestamp() + run_contract.max_duration_seconds
        return deadline_timestamp - current_time.timestamp()

    async def _execute_loaded_claim(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        run_contract: ResearchRunContract,
    ) -> ResearchRunExecution:
        if run_contract.research_strategy is not None:
            return await self._execute_multi_task_claim(
                run=run,
                claim=claim,
                coverage=coverage,
                modules=modules,
                strategy=run_contract.research_strategy,
                source_strategy=run_contract.research_strategy.source_strategy,
                max_concurrency=run_contract.max_concurrency,
                max_token_budget=run_contract.max_token_budget,
                collection_policy=run_contract.execution_policy.collection,
                adaptive_policy=run_contract.execution_policy.adaptive,
            )
        shape = select_orchestration_shape(
            OrchestrationSignals(
                coverage_key_count=len(coverage.keys),
                module_scope_count=len(modules),
                source_strategy_count=1,
                max_concurrency=run_contract.max_concurrency,
            )
        )
        if shape.module_strategy is not ModuleStrategy.SINGLE:
            return await self._execute_multi_task_claim(
                run=run,
                claim=claim,
                coverage=coverage,
                modules=modules,
                source_strategy="primary",
                max_concurrency=run_contract.max_concurrency,
                max_token_budget=run_contract.max_token_budget,
                collection_policy=run_contract.execution_policy.collection,
                adaptive_policy=run_contract.execution_policy.adaptive,
            )
        return await self._execute_single_task_claim(
            run=run,
            claim=claim,
            coverage=coverage,
            modules=modules,
            max_token_budget=run_contract.max_token_budget,
            source_strategy="primary",
            collection_policy=run_contract.execution_policy.collection,
            adaptive_policy=run_contract.execution_policy.adaptive,
        )

    async def _execute_single_task_claim(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        max_token_budget: int,
        source_strategy: str = "primary",
        collection_policy: ResearchCollectionPolicy | None = None,
        adaptive_policy: AdaptivePlanningPolicy | None = None,
    ) -> ResearchRunExecution:
        task = TaskEnvelope(
            run_id=run.id,
            task_key="research.single_task",
            basis_hash=run.basis_hash,
            plan_revision=1,
            capability="research",
            input_refs=(run.coverage_contract_ref or "",),
            dependency_task_ids=(),
            coverage_keys=tuple(item.key for item in coverage.keys),
            allowed_tool_ids=(),
            budget_ref=f"budget://agent-run/{run.id}",
            idempotency_key=f"research.single_task:{run.id}",
            collection_policy=collection_policy,
        )
        grant = ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref=f"deadline://agent-run/{run.id}",
            idempotency_prefix=task.idempotency_key,
            model_token_cap=max_token_budget,
        )
        graph = build_single_task_research_graph(
            checkpointer=self._checkpointer,
            executor=SingleTaskResearchExecutor(
                session_factory=self._session_factory,
                artifact_root=self._artifact_root,
                researcher=self._researcher,
                source_collector=self._source_collector,
            ),
            run=run,
            claim=claim,
            task=task,
            grant=grant,
        )
        try:
            question = "\n".join(
                (
                    f"Project objective: {coverage.objective}",
                    f"Source strategy: {source_strategy}",
                    *coverage.context_lines,
                    *(f"{item.key}: {item.question}" for item in coverage.keys),
                )
            )
            callback = self._graph_callback(run=run, graph_name="single_task_research")
            config: dict[str, Any] = execution_thread_config(
                thread_id=run.thread_id,
                generation=claim.generation,
            )
            config["callbacks"] = [callback]
            try:
                state = await graph.ainvoke(
                    {"run_id": str(run.id), "question": question},
                    config,
                )
            finally:
                callback.close()
        except AgentRunCancelled:
            return ResearchRunExecution(readiness="cancelled", agent_decision=None)
        except AgentRunPaused:
            return ResearchRunExecution(readiness="paused", agent_decision=None)
        except ResearchSourcesUnavailable as error:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code=error.reason_code,
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        except AgentRunBudgetLimitExceededError:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="runtime_budget_exhausted",
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        except ResearchContextBudgetError:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_context_budget_exceeded",
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        except ResearchModelInvocationError as error:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code=_model_failure_code(error),
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        except Exception as error:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_execution_failed",
                error=error,
            )
            raise
        if not isinstance(state.get("proposal_manifest_ref"), str):
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_graph_missing_proposal",
            )
            raise RuntimeError("ResearchGraph ended without a proposal manifest")
        proposal = await self._complete_coverage_or_expand_gaps(
            run=run,
            claim=claim,
            coverage=coverage,
            modules=modules,
            collection_policy=collection_policy,
            adaptive_policy=adaptive_policy or AdaptivePlanningPolicy(),
            initial_tasks=(task,),
            initial_proposal=ProposalManifest(
                run_id=run.id,
                basis_hash=run.basis_hash,
                artifact_ref=state["proposal_manifest_ref"],
                manifest_hash=state["proposal_manifest_hash"],
            ),
            source_strategy=source_strategy,
        )
        if proposal is None:
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        decision, _ = await ResearchDecisionBridge(
            session_factory=self._session_factory
        ).prepare_from_interrupt(run=run, claim=claim, graph=graph, proposal_override=proposal)
        return ResearchRunExecution(readiness="ready", agent_decision=decision)

    async def _execute_multi_task_claim(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        strategy: ResearchStrategyProposal | None = None,
        source_strategy: str = "primary",
        max_concurrency: int = RESEARCH_DEFAULT_MAX_CONCURRENCY,
        max_token_budget: int = 4_000,
        collection_policy: ResearchCollectionPolicy | None = None,
        adaptive_policy: AdaptivePlanningPolicy | None = None,
    ) -> ResearchRunExecution:
        tasks, questions = self._build_module_tasks(
            run=run,
            coverage=coverage,
            modules=modules,
            strategy=strategy,
            collection_policy=collection_policy,
        )
        reusable_memory_by_task_id, revalidation_task_ids = (
            await self._route_workstream_memory_for_tasks(
                run=run,
                coverage=coverage,
                modules=modules,
                tasks=tasks,
            )
        )
        for task_id in revalidation_task_ids:
            questions[task_id] = (
                questions[task_id]
                + "\n\nWorkstream revalidation required: the module or coverage applicability "
                "changed since prior admitted research. Retrieve fresh sources for this Run; "
                "do not treat any historical summary as evidence."
            )
        leaf_executor = SingleTaskResearchExecutor(
            session_factory=self._session_factory,
            artifact_root=self._artifact_root,
            researcher=self._researcher,
            source_collector=self._source_collector,
        )
        try:
            model_token_caps = self._allocate_initial_task_token_caps(
                tasks=tasks,
                max_token_budget=max_token_budget,
            )
            graph = build_admitted_ready_set_graph(
                checkpointer=self._checkpointer,
                session_factory=self._session_factory,
                executor=MultiTaskResearchLeafExecutor(
                    leaf_executor=leaf_executor,
                    run=run,
                    claim=claim,
                    questions_by_task_id=questions,
                    model_token_caps_by_task_id=model_token_caps,
                    dependency_context_loader=AdmittedDependencyContextLoader(
                        session_factory=self._session_factory,
                        artifact_root=self._artifact_root,
                        run=run,
                    ),
                    reusable_memory_by_task_id=reusable_memory_by_task_id,
                ),
                finalizer=MultiTaskResearchProposalFinalizer(
                    session_factory=self._session_factory,
                    artifact_root=self._artifact_root,
                    run=run,
                    objective=coverage.objective,
                ),
            )
            callback = self._graph_callback(run=run, graph_name="admitted_ready_set")
            config: dict[str, Any] = execution_thread_config(
                thread_id=run.thread_id,
                generation=claim.generation,
            )
            config["callbacks"] = [callback]
            try:
                state = await graph.ainvoke(
                    {
                        "tasks": tuple(item.model_dump(mode="json") for item in tasks),
                        "available_capacity": min(max_concurrency, len(tasks)),
                        "max_waves": None,
                    },
                    config,
                )
            finally:
                callback.close()
        except AgentRunCancelled:
            return ResearchRunExecution(readiness="cancelled", agent_decision=None)
        except AgentRunPaused:
            return ResearchRunExecution(readiness="paused", agent_decision=None)
        except ResearchSourcesUnavailable as error:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code=error.reason_code,
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        except AgentRunBudgetLimitExceededError:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="runtime_budget_exhausted",
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        except ResearchContextBudgetError:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_context_budget_exceeded",
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        except ResearchModelInvocationError as error:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code=_model_failure_code(error),
            )
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        except Exception as error:
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_execution_failed",
                error=error,
            )
            raise
        if not isinstance(state.get("proposal_manifest_ref"), str):
            await self._fail_claim_if_current(
                run=run,
                claim=claim,
                failure_code="research_graph_missing_proposal",
            )
            raise RuntimeError("multi-task ResearchGraph ended without a proposal manifest")
        proposal = await self._complete_coverage_or_expand_gaps(
            run=run,
            claim=claim,
            coverage=coverage,
            modules=modules,
            collection_policy=collection_policy,
            adaptive_policy=adaptive_policy or AdaptivePlanningPolicy(),
            initial_tasks=tasks,
            initial_proposal=ProposalManifest(
                run_id=run.id,
                basis_hash=run.basis_hash,
                artifact_ref=state["proposal_manifest_ref"],
                manifest_hash=state["proposal_manifest_hash"],
            ),
            source_strategy=source_strategy,
        )
        if proposal is None:
            return ResearchRunExecution(readiness="blocked", agent_decision=None)
        decision, _ = await ResearchDecisionBridge(
            session_factory=self._session_factory
        ).prepare_from_interrupt(run=run, claim=claim, graph=graph, proposal_override=proposal)
        return ResearchRunExecution(readiness="ready", agent_decision=decision)

    @staticmethod
    def _allocate_initial_task_token_caps(
        *,
        tasks: tuple[TaskEnvelope, ...],
        max_token_budget: int,
    ) -> dict[UUID, int]:
        """Divide the approved initial-call budget deterministically.

        Parallel branches cannot reserve a first-completion-wins share.  The
        deterministic initial allocation leaves any settled remainder for
        later bounded gap/verification tasks, which continue to use the
        ledger's actual remaining balance.
        """

        if max_token_budget < 1:
            raise ValueError("max_token_budget must be positive")
        if not tasks:
            return {}
        if max_token_budget < len(tasks):
            raise ResearchContextBudgetError("research_initial_task_budget_is_insufficient")
        ordered = tuple(sorted(tasks, key=lambda item: (item.task_key, str(item.id))))
        base, remainder = divmod(max_token_budget, len(ordered))
        return {
            task.id: base + (1 if index < remainder else 0)
            for index, task in enumerate(ordered)
        }

    @staticmethod
    def _build_module_tasks(
        *,
        run: AgentRun,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        strategy: ResearchStrategyProposal | None = None,
        collection_policy: ResearchCollectionPolicy | None = None,
    ) -> tuple[tuple[TaskEnvelope, ...], dict[Any, str]]:
        if strategy is not None:
            return ResearchRunExecutor._build_strategy_tasks(
                run=run,
                coverage=coverage,
                modules=modules,
                strategy=strategy,
                collection_policy=collection_policy,
            )
        tasks: list[TaskEnvelope] = []
        questions: dict[Any, str] = {}
        project_scope_keys = tuple(item for item in coverage.keys if not item.module_ids)
        ordered_modules = tuple(sorted(modules, key=lambda item: item.key))
        for index, module in enumerate(ordered_modules):
            module_keys = tuple(item for item in coverage.keys if str(module.id) in item.module_ids)
            if index == 0:
                module_keys = (*project_scope_keys, *module_keys)
            if not module_keys:
                raise DomainConflictError("module has no compiled research coverage")
            question = "\n".join(
                (
                    f"Project objective: {coverage.objective}",
                    *coverage.context_lines,
                    f"Module: {module.name} ({module.key})",
                    *(f"{item.key}: {item.question}" for item in module_keys),
                )
            )
            if len(question) > 2_000:
                raise DomainConflictError("module research task exceeds the bounded question limit")
            task_key = f"research.module.{module.key}"
            task = TaskEnvelope(
                id=uuid5(
                    NAMESPACE_URL,
                    f"aidison://research-task/{run.id}/{task_key}/revision/1",
                ),
                run_id=run.id,
                task_key=task_key,
                basis_hash=run.basis_hash,
                plan_revision=1,
                capability="research",
                input_refs=(run.coverage_contract_ref or "", f"module://{module.id}"),
                dependency_task_ids=(),
                coverage_keys=tuple(item.key for item in module_keys),
                allowed_tool_ids=(),
                budget_ref=f"budget://agent-run/{run.id}",
                idempotency_key=f"research.module:{run.id}:{module.id}:1",
                collection_policy=collection_policy,
            )
            tasks.append(task)
            questions[task.id] = question
        return tuple(tasks), questions

    @staticmethod
    def _build_strategy_tasks(
        *,
        run: AgentRun,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        strategy: ResearchStrategyProposal,
        collection_policy: ResearchCollectionPolicy | None = None,
    ) -> tuple[tuple[TaskEnvelope, ...], dict[Any, str]]:
        """Compile an approved strategy into server-owned task envelopes.

        Strategy text narrows each task's research intent, but the model never
        supplies Coverage Keys.  The compiler creates one ``strategy.*`` key
        per approved task and assigns the pre-existing module/project keys to
        deterministic primary tasks.  Thus every graph edge and coverage key
        remains replayable and reviewable after planning.
        """

        modules_by_id = {item.id: item for item in modules}
        if set(strategy.scope_module_ids) != set(modules_by_id):
            raise DomainConflictError("research strategy scope differs from frozen run scope")
        ordered_strategy_tasks = tuple(sorted(strategy.tasks, key=lambda item: item.task_key))
        task_ids = {
            item.task_key: uuid5(
                NAMESPACE_URL,
                f"aidison://research-task/{run.id}/strategy/{item.task_key}/revision/1",
            )
            for item in ordered_strategy_tasks
        }
        primary_task_key_by_module_id: dict[UUID, str] = {}
        for item in ordered_strategy_tasks:
            module_id = item.module_ids[0]
            primary_task_key_by_module_id.setdefault(module_id, item.task_key)
        coverage_by_module_id: dict[str, tuple[str, ...]] = {}
        project_scope_keys = tuple(
            item.key for item in coverage.keys if not item.module_ids
        )
        for module in modules:
            coverage_by_module_id[str(module.id)] = tuple(
                item.key
                for item in coverage.keys
                if item.module_ids == (str(module.id),)
                and not item.key.startswith("strategy.")
            )

        task_envelopes: list[TaskEnvelope] = []
        questions: dict[Any, str] = {}
        first_task_key = next(
            item.task_key
            for item in ordered_strategy_tasks
            if not item.depends_on_task_keys
        )
        coverage_questions = {item.key: item.question for item in coverage.keys}
        for planned_task in ordered_strategy_tasks:
            module_id = planned_task.module_ids[0]
            module = modules_by_id.get(module_id)
            if module is None:
                raise DomainConflictError("research strategy task references unavailable module")
            assigned_keys = [f"strategy.{planned_task.task_key}"]
            if primary_task_key_by_module_id[module_id] == planned_task.task_key:
                assigned_keys.extend(coverage_by_module_id[str(module_id)])
            if planned_task.task_key == first_task_key:
                assigned_keys.extend(project_scope_keys)
            assigned_keys = sorted(set(assigned_keys))
            if any(item not in coverage_questions for item in assigned_keys):
                raise DomainConflictError("research strategy task has no compiled coverage")
            method_guidance = tuple(
                f"Method {item.key}: {item.guidance}"
                for item in select_methods(
                    task=planned_task,
                    research_depth=(collection_policy.profile if collection_policy else "standard"),
                )
            )
            question = "\n".join(
                (
                    f"Project objective: {coverage.objective}",
                    *coverage.context_lines,
                    f"Research task: {planned_task.title}",
                    f"Task objective: {planned_task.objective}",
                    f"Module: {module.name} ({module.key})",
                    f"Source strategy: {strategy.source_strategy}",
                    f"Expected outputs: {', '.join(planned_task.expected_outputs)}",
                    "Stop when: " + "; ".join(planned_task.stop_conditions),
                    *(
                        (
                            "Research lenses: "
                            + " | ".join(planned_task.research_lenses),
                        )
                        if planned_task.research_lenses
                        else ()
                    ),
                    *(f"{item}: {coverage_questions[item]}" for item in assigned_keys),
                    *method_guidance,
                )
            )
            if len(question) > 2_000:
                raise DomainConflictError("strategy task question exceeds bounded limit")
            task_id = task_ids[planned_task.task_key]
            task_envelopes.append(
                TaskEnvelope(
                    id=task_id,
                    run_id=run.id,
                    task_key=f"research.strategy.{planned_task.task_key}",
                    basis_hash=run.basis_hash,
                    plan_revision=1,
                    capability="research",
                    input_refs=(
                        run.coverage_contract_ref or "",
                        run.run_contract_ref or "",
                        f"module://{module.id}",
                    ),
                    dependency_task_ids=tuple(
                        task_ids[item] for item in planned_task.depends_on_task_keys
                    ),
                    coverage_keys=tuple(assigned_keys),
                    allowed_tool_ids=(),
                    budget_ref=f"budget://agent-run/{run.id}",
                    idempotency_key=(
                        f"research.strategy:{run.id}:{planned_task.task_key}:1"
                    ),
                    collection_policy=collection_policy,
                )
            )
            questions[task_id] = question
        return tuple(task_envelopes), questions

    async def _route_workstream_memory_for_tasks(
        self,
        *,
        run: AgentRun,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        tasks: tuple[TaskEnvelope, ...],
    ) -> tuple[dict[UUID, tuple[ModuleMemoryItem, ...]], set[UUID]]:
        """Classify module-scoped task history before the first dispatch.

        Exact applicable memory can take the synthetic reuse leaf. A relevant
        but changed fingerprint keeps the normal task and adds a revalidation
        instruction. Project-scoped and dependent tasks remain fresh: they
        either combine multiple contracts or receive current-Run upstream
        leads, so an old single-module result is not enough authority.
        """

        coverage_by_key = {item.key: item for item in coverage.keys}
        modules_by_id = {str(item.id): item for item in modules if isinstance(item, Module)}
        reusable: dict[UUID, tuple[ModuleMemoryItem, ...]] = {}
        revalidate: set[UUID] = set()
        async with session_scope(self._session_factory) as session:
            store = PostgresDomainStore(session)
            memory = ModuleWorkstreamMemoryApplication(store)
            workstreams = {
                item.module_lineage_id: item
                for item in await store.list_module_workstreams(run.project_id)
            }
            items_by_id: dict[UUID, ModuleMemoryItem] = {}
            for workstream in workstreams.values():
                for item in await store.list_module_memory_items(workstream.id):
                    items_by_id[item.id] = item

            for task in tasks:
                if task.dependency_task_ids:
                    continue
                module_refs = tuple(
                    item for item in task.input_refs if item.startswith("module://")
                )
                if len(module_refs) != 1:
                    continue
                module = modules_by_id.get(module_refs[0].removeprefix("module://"))
                keys = tuple(coverage_by_key.get(key) for key in task.coverage_keys)
                if module is None or any(key is None for key in keys):
                    continue
                typed_keys = tuple(key for key in keys if key is not None)
                if any(key.module_ids != (str(module.id),) for key in typed_keys):
                    continue
                decisions: list[MemoryRouteDecision] = []
                for key in typed_keys:
                    decisions.append(
                        await memory.route_for_coverage(
                            project_id=run.project_id,
                            module=module,
                            coverage_key=key,
                        )
                    )
                if any(item.route is MemoryRoute.REVALIDATE for item in decisions):
                    revalidate.add(task.id)
                    continue
                if not decisions or any(item.route is not MemoryRoute.REUSE for item in decisions):
                    continue
                selected = tuple(
                    items_by_id[item.item_id]
                    for item in decisions
                    if item.item_id is not None and item.item_id in items_by_id
                )
                if len(selected) != len(typed_keys) or any(
                    item.source_result_id is None for item in selected
                ):
                    continue
                reusable[task.id] = selected
        return reusable, revalidate

    async def _load_research_material(
        self,
        *,
        run: AgentRun,
    ) -> tuple[CoverageContract, tuple[Any, ...], ResearchRunContract]:
        if run.coverage_contract_ref is None:
            raise DomainConflictError("Research AgentRun is missing its Coverage Contract")
        if run.run_contract_ref is None:
            raise DomainConflictError("Research AgentRun is missing its Run Contract")
        async with session_scope(self._session_factory) as session:
            store = PostgresDomainStore(session)
            project = await store.get_project(run.project_id)
            if project is None:
                raise DomainNotFoundError("project not found")
            if project.revision != run.basis_project_revision:
                raise DomainConflictError("Research AgentRun basis project revision is stale")
            active_modules = await ProjectApplication(store)._active_modules(project)
            if not active_modules:
                raise DomainConflictError("Research AgentRun has no active modules")
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            run_contract = ResearchRunContract.model_validate(
                await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=run.run_contract_ref,
                    expected_kind="research_run_contract",
                )
            )
            if run_contract.agent_run_id != run.id or run_contract.basis_hash != run.basis_hash:
                raise DomainConflictError("Research Run Contract is stale")
            modules_by_id = {item.id: item for item in active_modules}
            if any(item not in modules_by_id for item in run_contract.scope_module_ids):
                raise DomainConflictError("Research Run Contract scope is no longer active")
            modules = tuple(
                modules_by_id[item]
                for item in sorted(run_contract.scope_module_ids, key=lambda value: str(value))
            )
            coverage = CoverageContract.model_validate(
                await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=run.coverage_contract_ref,
                    expected_kind="research_coverage_contract",
                )
            )
            if coverage.basis_hash != run.basis_hash:
                raise DomainConflictError("Research Coverage Contract basis is stale")
            if any(
                any(UUID(item) not in set(run_contract.scope_module_ids) for item in key.module_ids)
                for key in coverage.keys
            ):
                raise DomainConflictError("Research Coverage Contract exceeds the frozen run scope")
            return coverage, modules, run_contract

    async def _fail_claim_if_current(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        failure_code: str,
        error: Exception | None = None,
    ) -> None:
        """Record a safe, user-visible failure audit before terminal transition.

        The event is intentionally a small classified summary, not raw provider
        output, prompts, stack traces or secrets.  A stale worker cannot append
        it because completion remains fenced by the same live claim.
        """

        try:
            async with session_scope(self._session_factory) as session:
                control = AgentRunControl(session)
                await control.ensure_active_claim(claim=claim)
                await PostgresDomainStore(session).append_event(
                    run.project_id,
                    "agent_run.failed",
                    {
                        "agent_run_id": str(run.id),
                        "failure_code": failure_code,
                        "failure_summary": _research_failure_summary(failure_code, error),
                    },
                )
                await control.complete(claim=claim, status=AgentRunStatus.FAILED)
        except Exception:
            return

    async def _complete_coverage_or_expand_gaps(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        collection_policy: ResearchCollectionPolicy | None,
        adaptive_policy: AdaptivePlanningPolicy,
        initial_tasks: tuple[TaskEnvelope, ...],
        initial_proposal: ProposalManifest,
        source_strategy: str = "primary",
    ) -> ProposalManifest | None:
        """Complete only explicit MUST gaps under the deterministic patch policy.

        A patch is derived from admitted observations, never from a model's request
        for more work.  The added tasks are deterministic from the frozen run,
        current plan revision and coverage keys; their Result/Admission records
        remain the recovery truth even though the patch planning loop is local.
        """

        consolidation = await self._consolidate_for_adaptation(run=run, coverage=coverage)
        if consolidation.snapshot.sufficiency.outcome is SufficiencyOutcome.COMPLETE:
            await self._record_workstream_memory(
                run=run,
                coverage=coverage,
                modules=modules,
                consolidation=consolidation,
            )
            return initial_proposal

        tasks = list(initial_tasks)
        plan_revision = 1
        patch_revision_count = 0
        consecutive_no_progress = 0
        previous_gaps = consolidation.snapshot.sufficiency.gap_coverage_keys
        last_repaired_gap_signature: tuple[str, ...] | None = None
        leaf_executor = SingleTaskResearchExecutor(
            session_factory=self._session_factory,
            artifact_root=self._artifact_root,
            researcher=self._researcher,
            source_collector=self._source_collector,
        )
        while consolidation.snapshot.sufficiency.outcome is not SufficiencyOutcome.COMPLETE:
            if consolidation.snapshot.sufficiency.outcome is SufficiencyOutcome.NEEDS_MORE_EVIDENCE:
                immediate_source_failure = await self._source_gap_reason_for_coverage(
                    run=run,
                    coverage_keys=consolidation.snapshot.sufficiency.gap_coverage_keys,
                )
                # No source adapter is a deployment/configuration condition,
                # not a research hypothesis. Retrying it cannot produce new
                # evidence, so keep the concrete error rather than treating it
                # as a user steering problem after a futile patch.
                if immediate_source_failure == "no_trusted_research_sources":
                    await self._fail_claim_if_current(
                        run=run,
                        claim=claim,
                        failure_code=immediate_source_failure,
                    )
                    return None
                current_gap_signature = await self._unresolved_gap_signature(
                    run=run,
                    snapshot=consolidation.snapshot,
                )
                plan = build_bounded_gap_patch(
                    AdaptivePlanningInput(
                        run_id=run.id,
                        basis_hash=run.basis_hash,
                        plan_revision=plan_revision,
                        existing_task_count=len(tasks),
                        patch_revision_count=patch_revision_count,
                        consecutive_no_progress=consecutive_no_progress,
                        repeated_unresolved_signature=(
                            current_gap_signature
                            if current_gap_signature == last_repaired_gap_signature
                            else ()
                        ),
                        coverage_contract=coverage,
                        snapshot=consolidation.snapshot,
                    allowed_tool_ids=(),
                    budget_ref=f"budget://agent-run/{run.id}",
                    policy=adaptive_policy,
                    )
                )
                if plan.action is not AdaptivePlanAction.CREATE_GAP_PATCH or plan.patch is None:
                    failure_code = (
                        "repeated_unresolved_gap_requires_user_steering"
                        if "repeated_unresolved_gap_requires_user_steering" in plan.reason_codes
                        else await self._source_gap_reason_for_coverage(
                            run=run,
                            coverage_keys=consolidation.snapshot.sufficiency.gap_coverage_keys,
                        )
                    )
                    await self._fail_claim_if_current(
                        run=run,
                        claim=claim,
                        failure_code=failure_code or "research_gap_patch_limit_reached",
                    )
                    return None
                await self._persist_gap_patch(run=run, patch=plan.patch)
                guidance_by_coverage_key = await self._build_gap_guidance(
                    run=run,
                    snapshot=consolidation.snapshot,
                )
                await self._persist_gap_guidance(
                    run=run,
                    patch=plan.patch,
                    guidance_by_coverage_key=guidance_by_coverage_key,
                )
                patch_tasks = self._materialize_gap_tasks(
                    run=run,
                    coverage=coverage,
                    modules=modules,
                    patch=plan.patch,
                    collection_policy=collection_policy,
                    guidance_by_coverage_key=guidance_by_coverage_key,
                    source_strategy=source_strategy,
                )
                for task, question in patch_tasks:
                    if not await self._task_is_admitted(run_id=run.id, task_id=task.id):
                        await leaf_executor.execute(
                            run=run,
                            claim=claim,
                            task=task,
                            grant=self._task_grant(run=run, claim=claim, task=task),
                            question=question,
                        )
                    tasks.append(task)
                patch_revision_count += 1
                plan_revision = plan.patch.next_plan_revision
            elif (
                consolidation.snapshot.sufficiency.outcome is SufficiencyOutcome.NEEDS_VERIFICATION
            ):
                verifier_tasks = self._materialize_verifier_tasks(
                    run=run,
                    coverage=coverage,
                    modules=modules,
                    plan_revision=plan_revision,
                    snapshot=consolidation.snapshot,
                    collection_policy=collection_policy,
                    source_strategy=source_strategy,
                )
                if not verifier_tasks:
                    await self._fail_claim_if_current(
                        run=run,
                        claim=claim,
                        failure_code="research_verifier_route_unavailable",
                    )
                    return None
                await self._persist_verifier_route(
                    run=run,
                    snapshot=consolidation.snapshot,
                    tasks=tuple(task for task, _ in verifier_tasks),
                )
                ran_new_verifier = False
                for task, question in verifier_tasks:
                    if not await self._task_is_admitted(run_id=run.id, task_id=task.id):
                        await leaf_executor.execute(
                            run=run,
                            claim=claim,
                            task=task,
                            grant=self._task_grant(run=run, claim=claim, task=task),
                            question=question,
                            independently_verified=True,
                        )
                        ran_new_verifier = True
                    tasks.append(task)
                if not ran_new_verifier:
                    await self._fail_claim_if_current(
                        run=run,
                        claim=claim,
                        failure_code="research_verifier_made_no_progress",
                    )
                    return None
            else:
                await self._fail_claim_if_current(
                    run=run,
                    claim=claim,
                    failure_code="research_sufficiency_blocked",
                )
                return None
            consolidation = await self._consolidate_for_adaptation(run=run, coverage=coverage)
            current_gaps = consolidation.snapshot.sufficiency.gap_coverage_keys
            consecutive_no_progress = (
                consecutive_no_progress + 1 if current_gaps == previous_gaps else 0
            )
            previous_gaps = current_gaps
            if consolidation.snapshot.sufficiency.outcome is SufficiencyOutcome.NEEDS_MORE_EVIDENCE:
                last_repaired_gap_signature = await self._unresolved_gap_signature(
                    run=run,
                    snapshot=consolidation.snapshot,
                )
            else:
                last_repaired_gap_signature = None

        if len(tasks) == len(initial_tasks):
            await self._record_workstream_memory(
                run=run,
                coverage=coverage,
                modules=modules,
                consolidation=consolidation,
            )
            return initial_proposal
        proposal = await self._aggregate_gap_patch_manifest(
            run=run,
            coverage=coverage,
            modules=modules,
            tasks=tuple(tasks),
        )
        await self._record_workstream_memory(
            run=run,
            coverage=coverage,
            modules=modules,
            consolidation=consolidation,
        )
        return proposal

    async def _consolidate_for_adaptation(
        self,
        *,
        run: AgentRun,
        coverage: CoverageContract,
    ) -> Any:
        return await ResearchConsolidationApplication(
            session_factory=self._session_factory,
            artifact_root=self._artifact_root,
        ).consolidate(
            run=run,
            coverage=coverage,
            policy=SufficiencyPolicy(
                evidence_expansion_available=True,
                verification_available=True,
            ),
        )

    async def _record_workstream_memory(
        self,
        *,
        run: AgentRun,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        consolidation: Any,
    ) -> None:
        """Write only complete admitted module coverage as cross-Run memory.

        Consolidation is the gate: raw model output, partial coverage and
        project-scoped obligations cannot become reusable memory. The writer
        is idempotent, so graph replay cannot duplicate an item or advance a
        workstream's optimistic revision twice.
        """

        async with session_scope(self._session_factory) as session:
            results = await AgentResultStore(session).admitted_results(run_id=run.id)
            await ModuleWorkstreamMemoryApplication(
                PostgresDomainStore(session)
            ).record_answered_coverage(
                project_id=run.project_id,
                project_revision=run.basis_project_revision,
                coverage=coverage,
                modules=modules,
                matrix=consolidation.snapshot.coverage,
                admitted_results=results,
            )

    async def _source_gap_reason_for_coverage(
        self,
        *,
        run: AgentRun,
        coverage_keys: tuple[str, ...],
    ) -> str | None:
        """Prefer the concrete collection failure over a generic patch-limit label."""

        if not coverage_keys:
            return None
        async with session_scope(self._session_factory) as session:
            diagnostics = await read_research_source_collection_diagnostics(
                session=session,
                artifact_root=self._artifact_root,
                run=run,
            )
        reasons: set[str] = set()
        for coverage_key in coverage_keys:
            value = diagnostics.get(coverage_key, {}).get("unavailable_reason_codes", [])
            if not isinstance(value, list):
                continue
            reasons.update(reason for reason in value if isinstance(reason, str))
        return min(reasons) if reasons else None

    async def _unresolved_gap_signature(self, *, run: AgentRun, snapshot: Any) -> tuple[str, ...]:
        """Canonical observable state used to detect a futile identical repair.

        The signature intentionally uses only admitted Coverage projection and
        diagnostic reason codes. It excludes model messages, source bodies and
        private context. A changed source set that creates new evidence changes
        the Coverage facts and therefore does not trigger this stop.
        """

        async with session_scope(self._session_factory) as session:
            evidence = await read_research_evidence_diagnostics(
                session=session,
                artifact_root=self._artifact_root,
                run=run,
            )
            collection = await read_research_source_collection_diagnostics(
                session=session,
                artifact_root=self._artifact_root,
                run=run,
            )
        entries: list[str] = []
        gaps = set(snapshot.sufficiency.gap_coverage_keys)
        for item in snapshot.coverage:
            if item.coverage_key not in gaps:
                continue
            rejected = evidence.get(item.coverage_key, {}).get("rejected_reason_codes", [])
            unavailable = collection.get(item.coverage_key, {}).get(
                "unavailable_reason_codes", []
            )
            rejected_codes = rejected if isinstance(rejected, list) else []
            unavailable_codes = unavailable if isinstance(unavailable, list) else []
            entries.append(
                "|".join(
                    (
                        item.coverage_key,
                        item.status.value,
                        str(item.observed_source_count),
                        str(item.observed_origin_count),
                        ",".join(sorted(item.missing_source_kinds)),
                        ",".join(
                            sorted(value for value in rejected_codes if isinstance(value, str))
                        ),
                        ",".join(
                            sorted(value for value in unavailable_codes if isinstance(value, str))
                        ),
                    )
                )
            )
        return tuple(sorted(entries))

    @staticmethod
    def _task_grant(*, run: AgentRun, claim: AgentRunClaim, task: TaskEnvelope) -> ExecutionGrant:
        return ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=claim.generation,
            lease_token=claim.lease_token,
            deadline_ref=f"deadline://agent-run/{run.id}/task/{task.id}",
            idempotency_prefix=task.idempotency_key,
        )

    async def _persist_gap_patch(self, *, run: AgentRun, patch: Any) -> None:
        async with session_scope(self._session_factory) as session:
            await ContentAddressedArtifactStore(session, self._artifact_root).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_gap_patch",
                value=patch.model_dump(mode="json"),
            )

    async def _build_gap_guidance(
        self,
        *,
        run: AgentRun,
        snapshot: Any,
    ) -> dict[str, str]:
        """Turn prior deterministic outcomes into bounded repair instructions.

        This is not an LLM self-critique and does not promote rejected claims.
        It gives the next isolated task the observable reason its predecessor
        was insufficient, so a gap patch changes research intent instead of
        blindly repeating the same broad request.
        """

        async with session_scope(self._session_factory) as session:
            diagnostics = await read_research_evidence_diagnostics(
                session=session,
                artifact_root=self._artifact_root,
                run=run,
            )
            collection = await read_research_source_collection_diagnostics(
                session=session,
                artifact_root=self._artifact_root,
                run=run,
            )
        guidance: dict[str, str] = {}
        for entry in snapshot.coverage:
            if entry.coverage_key not in snapshot.sufficiency.gap_coverage_keys:
                continue
            lines = [
                "Prior research feedback:",
                f"- coverage status: {entry.status.value}",
                (
                    "- admitted distinct sources: "
                    f"{entry.observed_source_count}/{entry.min_distinct_sources}"
                ),
                (
                    "- admitted distinct origins: "
                    f"{entry.observed_origin_count}/{entry.min_distinct_origins}"
                ),
            ]
            if entry.missing_source_kinds:
                lines.append(
                    "- still missing source kinds: " + ", ".join(entry.missing_source_kinds)
                )
            diagnostic = diagnostics.get(entry.coverage_key, {})
            rejected_reasons = diagnostic.get("rejected_reason_codes", [])
            if isinstance(rejected_reasons, list) and rejected_reasons:
                lines.append(
                    "- prior proposed claims were not admitted because: "
                    + ", ".join(item for item in rejected_reasons if isinstance(item, str))
                )
            collection_entry = collection.get(entry.coverage_key, {})
            collected_count = collection_entry.get("collected_source_count")
            if isinstance(collected_count, int):
                lines.append(f"- prior trusted source candidates collected: {collected_count}")
            repair_objective = (
                "- repair objective: seek a fresh, exact, unambiguous source span for this "
                "coverage key; do not repeat an unsupported or ambiguous assertion."
            )
            if entry.observed_origin_count < entry.min_distinct_origins:
                repair_objective += (
                    " The next source must come from a different URL origin than the "
                    "already admitted source material."
                )
            lines.append(repair_objective)
            guidance[entry.coverage_key] = "\n".join(lines)
        return guidance

    async def _persist_gap_guidance(
        self,
        *,
        run: AgentRun,
        patch: Any,
        guidance_by_coverage_key: dict[str, str],
    ) -> None:
        """Keep the deterministic feedback available for replay without private prompts."""

        async with session_scope(self._session_factory) as session:
            await ContentAddressedArtifactStore(session, self._artifact_root).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_gap_guidance",
                value={
                    "patch_idempotency_hash": patch.idempotency_hash,
                    "guidance_by_coverage_key": guidance_by_coverage_key,
                },
            )

    async def _persist_verifier_route(
        self,
        *,
        run: AgentRun,
        snapshot: Any,
        tasks: tuple[TaskEnvelope, ...],
    ) -> None:
        """Audit the deterministic risk route before any verifier dispatch."""

        async with session_scope(self._session_factory) as session:
            await ContentAddressedArtifactStore(session, self._artifact_root).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_verifier_route",
                value={
                    "sufficiency": snapshot.sufficiency.model_dump(mode="json"),
                    "tasks": tuple(item.model_dump(mode="json") for item in tasks),
                },
            )

    def _materialize_verifier_tasks(
        self,
        *,
        run: AgentRun,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        plan_revision: int,
        snapshot: Any,
        collection_policy: ResearchCollectionPolicy | None = None,
        source_strategy: str = "primary",
    ) -> tuple[tuple[TaskEnvelope, str], ...]:
        """Turn one risk-gated request into an isolated, read-only verifier task."""

        route = route_verifier_tasks(
            VerifierRoutingInput(
                run_id=run.id,
                basis_hash=run.basis_hash,
                plan_revision=plan_revision,
                coverage_contract=coverage,
                snapshot=snapshot,
                allowed_tool_ids=(),
                budget_ref=f"budget://agent-run/{run.id}",
            )
        )
        if not route.tasks:
            return ()
        coverage_by_key = {item.key: item for item in coverage.keys}
        modules_by_id = {str(item.id): item for item in modules}
        materialized: list[tuple[TaskEnvelope, str]] = []
        for request in route.tasks:
            coverage_key = request.coverage_keys[0]
            coverage_item = coverage_by_key[coverage_key]
            module_id = coverage_item.module_ids[0] if coverage_item.module_ids else None
            module = modules_by_id.get(module_id) if module_id is not None else None
            if module is None:
                raise DomainConflictError("research verifier task must target one active module")
            task = TaskEnvelope(
                id=uuid5(
                    NAMESPACE_URL,
                    f"aidison://research-verifier/{run.id}/{request.idempotency_hash}",
                ),
                run_id=run.id,
                task_key=request.task_key,
                basis_hash=run.basis_hash,
                plan_revision=request.plan_revision,
                capability="research_verifier",
                input_refs=tuple(
                    dict.fromkeys(
                        (
                            run.coverage_contract_ref or "",
                            f"module://{module.id}",
                            *request.input_refs,
                        )
                    )
                ),
                dependency_task_ids=(),
                coverage_keys=request.coverage_keys,
                allowed_tool_ids=request.allowed_tool_ids,
                budget_ref=request.budget_ref,
                idempotency_key=(
                    f"research-verifier:{run.id}:{request.idempotency_hash}:{request.task_key}"
                ),
                collection_policy=collection_policy,
            )
            question = "\n".join(
                (
                    f"Project objective: {coverage.objective}",
                    "Independent verification task: evaluate the stated coverage key from "
                    "freshly collected evidence; do not adopt prior agent conclusions.",
                    f"Source strategy: {source_strategy}",
                    f"Module: {module.name} ({module.key})",
                    f"{coverage_item.key}: {coverage_item.question}",
                    *(
                        ("Prior conflict identifiers: " + ", ".join(request.conflict_ids),)
                        if request.conflict_ids
                        else ()
                    ),
                )
            )
            if len(question) > 2_000:
                raise DomainConflictError("research verifier task exceeds the question limit")
            materialized.append((task, question))
        return tuple(materialized)

    def _materialize_gap_tasks(
        self,
        *,
        run: AgentRun,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        patch: Any,
        collection_policy: ResearchCollectionPolicy | None = None,
        guidance_by_coverage_key: dict[str, str] | None = None,
        source_strategy: str = "primary",
    ) -> tuple[tuple[TaskEnvelope, str], ...]:
        by_coverage_key = {item.key: item for item in coverage.keys}
        modules_by_id = {str(item.id): item for item in modules}
        result: list[tuple[TaskEnvelope, str]] = []
        for patch_task in patch.tasks:
            coverage_key = patch_task.coverage_keys[0]
            coverage_item = by_coverage_key[coverage_key]
            module_id = coverage_item.module_ids[0] if coverage_item.module_ids else None
            module = modules_by_id.get(module_id) if module_id is not None else None
            if module is None:
                raise DomainConflictError("bounded gap task must target one active module")
            task_id = uuid5(
                NAMESPACE_URL,
                f"aidison://research-task/{run.id}/{patch.idempotency_hash}/{patch_task.task_key}",
            )
            input_refs = tuple(
                dict.fromkeys(
                    (
                        run.coverage_contract_ref or "",
                        f"module://{module.id}",
                        *patch_task.input_refs,
                    )
                )
            )
            task = TaskEnvelope(
                id=task_id,
                run_id=run.id,
                task_key=patch_task.task_key,
                basis_hash=run.basis_hash,
                plan_revision=patch.next_plan_revision,
                capability=patch_task.capability,
                input_refs=tuple(item for item in input_refs if item),
                dependency_task_ids=(),
                coverage_keys=patch_task.coverage_keys,
                allowed_tool_ids=patch_task.allowed_tool_ids,
                budget_ref=patch_task.budget_ref,
                idempotency_key=f"research-gap:{run.id}:{patch.idempotency_hash}:{patch_task.task_key}",
                collection_policy=collection_policy,
            )
            question = "\n".join(
                (
                    f"Project objective: {coverage.objective}",
                    f"Research task: {task.task_key}",
                    f"Module: {module.name} ({module.key})",
                    f"{coverage_item.key}: {coverage_item.question}",
                    "This is a bounded evidence-gap follow-up. Seek a fresh, exact source "
                    "for this coverage key only; do not expand the project scope.",
                    f"Source strategy: {source_strategy}",
                    "Required source kinds: " + ", ".join(patch_task.required_source_kinds),
                    *(
                        (guidance_by_coverage_key.get(coverage_key, ""),)
                        if guidance_by_coverage_key is not None
                        and guidance_by_coverage_key.get(coverage_key)
                        else ()
                    ),
                )
            )
            if len(question) > 2_000:
                raise DomainConflictError("bounded gap task exceeds the question limit")
            result.append((task, question))
        return tuple(result)

    async def _task_is_admitted(self, *, run_id: UUID, task_id: UUID) -> bool:
        async with session_scope(self._session_factory) as session:
            return task_id in set(await AgentResultStore(session).admitted_task_ids(run_id=run_id))

    async def _aggregate_gap_patch_manifest(
        self,
        *,
        run: AgentRun,
        coverage: CoverageContract,
        modules: tuple[Any, ...],
        tasks: tuple[TaskEnvelope, ...],
    ) -> ProposalManifest:
        """Freeze every admitted task result so a patch cannot disappear before review."""

        module_by_id = {str(item.id): item for item in modules}
        async with session_scope(self._session_factory) as session:
            results = await AgentResultStore(session).admitted_results(run_id=run.id)
            result_by_task_id: dict[UUID, Any] = {}
            for result in results:
                if result.task_id in {item.id for item in tasks}:
                    if result.task_id in result_by_task_id:
                        raise DomainConflictError(
                            "research task has multiple admitted result variants"
                        )
                    result_by_task_id[result.task_id] = result
            if set(result_by_task_id) != {item.id for item in tasks}:
                raise DomainConflictError("adaptive proposal lacks an admitted task result")
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            rows: list[dict[str, object]] = []
            for task in sorted(
                tasks,
                key=lambda item: (item.plan_revision, item.task_key, str(item.id)),
            ):
                result = result_by_task_id[task.id]
                task_manifest = await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=result.artifact_ref,
                    expected_kind="research_proposal_manifest",
                )
                if not isinstance(task_manifest, dict):
                    raise DomainConflictError("research task manifest must be an object")
                payload = SingleTaskResearchPayload.model_validate(task_manifest.get("payload"))
                module_refs = tuple(
                    item.removeprefix("module://")
                    for item in task.input_refs
                    if item.startswith("module://")
                )
                if module_refs:
                    module = module_by_id.get(module_refs[0])
                elif len(modules) == 1:
                    module = modules[0]
                else:
                    module = None
                if module is None:
                    raise DomainConflictError("adaptive proposal task has no active module scope")
                rows.append(
                    {
                        "task_id": str(task.id),
                        "task_key": task.task_key,
                        "capability": task.capability,
                        "plan_revision": task.plan_revision,
                        "module_id": str(module.id),
                        "payload": payload.model_dump(mode="json"),
                        "evidence_refs": tuple(sorted(set(result.evidence_refs))),
                        "task_manifest_ref": result.artifact_ref,
                        "task_manifest_hash": result.manifest_hash,
                    }
                )
            artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_proposal_manifest",
                value={
                    "schema_version": "research-proposal-manifest-v3",
                    "question": coverage.objective,
                    "task_results": rows,
                },
            )
        return ProposalManifest(
            run_id=run.id,
            basis_hash=run.basis_hash,
            artifact_ref=artifact.ref,
            manifest_hash=artifact.content_hash,
        )


class ResearchLangGraphWorker:
    """Consume only the exact modern ResearchGraph binding."""

    def __init__(self, *, orchestration_worker: LangGraphOrchestrationWorker) -> None:
        self._orchestration_worker = orchestration_worker

    async def run_once(self, *, run_id: UUID | None = None) -> ResearchRunExecution | None:
        claimed = await self._orchestration_worker.claim_once(run_id=run_id)
        if claimed is None:
            return None
        if claimed.run.kind is not AgentRunKind.RESEARCH:
            raise RuntimeError("Research worker claimed a non-Research AgentRun")
        if not isinstance(claimed.compiled_graph, ResearchRunExecutor):
            raise RuntimeError("Research binding has no ResearchRunExecutor registration")
        async with self._orchestration_worker.lease_heartbeat(claimed.claim):
            return await claimed.compiled_graph.execute_claim(run=claimed.run, claim=claimed.claim)


__all__ = [
    "JsonModeSingleTaskResearcher",
    "ResearchLangGraphWorker",
    "ResearchRunExecution",
    "ResearchRunExecutor",
]
