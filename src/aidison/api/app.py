from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.agents.profiles import RESEARCH_WORKER_PROFILE
from aidison.api.schemas import (
    ApprovePatchRequest,
    ApproveRequirementsRequest,
    ConfirmLinesRequest,
    CreateCheckoutHandoffRequest,
    CreateProjectRequest,
    CreatePurchaseProposalRequest,
    FreezeSolutionRequest,
    ResearchProposalRequest,
    ResolveDecisionRequest,
    ResolveEffectApprovalRequest,
    SearchOffersRequest,
    SubmitObservationRequest,
)
from aidison.application.ports import DuplicateCommandError, OptimisticConcurrencyError
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
from aidison.application.workspace import build_workspace_projection
from aidison.domain.models import DecisionRequest as DomainDecisionRequest
from aidison.domain.models import EffectApprovalStatus, ImpactAnalysis
from aidison.infrastructure.database import create_session_factory
from aidison.infrastructure.orm import (
    AttemptRow,
    BudgetAccountRow,
    BudgetAllocationRow,
    BudgetOperationRow,
    DecisionRequestRow,
    DelegationRow,
    DomainEventRow,
    ImpactAnalysisRow,
    JobRow,
    JoinGroupRow,
)
from aidison.infrastructure.runtime import (
    PostgresRuntime,
    RuntimeConflictError,
    RuntimeNotFoundError,
)
from aidison.infrastructure.store import PostgresDomainStore
from aidison.providers.shopping import ShoppingConfigError, ShoppingProvider
from aidison.runtime.contracts import MAX_DELEGATION_WAVE_SIZE

SessionDependency = Annotated[AsyncSession, Depends()]


