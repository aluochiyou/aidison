from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import count
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID, uuid5

from langchain.agents.structured_output import StructuredOutputError
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
from openai import OpenAIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.agents.contracts import (
    CandidateDraft,
    CompatibilityDraft,
    DecisionOptionDraft,
    EvidenceDraft,
    ImpactProposalPayload,
    ResearchProposalPayload,
    SolutionProposalPayload,
)
from aidison.agents.impact import build_impact_agent
from aidison.agents.research import build_research_agent
from aidison.agents.solution import build_solution_agent
from aidison.application.execution import DurableJoinWaiter
from aidison.application.service import ProjectApplication
from aidison.artifacts.contracts import ArtifactMetadata, ArtifactStatus
from aidison.domain.models import (
    BomItem,
    Candidate,
    CompatibilityFinding,
    DecisionOption,
    DecisionRequest,
    DecisionStatus,
    EvidenceBinding,
    ImpactAnalysis,
    Module,
    ModulePatch,
    ModuleSelection,
    Observation,
    Project,
    RequirementRevision,
    SolutionPlanStep,
    SolutionProposal,
    SolutionVersion,
)
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.budget import (
    BudgetClaimStaleError,
    BudgetConflictError,
    BudgetLedger,
    BudgetLimitExceededError,
)
from aidison.infrastructure.planning import (
    PlanNotFoundError,
    PostgresPlanStore,
    build_revision_from_patch,
)
from aidison.infrastructure.profiles import ProfileRepository
from aidison.infrastructure.runtime import PostgresRuntime, RuntimeConflictError
from aidison.infrastructure.signals import PostgresSignalBus
from aidison.infrastructure.store import PostgresDomainStore
from aidison.providers.gateway import ProviderUnavailableError, build_chat_model
from aidison.runtime.contracts import (
    MAX_DELEGATION_WAVE_SIZE,
    BudgetOperationKind,
    BudgetOwnerKind,
    CommittedJoin,
    DelegationResult,
    DelegationSpec,
    DelegationStatus,
    DelegationWave,
    JobClaim,
    JobStatus,
    JoinMode,
    JoinPolicy,
    JoinReceipt,
    JoinSnapshot,
    ResultDisposition,
    RuntimeWorkItem,
)
from aidison.runtime.planning import (
    GapStatus,
    PlanPatchKind,
    PlanPatchProposal,
    ResearchGap,
    ResearchMode,
    TaskNode,
    build_research_shadow_plan,
)
from aidison.tools.github import (
    ControlledGitHubRead,
    GitHubBudgetBroker,
    GitHubMcpBackend,
    GitHubUnavailableError,
)
from aidison.tools.web_search import (
    ControlledWebSearch,
    PageFetcher,
    SafeHttpFetcher,
    SearchBackend,
    SearchBudgetBroker,
    SearchContext,
    SearchUnavailableError,
    TavilyMcpSearchBackend,
)


class AgentRunner(Protocol):
    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


AgentFactory = Callable[
    [BaseChatModel, ControlledWebSearch, SearchContext, ControlledGitHubRead | None, str],
    AgentRunner,
]
SolutionAgentFactory = Callable[[BaseChatModel], AgentRunner]
ImpactAgentFactory = Callable[[BaseChatModel], AgentRunner]


def _default_agent_factory(
    model: BaseChatModel,
    search: ControlledWebSearch,
    context: SearchContext,
    github: ControlledGitHubRead | None,
    system_prompt: str,
) -> AgentRunner:
    return cast(
        AgentRunner,
        build_research_agent(
            model=model,
            search=search,
            context=context,
            github=github,
            system_prompt=system_prompt,
        ),
    )


def _default_solution_agent_factory(model: BaseChatModel) -> AgentRunner:
    return cast(AgentRunner, build_solution_agent(model=model))


def _default_impact_agent_factory(model: BaseChatModel) -> AgentRunner:
    return cast(AgentRunner, build_impact_agent(model=model))


class _SessionArtifactSink:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        root: Path,
    ) -> None:
        self._factory = factory
        self._root = root

    async def put_bytes(
        self,
        *,
        project_id: UUID,
        attempt_id: UUID,
        basis_hash: str,
        kind: str,
        content: bytes,
        media_type: str,
        source_url: str | None = None,
    ) -> ArtifactMetadata:
        async with self._factory() as session:
            return await ContentAddressedArtifactStore(session, self._root).put_bytes(
                project_id=project_id,
                attempt_id=attempt_id,
                basis_hash=basis_hash,
                kind=kind,
                content=content,
                media_type=media_type,
                source_url=source_url,
            )


