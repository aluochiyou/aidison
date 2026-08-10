from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.budget import BudgetConflictError, BudgetLedger
from aidison.infrastructure.orm import (
    AttemptRow,
    BudgetAccountRow,
    DelegationRow,
    JobProfileBindingRow,
    JobRow,
    PlanHeadRow,
    PlanRevisionRow,
    PlanTaskClaimRow,
    PlanTaskRow,
    ProjectRow,
)
from aidison.infrastructure.planning import PlanNotFoundError, PostgresPlanStore
from aidison.infrastructure.profiles import ProfileNotFoundError, ProfileRepository
from aidison.infrastructure.runtime import PostgresRuntime, RuntimeConflictError
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import (
    AgentProfileRevision,
    AttemptStatus,
    BudgetOwnerKind,
    DelegationSpec,
    DelegationStatus,
    FailureClass,
    JobClaim,
    JobStatus,
    JoinMode,
    JoinPolicy,
    ProfileBinding,
)
from aidison.runtime.planning import (
    PlanTaskClaim,
    SchedulerSkip,
    SchedulerSkipReason,
    SchedulerTickResult,
    TaskClaimStatus,
    TaskDispatchIntent,
    TaskNode,
    TaskStatus,
)


class ReadySetConflictError(RuntimeError):
    """Raised when a ready-set dispatch races a stale or conflicting durable fact."""


class ReadySetDispatchSkipped(RuntimeError):
    """A durable re-check declined dispatch without invalidating the root claim."""

    def __init__(self, reason: SchedulerSkipReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ReadySetContext:
    """One immutable read of the durable facts a ready-set tick needs."""

    root_job_id: UUID
    project_id: UUID
    plan_revision: int
    ready_tasks: tuple[TaskNode, ...]
    active_total: int
    active_per_profile: dict[str, int]
    token_available: int
    tool_calls_available: int
    bound_roles: dict[str, ProfileBinding]
    profiles: dict[tuple[str, int], AgentProfileRevision]
    terminal_failed: int


_ACTIVE_JOB_STATUSES = {JobStatus.QUEUED.value, JobStatus.RUNNING.value}
_TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
}

_DEFAULT_RETRYABLE_FAILURES = frozenset(
    {FailureClass.TRANSIENT, FailureClass.TIMEOUT}
)


def classify_failure(normalized_error: str | None) -> FailureClass:
    """Classify a normalized worker error without treating unknown errors as retryable."""
    error = (normalized_error or "").lower()
    if "timeout" in error or "deadline" in error:
        return FailureClass.TIMEOUT
    if any(term in error for term in ("permission", "forbidden", "unauthorized", "auth")):
        return FailureClass.PERMISSION
    if any(term in error for term in ("budget", "quota", "token", "tool-call")):
        return FailureClass.BUDGET
    if any(term in error for term in ("configuration", "config", "invalid parameter")):
        return FailureClass.CONFIGURATION
    if any(term in error for term in ("evidence", "contradiction", "conflict")):
        return FailureClass.EVIDENCE_CONFLICT
    if any(
        term in error
        for term in ("provider", "unavailable", "network", "connection", "rate limit")
    ):
        return FailureClass.TRANSIENT
    return FailureClass.UNKNOWN_EFFECT


def _retry_policy_allows(
    profile: AgentProfileRevision,
    *,
    failure_class: FailureClass,
    physical_attempts: int,
) -> bool:
    """Apply one frozen profile retry policy with a fail-closed default."""
    policy = profile.retry_policy
    max_attempts = policy.get("max_physical_attempts", 1)
    if not isinstance(max_attempts, int) or max_attempts < 1:
        return False
    configured = policy.get("retryable_failure_classes")
    if configured is None:
        retryable = _DEFAULT_RETRYABLE_FAILURES
    elif not isinstance(configured, (list, tuple, set, frozenset)):
        return False
    else:
        values: set[FailureClass] = set()
        for value in configured:
            if not isinstance(value, str):
                continue
            try:
                values.add(FailureClass(value))
            except ValueError:
                continue
        retryable = frozenset(values)
    return physical_attempts < max_attempts and failure_class in retryable


def _task_kind_from_mode(mode: str) -> str:
    return mode.replace(".", "_").replace("-", "_")


def _intent_payload(intent: TaskDispatchIntent) -> dict[str, Any]:
    return intent.model_dump(mode="json")


def _is_tick_complete(
    *,
    plan_revision: int,
    active_total: int,
    ready_remaining: int,
    terminal_failed: int,
) -> bool:
    """A tick is complete only when a plan exists and no work is outstanding."""
    return (
        plan_revision > 0
        and active_total == 0
        and ready_remaining == 0
        and terminal_failed == 0
    )


