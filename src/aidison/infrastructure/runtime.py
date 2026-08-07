from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.agents.profiles import (
    BUILTIN_AGENT_PROFILES,
    IMPACT_PROPOSER_PROFILE,
    RESEARCH_WORKER_PROFILE,
    SOLUTION_PROPOSER_PROFILE,
)
from aidison.infrastructure.budget import (
    BudgetConflictError,
    BudgetLedger,
    BudgetLimitExceededError,
)
from aidison.infrastructure.orm import (
    AttemptResultRow,
    AttemptRow,
    DelegationRow,
    JobRow,
    JoinGroupRow,
    JoinReceiptRow,
    PlanTaskRow,
    ProjectRow,
)
from aidison.infrastructure.profiles import (
    ProfileConflictError,
    ProfileNotFoundError,
    ProfileRepository,
)
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import (
    MAX_DELEGATION_WAVE_SIZE,
    AttemptStatus,
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
    JoinStatus,
    RegisteredResult,
    RejectedResult,
    ResultDisposition,
    RuntimeWorkItem,
)


class RuntimeNotFoundError(RuntimeError):
    pass


class RuntimeConflictError(RuntimeError):
    pass


def _json_payload(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value.model_dump(mode="json"))


def _delegation_role_key(spec: DelegationSpec) -> str | None:
    return spec.role_key or {
        "research": "research-worker",
        "solution": "solution-worker",
        "impact": "impact-worker",
    }.get(spec.task_kind)


def _normalized_delegation_spec(payload: dict[str, Any]) -> DelegationSpec:
    spec = DelegationSpec.model_validate(payload)
    role_key = _delegation_role_key(spec)
    return spec if spec.role_key is not None else spec.model_copy(update={"role_key": role_key})