class _SessionSearchBudgetBroker(SearchBudgetBroker):
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._ordinal = count(1)

    async def reserve(
        self,
        *,
        query: str,
        max_results: int,
        context: SearchContext,
    ) -> UUID:
        ordinal = next(self._ordinal)
        request_hash = sha256(
            json.dumps(
                {"query": query, "max_results": max_results},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self._factory() as session:
            operation = await BudgetLedger(session).reserve_operation(
                allocation_id=context.budget_allocation_id,
                claim=context.claim,
                kind=BudgetOperationKind.TOOL,
                logical_step="research.web_search",
                physical_attempt_no=ordinal,
                idempotency_key=(f"{context.claim.attempt_id}:web_search:{ordinal}:{request_hash}"),
                request_hash=request_hash,
                provider="tavily",
                model_or_tool="web_search",
                reserved_tool_calls=1,
            )
            await session.commit()
            return operation.operation_id

    async def mark_dispatched(self, operation_id: UUID) -> None:
        async with self._factory() as session:
            await BudgetLedger(session).mark_dispatched(operation_id)
            await session.commit()

    async def settle(self, operation_id: UUID) -> None:
        async with self._factory() as session:
            await BudgetLedger(session).settle(
                operation_id,
                consumed_tokens=0,
                consumed_tool_calls=1,
            )
            await session.commit()


class _SessionGitHubBudgetBroker(GitHubBudgetBroker):
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._ordinal = count(1)

    async def reserve(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: SearchContext,
    ) -> UUID:
        ordinal = next(self._ordinal)
        request_hash = sha256(
            json.dumps(
                {"tool_name": tool_name, "arguments": arguments},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self._factory() as session:
            operation = await BudgetLedger(session).reserve_operation(
                allocation_id=context.budget_allocation_id,
                claim=context.claim,
                kind=BudgetOperationKind.TOOL,
                logical_step="research.github_read",
                physical_attempt_no=ordinal,
                idempotency_key=(
                    f"{context.claim.attempt_id}:github:{ordinal}:{request_hash}"
                ),
                request_hash=request_hash,
                provider="github",
                model_or_tool=tool_name,
                reserved_tool_calls=1,
            )
            await session.commit()
            return operation.operation_id

    async def mark_dispatched(self, operation_id: UUID) -> None:
        async with self._factory() as session:
            await BudgetLedger(session).mark_dispatched(operation_id)
            await session.commit()

    async def settle(self, operation_id: UUID) -> None:
        async with self._factory() as session:
            await BudgetLedger(session).settle(
                operation_id,
                consumed_tokens=0,
                consumed_tool_calls=1,
            )
            await session.commit()

    async def release_undispatched(self, operation_id: UUID) -> None:
        async with self._factory() as session:
            await BudgetLedger(session).release_undispatched(operation_id)
            await session.commit()

    async def mark_ambiguous(self, operation_id: UUID) -> None:
        async with self._factory() as session:
            await BudgetLedger(session).mark_ambiguous(
                operation_id,
                normalized_error="github_call_failed_after_dispatch",
            )
            await session.commit()


class _SessionModelBudgetHandler(AsyncCallbackHandler):
    """Persist every physical LangChain chat-model call before provider dispatch."""

    raise_error = True
    run_inline = True

    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        allocation_id: UUID,
        claim: JobClaim,
        provider: str,
        model_name: str,
        logical_step: str = "research.model",
    ) -> None:
        self._factory = factory
        self._allocation_id = allocation_id
        self._claim = claim
        self._provider = provider
        self._model_name = model_name
        self._logical_step = logical_step
        self._ordinal = count(1)
        self._operations: dict[UUID, tuple[UUID, int]] = {}
        self.input_tokens = 0
        self.output_tokens = 0

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del kwargs
        ordinal = next(self._ordinal)
        request_hash = sha256(
            json.dumps(
                {
                    "serialized": serialized,
                    "messages": [
                        [message.model_dump(mode="json") for message in batch] for batch in messages
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        async with self._factory() as session:
            ledger = BudgetLedger(session)
            allocation = await ledger.get_allocation_by_id(self._allocation_id)
            remaining_tokens = (
                allocation.token_grant - allocation.token_consumed - allocation.token_reserved
            )
            operation = await ledger.reserve_operation(
                allocation_id=self._allocation_id,
                claim=self._claim,
                kind=BudgetOperationKind.MODEL,
                logical_step=self._logical_step,
                physical_attempt_no=ordinal,
                idempotency_key=f"{self._claim.attempt_id}:model:{run_id}",
                request_hash=request_hash,
                provider=self._provider,
                model_or_tool=self._model_name,
                reserved_tokens=remaining_tokens,
            )
            await ledger.mark_dispatched(operation.operation_id)
            await session.commit()
        self._operations[run_id] = (operation.operation_id, operation.reserved_tokens)

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del kwargs
        operation = self._operations.pop(run_id, None)
        if operation is None:
            return
        operation_id, reserved_tokens = operation
        usage = self._usage(response)
        async with self._factory() as session:
            ledger = BudgetLedger(session)
            if usage is None or sum(usage) > reserved_tokens:
                await ledger.mark_ambiguous(
                    operation_id,
                    normalized_error="missing_or_unbounded_provider_usage",
                )
            else:
                input_tokens, output_tokens = usage
                await ledger.settle(
                    operation_id,
                    consumed_tokens=input_tokens + output_tokens,
                    consumed_tool_calls=0,
                )
                self.input_tokens += input_tokens
                self.output_tokens += output_tokens
            await session.commit()

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del error, kwargs
        operation = self._operations.pop(run_id, None)
        if operation is None:
            return
        async with self._factory() as session:
            await BudgetLedger(session).mark_ambiguous(
                operation[0],
                normalized_error="provider_call_failed_after_dispatch",
            )
            await session.commit()

    @staticmethod
    def _usage(response: LLMResult) -> tuple[int, int] | None:
        input_tokens = 0
        output_tokens = 0
        found = False
        for batch in response.generations:
            if not batch:
                continue
            message = getattr(batch[0], "message", None)
            metadata = getattr(message, "usage_metadata", None)
            if metadata:
                input_tokens += int(metadata.get("input_tokens", 0) or 0)
                output_tokens += int(metadata.get("output_tokens", 0) or 0)
                found = True
        if not found and response.llm_output:
            token_usage = response.llm_output.get("token_usage") or response.llm_output.get("usage")
            if isinstance(token_usage, dict):
                input_tokens = int(
                    token_usage.get("prompt_tokens", token_usage.get("input_tokens", 0)) or 0
                )
                output_tokens = int(
                    token_usage.get(
                        "completion_tokens",
                        token_usage.get("output_tokens", 0),
                    )
                    or 0
                )
                found = True
        return (input_tokens, output_tokens) if found else None


def merge_research_proposals(
    proposals: Sequence[ResearchProposalPayload],
) -> ResearchProposalPayload:
    if not proposals:
        raise ValueError("at least one research proposal is required")
    evidence: list[EvidenceDraft] = []
    candidates: list[CandidateDraft] = []
    findings: list[CompatibilityDraft] = []
    options: list[DecisionOptionDraft] = []
    questions: list[str] = []
    for proposal_index, proposal in enumerate(proposals):
        evidence_offset = len(evidence)
        candidate_offset = len(candidates)
        evidence.extend(proposal.evidence)
        candidates.extend(
            item.model_copy(
                update={
                    "evidence_indexes": tuple(
                        index + evidence_offset for index in item.evidence_indexes
                    )
                }
            )
            for item in proposal.candidates
        )
        findings.extend(
            item.model_copy(
                update={
                    "evidence_indexes": tuple(
                        index + evidence_offset for index in item.evidence_indexes
                    )
                }
            )
            for item in proposal.findings
        )
        if proposal.decision_question not in questions:
            questions.append(proposal.decision_question)
        options.extend(
            option.model_copy(
                update={
                    "option_id": f"s{proposal_index + 1}-{option.option_id}",
                    "candidate_indexes": tuple(
                        index + candidate_offset for index in option.candidate_indexes
                    ),
                    "evidence_indexes": tuple(
                        index + evidence_offset for index in option.evidence_indexes
                    ),
                }
            )
            for option in proposal.decision_options
        )
    return ResearchProposalPayload(
        evidence=tuple(evidence),
        candidates=tuple(candidates),
        findings=tuple(findings),
        decision_question=(
            questions[0] if len(questions) == 1 else "请选择满足当前模块约束和证据的方案组合。"
        ),
        decision_options=tuple(options[:8]),
    )


def map_proposal_to_domain(
    *,
    project_id: UUID,
    modules: Sequence[Module],
    proposal: ResearchProposalPayload,
    idempotency_namespace: UUID,
    observed_at: datetime,
) -> tuple[
    tuple[EvidenceBinding, ...],
    tuple[Candidate, ...],
    tuple[CompatibilityFinding, ...],
    tuple[DecisionOption, ...],
]:
    modules_by_key = {item.key: item for item in modules}
    referenced_keys = {
        *(item.module_key for item in proposal.evidence),
        *(item.module_key for item in proposal.candidates),
        *(key for item in proposal.findings for key in item.module_keys),
    }
    unknown_keys = referenced_keys - set(modules_by_key)
    if unknown_keys:
        raise ValueError(f"proposal references unknown module keys: {sorted(unknown_keys)}")

    evidence = tuple(
        EvidenceBinding(
            id=uuid5(idempotency_namespace, f"evidence:{index}"),
            project_id=project_id,
            module_id=modules_by_key[item.module_key].id,
            claim=item.claim,
            source_url=item.source_url,
            snapshot_hash=item.snapshot_hash,
            span_text=item.span_text,
            status=item.status,
            applicability=item.applicability,
            observed_at=observed_at,
        )
        for index, item in enumerate(proposal.evidence)
    )
    candidates = tuple(
        Candidate(
            id=uuid5(idempotency_namespace, f"candidate:{index}"),
            project_id=project_id,
            module_id=modules_by_key[item.module_key].id,
            name=item.name,
            description=item.description,
            attributes=item.attributes,
            evidence_binding_ids=tuple(evidence[index].id for index in item.evidence_indexes),
            risks=item.risks,
        )
        for index, item in enumerate(proposal.candidates)
    )
    findings = tuple(
        CompatibilityFinding(
            id=uuid5(idempotency_namespace, f"finding:{index}"),
            project_id=project_id,
            module_ids=tuple(modules_by_key[key].id for key in item.module_keys),
            rule_id=item.rule_id,
            status=item.status,
            summary=item.summary,
            evidence_binding_ids=tuple(evidence[index].id for index in item.evidence_indexes),
            required_test=item.required_test,
        )
        for index, item in enumerate(proposal.findings)
    )
    decision_options = tuple(
        DecisionOption(
            option_id=item.option_id,
            label=item.label,
            summary=item.summary,
            candidate_ids=tuple(candidates[index].id for index in item.candidate_indexes),
            evidence_binding_ids=tuple(evidence[index].id for index in item.evidence_indexes),
            risks=item.risks,
        )
        for item in proposal.decision_options
    )
    return evidence, candidates, findings, decision_options


def map_solution_proposal_to_domain(
    *,
    modules: Sequence[Module],
    proposal: SolutionProposalPayload,
) -> tuple[
    tuple[ModuleSelection, ...],
    tuple[BomItem, ...],
    tuple[SolutionPlanStep, ...],
    tuple[SolutionPlanStep, ...],
]:
    modules_by_key = {item.key: item for item in modules}
    referenced_keys = {
        *(item.module_key for item in proposal.module_selections),
        *(item.module_key for item in proposal.bom),
        *(key for item in proposal.implementation_steps for key in item.module_keys),
        *(key for item in proposal.verification_steps for key in item.module_keys),
    }
    unknown_keys = referenced_keys - set(modules_by_key)
    if unknown_keys:
        raise ValueError(
            f"solution proposal references unknown module keys: {sorted(unknown_keys)}"
        )

    selections = tuple(
        ModuleSelection(
            module_id=modules_by_key[item.module_key].id,
            candidate_id=item.candidate_id,
            candidate_name=item.candidate_name,
            rationale=item.rationale,
            evidence_binding_ids=item.evidence_binding_ids,
            risks=item.risks,
        )
        for item in proposal.module_selections
    )
    bom = tuple(
        BomItem(
            line_id=item.line_id,
            module_id=modules_by_key[item.module_key].id,
            candidate_id=item.candidate_id,
            name=item.name,
            quantity=item.quantity,
            unit=item.unit,
            evidence_binding_ids=item.evidence_binding_ids,
        )
        for item in proposal.bom
    )

    def map_steps(items: Sequence[Any]) -> tuple[SolutionPlanStep, ...]:
        return tuple(
            SolutionPlanStep(
                step_id=item.step_id,
                title=item.title,
                instruction=item.instruction,
                module_ids=tuple(modules_by_key[key].id for key in item.module_keys),
                acceptance=item.acceptance,
            )
            for item in items
        )

    return (
        selections,
        bom,
        map_steps(proposal.implementation_steps),
        map_steps(proposal.verification_steps),
    )


def map_impact_proposal_to_domain(
    *,
    modules: Sequence[Module],
    proposal: ImpactProposalPayload,
) -> tuple[
    tuple[ModulePatch, ...],
    tuple[BomItem, ...],
    tuple[SolutionPlanStep, ...],
    tuple[SolutionPlanStep, ...],
]:
    modules_by_key = {item.key: item for item in modules}
    referenced_keys = {
        *(item.module_key for item in proposal.module_patches),
        *(item.module_key for item in proposal.replacement_bom_items),
        *(key for item in proposal.replacement_implementation_steps for key in item.module_keys),
        *(key for item in proposal.replacement_verification_steps for key in item.module_keys),
    }
    unknown_keys = referenced_keys - set(modules_by_key)
    if unknown_keys:
        raise ValueError(f"impact proposal references unknown module keys: {sorted(unknown_keys)}")

    patches = tuple(
        ModulePatch(
            module_id=modules_by_key[item.module_key].id,
            base_snapshot_hash=item.base_snapshot_hash,
            replacement=ModuleSelection(
                module_id=modules_by_key[item.module_key].id,
                candidate_id=item.candidate_id,
                candidate_name=item.candidate_name,
                rationale=item.rationale,
                evidence_binding_ids=item.evidence_binding_ids,
                risks=item.risks,
            ),
        )
        for item in proposal.module_patches
    )
    bom = tuple(
        BomItem(
            line_id=item.line_id,
            module_id=modules_by_key[item.module_key].id,
            candidate_id=item.candidate_id,
            name=item.name,
            quantity=item.quantity,
            unit=item.unit,
            evidence_binding_ids=item.evidence_binding_ids,
        )
        for item in proposal.replacement_bom_items
    )

    def map_steps(items: Sequence[Any]) -> tuple[SolutionPlanStep, ...]:
        return tuple(
            SolutionPlanStep(
                step_id=item.step_id,
                title=item.title,
                instruction=item.instruction,
                module_ids=tuple(modules_by_key[key].id for key in item.module_keys),
                acceptance=item.acceptance,
            )
            for item in items
        )

    return (
        patches,
        bom,
        map_steps(proposal.replacement_implementation_steps),
        map_steps(proposal.replacement_verification_steps),
    )


class ResearchWorker:
    """Stateless executor over the single PostgreSQL durable runtime."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        signal_bus: PostgresSignalBus,
        artifact_root: Path,
        model_factory: Callable[[], BaseChatModel] = build_chat_model,
        search_backend_factory: Callable[[], SearchBackend] = TavilyMcpSearchBackend,
        github_backend_factory: Callable[[], GitHubMcpBackend] = GitHubMcpBackend,
        page_fetcher: PageFetcher | None = None,
        agent_factory: AgentFactory = _default_agent_factory,
        solution_agent_factory: SolutionAgentFactory = _default_solution_agent_factory,
        impact_agent_factory: ImpactAgentFactory = _default_impact_agent_factory,
        lease_seconds: int = 60,
        poll_seconds: float = 0.5,
        durable_recheck_seconds: float = 30,
    ) -> None:
        self._factory = session_factory
        self._artifact_root = artifact_root
        self._model_factory = model_factory
        self._search_backend_factory = search_backend_factory
        self._github_backend_factory = github_backend_factory
        self._page_fetcher = page_fetcher or SafeHttpFetcher()
        self._agent_factory = agent_factory
        self._solution_agent_factory = solution_agent_factory
        self._impact_agent_factory = impact_agent_factory
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._join_waiter = DurableJoinWaiter(
            session_factory=session_factory,
            signal_bus=signal_bus,
            durable_recheck_seconds=durable_recheck_seconds,
            signal_failure_recheck_seconds=poll_seconds,
        )

    async def run_once(self, *, worker_id: str) -> bool:
        work = await self._claim(worker_id)
        if work is None:
            return False
        await self._execute_with_heartbeat(work)
        return True

    async def run_forever(self, *, worker_id: str, concurrency: int = 3) -> None:
        if concurrency < 3:
            raise ValueError("research worker needs one controller slot and two child slots")
        tasks: set[asyncio.Task[None]] = set()
        while True:
            while len(tasks) < concurrency:
                work = await self._claim(worker_id)
                if work is None:
                    break
                tasks.add(asyncio.create_task(self._execute_with_heartbeat(work)))
            if not tasks:
                await asyncio.sleep(self._poll_seconds)
                continue
            done, tasks = await asyncio.wait(
                tasks,
                timeout=self._poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                await task

    async def _claim(self, worker_id: str) -> RuntimeWorkItem | None:
        async with self._factory() as session:
            return await PostgresRuntime(session).claim_next_work(
                worker_id=worker_id,
                lease_seconds=self._lease_seconds,
            )

    async def _wait_for_wave(
        self,
        *,
        work: RuntimeWorkItem,
        join_group_id: UUID,
        impossible_message: str,
    ) -> JoinSnapshot:
        snapshot = await self._join_waiter.wait(
            project_id=work.project_id,
            join_group_id=join_group_id,
            parent_claim=work.claim,
        )
        if snapshot.impossible:
            raise RuntimeConflictError(impossible_message)
        return snapshot

    async def _execute_with_heartbeat(self, work: RuntimeWorkItem) -> None:
        stop = asyncio.Event()
        owner = asyncio.current_task()
        heartbeat = asyncio.create_task(self._heartbeat(work, stop, owner))
        try:
            if work.delegation is None and work.kind == "research_wave":
                await self._run_parent(work)
            elif work.delegation is not None and work.kind == "delegated_research":
                await self._run_child(work)
            elif work.delegation is None and work.kind == "solution_wave":
                await self._run_solution_parent(work)
            elif work.delegation is not None and work.kind == "delegated_solution":
                await self._run_solution_child(work)
            elif work.delegation is None and work.kind == "impact_wave":
                await self._run_impact_parent(work)
            elif work.delegation is not None and work.kind == "delegated_impact":
                await self._run_impact_child(work)
            else:
                raise RuntimeConflictError(f"unsupported work kind: {work.kind}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if work.delegation is None:
                await self._fail_parent(work, self._error_code(exc))
            else:
                await self._register_child_failure(work, self._error_code(exc))
        finally:
            stop.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(
        self,
        work: RuntimeWorkItem,
        stop: asyncio.Event,
        owner: asyncio.Task[Any] | None,
    ) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                async with self._factory() as session:
                    await PostgresRuntime(session).renew_claim(
                        claim=work.claim,
                        lease_seconds=self._lease_seconds,
                    )
            except RuntimeConflictError:
                if owner is not None:
                    owner.cancel()
                return

    async def _load_project_context(
        self,
        work: RuntimeWorkItem,
    ) -> tuple[Project, RequirementRevision, tuple[Module, ...]]:
        async with self._factory() as session:
            store = PostgresDomainStore(session)
            project = await store.get_project(work.project_id)
            if (
                project is None
                or project.revision != work.claim.basis_project_revision
                or project.active_requirement_revision_id is None
            ):
                raise RuntimeConflictError("project basis is stale or has no requirements")
            requirement = await store.get_requirement_revision(
                project.active_requirement_revision_id
            )
            if requirement is None:
                raise RuntimeConflictError("active requirements are missing")
            modules = tuple(
                await store.list_modules(
                    project.id,
                    requirement_revision_id=requirement.id,
                )
            )
            return project, requirement, modules

    async def _run_child(self, work: RuntimeWorkItem) -> None:
        delegation = work.delegation
        if delegation is None:
            raise RuntimeConflictError("child work is missing its delegation")
        project, requirement, modules = await self._load_project_context(work)
        module_ids = {
            UUID(ref.removeprefix("module://"))
            for ref in delegation.input_refs
            if ref.startswith("module://")
        }
        assigned = tuple(item for item in modules if item.id in module_ids)
        if not assigned or {item.id for item in assigned} != module_ids:
            raise RuntimeConflictError("delegation module inputs are invalid")

        async with self._factory() as session:
            ledger = BudgetLedger(session)
            account_id = await ledger.get_account_id(delegation.parent_job_id)
            allocation = await ledger.get_allocation(
                account_id=account_id,
                owner_kind=BudgetOwnerKind.CHILD,
                owner_ref=work.claim.job_id,
            )
            profile = await ProfileRepository(session).get_revision(
                work.claim.profile_id,
                work.claim.profile_revision,
            )
        context = SearchContext(
            project_id=project.id,
            claim=work.claim,
            budget_allocation_id=allocation.allocation_id,
            basis_hash=work.claim.basis_hash,
            allowed_effects=delegation.allowed_effects,
            deadline=delegation.deadline,
        )
        artifact_sink = _SessionArtifactSink(self._factory, self._artifact_root)
        search = ControlledWebSearch(
            self._search_backend_factory(),
            self._page_fetcher,
            artifact_sink,
            _SessionSearchBudgetBroker(self._factory),
        )
        model = self._model_factory()
        model_metadata = cast(Any, model)
        model_budget = _SessionModelBudgetHandler(
            factory=self._factory,
            allocation_id=allocation.allocation_id,
            claim=work.claim,
            provider=type(model).__module__.split(".", 1)[0],
            model_name=str(
                getattr(model_metadata, "model_name", None)
                or getattr(model_metadata, "model", None)
                or type(model).__name__
            ),
        )
        github_enabled = "github_read" in profile.allowed_tool_classes
        prompt = self._research_prompt(
            project,
            requirement,
            assigned,
            tool_call_cap=profile.tool_call_cap,
            github_enabled=github_enabled,
        )
        async with AsyncExitStack() as stack:
            github: ControlledGitHubRead | None = None
            if github_enabled:
                github_session = await stack.enter_async_context(
                    self._github_backend_factory().session()
                )
                github = ControlledGitHubRead(
                    github_session,
                    artifact_sink,
                    _SessionGitHubBudgetBroker(self._factory),
                )
            agent = self._agent_factory(
                model,
                search,
                context,
                github,
                profile.prompt_template,
            )
            state = await agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"callbacks": [model_budget]},
            )
        structured = state.get("structured_response")
        if structured is None:
            raise ValueError("agent returned no structured_response")
        proposal = ResearchProposalPayload.model_validate(structured)
        assigned_keys = {item.key for item in assigned}
        output_keys = {
            *(item.module_key for item in proposal.evidence),
            *(item.module_key for item in proposal.candidates),
            *(key for item in proposal.findings for key in item.module_keys),
        }
        if not output_keys <= assigned_keys:
            raise ValueError("agent proposal crossed its assigned module boundary")
        await self._validate_evidence_snapshots(work, proposal)
        artifact = await self._put_json(
            work=work,
            kind="research_proposal",
            value=proposal.model_dump(mode="json"),
        )
        async with self._factory() as session:
            await BudgetLedger(session).close_allocation(allocation.allocation_id)
            await session.commit()
        async with self._factory() as session:
            record_gaps = bool(proposal.bounded_gaps) and not delegation.shard_key.startswith(
                "gap-"
            )
            registered = await PostgresRuntime(session).register_result(
                result=DelegationResult(
                    delegation_id=delegation.delegation_id,
                    child_job_id=work.claim.job_id,
                    attempt_id=work.claim.attempt_id,
                    child_claim_generation=work.claim.claim_generation,
                    status=DelegationStatus.SUCCEEDED,
                    basis_hash=work.claim.basis_hash,
                    proposal_ref=artifact.ref,
                    artifact_refs=(artifact.ref,),
                    input_tokens=model_budget.input_tokens,
                    output_tokens=model_budget.output_tokens,
                ),
                lease_token=work.claim.lease_token,
                commit=not record_gaps,
            )
            if registered.disposition is not ResultDisposition.ELIGIBLE:
                if record_gaps:
                    await session.commit()
                return
            if record_gaps:
                await self._record_gaps_from_proposal(
                    store=PostgresPlanStore(session),
                    delegation=delegation,
                    result_id=registered.result_id,
                    result_hash=registered.result_hash,
                    bounded_gaps=proposal.bounded_gaps,
                )
                await session.commit()

    async def _run_parent(self, work: RuntimeWorkItem) -> None:
        committed = await self._find_committed_join(work)
        if committed is not None:
            await self._resume_committed_join(work, committed)
            return

        project, _, modules = await self._load_project_context(work)
        if not modules:
            raise RuntimeConflictError("research requires at least one module")
        async with self._factory() as session:
            profiles = ProfileRepository(session)
            worker_binding = await profiles.get_binding(
                work.claim.job_id,
                "research-worker",
            )
            worker_profile = await profiles.get_revision(
                worker_binding.profile_id,
                worker_binding.profile_revision,
            )
        plan = build_research_shadow_plan(
            root_job_id=str(work.claim.job_id),
            basis_hash=work.claim.basis_hash,
            modules=modules,
            profile_id=worker_binding.profile_id,
            profile_revision=worker_binding.profile_revision,
            planner_profile_id=work.claim.profile_id,
            planner_profile_revision=work.claim.profile_revision,
            budget_ref=f"budget://job/{work.claim.job_id}",
            max_shards=min(
                worker_profile.concurrency_cap,
                MAX_DELEGATION_WAVE_SIZE,
            ),
        )
        # Keep replay deterministic from the frozen claim while allowing an N-way wave to
        # execute in bounded batches when the process has fewer child slots than shards.
        deadline = work.claim.lease_expires_at + timedelta(
            seconds=worker_profile.timeout_seconds * len(plan.nodes)
        )
        specs = tuple(
            DelegationSpec(
                delegation_id=uuid5(
                    work.claim.attempt_id,
                    f"research.parallel:{node.logical_key}",
                ),
                parent_job_id=work.claim.job_id,
                parent_attempt_id=work.claim.attempt_id,
                parent_claim_generation=work.claim.claim_generation,
                graph_step_id="research.parallel",
                profile_id=worker_binding.profile_id,
                profile_revision=worker_binding.profile_revision,
                basis_hash=work.claim.basis_hash,
                shard_key=node.logical_key.removeprefix("research."),
                idempotency_key=f"{work.claim.attempt_id}:research.parallel:{node.logical_key}",
                input_refs=node.input_refs,
                token_budget=worker_profile.token_cap,
                tool_call_budget=worker_profile.tool_call_cap,
                deadline=deadline,
            )
            for node in plan.nodes
        )
        policy = JoinPolicy(
            mode=JoinMode.ALL_REQUIRED,
            expected_delegation_ids=tuple(item.delegation_id for item in specs),
            min_successes=len(specs),
            deadline=deadline,
        )
        async with self._factory() as session:
            plan_store = PostgresPlanStore(session)
            await plan_store.create_initial(claim=work.claim, plan=plan)
            wave = await PostgresRuntime(session).create_delegation_wave(
                specs=specs,
                policy=policy,
            )
            for spec, child_job_id in zip(specs, wave.child_job_ids, strict=True):
                await plan_store.bind_task_job(
                    root_job_id=work.claim.job_id,
                    logical_key=f"research.{spec.shard_key}",
                    dispatched_job_id=child_job_id,
                )

        # ---- A: wait for primary wave ready, read proposals ----
        snapshot = await self._wait_for_wave(
            work=work,
            join_group_id=wave.join_group_id,
            impossible_message="research join is impossible",
        )

        proposals: list[ResearchProposalPayload] = []
        for ref in snapshot.accepted_proposal_refs:
            proposals.append(await self._read_proposal(work, ref))

        # Commit the primary wave before replanning. Its immutable receipt is the
        # durable recovery anchor if the parent is reclaimed during a gap wave.
        primary = merge_research_proposals(proposals)
        primary_artifact = await self._put_json(
            work=work,
            kind="merged_research_proposal",
            value=primary.model_dump(mode="json"),
        )
        async with self._factory() as session:
            receipt = await PostgresRuntime(session).commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref=primary_artifact.ref,
            )
        if receipt is None:
            raise RuntimeConflictError("research join was not committed")
        await self._finish_research_parent(
            work=work,
            project=project,
            modules=modules,
            primary=primary,
            receipt=receipt,
        )

    async def _find_committed_join(self, work: RuntimeWorkItem) -> CommittedJoin | None:
        async with self._factory() as session:
            return await PostgresRuntime(session).find_committed_join(
                parent_claim=work.claim,
                graph_step_id="research.parallel",
            )

    async def _resume_committed_join(
        self,
        work: RuntimeWorkItem,
        committed: CommittedJoin,
    ) -> None:
        proposal = await self._read_proposal(
            work,
            committed.receipt.merged_proposal_ref,
        )
        module_ids = {
            UUID(ref.removeprefix("module://"))
            for ref in committed.input_refs
            if ref.startswith("module://")
        }
        if not module_ids:
            raise RuntimeConflictError("committed join has no module inputs")

        async with self._factory() as session:
            store = PostgresDomainStore(session)
            project = await store.get_project(work.project_id)
            if project is None:
                raise RuntimeConflictError("committed join project is missing")
            project_modules = tuple(await store.list_modules(project.id))
        modules = tuple(item for item in project_modules if item.id in module_ids)
        if {item.id for item in modules} != module_ids:
            raise RuntimeConflictError("committed join module inputs are missing")

        await self._finish_research_parent(
            work=work,
            project=project,
            modules=modules,
            primary=proposal,
            receipt=committed.receipt,
        )

    async def _finish_research_parent(
        self,
        *,
        work: RuntimeWorkItem,
        project: Project,
        modules: tuple[Module, ...],
        primary: ResearchProposalPayload,
        receipt: JoinReceipt,
    ) -> None:
        gap_proposal = await self._ensure_gap_join(work=work)
        merged = merge_research_proposals(
            (primary,) if gap_proposal is None else (primary, gap_proposal)
        )
        evidence, candidates, findings, decision_options = map_proposal_to_domain(
            project_id=project.id,
            modules=modules,
            proposal=merged,
            idempotency_namespace=receipt.join_group_id,
            observed_at=receipt.committed_at,
        )
        async with self._factory() as session:
            decision = await ProjectApplication(
                PostgresDomainStore(session)
            ).submit_research_proposal(
                project_id=project.id,
                expected_project_revision=work.claim.basis_project_revision,
                evidence=evidence,
                candidates=candidates,
                findings=findings,
                decision_question=merged.decision_question,
                decision_options=decision_options,
                idempotency_key=f"research-join:{receipt.join_group_id}",
            )
        async with self._factory() as session:
            await PostgresRuntime(session).complete_claim(
                claim=work.claim,
                status=JobStatus.SUCCEEDED,
                result_ref=str(decision.id),
            )

    async def _run_solution_child(self, work: RuntimeWorkItem) -> None:
        delegation = work.delegation
        if delegation is None:
            raise RuntimeConflictError("solution child is missing its delegation")
        project, requirement, modules = await self._load_project_context(work)
        decision_id = self._decision_id(work)
        async with self._factory() as session:
            store = PostgresDomainStore(session)
            decision = await store.get_decision_request(decision_id)
            candidates = tuple(await store.list_candidates(project.id))
            evidence = tuple(await store.list_evidence_bindings(project.id))
            findings = tuple(await store.list_compatibility_findings(project.id))
        if (
            decision is None
            or decision.project_id != project.id
            or decision.status is not DecisionStatus.APPROVED
        ):
            raise RuntimeConflictError("solution child requires the approved frozen decision")

        async with self._factory() as session:
            ledger = BudgetLedger(session)
            account_id = await ledger.get_account_id(delegation.parent_job_id)
            allocation = await ledger.get_allocation(
                account_id=account_id,
                owner_kind=BudgetOwnerKind.CHILD,
                owner_ref=work.claim.job_id,
            )
        model = self._model_factory()
        model_metadata = cast(Any, model)
        model_budget = _SessionModelBudgetHandler(
            factory=self._factory,
            allocation_id=allocation.allocation_id,
            claim=work.claim,
            provider=type(model).__module__.split(".", 1)[0],
            model_name=str(
                getattr(model_metadata, "model_name", None)
                or getattr(model_metadata, "model", None)
                or type(model).__name__
            ),
            logical_step="solution.model",
        )
        agent = self._solution_agent_factory(model)
        state = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": self._solution_prompt(
                            project=project,
                            requirement=requirement,
                            modules=modules,
                            decision=decision,
                            candidates=candidates,
                            evidence=evidence,
                            findings=findings,
                        ),
                    }
                ]
            },
            config={"callbacks": [model_budget]},
        )
        structured = state.get("structured_response")
        if structured is None:
            raise ValueError("solution Agent returned no structured_response")
        proposal = SolutionProposalPayload.model_validate(structured)
        map_solution_proposal_to_domain(modules=modules, proposal=proposal)
        artifact = await self._put_json(
            work=work,
            kind="solution_proposal",
            value=proposal.model_dump(mode="json"),
        )
        async with self._factory() as session:
            await BudgetLedger(session).close_allocation(allocation.allocation_id)
            await session.commit()
        async with self._factory() as session:
            await PostgresRuntime(session).register_result(
                result=DelegationResult(
                    delegation_id=delegation.delegation_id,
                    child_job_id=work.claim.job_id,
                    attempt_id=work.claim.attempt_id,
                    child_claim_generation=work.claim.claim_generation,
                    status=DelegationStatus.SUCCEEDED,
                    basis_hash=work.claim.basis_hash,
                    proposal_ref=artifact.ref,
                    artifact_refs=(artifact.ref,),
                    input_tokens=model_budget.input_tokens,
                    output_tokens=model_budget.output_tokens,
                ),
                lease_token=work.claim.lease_token,
            )

    async def _run_solution_parent(self, work: RuntimeWorkItem) -> None:
        decision_id = self._decision_id(work)
        async with self._factory() as session:
            committed = await PostgresRuntime(session).find_committed_join(
                parent_claim=work.claim,
                graph_step_id="solution.propose",
            )
        if committed is not None:
            proposal = await self._read_solution_proposal(
                work,
                committed.receipt.merged_proposal_ref,
            )
            result = await self._submit_solution_proposal(
                work=work,
                proposal=proposal,
                artifact_ref=committed.receipt.merged_proposal_ref,
                join_group_id=committed.receipt.join_group_id,
            )
            async with self._factory() as session:
                await PostgresRuntime(session).complete_claim(
                    claim=work.claim,
                    status=JobStatus.SUCCEEDED,
                    result_ref=str(result.id),
                )
            return

        project, _, modules = await self._load_project_context(work)
        async with self._factory() as session:
            store = PostgresDomainStore(session)
            decision = await store.get_decision_request(decision_id)
            binding = await ProfileRepository(session).get_binding(
                work.claim.job_id,
                "solution-worker",
            )
        if (
            decision is None
            or decision.project_id != project.id
            or decision.status is not DecisionStatus.APPROVED
        ):
            raise RuntimeConflictError("solution parent requires an approved decision")
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        spec = DelegationSpec(
            parent_job_id=work.claim.job_id,
            parent_attempt_id=work.claim.attempt_id,
            parent_claim_generation=work.claim.claim_generation,
            graph_step_id="solution.propose",
            task_kind="solution",
            profile_id=binding.profile_id,
            profile_revision=binding.profile_revision,
            basis_hash=work.claim.basis_hash,
            shard_key="complete-solution",
            idempotency_key=f"{work.claim.attempt_id}:solution.propose:complete-solution",
            input_refs=(
                f"decision://{decision.id}",
                *(f"module://{item.id}" for item in modules),
            ),
            allowed_effects=("read",),
            token_budget=8_000,
            tool_call_budget=0,
            deadline=deadline,
        )
        policy = JoinPolicy(
            mode=JoinMode.ALL_REQUIRED,
            expected_delegation_ids=(spec.delegation_id,),
            min_successes=1,
            deadline=deadline,
        )
        async with self._factory() as session:
            wave = await PostgresRuntime(session).create_delegation_wave(
                specs=(spec,),
                policy=policy,
            )
        snapshot = await self._wait_for_wave(
            work=work,
            join_group_id=wave.join_group_id,
            impossible_message="solution proposal join is impossible",
        )
        if len(snapshot.accepted_proposal_refs) != 1:
            raise RuntimeConflictError("solution proposal join must accept exactly one result")
        artifact_ref = snapshot.accepted_proposal_refs[0]
        proposal = await self._read_solution_proposal(work, artifact_ref)
        async with self._factory() as session:
            receipt = await PostgresRuntime(session).commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref=artifact_ref,
            )
        if receipt is None:
            raise RuntimeConflictError("solution proposal join was not committed")
        result = await self._submit_solution_proposal(
            work=work,
            proposal=proposal,
            artifact_ref=receipt.merged_proposal_ref,
            join_group_id=receipt.join_group_id,
        )
        async with self._factory() as session:
            await PostgresRuntime(session).complete_claim(
                claim=work.claim,
                status=JobStatus.SUCCEEDED,
                result_ref=str(result.id),
            )

    async def _submit_solution_proposal(
        self,
        *,
        work: RuntimeWorkItem,
        proposal: SolutionProposalPayload,
        artifact_ref: str,
        join_group_id: UUID,
    ) -> SolutionProposal:
        async with self._factory() as session:
            store = PostgresDomainStore(session)
            project = await store.get_project(work.project_id)
            if project is None or project.active_requirement_revision_id is None:
                raise RuntimeConflictError("solution proposal project basis is missing")
            modules = tuple(
                await store.list_modules(
                    project.id,
                    requirement_revision_id=project.active_requirement_revision_id,
                )
            )
        selections, bom, implementation, verification = map_solution_proposal_to_domain(
            modules=modules,
            proposal=proposal,
        )
        async with self._factory() as session:
            binding = await ProfileRepository(session).get_binding(
                work.claim.job_id,
                "solution-worker",
            )
            return await ProjectApplication(PostgresDomainStore(session)).submit_solution_proposal(
                project_id=project.id,
                expected_project_revision=work.claim.basis_project_revision,
                decision_id=self._decision_id(work),
                module_selections=selections,
                evidence_binding_ids=proposal.evidence_binding_ids,
                compatibility_finding_ids=proposal.compatibility_finding_ids,
                bom=bom,
                implementation_steps=implementation,
                verification_steps=verification,
                risks=proposal.risks,
                unknowns=proposal.unknowns,
                consequences=proposal.consequences,
                artifact_ref=artifact_ref,
                profile_id=binding.profile_id,
                profile_revision=binding.profile_revision,
                idempotency_key=f"solution-join:{join_group_id}",
            )

    async def _read_solution_proposal(
        self,
        work: RuntimeWorkItem,
        ref: str,
    ) -> SolutionProposalPayload:
        async with self._factory() as session:
            value = await ContentAddressedArtifactStore(
                session,
                self._artifact_root,
            ).read_json_ref(
                project_id=work.project_id,
                basis_hash=work.claim.basis_hash,
                ref=ref,
            )
        return SolutionProposalPayload.model_validate(value)

    @staticmethod
    def _decision_id(work: RuntimeWorkItem) -> UUID:
        raw = work.request_payload.get("decision_id")
        if raw is None and work.delegation is not None:
            raw = next(
                (
                    ref.removeprefix("decision://")
                    for ref in work.delegation.input_refs
                    if ref.startswith("decision://")
                ),
                None,
            )
        if not isinstance(raw, str):
            raise RuntimeConflictError("solution work is missing decision_id")
        try:
            return UUID(raw)
        except ValueError as exc:
            raise RuntimeConflictError("solution work has an invalid decision_id") from exc

    @staticmethod
    def _solution_prompt(
        *,
        project: Project,
        requirement: RequirementRevision,
        modules: Sequence[Module],
        decision: DecisionRequest,
        candidates: Sequence[Candidate],
        evidence: Sequence[EvidenceBinding],
        findings: Sequence[CompatibilityFinding],
    ) -> str:
        payload = {
            "project": {"goal": project.goal},
            "requirement": requirement.model_dump(mode="json"),
            "modules": [item.model_dump(mode="json") for item in modules],
            "approved_decision": decision.model_dump(mode="json"),
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "compatibility_findings": [item.model_dump(mode="json") for item in findings],
        }
        return (
            "Create one complete, generic DIY solution proposal from only these canonical facts. "
            "Use exact IDs and module keys; do not invent refs.\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    async def _run_impact_child(self, work: RuntimeWorkItem) -> None:
        delegation = work.delegation
        if delegation is None:
            raise RuntimeConflictError("impact child is missing its delegation")
        project, requirement, modules = await self._load_project_context(work)
        observation_id = self._observation_id(work)
        async with self._factory() as session:
            store = PostgresDomainStore(session)
            observation = await store.get_observation(observation_id)
            base = (
                await store.get_solution_version(observation.solution_version_id)
                if observation is not None
                else None
            )
            candidates = tuple(await store.list_candidates(project.id))
            evidence = tuple(await store.list_evidence_bindings(project.id))
        if (
            observation is None
            or observation.project_id != project.id
            or base is None
            or project.active_solution_version_id != base.id
        ):
            raise RuntimeConflictError("impact child requires the active observation basis")

        async with self._factory() as session:
            ledger = BudgetLedger(session)
            account_id = await ledger.get_account_id(delegation.parent_job_id)
            allocation = await ledger.get_allocation(
                account_id=account_id,
                owner_kind=BudgetOwnerKind.CHILD,
                owner_ref=work.claim.job_id,
            )
        model = self._model_factory()
        model_metadata = cast(Any, model)
        model_budget = _SessionModelBudgetHandler(
            factory=self._factory,
            allocation_id=allocation.allocation_id,
            claim=work.claim,
            provider=type(model).__module__.split(".", 1)[0],
            model_name=str(
                getattr(model_metadata, "model_name", None)
                or getattr(model_metadata, "model", None)
                or type(model).__name__
            ),
            logical_step="impact.model",
        )
        agent = self._impact_agent_factory(model)
        state = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": self._impact_prompt(
                            project=project,
                            requirement=requirement,
                            modules=modules,
                            observation=observation,
                            base=base,
                            candidates=candidates,
                            evidence=evidence,
                        ),
                    }
                ]
            },
            config={"callbacks": [model_budget]},
        )
        structured = state.get("structured_response")
        if structured is None:
            raise ValueError("impact Agent returned no structured_response")
        proposal = ImpactProposalPayload.model_validate(structured)
        map_impact_proposal_to_domain(modules=modules, proposal=proposal)
        artifact = await self._put_json(
            work=work,
            kind="impact_proposal",
            value=proposal.model_dump(mode="json"),
        )
        async with self._factory() as session:
            await BudgetLedger(session).close_allocation(allocation.allocation_id)
            await session.commit()
        async with self._factory() as session:
            await PostgresRuntime(session).register_result(
                result=DelegationResult(
                    delegation_id=delegation.delegation_id,
                    child_job_id=work.claim.job_id,
                    attempt_id=work.claim.attempt_id,
                    child_claim_generation=work.claim.claim_generation,
                    status=DelegationStatus.SUCCEEDED,
                    basis_hash=work.claim.basis_hash,
                    proposal_ref=artifact.ref,
                    artifact_refs=(artifact.ref,),
                    input_tokens=model_budget.input_tokens,
                    output_tokens=model_budget.output_tokens,
                ),
                lease_token=work.claim.lease_token,
            )

    async def _run_impact_parent(self, work: RuntimeWorkItem) -> None:
        observation_id = self._observation_id(work)
        async with self._factory() as session:
            committed = await PostgresRuntime(session).find_committed_join(
                parent_claim=work.claim,
                graph_step_id="impact.propose",
            )
        if committed is not None:
            proposal = await self._read_impact_proposal(
                work,
                committed.receipt.merged_proposal_ref,
            )
            result = await self._submit_impact_analysis(
                work=work,
                proposal=proposal,
                artifact_ref=committed.receipt.merged_proposal_ref,
                join_group_id=committed.receipt.join_group_id,
            )
            async with self._factory() as session:
                await PostgresRuntime(session).complete_claim(
                    claim=work.claim,
                    status=JobStatus.SUCCEEDED,
                    result_ref=str(result.id),
                )
            return

        project, _, modules = await self._load_project_context(work)
        async with self._factory() as session:
            store = PostgresDomainStore(session)
            observation = await store.get_observation(observation_id)
            binding = await ProfileRepository(session).get_binding(
                work.claim.job_id,
                "impact-worker",
            )
        if (
            observation is None
            or observation.project_id != project.id
            or project.active_solution_version_id != observation.solution_version_id
        ):
            raise RuntimeConflictError("impact parent requires the active observation basis")
        deadline = datetime.now(UTC) + timedelta(minutes=5)
        spec = DelegationSpec(
            parent_job_id=work.claim.job_id,
            parent_attempt_id=work.claim.attempt_id,
            parent_claim_generation=work.claim.claim_generation,
            graph_step_id="impact.propose",
            task_kind="impact",
            profile_id=binding.profile_id,
            profile_revision=binding.profile_revision,
            basis_hash=work.claim.basis_hash,
            shard_key="complete-impact",
            idempotency_key=f"{work.claim.attempt_id}:impact.propose:complete-impact",
            input_refs=(
                f"observation://{observation.id}",
                *(f"module://{item.id}" for item in modules),
            ),
            allowed_effects=("read",),
            token_budget=8_000,
            tool_call_budget=0,
            deadline=deadline,
        )
        policy = JoinPolicy(
            mode=JoinMode.ALL_REQUIRED,
            expected_delegation_ids=(spec.delegation_id,),
            min_successes=1,
            deadline=deadline,
        )
        async with self._factory() as session:
            wave = await PostgresRuntime(session).create_delegation_wave(
                specs=(spec,),
                policy=policy,
            )
        snapshot = await self._wait_for_wave(
            work=work,
            join_group_id=wave.join_group_id,
            impossible_message="impact proposal join is impossible",
        )
        if len(snapshot.accepted_proposal_refs) != 1:
            raise RuntimeConflictError("impact proposal join must accept exactly one result")
        artifact_ref = snapshot.accepted_proposal_refs[0]
        proposal = await self._read_impact_proposal(work, artifact_ref)
        async with self._factory() as session:
            receipt = await PostgresRuntime(session).commit_join(
                join_group_id=wave.join_group_id,
                merged_proposal_ref=artifact_ref,
            )
        if receipt is None:
            raise RuntimeConflictError("impact proposal join was not committed")
        result = await self._submit_impact_analysis(
            work=work,
            proposal=proposal,
            artifact_ref=receipt.merged_proposal_ref,
            join_group_id=receipt.join_group_id,
        )
        async with self._factory() as session:
            await PostgresRuntime(session).complete_claim(
                claim=work.claim,
                status=JobStatus.SUCCEEDED,
                result_ref=str(result.id),
            )

    async def _submit_impact_analysis(
        self,
        *,
        work: RuntimeWorkItem,
        proposal: ImpactProposalPayload,
        artifact_ref: str,
        join_group_id: UUID,
    ) -> ImpactAnalysis:
        async with self._factory() as session:
            store = PostgresDomainStore(session)
            project = await store.get_project(work.project_id)
            if project is None or project.active_requirement_revision_id is None:
                raise RuntimeConflictError("impact proposal project basis is missing")
            modules = tuple(
                await store.list_modules(
                    project.id,
                    requirement_revision_id=project.active_requirement_revision_id,
                )
            )
        patches, bom, implementation, verification = map_impact_proposal_to_domain(
            modules=modules,
            proposal=proposal,
        )
        async with self._factory() as session:
            binding = await ProfileRepository(session).get_binding(
                work.claim.job_id,
                "impact-worker",
            )
            return await ProjectApplication(PostgresDomainStore(session)).submit_impact_analysis(
                project_id=project.id,
                expected_project_revision=work.claim.basis_project_revision,
                observation_id=self._observation_id(work),
                module_patches=patches,
                stale_evidence_binding_ids=proposal.stale_evidence_binding_ids,
                replacement_bom_items=bom,
                replacement_implementation_steps=implementation,
                replacement_verification_steps=verification,
                summary=proposal.summary,
                risks=proposal.risks,
                artifact_ref=artifact_ref,
                profile_id=binding.profile_id,
                profile_revision=binding.profile_revision,
                idempotency_key=f"impact-join:{join_group_id}",
            )

    async def _read_impact_proposal(
        self,
        work: RuntimeWorkItem,
        ref: str,
    ) -> ImpactProposalPayload:
        async with self._factory() as session:
            value = await ContentAddressedArtifactStore(
                session,
                self._artifact_root,
            ).read_json_ref(
                project_id=work.project_id,
                basis_hash=work.claim.basis_hash,
                ref=ref,
            )
        return ImpactProposalPayload.model_validate(value)

    @staticmethod
    def _observation_id(work: RuntimeWorkItem) -> UUID:
        raw = work.request_payload.get("observation_id")
        if raw is None and work.delegation is not None:
            raw = next(
                (
                    ref.removeprefix("observation://")
                    for ref in work.delegation.input_refs
                    if ref.startswith("observation://")
                ),
                None,
            )
        if not isinstance(raw, str):
            raise RuntimeConflictError("impact work is missing observation_id")
        try:
            return UUID(raw)
        except ValueError as exc:
            raise RuntimeConflictError("impact work has an invalid observation_id") from exc

    @staticmethod
    def _impact_prompt(
        *,
        project: Project,
        requirement: RequirementRevision,
        modules: Sequence[Module],
        observation: Observation,
        base: SolutionVersion,
        candidates: Sequence[Candidate],
        evidence: Sequence[EvidenceBinding],
    ) -> str:
        direct, transitive, affected, unaffected = ProjectApplication._impact_partition(
            modules,
            observation.affected_module_hints,
        )
        affected_set = set(affected)
        payload = {
            "project": {"goal": project.goal},
            "requirement": requirement.model_dump(mode="json"),
            "modules": [item.model_dump(mode="json") for item in modules],
            "observation": observation.model_dump(mode="json"),
            "base_solution": base.model_dump(mode="json"),
            "direct_affected_module_ids": [str(item) for item in direct],
            "transitive_affected_module_ids": [str(item) for item in transitive],
            "affected_module_ids": [str(item) for item in affected],
            "unaffected_module_ids": [str(item) for item in unaffected],
            "candidates": [
                item.model_dump(mode="json")
                for item in candidates
                if item.module_id in affected_set
            ],
            "evidence": [
                item.model_dump(mode="json") for item in evidence if item.module_id in affected_set
            ],
        }
        return (
            "Propose the smallest revision from only these canonical facts. The server-owned "
            "affected closure is authoritative. Use exact IDs, module keys and base snapshot "
            "hashes; do not invent refs.\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    async def _validate_evidence_snapshots(
        self,
        work: RuntimeWorkItem,
        proposal: ResearchProposalPayload,
    ) -> None:
        async with self._factory() as session:
            store = ContentAddressedArtifactStore(session, self._artifact_root)
            metadata: list[ArtifactMetadata] = []
            for kind in ("web_snapshot", "github_snapshot"):
                metadata.extend(
                    await store.list_metadata(
                        project_id=work.project_id,
                        attempt_id=work.claim.attempt_id,
                        kind=kind,
                    )
                )
        valid = {
            (item.source_url, item.content_hash)
            for item in metadata
            if item.status is ArtifactStatus.PRESENT
        }
        if any((item.source_url, item.snapshot_hash) not in valid for item in proposal.evidence):
            raise ValueError("proposal evidence is not backed by a staged evidence snapshot")

    async def _put_json(
        self,
        *,
        work: RuntimeWorkItem,
        kind: str,
        value: object,
    ) -> ArtifactMetadata:
        async with self._factory() as session:
            return await ContentAddressedArtifactStore(
                session,
                self._artifact_root,
            ).put_json(
                project_id=work.project_id,
                attempt_id=work.claim.attempt_id,
                basis_hash=work.claim.basis_hash,
                kind=kind,
                value=value,
            )

    async def _read_proposal(
        self,
        work: RuntimeWorkItem,
        ref: str,
    ) -> ResearchProposalPayload:
        async with self._factory() as session:
            store = ContentAddressedArtifactStore(session, self._artifact_root)
            value = await store.read_json_ref(
                project_id=work.project_id,
                basis_hash=work.claim.basis_hash,
                ref=ref,
            )
        return ResearchProposalPayload.model_validate(value)

    async def _register_child_failure(self, work: RuntimeWorkItem, code: str) -> None:
        delegation = work.delegation
        if delegation is None:
            return
        try:
            async with self._factory() as session:
                ledger = BudgetLedger(session)
                account_id = await ledger.get_account_id(delegation.parent_job_id)
                await ledger.reconcile_reclaimed_owner(
                    account_id=account_id,
                    owner_kind=BudgetOwnerKind.CHILD,
                    owner_ref=work.claim.job_id,
                )
                await PostgresRuntime(session).register_result(
                    result=DelegationResult(
                        delegation_id=delegation.delegation_id,
                        child_job_id=work.claim.job_id,
                        attempt_id=work.claim.attempt_id,
                        child_claim_generation=work.claim.claim_generation,
                        status=DelegationStatus.FAILED,
                        basis_hash=work.claim.basis_hash,
                        normalized_error=code,
                    ),
                    lease_token=work.claim.lease_token,
                )
        except (BudgetConflictError, RuntimeConflictError):
            return

    async def _fail_parent(self, work: RuntimeWorkItem, code: str) -> None:
        try:
            async with self._factory() as session:
                await PostgresRuntime(session).complete_claim(
                    claim=work.claim,
                    status=JobStatus.FAILED,
                    normalized_error=code,
                )
        except RuntimeConflictError:
            return

    @classmethod
    def _error_code(cls, exc: Exception) -> str:
        if isinstance(exc, ExceptionGroup):
            codes = {cls._error_code(item) for item in exc.exceptions}
            if len(codes) == 1:
                return codes.pop()
            return "worker_error"
        if isinstance(exc, (ProviderUnavailableError, OpenAIError)):
            return "provider_unavailable"
        if isinstance(exc, SearchUnavailableError):
            message = str(exc)
            if message == "search returned no fetchable public source":
                return "no_fetchable_source"
            if message.startswith(
                (
                    "Tavily MCP returned",
                    "Tavily MCP result failed",
                    "Tavily MCP search tool",
                    "Tavily MCP tool",
                    "Tavily MCP arguments",
                    "Tavily MCP result limit",
                    "Tavily MCP images",
                    "Tavily MCP raw content",
                )
            ):
                return "search_tool_contract_error"
            return "search_unavailable"
        if isinstance(exc, GitHubUnavailableError):
            return "github_unavailable"
        if isinstance(exc, BudgetConflictError):
            return "budget_conflict"
        if isinstance(exc, BudgetLimitExceededError):
            return "budget_limit_exceeded"
        if isinstance(exc, BudgetClaimStaleError):
            return "budget_claim_stale"
        if isinstance(exc, RuntimeConflictError):
            return "runtime_conflict"
        if isinstance(exc, (StructuredOutputError, ValueError)):
            return "invalid_agent_output"
        return "worker_error"

    async def _record_gaps_from_proposal(
        self,
        *,
        store: PostgresPlanStore,
        delegation: DelegationSpec,
        result_id: UUID,
        result_hash: str,
        bounded_gaps: tuple[str, ...],
    ) -> None:
        """Record bounded gaps from an eligible child result's output.

        Only records gaps for the returned shard task — the child's delegation
        carries the logical_key. The child's registered result was already
        accepted as ELIGIBLE by the runtime, so the source_result_id/hash
        are verified before calling.
        """
        task_logical_key = f"research.{delegation.shard_key}"
        current = await store.get_current(root_job_id=delegation.parent_job_id)
        for description in bounded_gaps[:4]:
            gap = ResearchGap(
                root_job_id=delegation.parent_job_id,
                task_logical_key=task_logical_key,
                plan_revision=current.revision,
                source_result_id=result_id,
                source_result_hash=result_hash,
                category="proposal_gap",
                description=description,
                module_refs=tuple(delegation.input_refs),
                priority=10,
                bound=1,
            )
            await store.record_gap(gap=gap)

    async def _ensure_gap_join(
        self,
        *,
        work: RuntimeWorkItem,
    ) -> ResearchProposalPayload | None:
        """Resume or run the one bounded gap wave for a committed primary join."""
        async with self._factory() as session:
            committed = await PostgresRuntime(session).find_committed_join(
                parent_claim=work.claim,
                graph_step_id="research.gap",
            )
        if committed is not None:
            return await self._read_proposal(work, committed.receipt.merged_proposal_ref)

        async with self._factory() as session:
            store = PostgresPlanStore(session)
            try:
                base = await store.get_current(root_job_id=work.claim.job_id)
            except PlanNotFoundError:
                return None

        # A revision already beyond the initial plan means a prior attempt landed
        # the patch. Reclaim resets its uncommitted child bindings to READY, so the
        # current attempt only needs to replay the existing frontier.
        if base.revision == 1:
            async with self._factory() as session:
                gaps = await PostgresPlanStore(session).list_open_gaps(
                    root_job_id=work.claim.job_id,
                    min_priority=1,
                )
            if not gaps:
                return None

            async with self._factory() as session:
                profiles = ProfileRepository(session)
                binding = await profiles.get_binding(work.claim.job_id, "research-worker")

            seen: set[str] = set()
            new_nodes: list[TaskNode] = []
            for gap in gaps:
                if gap.gap_hash in seen or len(new_nodes) >= 4:
                    continue
                seen.add(gap.gap_hash)
                new_nodes.append(
                    TaskNode(
                        logical_key=f"research.gap-{gap.gap_hash[:8]}",
                        objective=gap.description,
                        mode=ResearchMode.ATOM,
                        role_key="research-worker",
                        profile_id=binding.profile_id,
                        profile_revision=binding.profile_revision,
                        budget_ref=f"budget://job/{gap.root_job_id}",
                        depth=0,
                        input_refs=gap.module_refs,
                        success_criteria=("gap addressed with verifiable evidence",),
                        stop_criteria=("gap is resolved or refuted",),
                    )
                )
            if not new_nodes:
                return None
            next_revision = build_revision_from_patch(
                base=base,
                patch_kind=PlanPatchKind.EXPAND,
                trigger=f"{len(new_nodes)} gap(s) from eligible results",
                new_nodes=tuple(new_nodes),
                reason=f"revision {base.revision + 1}: bounded gap expansion",
            )
            patch = PlanPatchProposal(
                root_job_id=str(work.claim.job_id),
                base_revision=base.revision,
                base_plan_hash=base.plan_hash,
                kind=PlanPatchKind.EXPAND,
                trigger=f"{len(new_nodes)} gap(s) from eligible results",
                payload={"gap_hashes": sorted(seen)},
                new_plan=next_revision,
            )
            async with self._factory() as session:
                store = PostgresPlanStore(session)
                await store.apply_patch(claim=work.claim, patch=patch)
                await store.resolve_gaps(
                    root_job_id=work.claim.job_id,
                    gap_hashes=tuple(sorted(seen)),
                    status=GapStatus.ACCEPTED,
                )
                await session.commit()

        gap_wave, gap_specs = await self._dispatch_frontier(work=work)
        if gap_wave is None or not gap_specs:
            return None

        # Wait for gap wave readiness
        snapshot = await self._wait_for_wave(
            work=work,
            join_group_id=gap_wave.join_group_id,
            impossible_message="gap research join is impossible",
        )

        gap_proposals: list[ResearchProposalPayload] = []
        for ref in snapshot.accepted_proposal_refs:
            gap_proposals.append(await self._read_proposal(work, ref))
        merged = merge_research_proposals(gap_proposals)
        artifact = await self._put_json(
            work=work,
            kind="merged_gap_research_proposal",
            value=merged.model_dump(mode="json"),
        )
        async with self._factory() as session:
            receipt = await PostgresRuntime(session).commit_join(
                join_group_id=gap_wave.join_group_id,
                merged_proposal_ref=artifact.ref,
            )
        if receipt is None:
            raise RuntimeConflictError("gap research join was not committed")
        return merged

    async def _dispatch_frontier(
        self,
        *,
        work: RuntimeWorkItem,
    ) -> tuple[DelegationWave | None, tuple[DelegationSpec, ...]]:
        """Bind ready frontier gap-nodes to child Jobs.

        Returns (DelegationWave | None, DelegationSpec...).
        """
        async with self._factory() as session:
            profiles = ProfileRepository(session)
            worker_binding = await profiles.get_binding(
                work.claim.job_id,
                "research-worker",
            )
            worker_profile = await profiles.get_revision(
                worker_binding.profile_id,
                worker_binding.profile_revision,
            )
            store = PostgresPlanStore(session)
            ready = await store.list_ready_frontier(root_job_id=work.claim.job_id)

        gap_nodes = [n for n in ready if n.logical_key.startswith("research.gap-")]
        if not gap_nodes:
            return None, ()

        deadline = work.claim.lease_expires_at + timedelta(
            seconds=worker_profile.timeout_seconds * len(gap_nodes)
        )
        specs = tuple(
            DelegationSpec(
                delegation_id=uuid5(
                    work.claim.attempt_id,
                    f"research.gap:{node.logical_key}",
                ),
                parent_job_id=work.claim.job_id,
                parent_attempt_id=work.claim.attempt_id,
                parent_claim_generation=work.claim.claim_generation,
                graph_step_id="research.gap",
                profile_id=worker_binding.profile_id,
                profile_revision=worker_binding.profile_revision,
                basis_hash=work.claim.basis_hash,
                shard_key=node.logical_key.removeprefix("research."),
                idempotency_key=f"{work.claim.attempt_id}:research.gap:{node.logical_key}",
                input_refs=node.input_refs,
                token_budget=worker_profile.token_cap,
                tool_call_budget=worker_profile.tool_call_cap,
                deadline=deadline,
            )
            for node in gap_nodes
        )
        policy = JoinPolicy(
            mode=JoinMode.ALL_REQUIRED,
            expected_delegation_ids=tuple(item.delegation_id for item in specs),
            min_successes=len(specs),
            deadline=deadline,
        )
        async with self._factory() as session:
            runtime = PostgresRuntime(session)
            plan_store = PostgresPlanStore(session)
            wave = await runtime.create_delegation_wave(
                specs=specs,
                policy=policy,
            )
            for spec, child_job_id in zip(specs, wave.child_job_ids, strict=True):
                await plan_store.bind_task_job(
                    root_job_id=work.claim.job_id,
                    logical_key=f"research.{spec.shard_key}",
                    dispatched_job_id=child_job_id,
                )
        return wave, specs

    @staticmethod
    def _research_prompt(
        project: Project,
        requirement: RequirementRevision,
        modules: Sequence[Module],
        *,
        tool_call_cap: int,
        github_enabled: bool,
    ) -> str:
        module_text = "\n".join(
            f"- {item.key}: {item.name}; responsibility={item.responsibility}; "
            f"acceptance={list(item.acceptance)}; open_questions={list(item.open_questions)}"
            for item in modules
        )
        tool_instruction = (
            "Use at least one GitHub tool for repository or code evidence and at least one "
            "web_search call for public technical evidence. Every evidence item must cite a "
            "snapshot returned by one of these Aidison tools."
            if github_enabled
            else "Use web_search for every evidence item."
        )
        return (
            f"Project goal: {project.goal}\n"
            f"Approved requirement goal: {requirement.goal}\n"
            f"Hard constraints: {list(requirement.hard_constraints)}\n"
            f"Preferences: {list(requirement.preferences)}\n"
            f"Unknowns: {list(requirement.unknowns)}\n"
            f"Assigned modules (use these exact module_key values only):\n{module_text}\n"
            "Research concrete alternatives and compatibility constraints. "
            f"You may make at most {tool_call_cap} total tool calls; do not retry a failed tool. "
            f"{tool_instruction} Return only the structured response."
        )