class ReadySetRepository:
    """PostgreSQL primitives for the generic ready-set scheduler.

    It owns no business merge, evidence admission, or effect approval. Each public
    method runs inside the caller's session and commits its own transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_root(self, claim: JobClaim) -> JobRow:
        root = await self._session.scalar(
            select(JobRow).where(JobRow.id == claim.job_id).with_for_update()
        )
        project = None if root is None else await self._session.get(ProjectRow, root.project_id)
        if (
            root is None
            or project is None
            or root.status != JobStatus.RUNNING.value
            or root.current_generation != claim.claim_generation
            or root.lease_token != claim.lease_token
            or root.basis_hash != claim.basis_hash
            or root.basis_project_revision != claim.basis_project_revision
            or project.revision != root.basis_project_revision
            or root.cancel_requested
        ):
            raise RuntimeConflictError("root claim, project revision, or basis is stale")
        return root

    async def collect(self, claim: JobClaim) -> ReadySetContext:
        """Refresh the plan frontier and gather the accounting facts for one tick."""
        root = await self.lock_root(claim)
        store = PostgresPlanStore(self._session)
        try:
            ready_tasks = await store.refresh_frontier(root_job_id=root.id)
        except PlanNotFoundError:
            await self._session.commit()
            return ReadySetContext(
                root_job_id=root.id,
                project_id=root.project_id,
                plan_revision=0,
                ready_tasks=(),
                active_total=0,
                active_per_profile={},
                token_available=0,
                tool_calls_available=0,
                bound_roles={},
                profiles={},
                terminal_failed=0,
            )

        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root.id)
        )
        if head is None:
            raise ReadySetConflictError("root Job has no plan head")
        revision_row = await self._session.scalar(
            select(PlanRevisionRow).where(
                PlanRevisionRow.root_job_id == root.id,
                PlanRevisionRow.revision == head.current_revision,
            )
        )
        if revision_row is None:
            raise ReadySetConflictError("current plan revision is missing")

        tasks = list(
            await self._session.scalars(
                select(PlanTaskRow).where(PlanTaskRow.plan_revision_id == revision_row.id)
            )
        )
        dispatched_ids = [
            task.dispatched_job_id for task in tasks if task.dispatched_job_id is not None
        ]
        children = (
            list(
                await self._session.scalars(
                    select(JobRow).where(JobRow.id.in_(dispatched_ids))
                )
            )
            if dispatched_ids
            else []
        )
        child_by_id = {child.id: child for child in children}
        active_children: list[JobRow] = []
        for task in tasks:
            child_id = task.dispatched_job_id
            child = None if child_id is None else child_by_id.get(child_id)
            if child_id is not None and child is not None and child.status in _ACTIVE_JOB_STATUSES:
                active_children.append(child)
        active_per_profile: dict[str, int] = {}
        for child in active_children:
            profile_id = child.profile_id
            active_per_profile[profile_id] = active_per_profile.get(profile_id, 0) + 1

        token_available = 0
        tool_calls_available = 0
        try:
            account_id = await BudgetLedger(self._session).get_account_id(root.id)
            account = await self._session.get(BudgetAccountRow, account_id)
            if account is not None:
                token_available = account.token_cap - account.token_committed
                tool_calls_available = account.tool_call_cap - account.tool_calls_committed
        except BudgetConflictError:
            pass

        bindings = list(
            await self._session.scalars(
                select(JobProfileBindingRow).where(JobProfileBindingRow.root_job_id == root.id)
            )
        )
        bound_roles: dict[str, ProfileBinding] = {}
        for binding_row in bindings:
            bound_roles[binding_row.role_key] = ProfileBinding(
                root_job_id=binding_row.root_job_id,
                role_key=binding_row.role_key,
                profile_id=binding_row.profile_id,
                profile_revision=binding_row.profile_revision,
                definition_hash=binding_row.definition_hash,
            )

        profile_keys = {(item.profile_id, item.profile_revision) for item in bindings}
        profile_keys |= {(task.profile_id, task.profile_revision) for task in tasks}
        profiles: dict[tuple[str, int], AgentProfileRevision] = {}
        registry = ProfileRepository(self._session)
        for profile_id, revision in profile_keys:
            try:
                profiles[(profile_id, revision)] = await registry.get_revision(
                    profile_id, revision
                )
            except ProfileNotFoundError:
                pass

        terminal_failed = sum(
            1 for task in tasks if task.status == TaskStatus.FAILED.value
        )
        await self._session.commit()
        return ReadySetContext(
            root_job_id=root.id,
            project_id=root.project_id,
            plan_revision=head.current_revision,
            ready_tasks=ready_tasks,
            active_total=len(active_children),
            active_per_profile=active_per_profile,
            token_available=token_available,
            tool_calls_available=tool_calls_available,
            bound_roles=bound_roles,
            profiles=profiles,
            terminal_failed=terminal_failed,
        )

    async def claim_and_dispatch(
        self,
        *,
        claim: JobClaim,
        logical_key: str,
        token_budget: int,
        tool_call_budget: int,
        deadline: datetime,
        max_concurrency: int,
    ) -> PlanTaskClaim | None:
        """Atomically claim one ready task and dispatch its frozen child Job.

        Returns ``None`` when a competing scheduler already claimed or dispatched
        the task; the caller treats that as a safe no-op.
        """
        root = await self.lock_root(claim)
        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root.id).with_for_update()
        )
        if head is None:
            raise ReadySetConflictError("root Job has no plan")
        revision_row = await self._session.scalar(
            select(PlanRevisionRow).where(
                PlanRevisionRow.root_job_id == root.id,
                PlanRevisionRow.revision == head.current_revision,
            )
        )
        if revision_row is None:
            raise ReadySetConflictError("current plan revision is missing")
        task = await self._session.scalar(
            select(PlanTaskRow)
            .where(
                PlanTaskRow.plan_revision_id == revision_row.id,
                PlanTaskRow.logical_key == logical_key,
            )
            .with_for_update()
        )
        if task is None:
            raise ReadySetConflictError(f"plan task not found: {logical_key}")
        if task.dispatched_job_id is not None or task.status not in {
            TaskStatus.PLANNED.value,
            TaskStatus.READY.value,
        }:
            await self._session.commit()
            return None

        node = PostgresPlanStore._node_from_row(task)
        # collect() is only a scheduling hint. This transaction owns the root
        # row, so every fact which authorizes a child must be checked again
        # against the revision and bindings actually about to be dispatched.
        binding = await self._session.scalar(
            select(JobProfileBindingRow).where(
                JobProfileBindingRow.root_job_id == root.id,
                JobProfileBindingRow.role_key == node.role_key,
            )
        )
        if (
            binding is None
            or binding.profile_id != node.profile_id
            or binding.profile_revision != node.profile_revision
        ):
            await self._session.commit()
            raise ReadySetDispatchSkipped(
                SchedulerSkipReason.CAPABILITY,
                "role binding changed after ready-set collection",
            )
        try:
            profile = await ProfileRepository(self._session).get_revision(
                node.profile_id, node.profile_revision
            )
        except ProfileNotFoundError:
            await self._session.commit()
            raise ReadySetDispatchSkipped(
                SchedulerSkipReason.CAPABILITY,
                "frozen profile revision disappeared before dispatch",
            ) from None

        revision_tasks = list(
            await self._session.scalars(
                select(PlanTaskRow).where(PlanTaskRow.plan_revision_id == revision_row.id)
            )
        )
        active_child_ids = [
            item.dispatched_job_id
            for item in revision_tasks
            if item.dispatched_job_id is not None
        ]
        active_children = (
            list(
                await self._session.scalars(
                    select(JobRow).where(
                        JobRow.id.in_(active_child_ids),
                        JobRow.status.in_(tuple(_ACTIVE_JOB_STATUSES)),
                    )
                )
            )
            if active_child_ids
            else []
        )
        if len(active_children) >= max_concurrency:
            await self._session.commit()
            raise ReadySetDispatchSkipped(
                SchedulerSkipReason.CONCURRENCY,
                f"root active count reaches max_concurrency {max_concurrency}",
            )
        active_for_profile = sum(
            child.profile_id == node.profile_id for child in active_children
        )
        if active_for_profile >= profile.concurrency_cap:
            await self._session.commit()
            raise ReadySetDispatchSkipped(
                SchedulerSkipReason.CONCURRENCY,
                f"profile {node.profile_id} reaches concurrency_cap {profile.concurrency_cap}",
            )
        active_claim = await self._session.scalar(
            select(PlanTaskClaimRow.id).where(
                PlanTaskClaimRow.plan_revision_id == revision_row.id,
                PlanTaskClaimRow.task_id == task.id,
                PlanTaskClaimRow.status.in_(
                    (TaskClaimStatus.CLAIMED.value, TaskClaimStatus.DISPATCHED.value)
                ),
            )
        )
        if active_claim is not None:
            await self._session.commit()
            return None

        generation = (
            await self._session.scalar(
                select(func.coalesce(func.max(PlanTaskClaimRow.claim_generation), 0)).where(
                    PlanTaskClaimRow.task_id == task.id
                )
            )
            or 0
        ) + 1
        claim_id = uuid4()
        lease_token = uuid4()
        now = datetime.now(UTC)
        intent = TaskDispatchIntent(
            root_job_id=root.id,
            plan_revision=head.current_revision,
            task_logical_key=node.logical_key,
            claim_generation=generation,
            graph_step_id=f"ready:{node.logical_key}:claim{generation}",
            task_kind=_task_kind_from_mode(node.mode),
            role_key=node.role_key,
            profile_id=node.profile_id,
            profile_revision=node.profile_revision,
            basis_hash=claim.basis_hash,
            shard_key=node.logical_key,
            input_refs=node.input_refs,
            token_budget=token_budget,
            tool_call_budget=tool_call_budget,
            deadline=deadline,
        )
        self._session.add(
            PlanTaskClaimRow(
                id=claim_id,
                root_job_id=root.id,
                plan_revision_id=revision_row.id,
                plan_revision=head.current_revision,
                task_id=task.id,
                logical_key=node.logical_key,
                claim_generation=generation,
                lease_owner=claim.lease_owner,
                lease_token=lease_token,
                lease_expires_at=now + timedelta(seconds=300),
                status=TaskClaimStatus.CLAIMED.value,
                intent=_intent_payload(intent),
            )
        )
        await self._session.flush()

        spec = DelegationSpec(
            delegation_id=uuid5(
                claim.attempt_id, f"ready:{intent.task_logical_key}:claim{generation}"
            ),
            parent_job_id=root.id,
            parent_attempt_id=claim.attempt_id,
            parent_claim_generation=claim.claim_generation,
            graph_step_id=intent.graph_step_id,
            task_kind=intent.task_kind,
            role_key=intent.role_key,
            profile_id=intent.profile_id,
            profile_revision=intent.profile_revision,
            basis_hash=intent.basis_hash,
            shard_key=intent.shard_key,
            idempotency_key=f"{claim.attempt_id}:ready:{intent.task_logical_key}:claim{generation}",
            input_refs=intent.input_refs,
            token_budget=intent.token_budget,
            tool_call_budget=intent.tool_call_budget,
            deadline=intent.deadline,
        )
        try:
            wave = await PostgresRuntime(self._session).create_delegation_wave(
                specs=(spec,),
                policy=JoinPolicy(
                    mode=JoinMode.ALL_REQUIRED,
                    expected_delegation_ids=(spec.delegation_id,),
                    min_successes=1,
                    deadline=intent.deadline,
                ),
                commit=False,
            )
        except RuntimeConflictError as exc:
            # BudgetLedger is the final cross-workflow serialization point. A
            # lost race must roll back our provisional claim and surface as a
            # normal scheduler skip, never abort the entire tick.
            await self._session.rollback()
            message = str(exc)
            normalized = message.lower()
            if any(term in normalized for term in ("budget", "token", "tool-call")):
                raise ReadySetDispatchSkipped(SchedulerSkipReason.BUDGET, message) from None
            if "concurrency cap" in normalized:
                raise ReadySetDispatchSkipped(
                    SchedulerSkipReason.CONCURRENCY, message
                ) from None
            if "frozen agentprofile" in normalized or "frozen role binding" in normalized:
                raise ReadySetDispatchSkipped(
                    SchedulerSkipReason.CAPABILITY, message
                ) from None
            raise
        await PostgresPlanStore(self._session).bind_task_job(
            root_job_id=root.id,
            logical_key=node.logical_key,
            dispatched_job_id=wave.child_job_ids[0],
            commit=False,
        )
        claim_row = await self._session.get(PlanTaskClaimRow, claim_id)
        if claim_row is None:
            raise ReadySetConflictError("ready-set claim row disappeared")
        claim_row.status = TaskClaimStatus.DISPATCHED.value
        claim_row.child_job_id = wave.child_job_ids[0]
        await PostgresDomainStore(self._session).append_event(
            root.project_id,
            "ready_set.task_dispatched",
            {
                "root_job_id": str(root.id),
                "task_logical_key": node.logical_key,
                "claim_generation": generation,
                "child_job_id": str(wave.child_job_ids[0]),
            },
        )
        await self._session.commit()
        return self._claim_from_row(claim_row)

    async def settle_terminal_children(self, *, claim: JobClaim) -> int:
        """Release budget committed to terminal child Jobs so successors can dispatch."""
        root = await self.lock_root(claim)
        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root.id).with_for_update()
        )
        if head is None:
            await self._session.commit()
            return 0
        revision_row = await self._session.scalar(
            select(PlanRevisionRow).where(
                PlanRevisionRow.root_job_id == root.id,
                PlanRevisionRow.revision == head.current_revision,
            )
        )
        if revision_row is None:
            await self._session.commit()
            return 0
        tasks = list(
            await self._session.scalars(
                select(PlanTaskRow)
                .where(PlanTaskRow.plan_revision_id == revision_row.id)
                .with_for_update()
            )
        )
        dispatched_ids = [
            task.dispatched_job_id for task in tasks if task.dispatched_job_id is not None
        ]
        children = (
            list(
                await self._session.scalars(
                    select(JobRow).where(JobRow.id.in_(dispatched_ids)).with_for_update()
                )
            )
            if dispatched_ids
            else []
        )
        try:
            ledger = BudgetLedger(self._session)
            account_id = await ledger.get_account_id(root.id)
        except BudgetConflictError:
            await self._session.commit()
            return 0

        terminal_children = [child for child in children if child.status in _TERMINAL_JOB_STATUSES]
        claim_status_by_child = {
            child.id: {
                JobStatus.SUCCEEDED.value: TaskClaimStatus.SUCCEEDED.value,
                JobStatus.FAILED.value: TaskClaimStatus.FAILED.value,
                JobStatus.CANCELLED.value: TaskClaimStatus.CANCELLED.value,
            }[child.status]
            for child in terminal_children
        }
        now = datetime.now(UTC)
        for child_id, claim_status in claim_status_by_child.items():
            await self._session.execute(
                update(PlanTaskClaimRow)
                .where(
                    PlanTaskClaimRow.root_job_id == root.id,
                    PlanTaskClaimRow.child_job_id == child_id,
                    PlanTaskClaimRow.status == TaskClaimStatus.DISPATCHED.value,
                )
                .values(status=claim_status, completed_at=now)
            )
        settled = 0
        for child in terminal_children:
            try:
                allocation = await ledger.get_allocation(
                    account_id=account_id,
                    owner_kind=BudgetOwnerKind.CHILD,
                    owner_ref=child.id,
                )
                await ledger.close_allocation(allocation.allocation_id)
                settled += 1
            except BudgetConflictError:
                continue
        await self._session.commit()
        return settled

    async def retry_task(self, *, claim: JobClaim, logical_key: str) -> bool:
        """Re-open a failed plan task so the ready set can dispatch a fresh attempt."""
        root = await self.lock_root(claim)
        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root.id).with_for_update()
        )
        if head is None:
            await self._session.commit()
            return False
        revision_row = await self._session.scalar(
            select(PlanRevisionRow).where(
                PlanRevisionRow.root_job_id == root.id,
                PlanRevisionRow.revision == head.current_revision,
            )
        )
        if revision_row is None:
            await self._session.commit()
            return False
        task = await self._session.scalar(
            select(PlanTaskRow)
            .where(
                PlanTaskRow.plan_revision_id == revision_row.id,
                PlanTaskRow.logical_key == logical_key,
            )
            .with_for_update()
        )
        if task is None or task.status not in {
            TaskStatus.FAILED.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.SUPERSEDED.value,
        }:
            await self._session.commit()
            return False
        reopened = await self._reopen_task_for_retry(
            root=root,
            revision_row=revision_row,
            task=task,
        )
        await self._session.commit()
        return reopened

    async def _reopen_task_for_retry(
        self,
        *,
        root: JobRow,
        revision_row: PlanRevisionRow,
        task: PlanTaskRow,
    ) -> bool:
        """Reset one terminal plan task while holding its root transaction lock."""
        child_id = task.dispatched_job_id
        if child_id is not None:
            child = await self._session.get(JobRow, child_id)
            if child is not None and child.status in _TERMINAL_JOB_STATUSES:
                try:
                    ledger = BudgetLedger(self._session)
                    account_id = await ledger.get_account_id(root.id)
                    allocation = await ledger.get_allocation(
                        account_id=account_id,
                        owner_kind=BudgetOwnerKind.CHILD,
                        owner_ref=child_id,
                    )
                    await ledger.close_allocation(allocation.allocation_id)
                except BudgetConflictError:
                    pass
        await self._session.execute(
            update(PlanTaskClaimRow)
            .where(
                PlanTaskClaimRow.plan_revision_id == revision_row.id,
                PlanTaskClaimRow.task_id == task.id,
                PlanTaskClaimRow.status.in_(
                    (
                        TaskClaimStatus.CLAIMED.value,
                        TaskClaimStatus.DISPATCHED.value,
                        TaskClaimStatus.FAILED.value,
                        TaskClaimStatus.CANCELLED.value,
                    )
                ),
            )
            .values(status=TaskClaimStatus.SUPERSEDED.value, completed_at=datetime.now(UTC))
        )
        task.status = TaskStatus.PLANNED.value
        task.dispatched_job_id = None
        return True

    async def auto_retry_terminal_tasks(self, *, claim: JobClaim) -> int:
        """Re-open only policy-authorized, terminally failed tasks.

        The decision uses the profile revision pinned into the PlanTask row. Unknown,
        permission, configuration, budget, and evidence failures remain terminal for
        diagnosis rather than being retried blindly.
        """
        root = await self.lock_root(claim)
        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root.id).with_for_update()
        )
        if head is None:
            await self._session.commit()
            return 0
        revision_row = await self._session.scalar(
            select(PlanRevisionRow).where(
                PlanRevisionRow.root_job_id == root.id,
                PlanRevisionRow.revision == head.current_revision,
            )
        )
        if revision_row is None:
            await self._session.commit()
            return 0
        failed_tasks = list(
            await self._session.scalars(
                select(PlanTaskRow)
                .where(
                    PlanTaskRow.plan_revision_id == revision_row.id,
                    PlanTaskRow.status == TaskStatus.FAILED.value,
                    PlanTaskRow.dispatched_job_id.is_not(None),
                )
                .with_for_update()
            )
        )
        retries = 0
        profiles = ProfileRepository(self._session)
        for task in failed_tasks:
            child_id = task.dispatched_job_id
            if child_id is None:
                continue
            child = await self._session.get(JobRow, child_id)
            if child is None or child.status != JobStatus.FAILED.value:
                continue
            attempt = await self._session.scalar(
                select(AttemptRow)
                .where(AttemptRow.job_id == child_id)
                .order_by(AttemptRow.claim_generation.desc())
                .limit(1)
            )
            if attempt is None:
                continue
            try:
                profile = await profiles.get_revision(task.profile_id, task.profile_revision)
            except ProfileNotFoundError:
                continue
            physical_attempts = (
                await self._session.scalar(
                    select(func.coalesce(func.max(PlanTaskClaimRow.claim_generation), 0)).where(
                        PlanTaskClaimRow.task_id == task.id
                    )
                )
                or 0
            )
            failure_class = classify_failure(attempt.normalized_error)
            if not _retry_policy_allows(
                profile,
                failure_class=failure_class,
                physical_attempts=physical_attempts,
            ):
                continue
            if not await self._reopen_task_for_retry(
                root=root,
                revision_row=revision_row,
                task=task,
            ):
                continue
            await PostgresDomainStore(self._session).append_event(
                root.project_id,
                "ready_set.task_auto_retry_scheduled",
                {
                    "root_job_id": str(root.id),
                    "task_logical_key": task.logical_key,
                    "failure_class": failure_class.value,
                    "physical_attempts": physical_attempts,
                    "max_physical_attempts": profile.retry_policy.get(
                        "max_physical_attempts", 1
                    ),
                },
            )
            retries += 1
        await self._session.commit()
        return retries

    async def recover_expired_claims(self, *, claim: JobClaim) -> int:
        """Recover expired undispatched claims and queued children that never started."""
        root = await self.lock_root(claim)
        now = datetime.now(UTC)
        rows = list(
            await self._session.scalars(
                select(PlanTaskClaimRow)
                .where(
                    PlanTaskClaimRow.root_job_id == root.id,
                    PlanTaskClaimRow.status.in_(
                        (TaskClaimStatus.CLAIMED.value, TaskClaimStatus.DISPATCHED.value)
                    ),
                    PlanTaskClaimRow.lease_expires_at < now,
                )
                .with_for_update()
            )
        )
        child_ids = [row.child_job_id for row in rows if row.child_job_id is not None]
        children = (
            {
                child.id: child
                for child in await self._session.scalars(
                    select(JobRow).where(JobRow.id.in_(child_ids)).with_for_update()
                )
            }
            if child_ids
            else {}
        )
        task_ids = [row.task_id for row in rows]
        tasks = (
            {
                task.id: task
                for task in await self._session.scalars(
                    select(PlanTaskRow).where(PlanTaskRow.id.in_(task_ids)).with_for_update()
                )
            }
            if task_ids
            else {}
        )
        has_expired_dispatched = any(
            row.status == TaskClaimStatus.DISPATCHED.value for row in rows
        )
        ledger: BudgetLedger | None = None
        account_id: UUID | None = None
        if has_expired_dispatched:
            try:
                ledger = BudgetLedger(self._session)
                account_id = await ledger.get_account_id(root.id)
            except BudgetConflictError as exc:
                raise ReadySetConflictError(
                    "expired ready-set child budget account is unavailable"
                ) from exc

        recovered = 0
        for row in rows:
            if row.status == TaskClaimStatus.CLAIMED.value:
                row.status = TaskClaimStatus.FAILED.value
                row.completed_at = now
                recovered += 1
                continue

            child = None if row.child_job_id is None else children.get(row.child_job_id)
            # A running child owns a valid worker lease and must not be cancelled
            # merely because the scheduler's dispatch lease expired. Queued is the
            # only state that proves no worker ever claimed this child.
            if child is None or child.status != JobStatus.QUEUED.value:
                continue
            child.cancel_requested = True
            child.status = JobStatus.CANCELLED.value
            child.completed_at = now
            child.lease_owner = None
            child.lease_token = None
            child.lease_expires_at = None
            await self._session.execute(
                update(AttemptRow)
                .where(
                    AttemptRow.job_id == child.id,
                    AttemptRow.status == AttemptStatus.RUNNING.value,
                )
                .values(
                    status=AttemptStatus.CANCELLED.value,
                    completed_at=now,
                    normalized_error="ready_set_dispatch_lease_expired",
                )
            )
            await self._session.execute(
                update(DelegationRow)
                .where(
                    DelegationRow.child_job_id == child.id,
                    DelegationRow.status == "pending",
                )
                .values(status=DelegationStatus.CANCELLED.value, completed_at=now)
            )
            if ledger is None or account_id is None:
                raise ReadySetConflictError("expired ready-set child has no budget ledger")
            try:
                await ledger.reconcile_reclaimed_owner(
                    account_id=account_id,
                    owner_kind=BudgetOwnerKind.CHILD,
                    owner_ref=child.id,
                    normalized_error="ready_set_dispatch_lease_expired",
                )
            except BudgetConflictError as exc:
                raise ReadySetConflictError(
                    "expired ready-set child budget could not be reconciled"
                ) from exc
            task = tasks.get(row.task_id)
            if task is not None and task.dispatched_job_id == child.id:
                task.status = TaskStatus.PLANNED.value
                task.dispatched_job_id = None
            row.status = TaskClaimStatus.SUPERSEDED.value
            row.completed_at = now
            await PostgresDomainStore(self._session).append_event(
                root.project_id,
                "ready_set.task_dispatch_expired",
                {
                    "root_job_id": str(root.id),
                    "task_logical_key": row.logical_key,
                    "claim_generation": row.claim_generation,
                    "child_job_id": str(child.id),
                },
            )
            recovered += 1
        await self._session.commit()
        return recovered

    @staticmethod
    def _claim_from_row(row: PlanTaskClaimRow) -> PlanTaskClaim:
        return PlanTaskClaim(
            claim_id=row.id,
            root_job_id=row.root_job_id,
            plan_revision=row.plan_revision,
            task_logical_key=row.logical_key,
            claim_generation=row.claim_generation,
            lease_owner=row.lease_owner,
            lease_token=row.lease_token,
            lease_expires_at=row.lease_expires_at,
            status=TaskClaimStatus(row.status),
            child_job_id=row.child_job_id,
            intent=TaskDispatchIntent.model_validate(row.intent),
            created_at=row.created_at,
            completed_at=row.completed_at,
        )


class ReadySetScheduler:
    """Generic event-driven ready-set scheduler over the durable runtime.

    It holds one claimed root Job and dispatches plan tasks whose dependencies are
    satisfied, bounding in-flight work by ``default_concurrency``, frozen per-profile
    concurrency caps, and the root's budget account. Repeated or racing ticks are
    idempotent: the persistent claim ledger is the exactly-once barrier.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        default_concurrency: int = 8,
    ) -> None:
        if default_concurrency < 1:
            raise ValueError("default_concurrency must be positive")
        self._factory = session_factory
        self._default_concurrency = default_concurrency

    async def tick(self, claim: JobClaim) -> SchedulerTickResult:
        # Recover queued children whose scheduler-side dispatch lease expired
        # before they reached a worker, then make the recovered task visible to
        # the normal frontier refresh below.
        async with self._factory() as session:
            await ReadySetRepository(session).recover_expired_claims(claim=claim)
        # Settle terminal children first so released budget is visible to the
        # eligibility scan below and flows back into the ready set immediately.
        async with self._factory() as session:
            settled = await ReadySetRepository(session).settle_terminal_children(claim=claim)
        # Retry eligibility is a frozen per-profile policy. The helper only
        # reopens explicitly retryable terminal failures; the ordinary frontier
        # refresh below then dispatches them under the usual budget/capacity gates.
        async with self._factory() as session:
            auto_retries = await ReadySetRepository(session).auto_retry_terminal_tasks(
                claim=claim
            )
        async with self._factory() as session:
            context = await ReadySetRepository(session).collect(claim)

        dispatched: list[PlanTaskClaim] = []
        skipped: list[SchedulerSkip] = []
        active_total = context.active_total
        active_per_profile = dict(context.active_per_profile)
        token_available = context.token_available
        tool_calls_available = context.tool_calls_available

        for node in context.ready_tasks:
            skip = self._eligibility(
                node=node,
                context=context,
                active_total=active_total,
                active_per_profile=active_per_profile,
                token_available=token_available,
                tool_calls_available=tool_calls_available,
            )
            if skip is not None:
                skipped.append(skip)
                continue
            profile = context.profiles[(node.profile_id, node.profile_revision)]
            deadline = datetime.now(UTC) + timedelta(seconds=profile.timeout_seconds)
            try:
                async with self._factory() as session:
                    record = await ReadySetRepository(session).claim_and_dispatch(
                        claim=claim,
                        logical_key=node.logical_key,
                        token_budget=profile.token_cap,
                        tool_call_budget=profile.tool_call_cap,
                        deadline=deadline,
                        max_concurrency=self._default_concurrency,
                    )
            except ReadySetDispatchSkipped as exc:
                # A locked re-check can reject facts that were valid in the
                # earlier collect() snapshot. It is a normal, diagnosable
                # scheduler outcome rather than a failed root execution.
                skipped.append(
                    SchedulerSkip(
                        task_logical_key=node.logical_key,
                        reason=exc.reason,
                        detail=exc.detail,
                    )
                )
                continue
            if record is None:
                skipped.append(
                    SchedulerSkip(
                        task_logical_key=node.logical_key,
                        reason=SchedulerSkipReason.ALREADY_CLAIMED,
                        detail="a competing scheduler claimed this task",
                    )
                )
                continue
            dispatched.append(record)
            active_total += 1
            active_per_profile[profile.profile_id] = (
                active_per_profile.get(profile.profile_id, 0) + 1
            )
            token_available -= record.intent.token_budget
            tool_calls_available -= record.intent.tool_call_budget

        ready_remaining = max(0, len(context.ready_tasks) - len(dispatched))
        return SchedulerTickResult(
            root_job_id=claim.job_id,
            plan_revision=context.plan_revision,
            dispatched=tuple(dispatched),
            skipped=tuple(skipped),
            active_count=active_total,
            ready_remaining=ready_remaining,
            terminal_failed=context.terminal_failed,
            settled_child_count=settled,
            auto_retry_count=auto_retries,
            complete=_is_tick_complete(
                plan_revision=context.plan_revision,
                active_total=active_total,
                ready_remaining=ready_remaining,
                terminal_failed=context.terminal_failed,
            ),
        )

    async def retry_task(self, *, claim: JobClaim, logical_key: str) -> bool:
        async with self._factory() as session:
            return await ReadySetRepository(session).retry_task(
                claim=claim, logical_key=logical_key
            )

    async def recover_expired_claims(self, *, claim: JobClaim) -> int:
        async with self._factory() as session:
            return await ReadySetRepository(session).recover_expired_claims(claim=claim)

    def _eligibility(
        self,
        *,
        node: TaskNode,
        context: ReadySetContext,
        active_total: int,
        active_per_profile: dict[str, int],
        token_available: int,
        tool_calls_available: int,
    ) -> SchedulerSkip | None:
        binding = context.bound_roles.get(node.role_key)
        if (
            binding is None
            or binding.profile_id != node.profile_id
            or binding.profile_revision != node.profile_revision
        ):
            return SchedulerSkip(
                task_logical_key=node.logical_key,
                reason=SchedulerSkipReason.CAPABILITY,
                detail=f"role {node.role_key} is not bound to the frozen task profile",
            )
        profile = context.profiles.get((node.profile_id, node.profile_revision))
        if profile is None:
            return SchedulerSkip(
                task_logical_key=node.logical_key,
                reason=SchedulerSkipReason.CAPABILITY,
                detail="frozen profile revision is not registered",
            )
        if active_total >= self._default_concurrency:
            return SchedulerSkip(
                task_logical_key=node.logical_key,
                reason=SchedulerSkipReason.CONCURRENCY,
                detail=(
                    f"root active count {active_total} reaches max_concurrency "
                    f"{self._default_concurrency}"
                ),
            )
        if active_per_profile.get(profile.profile_id, 0) >= profile.concurrency_cap:
            return SchedulerSkip(
                task_logical_key=node.logical_key,
                reason=SchedulerSkipReason.CONCURRENCY,
                detail=(
                    f"profile {profile.profile_id} reaches concurrency_cap "
                    f"{profile.concurrency_cap}"
                ),
            )
        if token_available < profile.token_cap or tool_calls_available < profile.tool_call_cap:
            return SchedulerSkip(
                task_logical_key=node.logical_key,
                reason=SchedulerSkipReason.BUDGET,
                detail=(
                    f"budget shortfall tokens {token_available}/{profile.token_cap}, "
                    f"tools {tool_calls_available}/{profile.tool_call_cap}"
                ),
            )
        return None