def _result_hash(result: DelegationResult) -> str:
    encoded = json.dumps(
        result.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(encoded).hexdigest()


def _result_id(attempt_id: UUID, result_hash: str) -> UUID:
    return UUID(bytes=sha256(f"{attempt_id}:{result_hash}".encode()).digest()[:16])


_TERMINAL_DELEGATION_STATUSES = {
    DelegationStatus.SUCCEEDED.value,
    DelegationStatus.FAILED.value,
    DelegationStatus.CANCELLED.value,
    DelegationStatus.STALE.value,
    DelegationStatus.AMBIGUOUS.value,
}


def _evaluate_open_join(
    *,
    policy: JoinPolicy,
    delegations: Sequence[DelegationRow],
    now: datetime,
) -> tuple[list[DelegationRow], bool, bool, bool]:
    successes = [item for item in delegations if item.status == DelegationStatus.SUCCEEDED.value]
    all_terminal = all(item.status in _TERMINAL_DELEGATION_STATUSES for item in delegations)
    deadline_reached = now >= policy.deadline
    if policy.mode is JoinMode.ALL_REQUIRED:
        selected = successes
        ready = len(successes) == len(delegations)
    elif policy.mode is JoinMode.BOUNDED_PARTIAL:
        selected = successes
        ready = len(successes) >= policy.min_successes and (all_terminal or deadline_reached)
    else:
        selected = sorted(
            successes,
            key=lambda item: (item.completed_at or datetime.max.replace(tzinfo=UTC), item.id),
        )[:1]
        ready = bool(selected)
    impossible = (all_terminal or deadline_reached) and not ready
    return selected, ready, impossible, deadline_reached


def _join_rejections(
    *,
    policy: JoinPolicy,
    delegations: Sequence[DelegationRow],
    selected: Sequence[DelegationRow],
) -> tuple[RejectedResult, ...]:
    selected_ids = {item.id for item in selected}
    return tuple(
        RejectedResult(
            result_ref=str(item.id),
            reason=(
                "first_valid_superseded"
                if policy.mode is JoinMode.FIRST_VALID
                and item.status == DelegationStatus.SUCCEEDED.value
                and item.id not in selected_ids
                else item.status
            ),
        )
        for item in delegations
        if item.id not in selected_ids
    )


class PostgresRuntime:
    """Small durable scheduler beside Deep Agents, not a second Agent runtime."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PostgresDomainStore(session)

    async def create_job(
        self,
        *,
        project_id: UUID,
        kind: str,
        basis_hash: str,
        basis_project_revision: int,
        profile_id: str,
        profile_revision: int,
        parent_job_id: UUID | None = None,
        idempotency_key: str | None = None,
        request_payload: dict[str, Any] | None = None,
        token_budget_cap: int = 64_000,
        tool_call_budget_cap: int = 8,
    ) -> UUID:
        frozen_request_payload = request_payload or {}
        project = await self._session.get(ProjectRow, project_id)
        if project is None:
            raise RuntimeNotFoundError("project not found")
        profiles = ProfileRepository(self._session)
        try:
            await profiles.ensure_registered(BUILTIN_AGENT_PROFILES, activate=True)
            await profiles.get_revision(profile_id, profile_revision)
        except (ProfileConflictError, ProfileNotFoundError) as exc:
            raise RuntimeConflictError(str(exc)) from exc
        if idempotency_key is not None:
            existing = await self._session.scalar(
                select(JobRow).where(JobRow.request_key == idempotency_key)
            )
            if existing is not None:
                if (
                    existing.project_id != project_id
                    or existing.kind != kind
                    or existing.basis_hash != basis_hash
                    or existing.basis_project_revision != basis_project_revision
                    or existing.profile_id != profile_id
                    or existing.profile_revision != profile_revision
                    or existing.parent_job_id != parent_job_id
                    or existing.request_payload != frozen_request_payload
                ):
                    raise RuntimeConflictError("job idempotency key has a different frozen request")
                try:
                    await BudgetLedger(self._session).create_account(
                        root_job_id=existing.id,
                        project_id=project_id,
                        token_cap=token_budget_cap,
                        tool_call_cap=tool_call_budget_cap,
                    )
                except BudgetConflictError as exc:
                    raise RuntimeConflictError(str(exc)) from exc
                await self._session.commit()
                return existing.id
        if project.revision != basis_project_revision:
            raise RuntimeConflictError("job basis project revision is stale")
        job_id = uuid4()
        self._session.add(
            JobRow(
                id=job_id,
                request_key=idempotency_key,
                request_payload=frozen_request_payload,
                project_id=project_id,
                parent_job_id=parent_job_id,
                kind=kind,
                status=JobStatus.QUEUED.value,
                basis_hash=basis_hash,
                profile_id=profile_id,
                profile_revision=profile_revision,
                basis_project_revision=basis_project_revision,
                current_generation=0,
                cancel_requested=False,
            )
        )
        await self._session.flush()
        roles = {"orchestrator": (profile_id, profile_revision)}
        worker_profiles = {
            "research_wave": ("research-worker", RESEARCH_WORKER_PROFILE.profile_id),
            "solution_wave": ("solution-worker", SOLUTION_PROPOSER_PROFILE.profile_id),
            "impact_wave": ("impact-worker", IMPACT_PROPOSER_PROFILE.profile_id),
        }
        worker_role = worker_profiles.get(kind)
        if worker_role is not None:
            role_key, worker_profile_id = worker_role
            active_worker = await profiles.resolve_active(worker_profile_id)
            roles[role_key] = (active_worker.profile_id, active_worker.revision)
        try:
            await profiles.bind_revisions(root_job_id=job_id, roles=roles)
            await BudgetLedger(self._session).create_account(
                root_job_id=job_id,
                project_id=project_id,
                token_cap=token_budget_cap,
                tool_call_cap=tool_call_budget_cap,
            )
        except (ProfileConflictError, ProfileNotFoundError, BudgetConflictError) as exc:
            raise RuntimeConflictError(str(exc)) from exc
        await self._events.append_event(
            project_id,
            "job.queued",
            {"job_id": str(job_id), "kind": kind},
        )
        await self._session.commit()
        return job_id

    async def create_delegation_wave(
        self,
        *,
        specs: Sequence[DelegationSpec],
        policy: JoinPolicy,
        commit: bool = True,
    ) -> DelegationWave:
        if not specs or len(specs) > MAX_DELEGATION_WAVE_SIZE:
            raise RuntimeConflictError(
                f"delegation wave requires between one and {MAX_DELEGATION_WAVE_SIZE} children"
            )
        if set(policy.expected_delegation_ids) != {item.delegation_id for item in specs}:
            raise RuntimeConflictError("join policy does not match delegation specs")

        first = specs[0]
        if any(
            item.parent_job_id != first.parent_job_id
            or item.parent_attempt_id != first.parent_attempt_id
            or item.parent_claim_generation != first.parent_claim_generation
            or item.graph_step_id != first.graph_step_id
            or item.basis_hash != first.basis_hash
            for item in specs
        ):
            raise RuntimeConflictError("delegation wave does not share one frozen parent basis")

        parent = await self._session.scalar(
            select(JobRow).where(JobRow.id == first.parent_job_id).with_for_update()
        )
        attempt = await self._session.get(AttemptRow, first.parent_attempt_id)
        if parent is None or attempt is None:
            raise RuntimeNotFoundError("parent job or attempt not found")
        if (
            parent.status != JobStatus.RUNNING.value
            or parent.current_generation != first.parent_claim_generation
            or attempt.job_id != parent.id
            or attempt.claim_generation != parent.current_generation
            or parent.basis_hash != first.basis_hash
        ):
            raise RuntimeConflictError("parent claim or basis is stale")

        profiles = ProfileRepository(self._session)
        try:
            worker_role = _delegation_role_key(first)
            if worker_role is None:
                raise RuntimeConflictError("delegation requires an explicit frozen role binding")
            if any(_delegation_role_key(item) != worker_role for item in specs):
                raise RuntimeConflictError("delegation wave must share one frozen role binding")
            worker_binding = await profiles.get_binding(parent.id, worker_role)
            worker_profile = await profiles.get_revision(
                worker_binding.profile_id,
                worker_binding.profile_revision,
            )
        except (ProfileConflictError, ProfileNotFoundError) as exc:
            raise RuntimeConflictError(str(exc)) from exc
        if any(
            item.profile_id != worker_binding.profile_id
            or item.profile_revision != worker_binding.profile_revision
            or item.token_budget > worker_profile.token_cap
            or item.tool_call_budget > worker_profile.tool_call_cap
            for item in specs
        ):
            raise RuntimeConflictError("delegation exceeds its frozen AgentProfile")
        if len(specs) > worker_profile.concurrency_cap:
            raise RuntimeConflictError("delegation wave exceeds its frozen concurrency cap")
        ledger = BudgetLedger(self._session)
        try:
            account_id = await ledger.get_account_id(parent.id)
        except BudgetConflictError as exc:
            raise RuntimeConflictError(str(exc)) from exc

        existing_group = await self._session.scalar(
            select(JoinGroupRow)
            .where(
                JoinGroupRow.parent_attempt_id == first.parent_attempt_id,
                JoinGroupRow.graph_step_id == first.graph_step_id,
            )
            .with_for_update()
        )
        if existing_group is not None:
            stored_policy = JoinPolicy.model_validate(existing_group.policy)
            existing_delegations = list(
                await self._session.scalars(
                    select(DelegationRow)
                    .where(DelegationRow.join_group_id == existing_group.id)
                    .order_by(DelegationRow.shard_key, DelegationRow.id)
                )
            )
            requested_by_id = {item.delegation_id: item for item in specs}
            existing_by_id = {item.id: item for item in existing_delegations}
            payloads_match = all(
                _normalized_delegation_spec(existing_by_id[item.delegation_id].payload)
                == item.model_copy(update={"role_key": _delegation_role_key(item)})
                for item in specs
                if item.delegation_id in existing_by_id
            )
            if (
                stored_policy != policy
                or len(existing_delegations) != len(specs)
                or set(existing_by_id) != set(requested_by_id)
                or not payloads_match
            ):
                raise RuntimeConflictError("delegation wave replay does not match the frozen group")
            try:
                for item in existing_delegations:
                    await ledger.get_allocation(
                        account_id=account_id,
                        owner_kind=BudgetOwnerKind.CHILD,
                        owner_ref=item.child_job_id,
                    )
            except BudgetConflictError as exc:
                raise RuntimeConflictError(
                    "delegation replay is missing its frozen budget allocation"
                ) from exc
            if commit:
                await self._session.commit()
            return DelegationWave(
                join_group_id=existing_group.id,
                delegation_ids=tuple(item.delegation_id for item in specs),
                child_job_ids=tuple(
                    existing_by_id[item.delegation_id].child_job_id for item in specs
                ),
            )

        join_group_id = uuid4()
        self._session.add(
            JoinGroupRow(
                id=join_group_id,
                parent_job_id=parent.id,
                parent_attempt_id=attempt.id,
                parent_claim_generation=parent.current_generation,
                graph_step_id=first.graph_step_id,
                basis_hash=parent.basis_hash,
                basis_project_revision=parent.basis_project_revision,
                status=JoinStatus.OPEN.value,
                expected_count=len(specs),
                policy=_json_payload(policy),
            )
        )

        child_job_ids: list[UUID] = []
        for spec in specs:
            child_job_id = uuid4()
            child_job_ids.append(child_job_id)
            self._session.add(
                JobRow(
                    id=child_job_id,
                    project_id=parent.project_id,
                    parent_job_id=parent.id,
                    kind=f"delegated_{spec.task_kind}",
                    status=JobStatus.QUEUED.value,
                    basis_hash=spec.basis_hash,
                    profile_id=spec.profile_id,
                    profile_revision=spec.profile_revision,
                    basis_project_revision=parent.basis_project_revision,
                    current_generation=0,
                    cancel_requested=False,
                )
            )
        await self._session.flush()

        for spec, child_job_id in zip(specs, child_job_ids, strict=True):
            self._session.add(
                DelegationRow(
                    id=spec.delegation_id,
                    parent_job_id=spec.parent_job_id,
                    parent_attempt_id=spec.parent_attempt_id,
                    parent_claim_generation=spec.parent_claim_generation,
                    join_group_id=join_group_id,
                    child_job_id=child_job_id,
                    graph_step_id=spec.graph_step_id,
                    profile_id=spec.profile_id,
                    profile_revision=spec.profile_revision,
                    basis_hash=spec.basis_hash,
                    shard_key=spec.shard_key,
                    idempotency_key=spec.idempotency_key,
                    payload=_json_payload(spec),
                    status="pending",
                )
            )
            try:
                await ledger.allocate(
                    account_id=account_id,
                    owner_kind=BudgetOwnerKind.CHILD,
                    owner_ref=child_job_id,
                    token_grant=spec.token_budget,
                    tool_call_grant=spec.tool_call_budget,
                )
            except (BudgetConflictError, BudgetLimitExceededError) as exc:
                raise RuntimeConflictError(str(exc)) from exc
        await self._events.append_event(
            parent.project_id,
            "delegation.wave_created",
            {
                "join_group_id": str(join_group_id),
                "child_count": len(child_job_ids),
            },
        )
        if commit:
            await self._session.commit()
        return DelegationWave(
            join_group_id=join_group_id,
            delegation_ids=tuple(item.delegation_id for item in specs),
            child_job_ids=tuple(child_job_ids),
        )

    async def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> JobClaim | None:
        now = datetime.now(UTC)
        candidate = await self._session.scalar(
            select(JobRow)
            .where(
                JobRow.cancel_requested.is_(False),
                or_(
                    JobRow.status == JobStatus.QUEUED.value,
                    and_(
                        JobRow.status == JobStatus.RUNNING.value,
                        JobRow.lease_expires_at < now,
                    ),
                ),
            )
            .order_by(JobRow.created_at, JobRow.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if candidate is None:
            await self._session.commit()
            return None

        if candidate.status == JobStatus.RUNNING.value:
            open_groups = list(
                await self._session.scalars(
                    select(JoinGroupRow)
                    .where(
                        JoinGroupRow.parent_job_id == candidate.id,
                        JoinGroupRow.parent_claim_generation == candidate.current_generation,
                        JoinGroupRow.status == JoinStatus.OPEN.value,
                    )
                    .order_by(JoinGroupRow.id)
                    .with_for_update()
                )
            )
            open_group_ids = [item.id for item in open_groups]
            stale_child_ids = (
                list(
                    await self._session.scalars(
                        select(DelegationRow.child_job_id).where(
                            DelegationRow.join_group_id.in_(open_group_ids)
                        )
                    )
                )
                if open_group_ids
                else []
            )
            stale_children = (
                list(
                    await self._session.scalars(
                        select(JobRow)
                        .where(JobRow.id.in_(stale_child_ids))
                        .order_by(JobRow.id)
                        .with_for_update()
                    )
                )
                if stale_child_ids
                else []
            )
            for group in open_groups:
                group.status = JoinStatus.CANCELLED.value
            for child in stale_children:
                if child.status not in {
                    JobStatus.SUCCEEDED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                }:
                    child.cancel_requested = True
                    child.status = JobStatus.CANCELLED.value
                    child.completed_at = now
                    child.lease_owner = None
                    child.lease_token = None
                    child.lease_expires_at = None
            if open_group_ids:
                await self._session.execute(
                    update(DelegationRow)
                    .where(
                        DelegationRow.join_group_id.in_(open_group_ids),
                        DelegationRow.status == "pending",
                    )
                    .values(status=DelegationStatus.CANCELLED.value, completed_at=now)
                )
            if stale_child_ids:
                await self._session.execute(
                    update(PlanTaskRow)
                    .where(PlanTaskRow.dispatched_job_id.in_(stale_child_ids))
                    .values(
                        dispatched_job_id=None,
                        status="ready",
                    )
                )
                await self._session.execute(
                    update(AttemptRow)
                    .where(
                        AttemptRow.job_id.in_(stale_child_ids),
                        AttemptRow.status == AttemptStatus.RUNNING.value,
                    )
                    .values(status=AttemptStatus.CANCELLED.value, completed_at=now)
                )
                try:
                    account_id = await BudgetLedger(self._session).get_account_id(candidate.id)
                    for child_id in stale_child_ids:
                        await BudgetLedger(self._session).reconcile_reclaimed_owner(
                            account_id=account_id,
                            owner_kind=BudgetOwnerKind.CHILD,
                            owner_ref=child_id,
                        )
                except BudgetConflictError as exc:
                    raise RuntimeConflictError(
                        "parent reclaim could not reconcile child budget"
                    ) from exc
            await self._session.execute(
                update(AttemptRow)
                .where(
                    AttemptRow.job_id == candidate.id,
                    AttemptRow.claim_generation == candidate.current_generation,
                    AttemptRow.status == AttemptStatus.RUNNING.value,
                )
                .values(status=AttemptStatus.SUPERSEDED.value, completed_at=now)
            )

        attempt_number = (
            await self._session.scalar(
                select(func.coalesce(func.max(AttemptRow.number), 0)).where(
                    AttemptRow.job_id == candidate.id
                )
            )
            or 0
        ) + 1
        generation = candidate.current_generation + 1
        lease_token = uuid4()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        attempt_id = uuid4()
        candidate.status = JobStatus.RUNNING.value
        candidate.current_generation = generation
        candidate.lease_owner = worker_id
        candidate.lease_token = lease_token
        candidate.lease_expires_at = lease_expires_at
        self._session.add(
            AttemptRow(
                id=attempt_id,
                job_id=candidate.id,
                number=attempt_number,
                claim_generation=generation,
                status=AttemptStatus.RUNNING.value,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                started_at=now,
            )
        )
        await self._events.append_event(
            candidate.project_id,
            "job.claimed",
            {
                "job_id": str(candidate.id),
                "attempt_id": str(attempt_id),
                "generation": generation,
            },
        )
        await self._session.commit()
        return JobClaim(
            job_id=candidate.id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            claim_generation=generation,
            lease_token=lease_token,
            lease_owner=worker_id,
            lease_expires_at=lease_expires_at,
            basis_hash=candidate.basis_hash,
            basis_project_revision=candidate.basis_project_revision,
            profile_id=candidate.profile_id,
            profile_revision=candidate.profile_revision,
        )

    async def load_work_item(self, claim: JobClaim) -> RuntimeWorkItem:
        job = await self._session.get(JobRow, claim.job_id)
        if (
            job is None
            or job.status != JobStatus.RUNNING.value
            or job.current_generation != claim.claim_generation
            or job.lease_token != claim.lease_token
        ):
            raise RuntimeConflictError("claim is stale or cancelled")
        delegation = await self._session.scalar(
            select(DelegationRow).where(DelegationRow.child_job_id == job.id)
        )
        spec = None if delegation is None else DelegationSpec.model_validate(delegation.payload)
        work_item = RuntimeWorkItem(
            claim=claim,
            project_id=job.project_id,
            kind=job.kind,
            request_payload=job.request_payload,
            delegation=spec,
        )
        await self._session.commit()
        return work_item

    async def claim_next_work(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> RuntimeWorkItem | None:
        claim = await self.claim_next_job(worker_id=worker_id, lease_seconds=lease_seconds)
        if claim is None:
            return None
        return await self.load_work_item(claim)

    async def renew_claim(
        self,
        *,
        claim: JobClaim,
        lease_seconds: int = 60,
    ) -> datetime:
        expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        statement = (
            update(JobRow)
            .where(
                JobRow.id == claim.job_id,
                JobRow.status == JobStatus.RUNNING.value,
                JobRow.current_generation == claim.claim_generation,
                JobRow.lease_token == claim.lease_token,
                JobRow.cancel_requested.is_(False),
            )
            .values(lease_expires_at=expires_at)
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            await self._session.rollback()
            raise RuntimeConflictError("claim is stale or cancelled")
        await self._session.execute(
            update(AttemptRow)
            .where(AttemptRow.id == claim.attempt_id, AttemptRow.status == "running")
            .values(lease_expires_at=expires_at)
        )
        await self._session.commit()
        return expires_at

    async def register_result(
        self,
        *,
        result: DelegationResult,
        lease_token: UUID,
        commit: bool = True,
    ) -> RegisteredResult:
        delegation = await self._session.get(DelegationRow, result.delegation_id)
        attempt = await self._session.get(AttemptRow, result.attempt_id)
        if delegation is None or attempt is None:
            raise RuntimeNotFoundError("delegation or attempt not found")
        group = await self._session.scalar(
            select(JoinGroupRow)
            .where(JoinGroupRow.id == delegation.join_group_id)
            .with_for_update()
        )
        if group is None:
            raise RuntimeNotFoundError("join group not found")

        job_ids = sorted({delegation.parent_job_id, delegation.child_job_id}, key=str)
        jobs = list(
            await self._session.scalars(
                select(JobRow).where(JobRow.id.in_(job_ids)).order_by(JobRow.id).with_for_update()
            )
        )
        jobs_by_id = {item.id: item for item in jobs}
        parent = jobs_by_id.get(delegation.parent_job_id)
        child = jobs_by_id.get(delegation.child_job_id)
        if parent is None or child is None:
            raise RuntimeNotFoundError("parent or child job not found")
        project = await self._session.get(ProjectRow, child.project_id)
        if project is None:
            raise RuntimeNotFoundError("project not found")

        result_hash = _result_hash(result)
        result_id = _result_id(result.attempt_id, result_hash)
        existing = await self._session.get(AttemptResultRow, result_id)
        if existing is not None:
            if commit:
                await self._session.commit()
            return RegisteredResult(
                result_id=existing.id,
                result_hash=existing.result_hash,
                disposition=ResultDisposition(existing.disposition),
                quarantine_reason=existing.quarantine_reason,
            )

        stale_reasons: list[str] = []
        if attempt.job_id != child.id or result.child_job_id != child.id:
            stale_reasons.append("attempt_job_mismatch")
        if (
            child.status != JobStatus.RUNNING.value
            or child.current_generation != result.child_claim_generation
            or attempt.claim_generation != result.child_claim_generation
            or attempt.status != AttemptStatus.RUNNING.value
            or child.lease_token != lease_token
        ):
            stale_reasons.append("child_generation_or_lease_stale")
        if child.cancel_requested:
            stale_reasons.append("child_cancelled")
        if (
            parent.current_generation != delegation.parent_claim_generation
            or parent.status != JobStatus.RUNNING.value
            or parent.cancel_requested
        ):
            stale_reasons.append("parent_generation_stale")
        if group.status != JoinStatus.OPEN.value:
            stale_reasons.append("join_closed")
        if not (
            result.basis_hash
            == child.basis_hash
            == delegation.basis_hash
            == group.basis_hash
            == parent.basis_hash
        ):
            stale_reasons.append("basis_stale")
        if (
            project.revision != child.basis_project_revision
            or project.revision != group.basis_project_revision
        ):
            stale_reasons.append("project_revision_stale")

        disposition = ResultDisposition.QUARANTINED if stale_reasons else ResultDisposition.ELIGIBLE
        quarantine_reason = ",".join(stale_reasons) or None
        self._session.add(
            AttemptResultRow(
                id=result_id,
                project_id=child.project_id,
                job_id=child.id,
                attempt_id=attempt.id,
                claim_generation=result.child_claim_generation,
                basis_hash=result.basis_hash,
                result_hash=result_hash,
                disposition=disposition.value,
                quarantine_reason=quarantine_reason,
                payload=_json_payload(result),
            )
        )

        if disposition is ResultDisposition.ELIGIBLE:
            now = datetime.now(UTC)
            terminal_job_status = {
                DelegationStatus.SUCCEEDED: JobStatus.SUCCEEDED,
                DelegationStatus.FAILED: JobStatus.FAILED,
                DelegationStatus.CANCELLED: JobStatus.CANCELLED,
                DelegationStatus.STALE: JobStatus.FAILED,
                DelegationStatus.AMBIGUOUS: JobStatus.FAILED,
            }[result.status]
            terminal_attempt_status = {
                JobStatus.SUCCEEDED: AttemptStatus.SUCCEEDED,
                JobStatus.CANCELLED: AttemptStatus.CANCELLED,
            }.get(terminal_job_status, AttemptStatus.FAILED)
            child.status = terminal_job_status.value
            child.completed_at = now
            child.lease_owner = None
            child.lease_token = None
            child.lease_expires_at = None
            attempt.status = terminal_attempt_status.value
            attempt.completed_at = now
            attempt.result_ref = result.proposal_ref
            attempt.normalized_error = result.normalized_error
            delegation.status = result.status.value
            delegation.result_payload = _json_payload(result)
            delegation.completed_at = now
            plan_task = await self._session.scalar(
                select(PlanTaskRow)
                .where(PlanTaskRow.dispatched_job_id == child.id)
                .with_for_update()
            )
            if plan_task is not None:
                plan_task.status = {
                    JobStatus.SUCCEEDED.value: "succeeded",
                    JobStatus.FAILED.value: "failed",
                    JobStatus.CANCELLED.value: "cancelled",
                }[terminal_job_status.value]

        await self._events.append_event(
            child.project_id,
            "delegation.result_registered",
            {
                "delegation_id": str(delegation.id),
                "result_id": str(result_id),
                "disposition": disposition.value,
                "reason": quarantine_reason,
            },
        )
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return RegisteredResult(
            result_id=result_id,
            result_hash=result_hash,
            disposition=disposition,
            quarantine_reason=quarantine_reason,
        )

    async def _cancel_join_siblings(
        self,
        *,
        parent: JobRow,
        delegations: Sequence[DelegationRow],
        keep_child_ids: set[UUID],
        normalized_error: str,
    ) -> None:
        terminal_jobs = {
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        }
        candidate_ids = sorted(
            {item.child_job_id for item in delegations if item.child_job_id not in keep_child_ids},
            key=str,
        )
        if not candidate_ids:
            return
        observed_jobs = list(
            await self._session.scalars(
                select(JobRow).where(JobRow.id.in_(candidate_ids)).order_by(JobRow.id)
            )
        )
        cancelled_ids = [item.id for item in observed_jobs if item.status not in terminal_jobs]
        if not cancelled_ids:
            return

        try:
            ledger = BudgetLedger(self._session)
            account_id = await ledger.get_account_id(parent.id)
            for child_id in cancelled_ids:
                await ledger.reconcile_reclaimed_owner(
                    account_id=account_id,
                    owner_kind=BudgetOwnerKind.CHILD,
                    owner_ref=child_id,
                    normalized_error=normalized_error,
                )
        except BudgetConflictError as exc:
            raise RuntimeConflictError("join cleanup could not reconcile child budget") from exc

        jobs = list(
            await self._session.scalars(
                select(JobRow)
                .where(JobRow.id.in_(cancelled_ids))
                .order_by(JobRow.id)
                .with_for_update()
            )
        )
        cancelled_ids = [item.id for item in jobs if item.status not in terminal_jobs]
        if not cancelled_ids:
            return
        now = datetime.now(UTC)
        for job in jobs:
            if job.id not in cancelled_ids:
                continue
            job.cancel_requested = True
            job.status = JobStatus.CANCELLED.value
            job.completed_at = now
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
        await self._session.execute(
            update(AttemptRow)
            .where(
                AttemptRow.job_id.in_(cancelled_ids),
                AttemptRow.status == AttemptStatus.RUNNING.value,
            )
            .values(
                status=AttemptStatus.CANCELLED.value,
                completed_at=now,
                normalized_error=normalized_error,
            )
        )
        await self._session.execute(
            update(DelegationRow)
            .where(
                DelegationRow.child_job_id.in_(cancelled_ids),
                DelegationRow.status == "pending",
            )
            .values(
                status=DelegationStatus.CANCELLED.value,
                completed_at=now,
            )
        )
        await self._session.execute(
            update(PlanTaskRow)
            .where(
                PlanTaskRow.dispatched_job_id.in_(cancelled_ids),
                PlanTaskRow.status.in_(("planned", "ready", "dispatched", "running", "blocked")),
            )
            .values(status="cancelled")
        )
        await self._events.append_event(
            parent.project_id,
            "delegation.siblings_cancelled",
            {
                "child_job_ids": [str(item) for item in cancelled_ids],
                "reason": normalized_error,
            },
        )

    async def inspect_join(
        self,
        *,
        join_group_id: UUID,
        parent_claim: JobClaim,
    ) -> JoinSnapshot:
        loaded_group = await self._session.scalar(
            select(JoinGroupRow).where(JoinGroupRow.id == join_group_id)
        )
        if loaded_group is None:
            raise RuntimeNotFoundError("join group not found")
        group: JoinGroupRow = loaded_group
        parent = await self._session.get(JobRow, group.parent_job_id)
        if (
            parent is None
            or parent.id != parent_claim.job_id
            or parent.current_generation != parent_claim.claim_generation
            or parent.lease_token != parent_claim.lease_token
        ):
            raise RuntimeConflictError("parent claim is stale")

        delegations = list(
            await self._session.scalars(
                select(DelegationRow)
                .where(DelegationRow.join_group_id == join_group_id)
                .order_by(DelegationRow.shard_key, DelegationRow.id)
            )
        )
        policy = JoinPolicy.model_validate(group.policy)
        selected, ready, impossible, deadline_reached = _evaluate_open_join(
            policy=policy,
            delegations=delegations,
            now=datetime.now(UTC),
        )
        if group.status != JoinStatus.OPEN.value:
            ready = group.status == JoinStatus.JOINED.value
            impossible = group.status in {
                JoinStatus.CANCELLED.value,
                JoinStatus.EXPIRED.value,
                JoinStatus.FAILED.value,
            }
        if group.status == JoinStatus.OPEN.value and impossible:
            locked_group = await self._session.scalar(
                select(JoinGroupRow).where(JoinGroupRow.id == join_group_id).with_for_update()
            )
            if locked_group is None:
                raise RuntimeNotFoundError("join group not found")
            group = locked_group
            policy = JoinPolicy.model_validate(group.policy)
            delegations = list(
                await self._session.scalars(
                    select(DelegationRow)
                    .where(DelegationRow.join_group_id == join_group_id)
                    .order_by(DelegationRow.shard_key, DelegationRow.id)
                )
            )
            selected, ready, impossible, deadline_reached = _evaluate_open_join(
                policy=policy,
                delegations=delegations,
                now=datetime.now(UTC),
            )
            if group.status == JoinStatus.OPEN.value and impossible:
                await self._cancel_join_siblings(
                    parent=parent,
                    delegations=delegations,
                    keep_child_ids=set(),
                    normalized_error="join_closed_sibling_cancelled",
                )
                group.status = (
                    JoinStatus.EXPIRED.value if deadline_reached else JoinStatus.FAILED.value
                )
                await self._events.append_event(
                    parent.project_id,
                    "delegation.join_failed",
                    {
                        "join_group_id": str(group.id),
                        "reason": group.status,
                    },
                )

        accepted_refs: list[str] = []
        for item in selected:
            if item.result_payload is None:
                continue
            proposal_ref = item.result_payload.get("proposal_ref")
            if isinstance(proposal_ref, str):
                accepted_refs.append(proposal_ref)
        rejected = _join_rejections(
            policy=policy,
            delegations=delegations,
            selected=selected,
        )
        await self._session.commit()
        return JoinSnapshot(
            join_group_id=group.id,
            status=JoinStatus(group.status),
            ready=ready,
            impossible=impossible,
            accepted_proposal_refs=tuple(accepted_refs),
            rejected_results=rejected,
        )

    async def complete_claim(
        self,
        *,
        claim: JobClaim,
        status: JobStatus,
        result_ref: str | None = None,
        normalized_error: str | None = None,
    ) -> bool:
        if status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
            raise RuntimeConflictError("claim completion requires a terminal status")
        job = await self._session.scalar(
            select(JobRow).where(JobRow.id == claim.job_id).with_for_update()
        )
        attempt = await self._session.get(AttemptRow, claim.attempt_id)
        if job is None or attempt is None:
            raise RuntimeNotFoundError("job or attempt not found")
        if (
            job.status != JobStatus.RUNNING.value
            or job.current_generation != claim.claim_generation
            or job.lease_token != claim.lease_token
            or attempt.job_id != job.id
            or attempt.claim_generation != claim.claim_generation
            or attempt.status != AttemptStatus.RUNNING.value
        ):
            await self._session.rollback()
            raise RuntimeConflictError("claim is stale or terminal")
        if await self._session.scalar(
            select(DelegationRow.id).where(DelegationRow.child_job_id == job.id)
        ):
            await self._session.rollback()
            raise RuntimeConflictError("delegated child jobs must complete through register_result")

        now = datetime.now(UTC)
        job.status = status.value
        job.completed_at = now
        job.lease_owner = None
        job.lease_token = None
        job.lease_expires_at = None
        if status is JobStatus.CANCELLED:
            job.cancel_requested = True
        attempt.status = {
            JobStatus.SUCCEEDED: AttemptStatus.SUCCEEDED,
            JobStatus.FAILED: AttemptStatus.FAILED,
            JobStatus.CANCELLED: AttemptStatus.CANCELLED,
        }[status].value
        attempt.result_ref = result_ref
        attempt.normalized_error = normalized_error
        attempt.completed_at = now
        await self._events.append_event(
            job.project_id,
            "job.completed",
            {
                "job_id": str(job.id),
                "attempt_id": str(attempt.id),
                "status": status.value,
                "result_ref": result_ref,
            },
        )
        await self._session.commit()
        return True

    async def find_committed_join(
        self,
        *,
        parent_claim: JobClaim,
        graph_step_id: str,
    ) -> CommittedJoin | None:
        """Return the one committed downstream receipt resumable by a current claim."""

        parent = await self._session.scalar(
            select(JobRow).where(JobRow.id == parent_claim.job_id).with_for_update()
        )
        if (
            parent is None
            or parent.status != JobStatus.RUNNING.value
            or parent.current_generation != parent_claim.claim_generation
            or parent.lease_token != parent_claim.lease_token
            or parent.basis_hash != parent_claim.basis_hash
        ):
            raise RuntimeConflictError("parent claim is stale or cancelled")

        committed = list(
            (
                await self._session.execute(
                    select(JoinGroupRow, JoinReceiptRow)
                    .join(
                        JoinReceiptRow,
                        JoinReceiptRow.join_group_id == JoinGroupRow.id,
                    )
                    .where(
                        JoinGroupRow.parent_job_id == parent.id,
                        JoinGroupRow.graph_step_id == graph_step_id,
                        JoinGroupRow.status == JoinStatus.JOINED.value,
                    )
                    .order_by(JoinGroupRow.created_at, JoinGroupRow.id)
                )
            ).all()
        )
        if not committed:
            await self._session.commit()
            return None
        if len(committed) != 1:
            raise RuntimeConflictError("parent job has multiple committed joins for one graph step")

        group, receipt_row = committed[0]
        receipt = JoinReceipt.model_validate(receipt_row.payload)
        if (
            group.basis_hash != parent.basis_hash
            or receipt_row.basis_hash != group.basis_hash
            or receipt_row.parent_claim_generation != group.parent_claim_generation
            or receipt.basis_hash != parent.basis_hash
            or receipt.join_group_id != group.id
            or receipt.parent_claim_generation != group.parent_claim_generation
        ):
            raise RuntimeConflictError("committed join does not match the parent basis")

        delegations = list(
            await self._session.scalars(
                select(DelegationRow)
                .where(DelegationRow.join_group_id == group.id)
                .order_by(DelegationRow.shard_key, DelegationRow.id)
            )
        )
        if len(delegations) != group.expected_count:
            raise RuntimeConflictError("committed join has an incomplete delegation set")
        input_refs = tuple(
            sorted(
                {
                    ref
                    for row in delegations
                    for ref in DelegationSpec.model_validate(row.payload).input_refs
                }
            )
        )
        await self._session.commit()
        return CommittedJoin(receipt=receipt, input_refs=input_refs)

    async def commit_join(
        self,
        *,
        join_group_id: UUID,
        merged_proposal_ref: str,
    ) -> JoinReceipt | None:
        group = await self._session.scalar(
            select(JoinGroupRow).where(JoinGroupRow.id == join_group_id).with_for_update()
        )
        if group is None:
            raise RuntimeNotFoundError("join group not found")
        existing = await self._session.get(JoinReceiptRow, join_group_id)
        if existing is not None:
            await self._session.commit()
            return JoinReceipt.model_validate(existing.payload)
        if group.status != JoinStatus.OPEN.value:
            await self._session.commit()
            return None

        policy = JoinPolicy.model_validate(group.policy)
        delegations = list(
            await self._session.scalars(
                select(DelegationRow)
                .where(DelegationRow.join_group_id == join_group_id)
                .order_by(DelegationRow.shard_key, DelegationRow.id)
            )
        )
        parent = await self._session.get(JobRow, group.parent_job_id)
        project = None if parent is None else await self._session.get(ProjectRow, parent.project_id)
        if (
            parent is None
            or project is None
            or parent.status != JobStatus.RUNNING.value
            or parent.current_generation != group.parent_claim_generation
            or parent.cancel_requested
            or parent.basis_hash != group.basis_hash
            or project.revision != group.basis_project_revision
        ):
            if parent is not None:
                await self._cancel_join_siblings(
                    parent=parent,
                    delegations=delegations,
                    keep_child_ids=set(),
                    normalized_error="join_cancelled_sibling_cancelled",
                )
            group.status = JoinStatus.CANCELLED.value
            await self._session.commit()
            return None

        if {item.id for item in delegations} != set(policy.expected_delegation_ids):
            raise RuntimeConflictError("durable join group differs from frozen policy")

        selected, ready, impossible, deadline_reached = _evaluate_open_join(
            policy=policy,
            delegations=delegations,
            now=datetime.now(UTC),
        )
        if impossible:
            await self._cancel_join_siblings(
                parent=parent,
                delegations=delegations,
                keep_child_ids=set(),
                normalized_error="join_closed_sibling_cancelled",
            )
            group.status = JoinStatus.EXPIRED.value if deadline_reached else JoinStatus.FAILED.value
            await self._events.append_event(
                parent.project_id,
                "delegation.join_failed",
                {"join_group_id": str(group.id), "reason": group.status},
            )
            await self._session.commit()
            return None
        if not ready:
            await self._session.commit()
            return None

        accepted_hashes: list[str] = []
        for delegation in selected:
            accepted_hash = await self._session.scalar(
                select(AttemptResultRow.result_hash)
                .where(
                    AttemptResultRow.job_id == delegation.child_job_id,
                    AttemptResultRow.disposition == ResultDisposition.ELIGIBLE.value,
                )
                .order_by(AttemptResultRow.created_at.desc())
                .limit(1)
            )
            if accepted_hash is None:
                raise RuntimeConflictError("successful delegation has no eligible result")
            accepted_hashes.append(accepted_hash)

        await self._cancel_join_siblings(
            parent=parent,
            delegations=delegations,
            keep_child_ids={item.child_job_id for item in selected},
            normalized_error=(
                "first_valid_sibling_cancelled"
                if policy.mode is JoinMode.FIRST_VALID
                else "join_closed_sibling_cancelled"
            ),
        )
        rejected = _join_rejections(
            policy=policy,
            delegations=delegations,
            selected=selected,
        )
        receipt = JoinReceipt(
            join_group_id=join_group_id,
            accepted_result_hashes=tuple(accepted_hashes),
            rejected_results=rejected,
            merged_proposal_ref=merged_proposal_ref,
            basis_hash=group.basis_hash,
            parent_claim_generation=group.parent_claim_generation,
        )
        self._session.add(
            JoinReceiptRow(
                join_group_id=join_group_id,
                parent_claim_generation=receipt.parent_claim_generation,
                basis_hash=receipt.basis_hash,
                payload=_json_payload(receipt),
                committed_at=receipt.committed_at,
            )
        )
        group.status = JoinStatus.JOINED.value
        await self._events.append_event(
            parent.project_id,
            "delegation.joined",
            {
                "join_group_id": str(join_group_id),
                "accepted_count": len(accepted_hashes),
            },
        )
        await self._session.commit()
        return receipt

    async def cancel_job(self, job_id: UUID) -> bool:
        direct_group_ids = set(
            await self._session.scalars(
                select(JoinGroupRow.id).where(
                    JoinGroupRow.parent_job_id == job_id,
                    JoinGroupRow.status == JoinStatus.OPEN.value,
                )
            )
        )
        child_group_ids = set(
            await self._session.scalars(
                select(DelegationRow.join_group_id).where(DelegationRow.child_job_id == job_id)
            )
        )
        group_ids = sorted(direct_group_ids | child_group_ids, key=str)
        groups = (
            list(
                await self._session.scalars(
                    select(JoinGroupRow)
                    .where(JoinGroupRow.id.in_(group_ids))
                    .order_by(JoinGroupRow.id)
                    .with_for_update()
                )
            )
            if group_ids
            else []
        )
        child_ids = set(
            await self._session.scalars(
                select(DelegationRow.child_job_id).where(
                    DelegationRow.join_group_id.in_(direct_group_ids)
                )
            )
        )
        locked_job_ids = sorted({job_id, *child_ids}, key=str)
        requested_identity = await self._session.get(JobRow, job_id)
        if requested_identity is None:
            raise RuntimeNotFoundError("job not found")
        terminal = {
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        }
        if requested_identity.status in terminal:
            await self._session.commit()
            return False

        direct_delegations = (
            list(
                await self._session.scalars(
                    select(DelegationRow)
                    .where(DelegationRow.join_group_id.in_(direct_group_ids))
                    .order_by(DelegationRow.id)
                )
            )
            if direct_group_ids
            else []
        )
        if direct_delegations:
            await self._cancel_join_siblings(
                parent=requested_identity,
                delegations=direct_delegations,
                keep_child_ids=set(),
                normalized_error="parent_cancelled_sibling_cancelled",
            )

        jobs = list(
            await self._session.scalars(
                select(JobRow)
                .where(JobRow.id.in_(locked_job_ids))
                .order_by(JobRow.id)
                .with_for_update()
            )
        )
        jobs_by_id = {item.id: item for item in jobs}
        requested = jobs_by_id.get(job_id)
        if requested is None:
            raise RuntimeNotFoundError("job not found")
        if requested.status in terminal:
            await self._session.commit()
            return False

        now = datetime.now(UTC)
        for job in jobs:
            if job.status in terminal:
                continue
            job.cancel_requested = True
            job.status = JobStatus.CANCELLED.value
            job.completed_at = now
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
        if locked_job_ids:
            await self._session.execute(
                update(AttemptRow)
                .where(
                    AttemptRow.job_id.in_(locked_job_ids),
                    AttemptRow.status == AttemptStatus.RUNNING.value,
                )
                .values(status=AttemptStatus.CANCELLED.value, completed_at=now)
            )
        for group in groups:
            if group.status == JoinStatus.OPEN.value:
                group.status = JoinStatus.CANCELLED.value
        if direct_group_ids:
            await self._session.execute(
                update(DelegationRow)
                .where(
                    DelegationRow.join_group_id.in_(direct_group_ids),
                    DelegationRow.status == "pending",
                )
                .values(status=DelegationStatus.CANCELLED.value, completed_at=now)
            )
        await self._events.append_event(
            requested.project_id,
            "job.cancelled",
            {
                "job_id": str(job_id),
                "propagated_child_count": len(child_ids),
            },
        )
        await self._session.commit()
        return True
