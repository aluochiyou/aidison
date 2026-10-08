from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.language_models import BaseChatModel
from openai import APIConnectionError, APIStatusError, APITimeoutError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.agents.research_strategy import (
    JsonModeResearchStrategyPlanner,
    ResearchStrategyPlanner,
    ResponsesJsonSchemaResearchStrategyPlanner,
)
from aidison.api.errors import (
    ApiErrorCode,
    error_response,
    request_correlation,
    set_request_correlation_id,
)
from aidison.api.schemas import (
    ApprovePatchRequest,
    ApproveRequirementsRequest,
    ConfirmLinesRequest,
    CreateAgentRunControlRequest,
    CreateCheckoutHandoffRequest,
    CreateProjectRequest,
    CreateProjectReshapeRequest,
    CreatePurchaseProposalRequest,
    CreateRequirementsChangeRequest,
    CreateSelectionLockRequest,
    FreezeSolutionRequest,
    ModuleDiscoveryRequest,
    PostConversationMessageRequest,
    PreviewShoppingBudgetRequest,
    ProposeSpendBudgetRequest,
    RecordUserAdjustmentRequest,
    ResearchProposalRequest,
    ResearchStrategyPlanRequest,
    ResolveAgentRunDecisionRequest,
    ResolveConversationClarificationRequest,
    ResolveDecisionRequest,
    ResolveEffectApprovalRequest,
    ResolveExecutionPlanRequest,
    ResolveProjectReshapeRequest,
    ResolveRequirementsChangeRequest,
    ResolveSpendBudgetRequest,
    ReviewFailureRegressionCandidateRequest,
    SaveSolutionSnapshotRequest,
    SearchOffersRequest,
    StartImpactRunRequest,
    StartResearchRunRequest,
    StartSolutionRunRequest,
    SubmitObservationRequest,
    UploadProjectSourceDocumentRequest,
)
from aidison.application.agent_run_application import (
    DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING,
    DEFAULT_RESEARCH_RUNTIME_BINDING,
    DEFAULT_SOLUTION_RUNTIME_BINDING,
    AgentRunApplication,
)
from aidison.application.agent_run_trajectory import (
    AgentRunTrajectory,
    AgentRunTrajectoryService,
)
from aidison.application.failure_regression import (
    FailureRegressionCandidateCapture,
    FailureRegressionCandidateService,
    FailureRegressionReviewDecision,
    FailureRegressionReviewResult,
    GoldenRegressionEvaluation,
)
from aidison.application.impact_proposal_commit import ImpactProposalCommitApplication
from aidison.application.ports import DuplicateCommandError, OptimisticConcurrencyError
from aidison.application.proposal_commit import ProposalCommitApplication
from aidison.application.research_consolidation import (
    read_research_consolidation_snapshot,
    read_research_evidence_diagnostics,
    read_research_source_collection_diagnostics,
)
from aidison.application.research_quality import build_research_quality_payload
from aidison.application.research_strategy_planning import (
    ResearchStrategyPlanningError,
    ResearchStrategyPlanningService,
)
from aidison.application.service import (
    DomainConflictError,
    DomainNotFoundError,
    PreconditionFailedError,
    ProjectApplication,
    canonical_hash,
)
from aidison.application.shopping import (
    CartCreationError,
    OfferSearchError,
    ShoppingApplication,
    ShoppingSettings,
)
from aidison.application.solution_proposal_commit import SolutionProposalCommitApplication
from aidison.application.workspace import build_workspace_projection
from aidison.config import ConversationSettings, DraftSettings
from aidison.domain.models import DecisionRequest as DomainDecisionRequest
from aidison.domain.models import (
    EffectApprovalStatus,
    ExecutionPlanProposal,
    ExecutionPlanStatus,
    ImpactAnalysis,
    ModuleLineageChange,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProposedModule,
    RequirementsChangeProposal,
    RequirementsChangeProposalStatus,
    ShoppingBudgetSelection,
)
from aidison.engineering.coupling import EngineeringCouplingEdge, analyze_couplings
from aidison.evaluation.regression import GoldenRegressionTask
from aidison.infrastructure.agent_decisions import AgentRunDecisionStore
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_run_controls import AgentRunControlRequestStore
from aidison.infrastructure.agent_runs import (
    AgentRunConflictError,
    AgentRunControl,
    AgentRunNotFoundError,
)
from aidison.infrastructure.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ContentAddressedArtifactStore,
)
from aidison.infrastructure.database import create_session_factory
from aidison.infrastructure.orm import (
    AgentRunBudgetAccountRow,
    AgentRunBudgetOperationRow,
    AgentRunDecisionRow,
    AgentRunRow,
    DecisionRequestRow,
    DomainEventRow,
    ImpactAnalysisRow,
)
from aidison.infrastructure.project_documents import (
    ProjectSourceDocumentIntegrityError,
    ProjectSourceDocumentNotFoundError,
    ProjectSourceDocumentStore,
)
from aidison.infrastructure.store import PostgresDomainStore
from aidison.observability import (
    RuntimeTracer,
    RuntimeTracingSettings,
    TelemetryCorrelation,
    build_runtime_tracer,
)
from aidison.providers.shopping import ShoppingConfigError, ShoppingProvider
from aidison.providers.taobao import TaobaoAffiliateAdapter, TaobaoSettings
from aidison.research.consolidation import SufficiencyPolicy
from aidison.research.coverage import CoverageContract
from aidison.research.decision_contracts import AgentRunDecisionStatus
from aidison.research.defaults import (
    RESEARCH_DEFAULT_ALLOWED_TOOL_CLASSES,
    RESEARCH_DEFAULT_MAX_CONCURRENCY,
    RESEARCH_DEFAULT_MAX_DURATION_SECONDS,
    RESEARCH_DEFAULT_MAX_TOKEN_BUDGET,
)
from aidison.research.strategy import ResearchRunContract
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.runtime.contracts import MAX_DELEGATION_WAVE_SIZE, CoordinationMode
from aidison.runtime.control_requests import (
    AgentRunControlRequest,
    ControlRequestKind,
    ControlRequestStatus,
)
from aidison.solution.contracts import SolutionRiskClass

_logger = logging.getLogger(__name__)

SessionDependency = Annotated[AsyncSession, Depends()]
_PROJECT_SNAPSHOT_PROJECTION_VERSION = "project-snapshot.v1"
_PROJECT_EVENT_SCHEMA_VERSION = "project-event.v1"


async def _research_context_summary(
    *,
    artifacts: ContentAddressedArtifactStore,
    run: AgentRun,
) -> dict[str, object]:
    """Project a safe, aggregate view of private context selection.

    A context manifest records the deterministic input-budget decision, but its
    rendered prompt stays private.  This read model deliberately exposes only
    counts, token estimates, and omission codes so users can distinguish
    "source was not collected" from "source was collected but did not fit".
    """

    metadata = await artifacts.list_metadata(
        project_id=run.project_id,
        agent_run_id=run.id,
        kind="research_context_manifest",
    )
    selected_source_count = 0
    omitted_source_count = 0
    input_token_estimate = 0
    unreadable_manifest_count = 0
    omission_reason_counts: dict[str, int] = {}
    for item in metadata:
        try:
            value = await artifacts.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=item.ref,
                expected_kind="research_context_manifest",
            )
        except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
            unreadable_manifest_count += 1
            continue
        if not isinstance(value, dict):
            unreadable_manifest_count += 1
            continue
        estimate = value.get("input_token_estimate")
        if isinstance(estimate, int) and estimate >= 0:
            input_token_estimate += estimate
        selected_source_count += sum(
            1
            for selected in value.get("items", [])
            if isinstance(selected, dict)
            and isinstance(selected.get("source_ref"), str)
            and selected["source_ref"].startswith("source://")
        )
        for omitted in value.get("omitted_items", []):
            if not isinstance(omitted, dict):
                continue
            source_ref = omitted.get("source_ref")
            if not isinstance(source_ref, str) or not source_ref.startswith("source://"):
                continue
            omitted_source_count += 1
            reason = omitted.get("reason_code")
            if isinstance(reason, str) and reason:
                omission_reason_counts[reason] = omission_reason_counts.get(reason, 0) + 1
    return {
        "manifest_count": len(metadata),
        "unreadable_manifest_count": unreadable_manifest_count,
        "selected_source_count": selected_source_count,
        "omitted_source_count": omitted_source_count,
        "input_token_estimate": input_token_estimate,
        "omission_reason_counts": dict(sorted(omission_reason_counts.items())),
    }


async def _research_quality_by_run(
    *,
    session: AsyncSession,
    artifact_root: Path,
    agent_runs: Sequence[AgentRunRow],
) -> dict[UUID, dict[str, Any]]:
    """Build read-only quality summaries for Research runs in one API snapshot.

    A failed or partial run is still useful to inspect.  The summary therefore
    derives only from already admitted results and the frozen Coverage Contract;
    it never kicks off a retry or writes another consolidation Artifact.
    """

    summaries: dict[UUID, dict[str, Any]] = {}
    artifacts = ContentAddressedArtifactStore(
        session,
        artifact_root,
        record_integrity_status=False,
    )
    control = AgentRunControl(session)
    executable_statuses = {
        AgentRunStatus.QUEUED,
        AgentRunStatus.RUNNING,
        AgentRunStatus.WAITING,
    }
    for row in agent_runs:
        if row.kind != AgentRunKind.RESEARCH.value or row.coverage_contract_ref is None:
            continue
        try:
            run = await control.get(row.id)
            if run is None:  # Defensive: the original query supplied this row.
                continue
            if run.coverage_contract_ref is None:  # Defensive: the row was read before a retry.
                continue
            value = await artifacts.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=run.coverage_contract_ref,
                expected_kind="research_coverage_contract",
            )
            coverage = CoverageContract.model_validate(value)
            consolidation = await read_research_consolidation_snapshot(
                session=session,
                artifact_root=artifact_root,
                run=run,
                coverage=coverage,
                policy=SufficiencyPolicy(
                    evidence_expansion_available=run.status in executable_statuses,
                    verification_available=run.status in executable_statuses,
                ),
            )
            diagnostics = await read_research_evidence_diagnostics(
                session=session,
                artifact_root=artifact_root,
                run=run,
            )
            collection_diagnostics = await read_research_source_collection_diagnostics(
                session=session,
                artifact_root=artifact_root,
                run=run,
            )
            context_summary = await _research_context_summary(artifacts=artifacts, run=run)
            summaries[row.id] = build_research_quality_payload(
                coverage=coverage,
                snapshot=consolidation,
                evidence_diagnostics=diagnostics,
                source_collection_diagnostics=collection_diagnostics,
                context_summary=context_summary,
            )
        except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
            # A damaged historical Artifact must be visible, but must not make
            # the whole project workbench unavailable.
            summaries[row.id] = {
                "outcome": "unavailable",
                "reason_codes": ["research_quality_projection_unavailable"],
                "gap_coverage_keys": [],
                "conflict_count": 0,
                "coverage": [],
            }
    return summaries


async def _research_progress_by_run(
    *,
    session: AsyncSession,
    artifacts: ContentAddressedArtifactStore,
    agent_runs: Sequence[AgentRunRow],
) -> dict[UUID, dict[str, int]]:
    """Derive user-facing task progress without creating a second scheduler state.

    The frozen Run Contract owns the initial plan; accepted ResultEnvelope rows
    own completed work.  Gap and verifier tasks are visible when accepted as
    additional work, not guessed from a worker-local task pool.
    """

    progress: dict[UUID, dict[str, int]] = {}
    results = AgentResultStore(session)
    for run in agent_runs:
        if run.kind != AgentRunKind.RESEARCH.value or run.run_contract_ref is None:
            continue
        try:
            value = await artifacts.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=run.run_contract_ref,
                expected_kind="research_run_contract",
            )
            contract = ResearchRunContract.model_validate(value)
            initial_task_count = (
                len(contract.research_strategy.tasks)
                if contract.research_strategy is not None
                else 1
            )
            admitted_results = await results.admitted_results(run_id=run.id)
            result_status_by_task = {
                result.task_id: result.status.value for result in admitted_results
            }
            admitted_task_count = len(result_status_by_task)
            succeeded_task_count = sum(
                status == "succeeded" for status in result_status_by_task.values()
            )
            partial_task_count = sum(
                status == "partial" for status in result_status_by_task.values()
            )
            progress[run.id] = {
                "initial_task_count": initial_task_count,
                "admitted_task_count": admitted_task_count,
                "succeeded_task_count": succeeded_task_count,
                "partial_task_count": partial_task_count,
                "additional_task_count": max(0, admitted_task_count - initial_task_count),
            }
        except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
            continue
    return progress


async def _agent_run_failure_summaries(
    *,
    session: AsyncSession,
    project_id: UUID,
    run_ids: set[UUID],
) -> dict[UUID, str]:
    """Read the latest classified worker failure without replaying a Run.

    Failure events are append-only audit records.  The workspace uses this
    compact projection instead of exposing raw stack traces or provider output.
    """

    rows = list(
        await session.scalars(
            select(DomainEventRow)
            .where(
                DomainEventRow.project_id == project_id,
                DomainEventRow.event_type == "agent_run.failed",
            )
            .order_by(DomainEventRow.project_seq)
        )
    )
    summaries: dict[UUID, str] = {}
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        try:
            run_id = UUID(str(payload.get("agent_run_id", "")))
        except (TypeError, ValueError):
            continue
        summary = payload.get("failure_summary")
        if run_id in run_ids and isinstance(summary, str) and summary.strip():
            summaries[run_id] = summary.strip()[:500]
    return summaries