def _error(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status_code, content=payload)


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
    if encode_datetime:
        created_at = row.created_at.isoformat()
    return {
        "id": f"{project_id}:{row.project_seq}",
        "sequence": row.project_seq,
        "type": row.event_type,
        "payload": row.payload,
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
) -> FastAPI:
    api = FastAPI(title="Aidison API", version="0.1.0")
    api.state.session_factory = session_factory or create_session_factory()
    api.state.shopping_provider = shopping_provider
    api.state.artifact_root = artifact_root or Path("artifacts/data")
    api.state.effect_approval_ttl_seconds = (
        effect_approval_ttl_seconds
        if effect_approval_ttl_seconds is not None
        else ShoppingSettings().effect_approval_ttl_seconds
    )

    @api.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, "validation_error", "request validation failed", exc.errors())

    @api.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, "http_error", str(exc.detail))

    @api.exception_handler(DomainNotFoundError)
    async def not_found(_: Request, exc: DomainNotFoundError) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @api.exception_handler(PreconditionFailedError)
    @api.exception_handler(OptimisticConcurrencyError)
    async def precondition_failed(_: Request, exc: Exception) -> JSONResponse:
        return _error(412, "precondition_failed", str(exc))

    @api.exception_handler(DuplicateCommandError)
    async def duplicate_command(_: Request, exc: DuplicateCommandError) -> JSONResponse:
        return _error(409, "idempotency_conflict", str(exc))

    @api.exception_handler(DomainConflictError)
    @api.exception_handler(RuntimeConflictError)
    async def domain_conflict(
        _: Request,
        exc: DomainConflictError | RuntimeConflictError,
    ) -> JSONResponse:
        return _error(409, "domain_conflict", str(exc))

    @api.exception_handler(RuntimeNotFoundError)
    async def runtime_not_found(_: Request, exc: RuntimeNotFoundError) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @api.exception_handler(ShoppingConfigError)
    async def shopping_unavailable(_: Request, exc: ShoppingConfigError) -> JSONResponse:
        return _error(503, "shopping_unavailable", str(exc))

    @api.exception_handler(OfferSearchError)
    async def offer_search_failed(_: Request, exc: OfferSearchError) -> JSONResponse:
        return _error(502, "offer_search_failed", str(exc))

    @api.exception_handler(CartCreationError)
    async def cart_creation_failed(_: Request, exc: CartCreationError) -> JSONResponse:
        return _error(502, "cart_creation_failed", str(exc))

    @api.exception_handler(IntegrityError)
    async def persistence_conflict(_: Request, __: IntegrityError) -> JSONResponse:
        return _error(409, "persistence_conflict", "canonical write violates a constraint")

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/api/integration-health")
    async def integration_health(request: Request) -> dict[str, Any]:
        provider = getattr(request.app.state, "shopping_provider", None)
        return {
            "status": "ok",
            "shopping": {
                "provider": provider.name if provider else "none",
                "available": provider.available if provider else False,
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

    async def _set_current_project_etag(
        response: Response,
        store: PostgresDomainStore,
        project_id: UUID,
    ) -> None:
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found after shopping command")
        response.headers["ETag"] = f'"{project.revision}"'

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
        requirement, modules = await ProjectApplication(
            PostgresDomainStore(session)
        ).approve_requirements(
            project_id=project_id,
            expected_project_revision=revision,
            goal=body.goal,
            hard_constraints=body.hard_constraints,
            preferences=body.preferences,
            available_resources=body.available_resources,
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

    @api.get("/api/projects/{project_id}/modules")
    async def list_modules(project_id: UUID, session: DbSession) -> Any:
        store = PostgresDomainStore(session)
        project = await store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        return await store.list_modules(
            project_id,
            requirement_revision_id=project.active_requirement_revision_id,
        )

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
        "/api/projects/{project_id}/research-runs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_research_run(
        project_id: UUID,
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
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("requirements must be approved before research")
        modules = tuple(
            await store.list_modules(
                project_id,
                requirement_revision_id=project.active_requirement_revision_id,
            )
        )
        if not modules:
            raise DomainConflictError("research requires at least one active module")
        basis_hash = canonical_hash(project.active_requirement_revision_id, modules)
        child_count = min(
            len(modules),
            RESEARCH_WORKER_PROFILE.concurrency_cap,
            MAX_DELEGATION_WAVE_SIZE,
        )
        job_id = await PostgresRuntime(session).create_job(
            project_id=project_id,
            kind="research_wave",
            basis_hash=basis_hash,
            basis_project_revision=revision,
            profile_id="research-orchestrator",
            profile_revision=1,
            idempotency_key=f"research-run:{idempotency_key}",
            token_budget_cap=child_count * RESEARCH_WORKER_PROFILE.token_cap,
            tool_call_budget_cap=child_count * RESEARCH_WORKER_PROFILE.tool_call_cap,
        )
        response.headers["ETag"] = f'"{revision}"'
        return {
            "job_id": job_id,
            "status": "queued",
            "basis_hash": basis_hash,
            "project_revision": revision,
        }

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
        decision = await ProjectApplication(PostgresDomainStore(session)).resolve_decision(
            decision_id=decision_id,
            expected_project_revision=revision,
            selected_option_id=body.selected_option_id,
            basis_hash=body.basis_hash,
            idempotency_key=idempotency_key,
        )
        solution_job_id = await PostgresRuntime(session).create_job(
            project_id=decision.project_id,
            kind="solution_wave",
            basis_hash=canonical_hash(decision),
            basis_project_revision=revision + 1,
            profile_id="solution-orchestrator",
            profile_revision=1,
            idempotency_key=f"solution-run:{decision.id}",
            request_payload={"decision_id": str(decision.id)},
            token_budget_cap=16_000,
            tool_call_budget_cap=0,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {
            "decision": decision,
            "solution_job_id": solution_job_id,
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
        observation = await ProjectApplication(PostgresDomainStore(session)).submit_observation(
            project_id=project_id,
            expected_project_revision=revision,
            statement=body.statement,
            affected_module_ids=body.affected_module_ids,
            idempotency_key=idempotency_key,
        )
        impact_job_id = await PostgresRuntime(session).create_job(
            project_id=project_id,
            kind="impact_wave",
            basis_hash=canonical_hash(observation),
            basis_project_revision=revision + 1,
            profile_id="impact-orchestrator",
            profile_revision=1,
            idempotency_key=f"impact-run:{observation.id}",
            request_payload={"observation_id": str(observation.id)},
            token_budget_cap=16_000,
            tool_call_budget_cap=0,
        )
        response.headers["ETag"] = f'"{revision + 1}"'
        return {
            "observation": observation,
            "impact_job_id": impact_job_id,
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
        jobs = list(
            await session.scalars(
                select(JobRow)
                .where(JobRow.project_id == project_id)
                .order_by(JobRow.created_at, JobRow.id)
            )
        )
        job_ids = [item.id for item in jobs]
        attempts = (
            list(
                await session.scalars(
                    select(AttemptRow)
                    .where(AttemptRow.job_id.in_(job_ids))
                    .order_by(AttemptRow.started_at, AttemptRow.id)
                )
            )
            if job_ids
            else []
        )
        delegations = (
            list(
                await session.scalars(
                    select(DelegationRow)
                    .where(DelegationRow.parent_job_id.in_(job_ids))
                    .order_by(DelegationRow.created_at, DelegationRow.id)
                )
            )
            if job_ids
            else []
        )
        join_groups = (
            list(
                await session.scalars(
                    select(JoinGroupRow)
                    .where(JoinGroupRow.parent_job_id.in_(job_ids))
                    .order_by(JoinGroupRow.created_at, JoinGroupRow.id)
                )
            )
            if job_ids
            else []
        )
        budget_accounts = list(
            await session.scalars(
                select(BudgetAccountRow)
                .where(BudgetAccountRow.project_id == project_id)
                .order_by(BudgetAccountRow.created_at, BudgetAccountRow.id)
            )
        )
        account_ids = [item.id for item in budget_accounts]
        budget_allocations = (
            list(
                await session.scalars(
                    select(BudgetAllocationRow)
                    .where(BudgetAllocationRow.account_id.in_(account_ids))
                    .order_by(BudgetAllocationRow.created_at, BudgetAllocationRow.id)
                )
            )
            if account_ids
            else []
        )
        allocation_ids = [item.id for item in budget_allocations]
        budget_operations = (
            list(
                await session.scalars(
                    select(BudgetOperationRow)
                    .where(BudgetOperationRow.allocation_id.in_(allocation_ids))
                    .order_by(BudgetOperationRow.created_at, BudgetOperationRow.id)
                )
            )
            if allocation_ids
            else []
        )
        snapshot: dict[str, Any] = {
            "project": project,
            "requirements": await store.list_requirement_revisions(project_id),
            "modules": await store.list_modules(
                project_id,
                requirement_revision_id=project.active_requirement_revision_id,
            ),
            "evidence": await store.list_evidence_bindings(project_id),
            "candidates": await store.list_candidates(project_id),
            "compatibility_findings": await store.list_compatibility_findings(project_id),
            "decisions": [DomainDecisionRequest.model_validate(item.payload) for item in decisions],
            "solution_proposals": await store.list_solution_proposals(project_id),
            "solutions": await store.list_solution_versions(project_id),
            "observations": await store.list_observations(project_id),
            "impacts": [ImpactAnalysis.model_validate(item.payload) for item in impacts],
            "patch_sets": await store.list_patch_sets(project_id),
            "offer_snapshots": await store.list_offer_snapshots(project_id),
            "purchase_proposals": await store.list_purchase_proposals(project_id),
            "effect_approvals": await store.list_effect_approvals(project_id),
            "checkout_handoffs": await store.list_checkout_handoffs(project_id),
            "runtime": {
                "jobs": [
                    {
                        "id": item.id,
                        "parent_job_id": item.parent_job_id,
                        "kind": item.kind,
                        "status": item.status,
                        "profile_id": item.profile_id,
                        "profile_revision": item.profile_revision,
                        "basis_project_revision": item.basis_project_revision,
                        "generation": item.current_generation,
                        "created_at": item.created_at,
                        "completed_at": item.completed_at,
                    }
                    for item in jobs
                ],
                "attempts": [
                    {
                        "id": item.id,
                        "job_id": item.job_id,
                        "number": item.number,
                        "generation": item.claim_generation,
                        "status": item.status,
                        "started_at": item.started_at,
                        "completed_at": item.completed_at,
                        "normalized_error": item.normalized_error,
                    }
                    for item in attempts
                ],
                "delegations": [
                    {
                        "id": item.id,
                        "parent_job_id": item.parent_job_id,
                        "child_job_id": item.child_job_id,
                        "join_group_id": item.join_group_id,
                        "profile_id": item.profile_id,
                        "profile_revision": item.profile_revision,
                        "shard_key": item.shard_key,
                        "status": item.status,
                    }
                    for item in delegations
                ],
                "join_groups": [
                    {
                        "id": item.id,
                        "parent_job_id": item.parent_job_id,
                        "status": item.status,
                        "expected_count": item.expected_count,
                        "created_at": item.created_at,
                    }
                    for item in join_groups
                ],
                "budget_accounts": [
                    {
                        "id": item.id,
                        "root_job_id": item.root_job_id,
                        "status": item.status,
                        "token_cap": item.token_cap,
                        "token_committed": item.token_committed,
                        "tool_call_cap": item.tool_call_cap,
                        "tool_calls_committed": item.tool_calls_committed,
                    }
                    for item in budget_accounts
                ],
                "budget_allocations": [
                    {
                        "id": item.id,
                        "account_id": item.account_id,
                        "owner_kind": item.owner_kind,
                        "owner_ref": item.owner_ref,
                        "status": item.status,
                        "token_grant": item.token_grant,
                        "token_reserved": item.token_reserved,
                        "token_consumed": item.token_consumed,
                        "tool_call_grant": item.tool_call_grant,
                        "tool_calls_reserved": item.tool_calls_reserved,
                        "tool_calls_consumed": item.tool_calls_consumed,
                    }
                    for item in budget_allocations
                ],
                "budget_operations": [
                    {
                        "id": item.id,
                        "allocation_id": item.allocation_id,
                        "kind": item.kind,
                        "state": item.state,
                        "logical_step": item.logical_step,
                        "provider": item.provider,
                        "model_or_tool": item.model_or_tool,
                        "reserved_tokens": item.reserved_tokens,
                        "consumed_tokens": item.consumed_tokens,
                        "reserved_tool_calls": item.reserved_tool_calls,
                        "consumed_tool_calls": item.consumed_tool_calls,
                        "created_at": item.created_at,
                    }
                    for item in budget_operations
                ],
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
        session: DbSession,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        store = PostgresDomainStore(session)
        if await store.get_project(project_id) is None:
            raise DomainNotFoundError("project not found")
        initial_cursor = _parse_cursor(last_event_id, project_id)
        factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)

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

    @api.post("/api/projects/{project_id}/purchase-proposals", status_code=status.HTTP_201_CREATED)
    async def create_purchase_proposal(
        project_id: UUID,
        body: CreatePurchaseProposalRequest,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
        response: Response,
        session: DbSession,
    ) -> Any:
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

    @api.get("/api/artifacts/{artifact_id}/content")
    async def get_artifact_content(
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

        row = await session.scalar(select(ArtifactRow).where(ArtifactRow.id == artifact_id))
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
            content = await store.read_bytes(project_id=row.project_id, artifact_id=artifact_id)
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

    @api.get("/api/artifacts/{artifact_id}")
    async def get_artifact(
        artifact_id: UUID,
        session: DbSession,
    ) -> Any:
        """Read artifact metadata (content retrieval is via artifact+sha256 ref)."""
        from aidison.infrastructure.orm import ArtifactRow

        row = await session.scalar(select(ArtifactRow).where(ArtifactRow.id == artifact_id))
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

    return api


app = create_app()