async def _agent_run_proposal_preview(
    *,
    artifacts: ContentAddressedArtifactStore,
    project_id: UUID,
    basis_hash: str,
    proposal_manifest_ref: str,
) -> dict[str, Any] | None:
    """Extract a compact, reviewable proposal preview from an immutable Artifact."""

    try:
        value = await artifacts.read_json_ref(
            project_id=project_id,
            basis_hash=basis_hash,
            ref=proposal_manifest_ref,
        )
    except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    question = value.get("question")
    task_results = value.get("task_results")
    if not isinstance(question, str) or not isinstance(task_results, list):
        return None
    # A preview is a read model, but it must not make an unsupported model
    # recommendation look actionable while the canonical commit path would
    # reject it. Group all task variants by module so adaptive gap/verifier
    # tasks may contribute evidence without replacing the primary recommendation.
    rows_by_module_id: dict[str, list[dict[str, Any]]] = {}
    for item in task_results:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        module_id = item.get("module_id")
        if not isinstance(payload, dict) or not isinstance(module_id, str):
            continue
        recommended = payload.get("recommended_option")
        summary = payload.get("summary")
        alternatives = payload.get("alternatives")
        evidence_refs = item.get("evidence_refs")
        if not isinstance(recommended, str) or not isinstance(summary, str):
            continue
        rows_by_module_id.setdefault(module_id, []).append(
            {
                "task_key": item.get("task_key"),
                "capability": item.get("capability", "research"),
                "plan_revision": item.get("plan_revision", 1),
                "recommended_option": recommended,
                "summary": summary,
                "alternatives": (
                    [candidate for candidate in alternatives[:2] if isinstance(candidate, str)]
                    if isinstance(alternatives, list)
                    else []
                ),
                "evidence_refs": (
                    tuple(ref for ref in evidence_refs if isinstance(ref, str))
                    if isinstance(evidence_refs, list)
                    else ()
                ),
            }
        )
    module_summaries: list[dict[str, Any]] = []
    unsupported_module_ids: list[str] = []
    for module_id, rows in sorted(rows_by_module_id.items()):
        primary_rows = [item for item in rows if item["capability"] == "research"]
        if not primary_rows:
            unsupported_module_ids.append(module_id)
            continue
        primary = max(
            primary_rows,
            key=lambda item: (
                item["plan_revision"] if isinstance(item["plan_revision"], int) else 0,
                item["task_key"] if isinstance(item["task_key"], str) else "",
            ),
        )
        evidence_refs = {
            ref for item in rows for ref in item["evidence_refs"] if isinstance(ref, str)
        }
        if not evidence_refs:
            unsupported_module_ids.append(module_id)
            continue
        module_summaries.append(
            {
                "module_id": module_id,
                "recommended_option": primary["recommended_option"],
                "summary": primary["summary"],
                "alternatives": primary["alternatives"],
                "evidence_count": len(evidence_refs),
            }
        )
    return {
        "question": question,
        "module_summaries": module_summaries,
        "adoption_allowed": bool(module_summaries) and not unsupported_module_ids,
        "unsupported_module_ids": unsupported_module_ids,
    }


def _error(
    status_code: int,
    code: ApiErrorCode,
    message: str,
    details: object | None = None,
) -> JSONResponse:
    return error_response(status_code, code, message, details=details)


def _consume_detached_planning_task(task: asyncio.Future[Any]) -> None:
    """Observe a timed-out SDK task without delaying the HTTP response.

    Some provider clients postpone cancellation until an in-flight network
    operation resolves. Planning calls have no persistence authority until the
    awaiting API handler receives their value, so a late result must be
    discarded rather than allowed to extend the user-facing deadline.
    """

    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def _await_planning_operation(
    operation: Awaitable[Any], *, timeout_seconds: float
) -> Any:
    """Enforce a wall-clock planning deadline even if an SDK delays cancellation."""

    task = asyncio.ensure_future(operation)
    try:
        done, _ = await asyncio.wait((task,), timeout=timeout_seconds)
    except asyncio.CancelledError:
        # A reverse proxy or browser may disconnect before the API deadline.
        # The planner owns no persistence authority, so its now-unobservable
        # work must not keep consuming a provider connection in the background.
        task.cancel()
        task.add_done_callback(_consume_detached_planning_task)
        raise
    if task in done:
        return task.result()
    task.cancel()
    task.add_done_callback(_consume_detached_planning_task)
    raise TimeoutError("planning operation exceeded its API deadline")


def _http_error_code(status_code: int) -> ApiErrorCode:
    return {
        400: ApiErrorCode.INVALID_COMMAND,
        401: ApiErrorCode.UNAUTHENTICATED,
        403: ApiErrorCode.POLICY_DENIED,
        404: ApiErrorCode.RESOURCE_NOT_FOUND,
        409: ApiErrorCode.DOMAIN_CONFLICT,
        422: ApiErrorCode.INVALID_COMMAND,
        429: ApiErrorCode.INGRESS_RATE_LIMITED,
        502: ApiErrorCode.MODEL_OUTPUT_INVALID,
        503: ApiErrorCode.DEPENDENCY_UNAVAILABLE,
        504: ApiErrorCode.MODEL_REQUEST_TIMED_OUT,
    }.get(status_code, ApiErrorCode.INTERNAL_ERROR)


def _validation_details(exc: RequestValidationError) -> tuple[dict[str, object], ...]:
    """Expose locations and constraints, never a rejected input value or body."""
    return tuple(
        {
            "location": [str(part) for part in item.get("loc", ())],
            "type": str(item.get("type", "validation_error")),
            "message": str(item.get("msg", "request validation failed")),
        }
        for item in exc.errors()
    )


def _parse_revision(value: str) -> int:
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    normalized = normalized.strip('"')
    try:
        revision = int(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="If-Match must contain a revision") from exc
    if revision < 1:
        raise HTTPException(status_code=422, detail="If-Match revision must be positive")
    return revision


def _parse_cursor(value: str | None, project_id: UUID) -> int:
    if value is None or value == "":
        return 0
    sequence_text = value
    if ":" in value:
        project_text, sequence_text = value.rsplit(":", 1)
        if project_text != str(project_id):
            raise HTTPException(status_code=422, detail="event cursor belongs to another project")
    try:
        sequence = int(sequence_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid event cursor") from exc
    if sequence < 0:
        raise HTTPException(status_code=422, detail="event cursor cannot be negative")
    return sequence


def _project_event_payload(
    project_id: UUID,
    row: DomainEventRow,
    *,
    encode_datetime: bool = False,
) -> dict[str, Any]:
    created_at: datetime | str = row.created_at
    occurred_at: datetime | str = row.occurred_at
    if encode_datetime:
        created_at = row.created_at.isoformat()
        occurred_at = row.occurred_at.isoformat()
    return {
        "schema_version": _PROJECT_EVENT_SCHEMA_VERSION,
        "id": f"{project_id}:{row.project_seq}",
        "sequence": row.project_seq,
        "type": row.event_type,
        "payload": row.payload,
        "event_id": str(row.event_id),
        "event_schema_version": row.schema_version,
        "aggregate_type": row.aggregate_type,
        "aggregate_id": str(row.aggregate_id) if row.aggregate_id is not None else None,
        "aggregate_version": row.aggregate_version,
        "occurred_at": occurred_at,
        "payload_hash": row.payload_hash,
        "correlation_id": str(row.correlation_id) if row.correlation_id is not None else None,
        "causation_id": str(row.causation_id) if row.causation_id is not None else None,
        "actor": row.actor,
        "source_component": row.source_component,
        "artifact_refs": row.artifact_refs or [],
        "created_at": created_at,
    }


async def _session_dependency(request: Request) -> AsyncIterator[AsyncSession]:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            yield session
        except BaseException:
            if session.in_transaction():
                await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(_session_dependency)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=300),
]
IfMatch = Annotated[str, Header(alias="If-Match", min_length=1)]


def create_app(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    shopping_provider: ShoppingProvider | None = None,
    artifact_root: Path | None = None,
    effect_approval_ttl_seconds: int | None = None,
    conversation_model_factory: Callable[[], BaseChatModel] | None = None,
    module_discovery_model_factory: Callable[[], BaseChatModel] | None = None,
    research_strategy_model_factory: Callable[[], BaseChatModel] | None = None,
    research_strategy_planner_factory: Callable[[], ResearchStrategyPlanner] | None = None,
    runtime_tracer: RuntimeTracer | None = None,
    enable_legacy_purchase_writes: bool = False,
) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            cast(RuntimeTracer, app.state.runtime_tracer).flush()

    api = FastAPI(title="Aidison API", version="0.1.0", lifespan=_lifespan)
    api.state.session_factory = session_factory or create_session_factory()
    api.state.shopping_provider = shopping_provider
    api.state.runtime_tracer = runtime_tracer or build_runtime_tracer(RuntimeTracingSettings())
    # Keep API and worker on the same durable artifact volume in deployment.
    # The explicit argument remains the test/in-process override.
    api.state.artifact_root = artifact_root or Path(
        os.getenv("AIDISON_ARTIFACT_ROOT", "artifacts/data")
    )
    api.state.effect_approval_ttl_seconds = (
        effect_approval_ttl_seconds
        if effect_approval_ttl_seconds is not None
        else ShoppingSettings().effect_approval_ttl_seconds
    )
    api.state.conversation_settings = ConversationSettings()
    api.state.module_discovery_model_factory = module_discovery_model_factory
    if (
        research_strategy_model_factory is not None
        and research_strategy_planner_factory is not None
    ):
        raise ValueError("provide only one research strategy factory")
    if research_strategy_planner_factory is not None:
        api.state.research_strategy_planner_factory = research_strategy_planner_factory
    elif research_strategy_model_factory is None:

        def _research_strategy_planner_factory() -> ResearchStrategyPlanner:
            from aidison.providers.gateway import (
                ProviderSettings,
                build_deepseek_responses_client,
            )

            settings = ProviderSettings()
            return ResponsesJsonSchemaResearchStrategyPlanner(
                client=build_deepseek_responses_client(settings),
                model=settings.deepseek_model,
                timeout_seconds=settings.request_timeout_seconds,
            )

        api.state.research_strategy_planner_factory = _research_strategy_planner_factory
    else:
        api.state.research_strategy_planner_factory = (
            lambda: JsonModeResearchStrategyPlanner(research_strategy_model_factory())
        )

    @api.middleware("http")
    async def bind_request_correlation(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = f"req-{uuid4()}"
        token = set_request_correlation_id(request_id)
        try:
            tracer = cast(RuntimeTracer, request.app.state.runtime_tracer)
            with tracer.span(
                name="aidison.api.request",
                correlation=TelemetryCorrelation(request_id=request_id),
                attributes={"http.method": request.method},
            ):
                response = await call_next(request)
            response.headers.setdefault("X-Request-ID", request_id)
            return response
        finally:
            request_correlation.reset(token)

    @api.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(
            422,
            ApiErrorCode.INVALID_COMMAND,
            "request validation failed",
            _validation_details(exc),
        )

    @api.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        code = _http_error_code(exc.status_code)
        message = (
            "dependency is temporarily unavailable"
            if code is ApiErrorCode.DEPENDENCY_UNAVAILABLE
            else "model planning timed out; retry or narrow the requested scope"
            if code is ApiErrorCode.MODEL_REQUEST_TIMED_OUT
            else (
                "model output did not match the required planning contract; "
                "retry or adjust the planning brief"
            )
            if code is ApiErrorCode.MODEL_OUTPUT_INVALID
            else "request cannot be completed"
        )
        return _error(exc.status_code, code, message)

    @api.exception_handler(DomainNotFoundError)
    async def not_found(_: Request, exc: DomainNotFoundError) -> JSONResponse:
        return _error(404, ApiErrorCode.RESOURCE_NOT_FOUND, "requested resource was not found")

    @api.exception_handler(ProjectSourceDocumentNotFoundError)
    async def project_source_document_not_found(
        _: Request, exc: ProjectSourceDocumentNotFoundError
    ) -> JSONResponse:
        return _error(404, ApiErrorCode.RESOURCE_NOT_FOUND, "requested resource was not found")

    @api.exception_handler(ProjectSourceDocumentIntegrityError)
    async def project_source_document_integrity(
        _: Request, exc: ProjectSourceDocumentIntegrityError
    ) -> JSONResponse:
        return _error(
            409,
            ApiErrorCode.DOMAIN_CONFLICT,
            "project source document is unavailable; inspect or replace it before research",
        )

    @api.exception_handler(PreconditionFailedError)
    @api.exception_handler(OptimisticConcurrencyError)
    async def precondition_failed(_: Request, exc: Exception) -> JSONResponse:
        return _error(
            412,
            ApiErrorCode.STALE_PROJECT_REVISION,
            "project changed; refresh before applying this command",
        )

    @api.exception_handler(DuplicateCommandError)
    async def duplicate_command(_: Request, exc: DuplicateCommandError) -> JSONResponse:
        return _error(
            409,
            ApiErrorCode.IDEMPOTENCY_CONFLICT,
            "idempotency key conflicts with a different command payload",
        )

    @api.exception_handler(DomainConflictError)
    async def domain_conflict(
        _: Request,
        exc: DomainConflictError,
    ) -> JSONResponse:
        return _error(
            409,
            ApiErrorCode.DOMAIN_CONFLICT,
            "command conflicts with current project state",
        )

    @api.exception_handler(AgentRunNotFoundError)
    async def agent_run_not_found(_: Request, exc: AgentRunNotFoundError) -> JSONResponse:
        return _error(404, ApiErrorCode.RESOURCE_NOT_FOUND, "requested AgentRun was not found")

    @api.exception_handler(AgentRunConflictError)
    async def agent_run_conflict(_: Request, exc: AgentRunConflictError) -> JSONResponse:
        return _error(
            409,
            ApiErrorCode.DOMAIN_CONFLICT,
            "AgentRun command conflicts with current state",
        )

    @api.exception_handler(ShoppingConfigError)
    async def shopping_unavailable(_: Request, exc: ShoppingConfigError) -> JSONResponse:
        return _error(503, ApiErrorCode.DEPENDENCY_UNAVAILABLE, "shopping provider is unavailable")

    @api.exception_handler(OfferSearchError)
    async def offer_search_failed(_: Request, exc: OfferSearchError) -> JSONResponse:
        return _error(
            503,
            ApiErrorCode.DEPENDENCY_UNAVAILABLE,
            "offer search dependency is unavailable",
        )

    @api.exception_handler(CartCreationError)
    async def cart_creation_failed(_: Request, exc: CartCreationError) -> JSONResponse:
        return _error(
            503,
            ApiErrorCode.DEPENDENCY_UNAVAILABLE,
            "shopping dependency is unavailable",
        )

    @api.exception_handler(IntegrityError)
    async def persistence_conflict(_: Request, __: IntegrityError) -> JSONResponse:
        return _error(409, ApiErrorCode.DOMAIN_CONFLICT, "command conflicts with canonical state")

    @api.exception_handler(APIStatusError)
    async def model_provider_status_error(_: Request, exc: APIStatusError) -> JSONResponse:
        """Never surface a provider's billing payload as an internal API error.

        A 402 is a configuration/account action, not a project conflict and not
        a retryable model failure.  No planning command has persistence
        authority before its model call returns, so this response also
        guarantees that the caller can retry after changing provider settings
        without having to repair project state.
        """

        if exc.status_code == 402:
            return _error(
                402,
                ApiErrorCode.MODEL_CREDIT_EXHAUSTED,
                (
                    "model service credit is unavailable; recharge or switch the "
                    "configured model before retrying"
                ),
            )
        return _error(
            503,
            ApiErrorCode.DEPENDENCY_UNAVAILABLE,
            "model provider is temporarily unavailable",
        )

    @api.exception_handler(APITimeoutError)
    async def model_provider_timeout(_: Request, __: APITimeoutError) -> JSONResponse:
        return _error(
            504,
            ApiErrorCode.MODEL_REQUEST_TIMED_OUT,
            "model planning timed out; retry or narrow the requested scope",
        )

    @api.exception_handler(APIConnectionError)
    async def model_provider_connection_error(
        _: Request, __: APIConnectionError
    ) -> JSONResponse:
        return _error(
            503,
            ApiErrorCode.DEPENDENCY_UNAVAILABLE,
            "model provider is temporarily unavailable",
        )

    @api.exception_handler(Exception)
    async def unexpected_error(_: Request, __: Exception) -> JSONResponse:
        return _error(500, ApiErrorCode.INTERNAL_ERROR, "internal server error")

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/api/integration-health")
    async def integration_health(request: Request) -> dict[str, Any]:
        provider = getattr(request.app.state, "shopping_provider", None)
        if provider is None:
            return {
                "status": "ok",
                "shopping": {
                    "provider": "none",
                    "available": False,
                    "search": False,
                    "handoff_kinds": [],
                },
            }
        capabilities = provider.capabilities
        return {
            "status": "ok",
            "shopping": {
                "provider": provider.name,
                "available": provider.available,
                # Search-only providers (e.g. Taobao) declare search with an
                # empty handoff_kinds; cart-capable providers list their kinds.
                "search": capabilities.search,
                "handoff_kinds": sorted(kind.value for kind in capabilities.handoff_kinds),
            },
        }

    # ── Helper ─────────────────────────────────────────────────────

    def _shopping_app(session: AsyncSession) -> ShoppingApplication:
        provider = getattr(api.state, "shopping_provider", None)
        if provider is None:
            raise ShoppingConfigError("no shopping provider configured")
        return ShoppingApplication(
            PostgresDomainStore(session),
            provider,
            effect_approval_ttl_seconds=api.state.effect_approval_ttl_seconds,
        )

    def _shopping_write_disabled() -> JSONResponse:
        """Enforce the product's read-only shopping boundary on the server.

        Historical PurchaseProposal and checkout records remain readable, but
        this product profile supports only offer search, recommendation, budget
        preview, and external product links.  The opt-in is exclusively for
        the legacy integration fixture; the application entry point uses the
        safe default.
        """
        return _error(
            410,
            ApiErrorCode.POLICY_DENIED,
            "purchase, approval, and checkout operations are disabled; "
            "use product links to continue on the provider site",
        )

    async def _set_current_project_etag(
        response: Response,
        store: PostgresDomainStore,
        project_id: UUID,
    ) -> None:
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found after shopping command")
        response.headers["ETag"] = f'"{project.revision}"'

    async def _require_execution_plan(
        *,
        store: PostgresDomainStore,
        project_id: UUID,
        execution_plan_id: UUID,
        authorization_basis_hash: str,
        required_mode: CoordinationMode,
        required_concurrency: int,
        required_token_budget: int,
    ) -> ExecutionPlanProposal:
        plan = await store.get_execution_plan_proposal(execution_plan_id)
        if plan is None or plan.project_id != project_id:
            raise DomainNotFoundError("execution plan not found")
        if not plan.authorizes_execution:
            raise PreconditionFailedError("execution plan is not approved")
        if plan.basis_hash != authorization_basis_hash:
            raise PreconditionFailedError("execution plan basis is stale")
        if required_mode not in plan.allowed_coordination_modes:
            raise DomainConflictError("execution plan does not allow the requested coordination")
        if plan.max_concurrency < required_concurrency:
            raise DomainConflictError("execution plan concurrency cap is insufficient")
        if plan.max_token_budget < required_token_budget:
            raise DomainConflictError("execution plan token budget is insufficient")
        return plan

    async def _active_modules_for_project(
        store: PostgresDomainStore, project: Any
    ) -> tuple[Any, ...]:
        if project.active_blueprint_id is None:
            return ()
        blueprint = await store.get_project_blueprint(project.active_blueprint_id)
        if blueprint is None or blueprint.status.value != "active":
            raise DomainConflictError("project active blueprint is unavailable")
        modules_by_id = {item.id: item for item in await store.list_modules(project.id)}
        try:
            dependencies_by_target: dict[UUID, list[UUID]] = {
                module_id: [] for module_id in blueprint.module_ids
            }
            for source_id, target_id in blueprint.dependency_edges:
                dependencies_by_target[target_id].append(source_id)
            return tuple(
                modules_by_id[item_id].model_copy(
                    update={"dependency_ids": tuple(dependencies_by_target[item_id])}
                )
                for item_id in blueprint.module_ids
            )
        except KeyError as exc:
            raise DomainConflictError(
                "project active blueprint references a missing module"
            ) from exc

    async def _enqueue_research_run(
        *,
        session: AsyncSession,
        project_id: UUID,
        execution_plan_id: UUID,
        expected_project_revision: int,
        commit: bool,
    ) -> dict[str, Any]:
        """Create the one idempotent LangGraph AgentRun authorized by a plan."""
        run = await AgentRunApplication(
            session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).enqueue_research_run(
            project_id=project_id,
            execution_plan_id=execution_plan_id,
            expected_project_revision=expected_project_revision,
            runtime_binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
            commit=commit,
        )
        return {
            "agent_run": run,
            "status": "queued",
            "basis_hash": run.basis_hash,
            "project_revision": expected_project_revision,
        }

    # ── Project routes ──────────────────────────────────────────────

    @api.post("/api/projects", status_code=status.HTTP_201_CREATED)
    async def create_project(
        body: CreateProjectRequest,
        idempotency_key: IdempotencyKey,
        response: Response,
        session: DbSession,
    ) -> Any:
        project = await ProjectApplication(PostgresDomainStore(session)).create_project(
            name=body.name,
            goal=body.goal,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{project.revision}"'
        return project

    @api.get("/api/projects")
    async def list_projects(session: DbSession) -> Any:
        return await PostgresDomainStore(session).list_projects()

    @api.get("/api/projects/{project_id}")
    async def get_project(project_id: UUID, response: Response, session: DbSession) -> Any:
        project = await PostgresDomainStore(session).get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        response.headers["ETag"] = f'"{project.revision}"'
        return project

    @api.post(
        "/api/projects/{project_id}/source-documents",
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_project_source_document(
        project_id: UUID,
        body: UploadProjectSourceDocumentRequest,
        idempotency_key: IdempotencyKey,
        response: Response,
        session: DbSession,
    ) -> Any:
        """Register immutable user material without changing approved project facts.

        This deliberately has no ``If-Match`` requirement: concurrent uploads
        expand research context but do not alter a RequirementRevision, module
        graph, or SolutionVersion. Exact retries remain idempotent and each
        accepted command is retained in the project event stream.
        """

        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        payload_hash = canonical_hash(
            "upload-project-source-document",
            project_id,
            body.name,
            body.content,
            body.media_type,
        )
        command_key = f"project-source-document:{project_id}:{idempotency_key}"
        receipt = await store.claim_command(command_key, payload_hash)
        documents = ProjectSourceDocumentStore(
            session,
            Path(cast(Path, api.state.artifact_root)),
        )
        if receipt is not None:
            document = await documents.get(
                project_id=project_id, document_id=UUID(receipt)
            )
            response.headers["ETag"] = f'"{project.revision}"'
            return document

        document = await documents.put_text(
            project_id=project_id,
            name=body.name,
            content=body.content,
            media_type=body.media_type,
            commit=False,
        )
        await store.save_command_receipt(command_key, payload_hash, str(document.id))
        await store.append_event(
            project_id,
            "project_source_document.submitted",
            {
                "document_id": str(document.id),
                "name": document.name,
                "content_hash": document.content_hash,
                "media_type": document.media_type,
                "size_bytes": document.size_bytes,
            },
        )
        await session.commit()
        response.headers["ETag"] = f'"{project.revision}"'
        return document

    @api.get("/api/projects/{project_id}/source-documents")
    async def list_project_source_documents(
        project_id: UUID,
        session: DbSession,
    ) -> tuple[Any, ...]:
        if await PostgresDomainStore(session).get_project(project_id) is None:
            raise DomainNotFoundError("project not found")
        return await ProjectSourceDocumentStore(
            session,
            Path(cast(Path, api.state.artifact_root)),
        ).list_all(project_id=project_id)

    @api.post(
        "/api/projects/{project_id}/source-documents/{document_id}/quarantine"
    )
    async def quarantine_project_source_document(
        project_id: UUID,
        document_id: UUID,
        idempotency_key: IdempotencyKey,
        response: Response,
        session: DbSession,
    ) -> Any:
        """Exclude one user document from future research without erasing audit data."""

        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        payload_hash = canonical_hash("quarantine-project-source-document", project_id, document_id)
        command_key = f"project-source-document-quarantine:{project_id}:{idempotency_key}"
        receipt = await store.claim_command(command_key, payload_hash)
        documents = ProjectSourceDocumentStore(
            session,
            Path(cast(Path, api.state.artifact_root)),
        )
        if receipt is not None:
            document = await documents.get(
                project_id=project_id, document_id=UUID(receipt)
            )
            response.headers["ETag"] = f'"{project.revision}"'
            return document
        document, changed = await documents.quarantine(
            project_id=project_id,
            document_id=document_id,
            commit=False,
        )
        await store.save_command_receipt(command_key, payload_hash, str(document.id))
        if changed:
            await store.append_event(
                project_id,
                "project_source_document.quarantined",
                {
                    "document_id": str(document.id),
                    "content_hash": document.content_hash,
                },
            )
        await session.commit()
        response.headers["ETag"] = f'"{project.revision}"'
        return document

    @api.post("/api/projects/{project_id}/source-documents/{document_id}/restore")
    async def restore_project_source_document(
        project_id: UUID,
        document_id: UUID,
        idempotency_key: IdempotencyKey,
        response: Response,
        session: DbSession,
    ) -> Any:
        """Re-enable verified material after a deliberate quarantine review."""

        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        payload_hash = canonical_hash("restore-project-source-document", project_id, document_id)
        command_key = f"project-source-document-restore:{project_id}:{idempotency_key}"
        receipt = await store.claim_command(command_key, payload_hash)
        documents = ProjectSourceDocumentStore(
            session,
            Path(cast(Path, api.state.artifact_root)),
        )
        if receipt is not None:
            document = await documents.get(
                project_id=project_id, document_id=UUID(receipt)
            )
            response.headers["ETag"] = f'"{project.revision}"'
            return document
        document, changed = await documents.restore(
            project_id=project_id,
            document_id=document_id,
            commit=False,
        )
        await store.save_command_receipt(command_key, payload_hash, str(document.id))
        if changed:
            await store.append_event(
                project_id,
                "project_source_document.restored",
                {
                    "document_id": str(document.id),
                    "content_hash": document.content_hash,
                },
            )
        await session.commit()
        response.headers["ETag"] = f'"{project.revision}"'
        return document

    @api.get("/api/projects/{project_id}/source-documents/{document_id}/content")
    async def read_project_source_document(
        project_id: UUID,
        document_id: UUID,
        session: DbSession,
    ) -> Response:
        document, content = await ProjectSourceDocumentStore(
            session,
            Path(cast(Path, api.state.artifact_root)),
        ).read_text(
            project_id=project_id,
            document_id=document_id,
            require_active=False,
        )
        return Response(
            content=content,
            media_type=document.media_type,
            headers={"X-Content-SHA256": document.content_hash},
        )

    @api.post("/api/projects/{project_id}/requirements")
    async def approve_requirements(
        project_id: UUID,
        body: ApproveRequirementsRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        revision = _parse_revision(if_match)
        if body.modules:
            raise DomainConflictError(
                "initial requirements cannot include modules; use module discovery"
            )
        requirement, modules = await ProjectApplication(
            PostgresDomainStore(session)
        ).approve_requirements(
            project_id=project_id,
            expected_project_revision=revision,
            goal=body.goal,
            hard_constraints=body.hard_constraints,
            preferences=body.preferences,
            available_resources=body.available_resources,
            usage_context=body.usage_context,
            budget_context=body.budget_context,
            skill_context=body.skill_context,
            unknowns=body.unknowns,
            modules=tuple(item.model_dump() for item in body.modules),
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {
            "requirement": requirement,
            "modules": modules,
            "project_revision": revision + 1,
        }

    @api.post(
        "/api/projects/{project_id}/module-discovery",
        status_code=status.HTTP_201_CREATED,
    )
    async def discover_initial_modules(
        project_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
        body: ModuleDiscoveryRequest | None = None,
    ) -> dict[str, Any]:
        """Create a reviewable first module structure from confirmed requirements.

        This is an explicitly user-triggered model call. It only persists a
        ``ProjectReshapeProposal``; applying that proposal remains the command
        that creates live modules and a blueprint.
        """
        from aidison.agents.module_discovery import JsonModeInitialModuleDiscoveryAgent
        from aidison.providers.gateway import (
            ProviderSettings,
            ProviderUnavailableError,
            build_chat_model,
        )

        body = body or ModuleDiscoveryRequest()

        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("requirements must be approved before module discovery")
        if project.active_blueprint_id is not None:
            raise DomainConflictError("project already has a confirmed module structure")
        # Hold a transaction-scoped PostgreSQL advisory lock before checking
        # for a pending proposal or calling the external model. The lock key is
        # intentionally separate from the persisted command receipt key: it
        # protects generation cost while the latter protects proposal writes.
        discovery_lock_key = (
            f"initial-module-discovery-lock:{project_id}:{project.active_requirement_revision_id}"
        )
        await store.claim_command(
            discovery_lock_key,
            canonical_hash(
                "initial-module-discovery-lock",
                project_id,
                project.active_requirement_revision_id,
            ),
        )
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("requirements must be approved before module discovery")
        if project.active_blueprint_id is not None:
            raise DomainConflictError("project already has a confirmed module structure")
        pending = [
            item
            for item in await store.list_project_reshape_proposals(project_id)
            if item.status is ProjectReshapeStatus.PROPOSED and item.basis_blueprint_id is None
        ]
        if pending:
            response.headers["ETag"] = f'"{revision}"'
            return {"reshape_proposal": pending[0], "project_revision": revision}
        requirement = await store.get_requirement_revision(project.active_requirement_revision_id)
        if requirement is None:
            raise DomainConflictError("active requirement revision is unavailable")
        confirmed_requirements = json.dumps(
            {
                "structure_depth": body.structure_depth,
                "planning_brief": body.planning_brief,
                "planning_guidance": {
                    "focused": "Prefer the minimum practical set of boundaries.",
                    "standard": (
                        "Separate responsibilities when their interfaces or acceptance "
                        "conditions differ."
                    ),
                    "deep": (
                        "For genuinely coupled systems, examine responsibility, interface, "
                        "acceptance, failure containment, and dependency direction before "
                        "choosing boundaries."
                    ),
                }[body.structure_depth],
                "goal": requirement.goal,
                "usage_context": requirement.usage_context,
                "budget_context": requirement.budget_context,
                "skill_context": requirement.skill_context,
                "hard_constraints": requirement.hard_constraints,
                "preferences": requirement.preferences,
                "available_resources": requirement.available_resources,
                "unknowns": requirement.unknowns,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        factory = api.state.module_discovery_model_factory
        try:
            provider_settings = ProviderSettings()
            model = (
                factory()
                if factory is not None
                else build_chat_model(
                    provider_settings,
                    max_tokens={"focused": 1_400, "standard": 2_000, "deep": 3_600}[
                        body.structure_depth
                    ],
                    thinking="enabled",
                )
            )
            generated = await _await_planning_operation(
                JsonModeInitialModuleDiscoveryAgent(model).ainvoke(confirmed_requirements),
                timeout_seconds=provider_settings.request_timeout_seconds,
            )
        except ProviderUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="module discovery model timed out; retry or edit requirements manually",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=502, detail="module discovery returned invalid output"
            ) from exc
        # Semantic idempotency is scoped to the approved requirement basis,
        # rather than a browser-generated click key. This serializes concurrent
        # explicit requests into one reviewable initial structure proposal.
        internal_key = (
            "initial-module-discovery:"
            + canonical_hash(
                project_id,
                project.active_requirement_revision_id,
                body.model_dump(mode="json"),
            )
        )
        proposal = await ProjectApplication(store).propose_project_reshape(
            proposal=ProjectReshapeProposal(
                project_id=project_id,
                basis_blueprint_id=None,
                target_goal=requirement.goal,
                summary=generated.summary,
                new_module_keys=tuple(item.key for item in generated.modules),
                new_modules=tuple(
                    ProposedModule(**item.model_dump()) for item in generated.modules
                ),
            ),
            expected_project_revision=revision,
            idempotency_key=internal_key,
        )
        response.headers["ETag"] = f'"{revision}"'
        return {"reshape_proposal": proposal, "project_revision": revision}

    @api.get("/api/projects/{project_id}/modules")
    async def list_modules(project_id: UUID, session: DbSession) -> Any:
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        return await _active_modules_for_project(store, project)

    @api.get("/api/projects/{project_id}/blueprint")
    async def get_active_blueprint(project_id: UUID, session: DbSession) -> Any:
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.active_blueprint_id is None:
            raise DomainNotFoundError("project blueprint not found")
        blueprint = await store.get_project_blueprint(project.active_blueprint_id)
        if blueprint is None:
            raise DomainConflictError("project references a missing active blueprint")
        return blueprint

    @api.get("/api/projects/{project_id}/modules/{module_id}/configurations")
    async def list_module_configurations(
        project_id: UUID, module_id: UUID, session: DbSession
    ) -> Any:
        return await PostgresDomainStore(session).list_module_configurations(project_id, module_id)

    @api.get("/api/projects/{project_id}/selection-locks")
    async def list_selection_locks(
        project_id: UUID, session: DbSession, module_id: UUID | None = None
    ) -> Any:
        return await PostgresDomainStore(session).list_selection_locks(project_id, module_id)

    @api.get("/api/projects/{project_id}/adjustment-batches")
    async def list_adjustment_batches(project_id: UUID, session: DbSession) -> Any:
        return await PostgresDomainStore(session).list_adjustment_batches(project_id)

    @api.post("/api/projects/{project_id}/modules/{module_id}/selection-locks", status_code=201)
    async def create_selection_lock(
        project_id: UUID,
        module_id: UUID,
        body: CreateSelectionLockRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        lock = await ProjectApplication(PostgresDomainStore(session)).create_selection_lock(
            project_id=project_id,
            module_id=module_id,
            candidate_id=body.candidate_id,
            reason=body.reason,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {"selection_lock": lock, "project_revision": revision + 1}

    @api.post("/api/selection-locks/{lock_id}/unlock")
    async def release_selection_lock(
        lock_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        lock = await ProjectApplication(PostgresDomainStore(session)).release_selection_lock(
            lock_id=lock_id,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {"selection_lock": lock, "project_revision": revision + 1}

    @api.post("/api/projects/{project_id}/modules/{module_id}/adjustments", status_code=201)
    async def record_user_adjustment(
        project_id: UUID,
        module_id: UUID,
        body: RecordUserAdjustmentRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        adjustment = await ProjectApplication(PostgresDomainStore(session)).record_user_adjustment(
            project_id=project_id,
            module_id=module_id,
            kind=body.kind,
            target=body.target,
            batch_window_seconds=DraftSettings().adjustment_batch_window_seconds,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {"adjustment": adjustment, "project_revision": revision + 1}

    @api.post("/api/adjustment-batches/{batch_id}/flush")
    async def flush_adjustment_batch(
        batch_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)
        batch, preview = await app.flush_adjustment_batch_with_preview(
            batch_id=batch_id,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {
            "adjustment_batch": batch,
            "change_impact_preview": preview,
            "project_revision": revision + 1,
        }

    @api.get("/api/projects/{project_id}/draft-history")
    async def list_draft_history(project_id: UUID, session: DbSession) -> Any:
        return await PostgresDomainStore(session).list_draft_history_entries(project_id)

    @api.get("/api/projects/{project_id}/solution-snapshots")
    async def list_solution_snapshots(project_id: UUID, session: DbSession) -> Any:
        return await PostgresDomainStore(session).list_solution_snapshots(project_id)

    @api.post("/api/projects/{project_id}/solution-snapshots", status_code=status.HTTP_201_CREATED)
    async def save_solution_snapshot(
        project_id: UUID,
        body: SaveSolutionSnapshotRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        snapshot = await ProjectApplication(PostgresDomainStore(session)).save_solution_snapshot(
            project_id=project_id,
            label=body.label,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {"solution_snapshot": snapshot, "project_revision": revision + 1}

    @api.post("/api/solution-snapshots/{snapshot_id}/restore")
    async def restore_solution_snapshot(
        snapshot_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        snapshot = await ProjectApplication(PostgresDomainStore(session)).restore_solution_snapshot(
            snapshot_id=snapshot_id,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {"solution_snapshot": snapshot, "project_revision": revision + 1}

    @api.post(
        "/api/projects/{project_id}/execution-plans/research-strategy",
        status_code=status.HTTP_201_CREATED,
    )
    async def propose_research_strategy_execution_plan(
        project_id: UUID,
        body: ResearchStrategyPlanRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        """Generate a typed strategy proposal; it remains inert until approval."""
        from aidison.providers.gateway import (
            ProviderSettings,
            ProviderUnavailableError,
        )

        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("requirements must be approved before research planning")
        requirement = await store.get_requirement_revision(project.active_requirement_revision_id)
        if requirement is None:
            raise DomainConflictError("active requirement revision is unavailable")
        active_modules = await _active_modules_for_project(store, project)
        if not active_modules:
            raise DomainConflictError("research planning requires at least one active module")
        modules_by_id = {item.id: item for item in active_modules}
        scope_ids = body.module_ids or tuple(item.id for item in active_modules)
        has_duplicate_or_unknown_scope = len(set(scope_ids)) != len(scope_ids) or any(
            item not in modules_by_id for item in scope_ids
        )
        if has_duplicate_or_unknown_scope:
            raise DomainConflictError(
                "research strategy scope contains inactive or unknown modules"
            )
        scope_modules = tuple(modules_by_id[item] for item in scope_ids)
        objective = body.objective or (
            f"为项目目标“{project.goal}”收集可验证的候选方案、约束与兼容性证据。"
        )
        requested_concurrency = body.max_concurrency or RESEARCH_DEFAULT_MAX_CONCURRENCY
        try:
            strategy = await _await_planning_operation(
                ResearchStrategyPlanningService(
                    planner_factory=api.state.research_strategy_planner_factory
                ).propose(
                    objective=objective,
                    requirement=requirement,
                    scope_modules=scope_modules,
                    research_depth=body.research_depth,
                    source_strategy=body.source_strategy,
                    planning_mode="hierarchical",
                    planning_max_concurrency=min(
                        requested_concurrency,
                        MAX_DELEGATION_WAVE_SIZE,
                    ),
                ),
                timeout_seconds=ProviderSettings().request_timeout_seconds,
            )
        except ProviderUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="research strategy model timed out; retry or narrow the requested scope",
            ) from exc
        except ResearchStrategyPlanningError as exc:
            _logger.warning(
                "research strategy planning rejected diagnostics=%s",
                exc.diagnostics,
            )
            raise HTTPException(
                status_code=502, detail=str(exc)
            ) from exc
        max_concurrency = min(
            len(strategy.tasks), requested_concurrency, MAX_DELEGATION_WAVE_SIZE
        )
        requires_independent_verification = (
            body.requires_independent_verification
            if body.requires_independent_verification is not None
            else body.research_depth == "deep"
        )
        proposal = ExecutionPlanProposal(
            project_id=project_id,
            basis_hash=canonical_hash(project.active_requirement_revision_id, active_modules),
            objective=objective,
            work_summary=(strategy.summary, *strategy.decision_notes),
            allowed_coordination_modes=(CoordinationMode.DECOMPOSE,),
            max_concurrency=max_concurrency,
            max_token_budget=body.max_token_budget or RESEARCH_DEFAULT_MAX_TOKEN_BUDGET,
            max_duration_seconds=(
                body.max_duration_seconds or RESEARCH_DEFAULT_MAX_DURATION_SECONDS
            ),
            research_depth=body.research_depth,
            allowed_tool_classes=RESEARCH_DEFAULT_ALLOWED_TOOL_CLASSES,
            allowed_effects=(),
            requires_independent_verification=requires_independent_verification,
            requires_result_approval=True,
            research_strategy=strategy,
        )
        proposed = await ProjectApplication(store).propose_execution_plan(
            proposal=proposal,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision}"'
        return {"execution_plan": proposed, "project_revision": revision}

    @api.post("/api/execution-plans/{proposal_id}/resolve")
    async def resolve_execution_plan(
        proposal_id: UUID,
        body: ResolveExecutionPlanRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        revision = _parse_revision(if_match)
        decision = ExecutionPlanStatus(body.decision)
        if body.enqueue_research and decision is not ExecutionPlanStatus.APPROVED:
            raise HTTPException(
                status_code=422,
                detail="enqueue_research is only valid when approving an execution plan",
            )
        application = ProjectApplication(PostgresDomainStore(session))
        if body.enqueue_research:
            try:
                proposal = await application.resolve_execution_plan(
                    proposal_id=proposal_id,
                    decision=decision,
                    scope_hash=body.scope_hash,
                    expected_project_revision=revision,
                    idempotency_key=idempotency_key,
                    commit=False,
                )
                queued = await _enqueue_research_run(
                    session=session,
                    project_id=proposal.project_id,
                    execution_plan_id=proposal.id,
                    expected_project_revision=revision,
                    commit=False,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            response.headers["ETag"] = f'"{revision}"'
            return {
                "execution_plan": proposal,
                "agent_run": queued["agent_run"],
                "project_revision": revision,
            }
        proposal = await application.resolve_execution_plan(
            proposal_id=proposal_id,
            decision=decision,
            scope_hash=body.scope_hash,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision}"'
        return {"execution_plan": proposal, "project_revision": revision}

    @api.get("/api/projects/{project_id}/reshape-proposals")
    async def list_project_reshape_proposals(project_id: UUID, session: DbSession) -> Any:
        return await PostgresDomainStore(session).list_project_reshape_proposals(project_id)

    @api.get("/api/projects/{project_id}/engineering-coupling-analysis")
    async def read_engineering_coupling_analysis(project_id: UUID, session: DbSession) -> Any:
        """Read the active blueprint's cyclic engineering graph without mutating it."""

        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.active_blueprint_id is None:
            raise DomainConflictError("engineering coupling analysis requires an active blueprint")
        blueprint = await store.get_project_blueprint(project.active_blueprint_id)
        if blueprint is None:
            raise DomainConflictError("active project blueprint is unavailable")
        modules = await _active_modules_for_project(store, project)
        module_lineage_ids = tuple(
            item.lineage_id for item in modules if item.lineage_id is not None
        )
        if len(module_lineage_ids) != len(modules):
            raise DomainConflictError("active modules have incomplete lineage identities")
        lineages = {item.id: item for item in await store.list_module_lineages(project_id)}
        analysis = analyze_couplings(
            lineage_ids=module_lineage_ids,
            edges=blueprint.engineering_couplings,
        )
        return {
            "blueprint_id": str(blueprint.id),
            "components": [
                {
                    "component_id": index,
                    "lineage_ids": [str(item) for item in component],
                    "stable_keys": [lineages[item].stable_key for item in component],
                    "is_joint_design_group": len(component) > 1,
                }
                for index, component in enumerate(analysis.strongly_connected_components)
            ],
            "condensation_edges": [list(item) for item in analysis.condensation_edges],
            "transitive_reduction_edges": [
                list(item) for item in analysis.transitive_reduction_edges
            ],
            "couplings": [item.model_dump(mode="json") for item in blueprint.engineering_couplings],
        }

    @api.post("/api/projects/{project_id}/reshape-proposals", status_code=status.HTTP_201_CREATED)
    async def create_project_reshape_proposal(
        project_id: UUID,
        body: CreateProjectReshapeRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.active_blueprint_id is None:
            raise DomainConflictError(
                "initial module structure must be generated through module discovery"
            )
        proposal = await ProjectApplication(store).propose_project_reshape(
            proposal=ProjectReshapeProposal(
                project_id=project_id,
                basis_blueprint_id=project.active_blueprint_id,
                target_goal=body.target_goal,
                summary=body.summary,
                affected_module_ids=body.affected_module_ids,
                unchanged_module_ids=body.unchanged_module_ids,
                new_module_keys=tuple(item.key for item in body.new_modules),
                new_modules=tuple(ProposedModule(**item.model_dump()) for item in body.new_modules),
                lineage_changes=tuple(
                    ModuleLineageChange(**item.model_dump()) for item in body.lineage_changes
                ),
                dependency_edges=(
                    None
                    if body.dependency_edges is None
                    else tuple(
                        (item.source_module_id, item.target_module_id)
                        for item in body.dependency_edges
                    )
                ),
                engineering_couplings=(
                    None
                    if body.engineering_couplings is None
                    else tuple(
                        EngineeringCouplingEdge(**item.model_dump())
                        for item in body.engineering_couplings
                    )
                ),
            ),
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision}"'
        return {"reshape_proposal": proposal, "project_revision": revision}

    @api.post("/api/reshape-proposals/{proposal_id}/resolve")
    async def resolve_project_reshape_proposal(
        proposal_id: UUID,
        body: ResolveProjectReshapeRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        proposal = await ProjectApplication(PostgresDomainStore(session)).resolve_project_reshape(
            proposal_id=proposal_id,
            decision=ProjectReshapeStatus(body.decision),
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        project_revision = revision + (1 if proposal.status is ProjectReshapeStatus.APPLIED else 0)
        response.headers["ETag"] = f'"{project_revision}"'
        return {"reshape_proposal": proposal, "project_revision": project_revision}

    @api.get("/api/projects/{project_id}/requirements-change-proposals")
    async def list_requirements_change_proposals(project_id: UUID, session: DbSession) -> Any:
        return await PostgresDomainStore(session).list_requirements_change_proposals(project_id)

    @api.post(
        "/api/projects/{project_id}/requirements-change-proposals",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_requirements_change_proposal(
        project_id: UUID,
        body: CreateRequirementsChangeRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.active_requirement_revision_id is None or project.active_blueprint_id is None:
            raise DomainConflictError("requirements changes require a confirmed module structure")
        proposal = await ProjectApplication(store).propose_requirements_change(
            proposal=RequirementsChangeProposal(
                project_id=project_id,
                basis_requirement_revision_id=project.active_requirement_revision_id,
                basis_blueprint_id=project.active_blueprint_id,
                target_goal=body.target_goal,
                hard_constraints=body.hard_constraints,
                preferences=body.preferences,
                available_resources=body.available_resources,
                usage_context=body.usage_context,
                budget_context=body.budget_context,
                skill_context=body.skill_context,
                unknowns=body.unknowns,
                summary=body.summary,
                modules=tuple(ProposedModule(**item.model_dump()) for item in body.modules),
            ),
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision}"'
        return {"requirements_change_proposal": proposal, "project_revision": revision}

    @api.post("/api/requirements-change-proposals/{proposal_id}/resolve")
    async def resolve_requirements_change_proposal(
        proposal_id: UUID,
        body: ResolveRequirementsChangeRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        proposal = await ProjectApplication(
            PostgresDomainStore(session)
        ).resolve_requirements_change(
            proposal_id=proposal_id,
            decision=RequirementsChangeProposalStatus(body.decision),
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        # Apply writes the new requirement/module/blueprint and records the
        # applied proposal in one transaction (revision +1); reject changes no
        # fact and keeps the current revision.
        is_applied = proposal.status is RequirementsChangeProposalStatus.APPLIED
        project_revision = revision + (1 if is_applied else 0)
        invalidated_runs: tuple[AgentRun, ...] = ()
        if is_applied:
            control = AgentRunControl(session)
            invalidated_runs = await control.invalidate_stale_project_basis(
                project_id=proposal.project_id,
                current_project_revision=project_revision,
            )
            request_store = AgentRunControlRequestStore(session)
            proposal_refs = {
                str(proposal.id),
                f"requirements-change-proposal://{proposal.id}",
            }
            for run in invalidated_runs:
                for request in await request_store.list_for_run(agent_run_id=run.id):
                    if (
                        request.kind is ControlRequestKind.BASIS_STEERING
                        and request.status is ControlRequestStatus.REQUESTED
                        and request.payload.get("change_request_ref") in proposal_refs
                    ):
                        await request_store.acknowledge(request.id)
                await PostgresDomainStore(session).append_event(
                    proposal.project_id,
                    "agent_run.basis_invalidated",
                    {
                        "agent_run_id": str(run.id),
                        "basis_project_revision": run.basis_project_revision,
                        "current_project_revision": project_revision,
                        "running": run.status.value == "running",
                    },
                )
            await session.commit()
        response.headers["ETag"] = f'"{project_revision}"'
        return {
            "requirements_change_proposal": proposal,
            "project_revision": project_revision,
            "invalidated_agent_run_ids": [str(run.id) for run in invalidated_runs],
        }

    @api.get("/api/projects/{project_id}/change-impact-previews")
    async def list_change_impact_previews(project_id: UUID, session: DbSession) -> Any:
        return await PostgresDomainStore(session).list_change_impact_previews(project_id)

    @api.post("/api/projects/{project_id}/research-proposals")
    async def submit_research_proposal(
        project_id: UUID,
        body: ResearchProposalRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        revision = _parse_revision(if_match)
        decision = await ProjectApplication(PostgresDomainStore(session)).submit_research_proposal(
            project_id=project_id,
            expected_project_revision=revision,
            evidence=body.evidence,
            candidates=body.candidates,
            findings=body.findings,
            decision_question=body.decision_question,
            decision_options=body.decision_options,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {"decision": decision, "project_revision": revision + 1}

    @api.post(
        "/api/projects/{project_id}/agent-runs/research",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enqueue_langgraph_research_run(
        project_id: UUID,
        body: StartResearchRunRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        """Queue the LangGraph AgentRun authorized by an approved plan."""

        revision = _parse_revision(if_match)
        run = await AgentRunApplication(
            session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).enqueue_research_run(
            project_id=project_id,
            execution_plan_id=body.execution_plan_id,
            expected_project_revision=revision,
            runtime_binding=DEFAULT_RESEARCH_RUNTIME_BINDING,
            retry_failed=body.retry_failed,
        )
        response.headers["ETag"] = f'"{revision}"'
        return {"agent_run": run, "project_revision": revision}

    @api.post(
        "/api/projects/{project_id}/agent-runs/solution",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enqueue_langgraph_solution_run(
        project_id: UUID,
        body: StartSolutionRunRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        """Queue one evidence-bound SolutionGraph Run after a user-selected Decision."""

        revision = _parse_revision(if_match)
        run = await AgentRunApplication(
            session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).enqueue_solution_run(
            project_id=project_id,
            decision_id=body.decision_id,
            expected_project_revision=revision,
            runtime_binding=DEFAULT_SOLUTION_RUNTIME_BINDING,
            risk_class=SolutionRiskClass(body.risk_class),
        )
        response.headers["ETag"] = f'"{revision}"'
        return {"agent_run": run, "project_revision": revision}

    @api.post(
        "/api/projects/{project_id}/agent-runs/impact",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enqueue_langgraph_impact_run(
        project_id: UUID,
        body: StartImpactRunRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        """Queue an ImpactGraph only from a current version-pinned Observation."""

        revision = _parse_revision(if_match)
        run = await AgentRunApplication(
            session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).enqueue_impact_run(
            project_id=project_id,
            observation_id=body.observation_id,
            expected_project_revision=revision,
            runtime_binding=DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING,
        )
        response.headers["ETag"] = f'"{revision}"'
        return {"agent_run": run, "project_revision": revision}

    @api.post("/api/agent-run-decisions/{decision_id}/resolve")
    async def resolve_agent_run_decision(
        decision_id: UUID,
        body: ResolveAgentRunDecisionRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        """Apply a durable user decision, then commit the proposal through Domain only."""

        revision = _parse_revision(if_match)
        answer = AgentRunDecisionStatus(body.decision)
        resolved = await AgentRunDecisionStore(session).resolve(
            decision_id=decision_id,
            basis_hash=body.basis_hash,
            answer=answer,
        )
        artifact_root = Path(getattr(api.state, "artifact_root", "artifacts/data"))
        run = await AgentRunControl(session).get(resolved.agent_run_id)
        if run is None:
            raise AgentRunNotFoundError("AgentRun not found")
        if run.kind is AgentRunKind.SOLUTION:
            solution = await SolutionProposalCommitApplication(
                session,
                artifact_root=artifact_root,
            ).commit_approved_decision(
                agent_run_decision_id=resolved.id,
                expected_project_revision=revision,
            )
            next_revision = revision + 2 if solution is not None else revision
            response.headers["ETag"] = f'"{next_revision}"'
            return {
                "agent_run_decision": resolved,
                "solution": solution,
                "project_revision": next_revision,
            }
        if run.kind is AgentRunKind.IMPACT:
            impact = await ImpactProposalCommitApplication(
                session,
                artifact_root=artifact_root,
            ).commit_approved_decision(
                agent_run_decision_id=resolved.id,
                expected_project_revision=revision,
            )
            next_revision = revision + 2 if impact is not None else revision
            response.headers["ETag"] = f'"{next_revision}"'
            return {
                "agent_run_decision": resolved,
                "impact": impact.impact if impact is not None else None,
                "patch_set": impact.patch_set if impact is not None else None,
                "solution": impact.solution if impact is not None else None,
                "project_revision": next_revision,
            }
        canonical = await ProposalCommitApplication(
            session,
            artifact_root=artifact_root,
        ).commit_approved_decision(
            agent_run_decision_id=resolved.id,
            expected_project_revision=revision,
        )
        next_revision = revision + 2 if canonical is not None else revision
        response.headers["ETag"] = f'"{next_revision}"'
        return {
            "agent_run_decision": resolved,
            "canonical_decision": canonical,
            "project_revision": next_revision,
        }

    @api.get(
        "/api/projects/{project_id}/agent-runs/{run_id}/trajectory",
        response_model=AgentRunTrajectory,
    )
    async def get_agent_run_trajectory(
        project_id: UUID,
        run_id: UUID,
        session: DbSession,
    ) -> AgentRunTrajectory:
        """Return payload-free lifecycle, attempt and replay-ledger diagnostics."""

        return await AgentRunTrajectoryService(session).build(
            project_id=project_id,
            agent_run_id=run_id,
        )

    @api.post(
        "/api/projects/{project_id}/agent-runs/{run_id}/evaluation-candidates",
        response_model=FailureRegressionCandidateCapture,
        status_code=status.HTTP_201_CREATED,
    )
    async def capture_failed_agent_run_for_evaluation(
        project_id: UUID,
        run_id: UUID,
        idempotency_key: IdempotencyKey,
        session: DbSession,
    ) -> FailureRegressionCandidateCapture:
        """Freeze one failed Run for human labeling; never auto-promote it."""

        return await FailureRegressionCandidateService(
            session=session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).capture(
            project_id=project_id,
            agent_run_id=run_id,
            idempotency_key=idempotency_key,
        )

    @api.get(
        "/api/projects/{project_id}/agent-runs/{run_id}/evaluation-candidates",
        response_model=list[FailureRegressionCandidateCapture],
    )
    async def list_failed_agent_run_evaluation_candidates(
        project_id: UUID,
        run_id: UUID,
        session: DbSession,
    ) -> tuple[FailureRegressionCandidateCapture, ...]:
        return await FailureRegressionCandidateService(
            session=session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).list(project_id=project_id, agent_run_id=run_id)

    @api.post(
        "/api/projects/{project_id}/agent-runs/{run_id}/evaluation-candidates/"
        "{candidate_key}/review",
        response_model=FailureRegressionReviewResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def review_failed_agent_run_evaluation_candidate(
        project_id: UUID,
        run_id: UUID,
        candidate_key: str,
        request: ReviewFailureRegressionCandidateRequest,
        idempotency_key: IdempotencyKey,
        session: DbSession,
    ) -> FailureRegressionReviewResult:
        """Human-review one candidate; only approved replayable input becomes Golden."""

        return await FailureRegressionCandidateService(
            session=session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).review(
            project_id=project_id,
            agent_run_id=run_id,
            candidate_key=candidate_key,
            decision=FailureRegressionReviewDecision(request.decision),
            reviewed_by=request.reviewed_by,
            review_notes=request.review_notes,
            expected_outcome=request.expected_outcome,
            oracle=request.oracle,
            idempotency_key=idempotency_key,
        )

    @api.get(
        "/api/projects/{project_id}/agent-runs/{run_id}/golden-regression-tasks",
        response_model=list[GoldenRegressionTask],
    )
    async def list_golden_regression_tasks(
        project_id: UUID,
        run_id: UUID,
        session: DbSession,
    ) -> tuple[GoldenRegressionTask, ...]:
        return await FailureRegressionCandidateService(
            session=session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).list_golden_tasks(project_id=project_id, agent_run_id=run_id)

    @api.post(
        "/api/projects/{project_id}/agent-runs/{source_run_id}/golden-regression-tasks/"
        "{golden_task_key}/evaluate/{observed_run_id}",
        response_model=GoldenRegressionEvaluation,
        status_code=status.HTTP_201_CREATED,
    )
    async def evaluate_agent_run_against_golden_task(
        project_id: UUID,
        source_run_id: UUID,
        golden_task_key: str,
        observed_run_id: UUID,
        idempotency_key: IdempotencyKey,
        session: DbSession,
    ) -> GoldenRegressionEvaluation:
        return await FailureRegressionCandidateService(
            session=session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).evaluate_golden_task(
            project_id=project_id,
            source_agent_run_id=source_run_id,
            golden_task_key=golden_task_key,
            observed_agent_run_id=observed_run_id,
            idempotency_key=idempotency_key,
        )

    @api.post("/api/projects/{project_id}/agent-runs/{run_id}/cancel")
    async def cancel_agent_run(
        project_id: UUID,
        run_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        """Cancel a non-running LangGraph Run without pretending to stop an active worker."""
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != revision:
            raise PreconditionFailedError("project revision is stale")
        control = AgentRunControl(session)
        run = await control.get(run_id)
        if run is None or run.project_id != project_id:
            raise AgentRunNotFoundError("AgentRun not found")
        cancellation_receipt = await control.request_cancel_with_receipt(run_id=run_id)
        if cancellation_receipt.changed:
            event_type = (
                "agent_run.cancelled"
                if cancellation_receipt.run.status.value == "cancelled"
                else "agent_run.cancel_requested"
            )
            await store.append_event(
                project_id,
                event_type,
                {
                    "agent_run_id": str(cancellation_receipt.run.id),
                    "idempotency_key": idempotency_key,
                    "running": cancellation_receipt.run.status.value == "running",
                },
            )
        await session.commit()
        response.headers["ETag"] = f'"{revision}"'
        return {"agent_run": cancellation_receipt.run, "project_revision": revision}

    @api.post("/api/projects/{project_id}/agent-runs/{run_id}/controls")
    async def create_agent_run_control_request(
        project_id: UUID,
        run_id: UUID,
        body: CreateAgentRunControlRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != revision:
            raise PreconditionFailedError("project revision is stale")
        run = await AgentRunControl(session).get(run_id)
        if run is None or run.project_id != project_id:
            raise AgentRunNotFoundError("AgentRun not found")
        if run.status.value in {"succeeded", "failed", "cancelled"}:
            raise AgentRunConflictError("terminal AgentRun cannot accept a control request")
        if run.basis_hash != body.basis_hash:
            raise PreconditionFailedError("control request basis is stale")
        kind = ControlRequestKind(body.kind)
        payload: dict[str, object] = {}
        if kind is ControlRequestKind.RUNTIME_STEERING:
            payload = {"instruction": body.instruction or ""}
        elif kind is ControlRequestKind.BASIS_STEERING:
            payload = {"change_request_ref": body.change_request_ref or ""}
        request_receipt = await AgentRunControlRequestStore(session).request_with_receipt(
            AgentRunControlRequest(
                agent_run_id=run.id,
                kind=kind,
                basis_hash=run.basis_hash,
                payload=payload,
                idempotency_key=idempotency_key,
            )
        )
        if request_receipt.created:
            await store.append_event(
                project_id,
                "agent_run.control_requested",
                {
                    "agent_run_id": str(run.id),
                    "control_request_id": str(request_receipt.request.id),
                    "kind": kind.value,
                },
            )
        await session.commit()
        response.headers["ETag"] = f'"{revision}"'
        return {
            "control_request": request_receipt.request,
            "project_revision": revision,
        }

    @api.post("/api/projects/{project_id}/agent-runs/{run_id}/resume")
    async def resume_paused_agent_run(
        project_id: UUID,
        run_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        """Requeue only a Run paused before graph dispatch.

        A checkpointed LangGraph interrupt is a user Decision boundary, not a
        generic resume button.  Its dedicated Decision bridge remains the only
        legal way to choose its checkpoint anchor and continue it.
        """

        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != revision:
            raise PreconditionFailedError("project revision is stale")
        control = AgentRunControl(session)
        run = await control.get(run_id)
        if run is None or run.project_id != project_id:
            raise AgentRunNotFoundError("AgentRun not found")
        requests = await AgentRunControlRequestStore(session).list_for_run(agent_run_id=run.id)
        has_acknowledged_pause = any(
            item.kind is ControlRequestKind.PAUSE
            and item.status is ControlRequestStatus.ACKNOWLEDGED
            and item.basis_hash == run.basis_hash
            for item in requests
        )
        if not has_acknowledged_pause:
            raise AgentRunConflictError("AgentRun has no acknowledged pause request")
        resume_receipt = await control.resume_after_pause_with_receipt(run_id=run.id)
        if resume_receipt.changed:
            await store.append_event(
                project_id,
                "agent_run.resumed",
                {
                    "agent_run_id": str(resume_receipt.run.id),
                    "idempotency_key": idempotency_key,
                    "reason": "acknowledged_pause",
                },
            )
        await session.commit()
        response.headers["ETag"] = f'"{revision}"'
        return {"agent_run": resume_receipt.run, "project_revision": revision}

    @api.post("/api/decisions/{decision_id}/resolve")
    async def resolve_decision(
        decision_id: UUID,
        body: ResolveDecisionRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        pending_decision = await store.get_decision_request(decision_id)
        if pending_decision is None:
            raise DomainNotFoundError("decision request not found")
        project = await store.get_project(pending_decision.project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        modules = await _active_modules_for_project(store, project)
        authorization_basis_hash = canonical_hash(
            project.active_requirement_revision_id, tuple(modules)
        )
        await _require_execution_plan(
            store=store,
            project_id=project.id,
            execution_plan_id=body.execution_plan_id,
            authorization_basis_hash=authorization_basis_hash,
            required_mode=CoordinationMode.DECOMPOSE,
            required_concurrency=1,
            required_token_budget=16_000,
        )
        decision = await ProjectApplication(store).resolve_decision(
            decision_id=decision_id,
            expected_project_revision=revision,
            selected_option_id=body.selected_option_id,
            basis_hash=body.basis_hash,
            idempotency_key=idempotency_key,
        )
        solution_run = await AgentRunApplication(
            session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).enqueue_solution_run(
            project_id=decision.project_id,
            decision_id=decision.id,
            expected_project_revision=revision + 1,
            runtime_binding=DEFAULT_SOLUTION_RUNTIME_BINDING,
            risk_class=SolutionRiskClass.STANDARD,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {
            "decision": decision,
            "solution_run": solution_run,
            "project_revision": revision + 1,
        }

    @api.post("/api/projects/{project_id}/solutions")
    async def freeze_solution(
        project_id: UUID,
        body: FreezeSolutionRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        revision = _parse_revision(if_match)
        solution = await ProjectApplication(PostgresDomainStore(session)).freeze_solution(
            project_id=project_id,
            expected_project_revision=revision,
            solution_proposal_id=body.solution_proposal_id,
            basis_hash=body.basis_hash,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {"solution": solution, "project_revision": revision + 1}

    @api.post("/api/projects/{project_id}/observations")
    async def submit_observation(
        project_id: UUID,
        body: SubmitObservationRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        modules = await _active_modules_for_project(store, project)
        authorization_basis_hash = canonical_hash(
            project.active_requirement_revision_id, tuple(modules)
        )
        await _require_execution_plan(
            store=store,
            project_id=project_id,
            execution_plan_id=body.execution_plan_id,
            authorization_basis_hash=authorization_basis_hash,
            required_mode=CoordinationMode.DECOMPOSE,
            required_concurrency=1,
            required_token_budget=16_000,
        )
        observation = await ProjectApplication(store).submit_observation(
            project_id=project_id,
            expected_project_revision=revision,
            statement=body.statement,
            affected_module_ids=body.affected_module_ids,
            idempotency_key=idempotency_key,
        )
        impact_run = await AgentRunApplication(
            session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
        ).enqueue_impact_run(
            project_id=project_id,
            observation_id=observation.id,
            expected_project_revision=revision + 1,
            runtime_binding=DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {
            "observation": observation,
            "impact_run": impact_run,
            "project_revision": revision + 1,
        }

    @api.post("/api/impacts/{impact_id}/approve")
    async def approve_impact(
        impact_id: UUID,
        body: ApprovePatchRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> dict[str, Any]:
        revision = _parse_revision(if_match)
        patch_set, solution = await ProjectApplication(
            PostgresDomainStore(session)
        ).approve_impact_and_patch(
            impact_id=impact_id,
            expected_project_revision=revision,
            basis_hash=body.basis_hash,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {
            "patch_set": patch_set,
            "solution": solution,
            "project_revision": revision + 1,
        }

    @api.get("/api/projects/{project_id}/snapshot")
    async def project_snapshot(project_id: UUID, session: DbSession) -> dict[str, Any]:
        if session.bind and session.bind.dialect.name == "postgresql":
            # Domain facts and event cursor must describe one durable read point.
            # PostgreSQL READ COMMITTED otherwise gives each SELECT a fresh view.
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        decisions = list(
            await session.scalars(
                select(DecisionRequestRow).where(DecisionRequestRow.project_id == project_id)
            )
        )

        impacts = list(
            await session.scalars(
                select(ImpactAnalysisRow).where(ImpactAnalysisRow.project_id == project_id)
            )
        )
        agent_runs = list(
            await session.scalars(
                select(AgentRunRow)
                .where(AgentRunRow.project_id == project_id)
                .order_by(AgentRunRow.created_at, AgentRunRow.id)
            )
        )
        agent_run_decision_rows = list(
            await session.scalars(
                select(AgentRunDecisionRow)
                .where(AgentRunDecisionRow.project_id == project_id)
                .order_by(AgentRunDecisionRow.created_at, AgentRunDecisionRow.id)
            )
        )
        snapshot_artifacts = ContentAddressedArtifactStore(
            session,
            Path(getattr(api.state, "artifact_root", "artifacts/data")),
            record_integrity_status=False,
        )
        agent_run_decision_previews = {
            decision.id: await _agent_run_proposal_preview(
                artifacts=snapshot_artifacts,
                project_id=project_id,
                basis_hash=decision.basis_hash,
                proposal_manifest_ref=decision.proposal_manifest_ref,
            )
            for decision in agent_run_decision_rows
            if decision.status == AgentRunDecisionStatus.PENDING.value
        }
        agent_run_controls = {
            run.id: await AgentRunControlRequestStore(session).list_for_run(agent_run_id=run.id)
            for run in agent_runs
        }
        research_quality_by_run = await _research_quality_by_run(
            session=session,
            artifact_root=Path(getattr(api.state, "artifact_root", "artifacts/data")),
            agent_runs=agent_runs,
        )
        research_progress_by_run = await _research_progress_by_run(
            session=session,
            artifacts=snapshot_artifacts,
            agent_runs=agent_runs,
        )
        agent_run_failure_by_id = await _agent_run_failure_summaries(
            session=session,
            project_id=project_id,
            run_ids={run.id for run in agent_runs},
        )
        agent_run_budget_accounts = list(
            await session.scalars(
                select(AgentRunBudgetAccountRow)
                .join(AgentRunRow, AgentRunBudgetAccountRow.agent_run_id == AgentRunRow.id)
                .where(AgentRunRow.project_id == project_id)
                .order_by(AgentRunBudgetAccountRow.created_at)
            )
        )
        agent_run_budget_account_ids = [account.id for account in agent_run_budget_accounts]
        agent_run_budget_operations = (
            list(
                await session.scalars(
                    select(AgentRunBudgetOperationRow)
                    .where(AgentRunBudgetOperationRow.account_id.in_(agent_run_budget_account_ids))
                    .order_by(AgentRunBudgetOperationRow.created_at)
                )
            )
            if agent_run_budget_account_ids
            else []
        )
        active_modules = await _active_modules_for_project(store, project)
        configuration_groups = await asyncio.gather(
            *[store.list_module_configurations(project_id, module.id) for module in active_modules]
        )
        active_conversation_session = await store.get_active_conversation_session(project_id)
        recent_conversation_turns: Sequence[Any] = ()
        open_conversation_clarifications: Sequence[Any] = ()
        open_conversation_action_proposals: Sequence[Any] = ()
        active_context_summaries: Sequence[Any] = ()
        if active_conversation_session is not None:
            recent_conversation_turns = await store.list_conversation_turns(
                active_conversation_session.id, limit=50
            )
            open_conversation_clarifications = await store.list_active_clarifications(
                active_conversation_session.id
            )
            open_conversation_action_proposals = await store.list_active_action_proposals(
                active_conversation_session.id
            )
            active_context_summaries = await store.list_active_context_summaries(
                project_id, active_conversation_session.id
            )
        snapshot: dict[str, Any] = {
            "project": project,
            "blueprints": await store.list_project_blueprints(project_id),
            "requirements": await store.list_requirement_revisions(project_id),
            "modules": active_modules,
            "module_configurations": [item for group in configuration_groups for item in group],
            "selection_locks": await store.list_selection_locks(project_id),
            "adjustment_batches": await store.list_adjustment_batches(project_id),
            "draft_history": await store.list_draft_history_entries(project_id),
            "solution_snapshots": await store.list_solution_snapshots(project_id),
            "evidence": await store.list_evidence_bindings(project_id),
            "candidates": await store.list_candidates(project_id),
            "compatibility_findings": await store.list_compatibility_findings(project_id),
            "decisions": [DomainDecisionRequest.model_validate(item.payload) for item in decisions],
            "solution_proposals": await store.list_solution_proposals(project_id),
            "execution_plans": await store.list_execution_plan_proposals(project_id),
            "reshape_proposals": await store.list_project_reshape_proposals(project_id),
            "requirements_change_proposals": (
                await store.list_requirements_change_proposals(project_id)
            ),
            "agent_runs": [
                {
                    "id": run.id,
                    "kind": run.kind,
                    "basis_hash": run.basis_hash,
                    "basis_project_revision": run.basis_project_revision,
                    "status": run.status,
                    "cancel_requested": run.cancel_requested,
                    "latest_error": agent_run_failure_by_id.get(run.id),
                    "coverage_contract_ref": run.coverage_contract_ref,
                    "research_quality": research_quality_by_run.get(run.id),
                    "research_progress": research_progress_by_run.get(run.id),
                    "created_at": run.created_at,
                    "started_at": run.started_at,
                    "updated_at": run.updated_at,
                    "completed_at": run.completed_at,
                    "control_requests": [
                        {
                            "id": control.id,
                            "kind": control.kind.value,
                            "status": control.status.value,
                            "created_at": control.created_at,
                            "acknowledged_at": control.acknowledged_at,
                        }
                        for control in agent_run_controls[run.id]
                    ],
                }
                for run in agent_runs
            ],
            "agent_run_decisions": [
                {
                    "id": decision.id,
                    "agent_run_id": decision.agent_run_id,
                    "basis_hash": decision.basis_hash,
                    "status": decision.status,
                    "proposal_preview": agent_run_decision_previews.get(decision.id),
                    "created_at": decision.created_at,
                    "resolved_at": decision.resolved_at,
                }
                for decision in agent_run_decision_rows
            ],
            "agent_run_budget_accounts": [
                {
                    "id": account.id,
                    "agent_run_id": account.agent_run_id,
                    "token_cap": account.token_cap,
                    "tool_call_cap": account.tool_call_cap,
                    "token_reserved": account.token_reserved,
                    "tool_calls_reserved": account.tool_calls_reserved,
                    "token_consumed": account.token_consumed,
                    "tool_calls_consumed": account.tool_calls_consumed,
                    "state": account.state,
                    "created_at": account.created_at,
                    "closed_at": account.closed_at,
                }
                for account in agent_run_budget_accounts
            ],
            "agent_run_budget_operations": [
                {
                    "id": operation.id,
                    "account_id": operation.account_id,
                    "kind": operation.kind,
                    "logical_step": operation.logical_step,
                    "physical_attempt_no": operation.physical_attempt_no,
                    "provider": operation.provider,
                    "target": operation.target,
                    "state": operation.state,
                    "reserved_tokens": operation.reserved_tokens,
                    "reserved_tool_calls": operation.reserved_tool_calls,
                    "consumed_tokens": operation.consumed_tokens,
                    "consumed_tool_calls": operation.consumed_tool_calls,
                    "normalized_error": operation.normalized_error,
                    "created_at": operation.created_at,
                    "settled_at": operation.settled_at,
                }
                for operation in agent_run_budget_operations
            ],
            "change_impact_previews": await store.list_change_impact_previews(project_id),
            "solutions": await store.list_solution_versions(project_id),
            "observations": await store.list_observations(project_id),
            "impacts": [ImpactAnalysis.model_validate(item.payload) for item in impacts],
            "patch_sets": await store.list_patch_sets(project_id),
            "offer_snapshots": await store.list_offer_snapshots(project_id),
            "purchase_proposals": await store.list_purchase_proposals(project_id),
            "effect_approvals": await store.list_effect_approvals(project_id),
            "checkout_handoffs": await store.list_checkout_handoffs(project_id),
            "spend_budget": await store.get_active_spend_budget(project_id),
            "spend_budget_proposals": await store.list_spend_budget_proposals(project_id),
            "spend_budget_impact_previews": (
                await store.list_spend_budget_impact_previews(project_id)
            ),
            "conversation": {
                "active_session": active_conversation_session,
                "recent_turns": recent_conversation_turns,
                "open_clarifications": open_conversation_clarifications,
                "open_action_proposals": open_conversation_action_proposals,
                "context_summaries": active_context_summaries,
            },
        }
        event_cursor = await session.scalar(
            select(func.max(DomainEventRow.project_seq)).where(
                DomainEventRow.project_id == project_id
            )
        )
        snapshot["workspace"] = build_workspace_projection(
            snapshot,
            event_cursor=event_cursor or 0,
        )
        snapshot["projection_version"] = _PROJECT_SNAPSHOT_PROJECTION_VERSION
        snapshot["event_cursor"] = event_cursor or 0
        return snapshot

    @api.get("/api/projects/{project_id}/workspace")
    async def project_workspace(project_id: UUID, session: DbSession) -> Any:
        """Return the stable user-facing projection without exposing runtime internals."""

        snapshot = await project_snapshot(project_id, session)
        return snapshot["workspace"]

    @api.get("/api/projects/{project_id}/events")
    async def list_events(
        project_id: UUID,
        session: DbSession,
        after: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if not 0 <= after or not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="invalid event cursor or limit")
        store = PostgresDomainStore(session)
        if await store.get_project(project_id) is None:
            raise DomainNotFoundError("project not found")
        rows = await store.list_events(project_id, after_sequence=after, limit=limit)
        return [_project_event_payload(project_id, row) for row in rows]

    @api.get("/api/projects/{project_id}/events/stream")
    async def stream_events(
        project_id: UUID,
        request: Request,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
        # Do not bind the request-scoped DbSession to this unbounded response.
        # FastAPI only closes dependencies after StreamingResponse finishes;
        # keeping a read transaction open here can block LangGraph Saver DDL
        # and prevent an otherwise ready worker from claiming a Run.
        async with factory() as initial_session:
            if await PostgresDomainStore(initial_session).get_project(project_id) is None:
                raise DomainNotFoundError("project not found")
        initial_cursor = _parse_cursor(last_event_id, project_id)

        async def event_source() -> AsyncIterator[str]:
            cursor = initial_cursor
            while True:
                async with factory() as event_session:
                    rows = await PostgresDomainStore(event_session).list_events(
                        project_id,
                        after_sequence=cursor,
                        limit=200,
                    )
                if rows:
                    for row in rows:
                        cursor = row.project_seq
                        data = json.dumps(
                            _project_event_payload(project_id, row, encode_datetime=True),
                            separators=(",", ":"),
                        )
                        yield (
                            f"id: {project_id}:{row.project_seq}\n"
                            f"event: {row.event_type}\n"
                            f"data: {data}\n\n"
                        )
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── V1 Shopping routes ──────────────────────────────────────────────

    @api.post("/api/projects/{project_id}/shopping/offers/search")
    async def search_offers(
        project_id: UUID,
        body: SearchOffersRequest,
        if_match: IfMatch,
        session: DbSession,
    ) -> Any:
        revision = _parse_revision(if_match)
        snapshots = await _shopping_app(session).search_offers(
            project_id=project_id,
            expected_project_revision=revision,
            query=body.query,
            bom_line_id=body.bom_line_id,
            region=body.region,
            max_results=body.max_results,
        )
        return snapshots  # Return the array directly — no raw_offers

    @api.post("/api/projects/{project_id}/shopping/budget-summary")
    async def preview_shopping_budget(
        project_id: UUID,
        body: PreviewShoppingBudgetRequest,
        session: DbSession,
    ) -> Any:
        """Read-only summary of selected offer snapshots against Project budget."""
        summary = await _shopping_app(session).preview_project_spend_budget(
            project_id=project_id,
            selections=tuple(
                ShoppingBudgetSelection(
                    offer_snapshot_id=item.offer_snapshot_id,
                    quantity=item.quantity,
                )
                for item in body.selections
            ),
        )
        return summary

    @api.get("/api/projects/{project_id}/shopping/recommendations/{bom_line_id}")
    async def recommend_shopping_offers(
        project_id: UUID,
        bom_line_id: str,
        session: DbSession,
        limit: int = 10,
    ) -> Any:
        """Return explainable, non-binding rankings from saved offer snapshots."""
        return await _shopping_app(session).recommend_offer_snapshots(
            project_id=project_id,
            bom_line_id=bom_line_id,
            limit=limit,
        )

    @api.post("/api/projects/{project_id}/purchase-proposals", status_code=status.HTTP_201_CREATED)
    async def create_purchase_proposal(
        project_id: UUID,
        body: CreatePurchaseProposalRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        if not enable_legacy_purchase_writes:
            return _shopping_write_disabled()
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        app = _shopping_app(session)

        snapshot = await store.get_offer_snapshot(body.offer_snapshot_id)
        if snapshot is None:
            raise DomainNotFoundError("offer snapshot not found")
        if snapshot.project_id != project_id:
            raise DomainConflictError("offer snapshot belongs to another project")

        proposal = await app.create_purchase_proposal(
            project_id=project_id,
            expected_project_revision=revision,
            solution_version_id=body.solution_version_id,
            offer_snapshot=snapshot,
            quantity=body.quantity,
            region=body.region,
            currency=body.currency,
            shipping_estimate=body.shipping_estimate,
            tax_estimate=body.tax_estimate,
            max_total=body.max_total,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return proposal

    @api.post("/api/purchase-proposals/{proposal_id}/confirm-lines")
    async def confirm_proposal_lines(
        proposal_id: UUID,
        body: ConfirmLinesRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        if not enable_legacy_purchase_writes:
            return _shopping_write_disabled()
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        proposal = await store.get_purchase_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("purchase proposal not found")

        result = await _shopping_app(session).confirm_proposal_lines(
            proposal_id=proposal_id,
            expected_proposal_basis=proposal.basis_hash,
            confirmed_line_ids=body.confirmed_line_ids,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return result

    @api.post(
        "/api/purchase-proposals/{proposal_id}/effect-approvals",
        status_code=status.HTTP_201_CREATED,
    )
    async def request_effect_approval_route(
        proposal_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        if not enable_legacy_purchase_writes:
            return _shopping_write_disabled()
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        proposal = await store.get_purchase_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("purchase proposal not found")
        approval = await _shopping_app(session).request_effect_approval(
            proposal_id=proposal_id,
            expected_proposal_basis=proposal.basis_hash,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        await _set_current_project_etag(response, store, approval.project_id)
        return approval

    @api.post("/api/effect-approvals/{approval_id}/resolve")
    async def resolve_effect_approval_route(
        approval_id: UUID,
        body: ResolveEffectApprovalRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        if not enable_legacy_purchase_writes:
            return _shopping_write_disabled()
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        approval = await _shopping_app(session).resolve_effect_approval(
            approval_id=approval_id,
            decision=EffectApprovalStatus(body.decision),
            scope_hash=body.scope_hash,
            reason=body.reason,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        await _set_current_project_etag(response, store, approval.project_id)
        return approval

    @api.post(
        "/api/purchase-proposals/{proposal_id}/checkout-handoffs",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_checkout_handoff_route(
        proposal_id: UUID,
        body: CreateCheckoutHandoffRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
        if not enable_legacy_purchase_writes:
            return _shopping_write_disabled()
        revision = _parse_revision(if_match)
        store = PostgresDomainStore(session)
        proposal = await store.get_purchase_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("purchase proposal not found")

        handoff = await _shopping_app(session).create_checkout_handoff(
            proposal_id=proposal_id,
            effect_approval_id=body.effect_approval_id,
            expected_proposal_basis=proposal.basis_hash,
            expected_project_revision=revision,
            idempotency_key=idempotency_key,
        )
        await _set_current_project_etag(response, store, handoff.project_id)
        return handoff

    @api.get("/api/projects/{project_id}/artifacts/{artifact_id}/content")
    async def get_artifact_content(
        project_id: UUID,
        artifact_id: UUID,
        request: Request,
        session: DbSession,
    ) -> Any:
        """Read artifact bytes with content-hash verification.

        Only text/markdown and application/json media types are served
        inline.  All others return 415.
        """
        from aidison.infrastructure.artifacts import (
            ArtifactIntegrityError,
            ArtifactNotFoundError,
            ContentAddressedArtifactStore,
        )
        from aidison.infrastructure.orm import ArtifactRow

        row = await session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.id == artifact_id,
                ArtifactRow.project_id == project_id,
            )
        )
        if row is None:
            raise DomainNotFoundError("artifact not found")

        allowed = {"text/markdown", "application/json", "text/plain"}
        if row.media_type not in allowed:
            raise HTTPException(
                status_code=415,
                detail="artifact media type is not supported for inline content",
            )

        root = Path(getattr(request.app.state, "artifact_root", "/data/artifacts"))
        store = ContentAddressedArtifactStore(session, root)
        try:
            content = await store.read_bytes(project_id=project_id, artifact_id=artifact_id)
        except ArtifactNotFoundError as exc:
            raise DomainNotFoundError(str(exc)) from exc
        except ArtifactIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return Response(
            content=content,
            media_type=row.media_type,
            headers={
                "Content-Disposition": f'inline; filename="{artifact_id}"',
                "X-Content-Hash": row.content_hash,
            },
        )

    @api.get("/api/projects/{project_id}/artifacts/{artifact_id}")
    async def get_artifact(
        project_id: UUID,
        artifact_id: UUID,
        session: DbSession,
    ) -> Any:
        """Read artifact metadata (content retrieval is via artifact+sha256 ref)."""
        from aidison.infrastructure.orm import ArtifactRow

        row = await session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.id == artifact_id,
                ArtifactRow.project_id == project_id,
            )
        )
        if row is None:
            raise DomainNotFoundError("artifact not found")
        return {
            "id": row.id,
            "project_id": row.project_id,
            "kind": row.kind,
            "content_hash": row.content_hash,
            "size_bytes": row.size_bytes,
            "media_type": row.media_type,
            "status": row.status,
            "source_url": row.source_url,
            "created_at": row.created_at,
        }

    _register_spend_budget_endpoints(api)
    _register_conversation_endpoints(
        api,
        conversation_model_factory,
        api.state.conversation_settings,
        api.state.research_strategy_planner_factory,
    )

    return api


# ── Conversation endpoints ───────────────────────────────────────────────────


def _register_spend_budget_endpoints(api: FastAPI) -> None:
    from aidison.application.service import ProjectApplication
    from aidison.domain.models import (
        SpendBudgetCostItem,
        SpendBudgetProposalStatus,
    )

    @api.get("/api/projects/{project_id}/spend-budget")
    async def get_active_spend_budget(
        project_id: UUID,
        session: DbSession,
    ) -> dict[str, Any] | None:
        """Return the active SpendBudget revision, or null."""
        store = PostgresDomainStore(session)
        active = await store.get_active_spend_budget(project_id)
        if active is None:
            return {}
        return {"spend_budget": active.model_dump(mode="json")}

    @api.get("/api/projects/{project_id}/spend-budget/proposals")
    async def list_spend_budget_proposals(
        project_id: UUID,
        session: DbSession,
    ) -> dict[str, Any]:
        """List all SpendBudgetProposals for this project."""
        store = PostgresDomainStore(session)
        proposals = await store.list_spend_budget_proposals(project_id)
        return {"spend_budget_proposals": [p.model_dump(mode="json") for p in proposals]}

    @api.post("/api/projects/{project_id}/spend-budget/proposals")
    async def propose_spend_budget(
        project_id: UUID,
        body: ProposeSpendBudgetRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        session: DbSession,
    ) -> dict[str, Any]:
        """Create a SpendBudgetProposal from the workbench.

        This never applies the budget or changes the active revision.
        """
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)
        revision = _parse_revision(if_match)
        try:
            proposal = await app.propose_spend_budget(
                project_id=project_id,
                amount=body.amount,
                currency=body.currency,
                summary=body.summary,
                expected_project_revision=revision,
                idempotency_key=idempotency_key,
            )
            await session.commit()
        except PreconditionFailedError as exc:
            await session.rollback()
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except DomainConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DomainNotFoundError as exc:
            await session.rollback()
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"spend_budget_proposal": proposal.model_dump(mode="json")}

    @api.get(
        "/api/spend-budget-proposals/{proposal_id}/impact-preview",
    )
    async def get_spend_budget_impact_preview(
        proposal_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        session: DbSession,
        cost_items: str | None = None,
    ) -> dict[str, Any]:
        """Compute a read-only budget-impact preview.

        Accepts ``cost_items`` as a JSON-encoded list of objects with
        ``ref``, ``amount``, ``currency``, and optional ``observed_at``.
        The preview never modifies SelectionLock, freeze plans, or product
        state.
        """
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)
        revision = _parse_revision(if_match)

        parsed_items: list[SpendBudgetCostItem] = []
        if cost_items:
            try:
                raw = json.loads(cost_items)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422, detail="cost_items must be valid JSON"
                ) from exc
            if not isinstance(raw, list):
                raise HTTPException(status_code=422, detail="cost_items must be a JSON array")
            for item in raw:
                try:
                    observed_at_raw = item.get("observed_at")
                    observed_at: datetime | None = None
                    if observed_at_raw is not None:
                        observed_at = datetime.fromisoformat(
                            str(observed_at_raw).replace("Z", "+00:00")
                        )
                    parsed_items.append(
                        SpendBudgetCostItem(
                            ref=str(item["ref"]),
                            amount=str(item.get("amount", "")),
                            currency=str(item.get("currency", "")),
                            observed_at=observed_at,
                        )
                    )
                except (KeyError, ValueError, TypeError) as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"invalid cost item: {exc}",
                    ) from exc

        try:
            preview = await app.preview_spend_budget_impact(
                proposal_id=proposal_id,
                cost_items=parsed_items,
                expected_project_revision=revision,
                idempotency_key=idempotency_key,
            )
            await session.commit()
        except PreconditionFailedError as exc:
            await session.rollback()
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except DomainConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DomainNotFoundError as exc:
            await session.rollback()
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"spend_budget_impact_preview": preview.model_dump(mode="json")}

    @api.post("/api/spend-budget-proposals/{proposal_id}/resolve")
    async def resolve_spend_budget_proposal(
        proposal_id: UUID,
        body: ResolveSpendBudgetRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        session: DbSession,
    ) -> dict[str, Any]:
        """Resolve a SpendBudgetProposal (approve or reject).

        APPLIED creates a SpendBudgetRevision, bumps project.revision,
        and supersedes the prior active revision.
        REJECTED marks the proposal rejected without changing the project.
        """
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)
        revision = _parse_revision(if_match)
        decision = (
            SpendBudgetProposalStatus.APPLIED
            if body.decision == "applied"
            else SpendBudgetProposalStatus.REJECTED
        )
        try:
            result = await app.resolve_spend_budget(
                proposal_id=proposal_id,
                decision=decision,
                expected_project_revision=revision,
                idempotency_key=idempotency_key,
            )
            await session.commit()
        except PreconditionFailedError as exc:
            await session.rollback()
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except DomainConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DomainNotFoundError as exc:
            await session.rollback()
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"spend_budget_revision": result.model_dump(mode="json")}


def _register_conversation_endpoints(
    api: FastAPI,
    conversation_model_factory: Callable[[], BaseChatModel] | None = None,
    conversation_settings: ConversationSettings | None = None,
    research_strategy_planner_factory: Callable[[], ResearchStrategyPlanner] | None = None,
) -> None:
    from aidison.application.conversation import (
        ConversationApplication,
        ConversationConflictError,
        ConversationNotFoundError,
        ConversationPreconditionFailedError,
    )
    from aidison.providers.gateway import ProviderSettings, build_chat_model

    # Use provided factory, or default to DeepSeek provider
    settings = conversation_settings or ConversationSettings()
    if conversation_model_factory is not None:
        _conv_factory = conversation_model_factory
    else:

        def _conv_factory() -> Any:
            return build_chat_model(
                ProviderSettings(),
                temperature=settings.model_temperature,
                max_tokens=settings.max_tokens_per_turn,
            )

    def _make_app(session: AsyncSession) -> ConversationApplication:
        return ConversationApplication(
            store_factory=lambda: PostgresDomainStore(session),
            model_factory=_conv_factory,
            research_strategy_planner=(
                partial(
                    ResearchStrategyPlanningService(
                        planner_factory=research_strategy_planner_factory
                        if research_strategy_planner_factory is not None
                        else lambda: JsonModeResearchStrategyPlanner(_conv_factory())
                    ).propose,
                    planning_mode="hierarchical",
                )
            ),
            max_turns=settings.max_turns,
        )

    @api.post("/api/projects/{project_id}/conversation/messages")
    async def post_conversation_message(
        project_id: UUID,
        body: PostConversationMessageRequest,
        idempotency_key: IdempotencyKey,
        session: DbSession,
    ) -> dict[str, Any]:
        """Submit a user message. Does NOT bump Project.revision."""
        app = _make_app(session)
        try:
            result = await app.post_message(
                project_id=project_id,
                content=body.content,
                idempotency_key=idempotency_key,
                session_id=body.session_id,
            )
            await session.commit()
        except ConversationNotFoundError as exc:
            await session.rollback()
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConversationPreconditionFailedError as exc:
            await session.rollback()
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ConversationConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            await session.rollback()
            raise
        return _serialize_conversation_result(result)

    @api.get("/api/projects/{project_id}/conversation/sessions")
    async def list_conversation_sessions(
        project_id: UUID,
        session: DbSession,
    ) -> dict[str, Any]:
        app = _make_app(session)
        sessions = await app.list_sessions(project_id)
        return {"sessions": [s.model_dump(mode="json") for s in sessions]}

    @api.get("/api/projects/{project_id}/conversation/sessions/{session_id}/turns")
    async def list_conversation_turns(
        project_id: UUID,
        session_id: UUID,
        session: DbSession,
        after: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        store = PostgresDomainStore(session)
        conversation_session = await store.get_conversation_session(session_id)
        if conversation_session is None or conversation_session.project_id != project_id:
            raise DomainNotFoundError("conversation session not found")
        turns = await store.list_conversation_turns(session_id, after=after, limit=limit + 1)
        has_more = len(turns) > limit
        page = turns[:limit]
        return {
            "turns": [t.model_dump(mode="json") for t in page],
            "has_more": has_more,
        }

    @api.post("/api/projects/{project_id}/conversation/proposals/{proposal_id}/accept")
    async def accept_conversation_action_proposal(
        project_id: UUID,
        proposal_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        session: DbSession,
    ) -> dict[str, Any]:
        """Accept an action proposal after If-Match + secondary payload validation."""
        revision = _parse_revision(if_match)
        app = _make_app(session)
        try:
            result = await app.accept_action_proposal(
                proposal_id=proposal_id,
                project_id=project_id,
                expected_project_revision=revision,
            )
            await session.commit()
        except ConversationNotFoundError as exc:
            await session.rollback()
            raise HTTPException(status_code=404, detail="action proposal not found") from exc
        except ConversationPreconditionFailedError as exc:
            await session.rollback()
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ConversationConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            await session.rollback()
            # A persisted proposal can be stale or model-produced, so this is
            # still client-correctable input validation rather than a server
            # failure. Keep its contract aligned with other API validation.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "proposal": result["proposal"].model_dump(mode="json"),
            "result_ref": result["result_ref"],
        }

    @api.post("/api/projects/{project_id}/conversation/proposals/{proposal_id}/reject")
    async def reject_conversation_action_proposal(
        project_id: UUID,
        proposal_id: UUID,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        session: DbSession,
    ) -> dict[str, Any]:
        """Reject an action proposal without executing its command.

        Scoped to the project, guarded by If-Match against the project
        revision, and replayed through Idempotency-Key like accept.  Rejection
        never dispatches a command, creates a job, or touches SelectionLock /
        budget state — it only persists the REJECTED status, a resolution
        turn, and an audit event.
        """
        revision = _parse_revision(if_match)
        app = _make_app(session)
        try:
            result = await app.reject_action_proposal(
                proposal_id=proposal_id,
                project_id=project_id,
                expected_project_revision=revision,
                idempotency_key=idempotency_key,
            )
            await session.commit()
        except ConversationNotFoundError as exc:
            await session.rollback()
            raise HTTPException(status_code=404, detail="action proposal not found") from exc
        except ConversationPreconditionFailedError as exc:
            await session.rollback()
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ConversationConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "proposal": result["proposal"].model_dump(mode="json"),
            "result_ref": result["result_ref"],
        }

    @api.post("/api/projects/{project_id}/conversation/clarifications/{clarification_id}/resolve")
    async def resolve_conversation_clarification(
        project_id: UUID,
        clarification_id: UUID,
        body: ResolveConversationClarificationRequest,
        idempotency_key: IdempotencyKey,
        session: DbSession,
    ) -> dict[str, Any]:
        """Resolve a clarification, then continue the governed dialogue.

        The user's answer is persisted as a user turn plus the
        ``conversation.clarification_resolved`` event before any provider
        call. If another item in the existing clarification batch is pending,
        the response is a deterministic acknowledgement; otherwise the active
        session runs one bounded-context, no-tool, JSON-mode conversation turn.
        A provider failure yields the deterministic fallback. Replaying the
        same Idempotency-Key returns the already-committed answer and follow-up
        without duplicates, and Project.revision never changes here.
        """
        app = _make_app(session)
        try:
            result = await app.resolve_clarification_and_continue(
                project_id=project_id,
                clarification_id=clarification_id,
                response=body.response,
                idempotency_key=idempotency_key,
            )
            await session.commit()
        except ConversationNotFoundError as exc:
            await session.rollback()
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConversationPreconditionFailedError as exc:
            await session.rollback()
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except ConversationConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            await session.rollback()
            raise
        return {
            "clarification_id": str(clarification_id),
            "status": "resolved",
            "user_turn": result["user_turn"].model_dump(mode="json"),
            "assistant_turn": (
                result["assistant_turn"].model_dump(mode="json")
                if result["assistant_turn"] is not None
                else None
            ),
            "session_id": result["session_id"],
        }

    @api.post("/api/projects/{project_id}/conversation/sessions/new")
    async def new_conversation_session(
        project_id: UUID,
        idempotency_key: IdempotencyKey,
        session: DbSession,
    ) -> dict[str, Any]:
        app = _make_app(session)
        new_session = await app.new_session(project_id)
        await session.commit()
        return new_session.model_dump(mode="json")

    @api.post("/api/projects/{project_id}/conversation/sessions/{session_id}/close")
    async def close_conversation_session(
        project_id: UUID,
        session_id: UUID,
        idempotency_key: IdempotencyKey,
        session: DbSession,
    ) -> dict[str, Any]:
        app = _make_app(session)
        store = PostgresDomainStore(session)
        conversation_session = await store.get_conversation_session(session_id)
        if conversation_session is None or conversation_session.project_id != project_id:
            raise DomainNotFoundError("conversation session not found")
        try:
            closed = await app.close_session(session_id)
            await session.commit()
        except DomainConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="session not active") from exc
        return closed.model_dump(mode="json")


def _serialize_conversation_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_turn": result["user_turn"].model_dump(mode="json"),
        "assistant_turn": (
            result["assistant_turn"].model_dump(mode="json")
            if result["assistant_turn"] is not None
            else None
        ),
        "session_id": result["session_id"],
    }


# ── Module-level app ─────────────────────────────────────────────────────────


def build_default_shopping_provider() -> ShoppingProvider | None:
    """Compose the runtime shopping provider from non-secret config + env secrets.

    Provider selection (``shopping.provider``) and Taobao non-secrets
    (``adzone_id``, timeouts, result caps) come from config.yaml.  Secrets
    (TAOBAO_APP_KEY, TAOBAO_APP_SECRET) come from the process environment
    only.  Taobao is wired search-only: ``available`` is False until both
    secrets and ``adzone_id`` are present, and its capabilities declare no
    handoff kinds.
    """
    provider_name = ShoppingSettings().provider.strip().lower()
    if provider_name != "taobao":
        return None
    settings = TaobaoSettings()
    return TaobaoAffiliateAdapter(
        app_key=os.getenv("TAOBAO_APP_KEY"),
        app_secret=os.getenv("TAOBAO_APP_SECRET"),
        adzone_id=settings.adzone_id or None,
        search_timeout_seconds=settings.search_timeout_seconds,
        max_search_results=settings.max_search_results,
    )


app = create_app(shopping_provider=build_default_shopping_provider())
