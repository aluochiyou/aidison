from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aidison.infrastructure.orm import (
    JobRow,
    PlanHeadRow,
    PlanPatchRow,
    PlanRevisionRow,
    PlanTaskEdgeRow,
    PlanTaskRow,
    ProjectRow,
    ReplanReceiptRow,
)
from aidison.runtime.contracts import JobClaim, JobStatus
from aidison.runtime.planning import (
    OrchestrationPlanRevision,
    PlanPatchKind,
    PlanPatchProposal,
    ReplanReceipt,
    TaskEdge,
    TaskEdgeKind,
    TaskNode,
    TaskStatus,
)


class PlanConflictError(RuntimeError):
    """Raised when an immutable plan or its compare-and-swap basis is stale."""


class PlanNotFoundError(RuntimeError):
    pass


class PostgresPlanStore:
    """Durable plan history layered over the existing Job/Attempt runtime.

    It deliberately owns no lease, child result, or budget state.  Those facts remain
    in `PostgresRuntime`; this adapter only protects plan history and its current head.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_initial(
        self,
        *,
        claim: JobClaim,
        plan: OrchestrationPlanRevision,
        commit: bool = True,
    ) -> OrchestrationPlanRevision:
        if plan.revision != 1 or plan.parent_revision is not None:
            raise PlanConflictError("initial plan must be revision 1 without a parent")
        root = await self._lock_current_root(claim)
        self._assert_plan_matches_root(plan=plan, root=root)
        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root.id).with_for_update()
        )
        if head is not None:
            current = await self._get_revision(root.id, head.current_revision)
            if current.plan_hash != plan.plan_hash:
                raise PlanConflictError("root Job already has a different initial plan")
            if commit:
                await self._session.commit()
            return current

        await self._insert_revision(plan=plan, root=root)
        self._session.add(
            PlanHeadRow(
                root_job_id=root.id,
                current_revision=plan.revision,
                current_plan_hash=plan.plan_hash,
            )
        )
        if commit:
            await self._session.commit()
        return plan

    async def apply_patch(
        self,
        *,
        claim: JobClaim,
        patch: PlanPatchProposal,
        commit: bool = True,
    ) -> ReplanReceipt:
        root = await self._lock_current_root(claim)
        if patch.root_job_id != str(root.id):
            raise PlanConflictError("plan patch does not belong to the claimed root Job")
        if patch.new_plan.basis_hash != root.basis_hash:
            raise PlanConflictError("plan patch basis hash is stale")

        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root.id).with_for_update()
        )
        if head is None:
            raise PlanNotFoundError("root Job has no initial plan")

        existing = await self._session.scalar(
            select(ReplanReceiptRow)
            .where(
                ReplanReceiptRow.root_job_id == root.id,
                ReplanReceiptRow.parent_claim_generation == claim.claim_generation,
                ReplanReceiptRow.base_revision == patch.base_revision,
                ReplanReceiptRow.patch_hash == patch.patch_hash,
            )
            .with_for_update()
        )
        if existing is not None:
            new_revision = await self._session.scalar(
                select(PlanRevisionRow.revision).where(
                    PlanRevisionRow.id == existing.new_plan_revision_id
                )
            )
            if new_revision is None:
                raise PlanConflictError("replan receipt points to a missing immutable revision")
            receipt = self._receipt_from_row(existing, new_revision=new_revision)
            if (
                receipt.parent_attempt_id != claim.attempt_id
                or receipt.basis_hash != claim.basis_hash
            ):
                raise PlanConflictError("replan receipt identity does not match the current claim")
            if commit:
                await self._session.commit()
            return receipt

        if (
            head.current_revision != patch.base_revision
            or head.current_plan_hash != patch.base_plan_hash
        ):
            raise PlanConflictError("plan head is stale for this patch")
        base = await self._get_revision(root.id, patch.base_revision, lock=True)
        if base.plan_hash != patch.base_plan_hash:
            raise PlanConflictError("base plan revision does not match the plan head")
        if patch.new_plan.basis_hash != base.basis_hash:
            raise PlanConflictError("replan cannot change the root basis")
        if patch.new_plan.revision != base.revision + 1:
            raise PlanConflictError("replan must create exactly the next revision")

        revision_row = await self._insert_revision(plan=patch.new_plan, root=root)
        self._session.add(
            PlanPatchRow(
                id=uuid4(),
                root_job_id=root.id,
                base_revision=patch.base_revision,
                target_revision=patch.new_plan.revision,
                patch_hash=patch.patch_hash,
                kind=patch.kind.value,
                trigger=patch.trigger,
                payload=patch.model_dump(mode="json", exclude={"patch_hash", "new_plan"}),
            )
        )
        receipt = ReplanReceipt(
            root_job_id=root.id,
            parent_attempt_id=claim.attempt_id,
            parent_claim_generation=claim.claim_generation,
            basis_hash=claim.basis_hash,
            base_revision=patch.base_revision,
            new_revision=patch.new_plan.revision,
            patch_hash=patch.patch_hash,
        )
        self._session.add(
            ReplanReceiptRow(
                id=uuid4(),
                root_job_id=root.id,
                parent_attempt_id=receipt.parent_attempt_id,
                parent_claim_generation=receipt.parent_claim_generation,
                basis_hash=receipt.basis_hash,
                base_revision=receipt.base_revision,
                patch_hash=receipt.patch_hash,
                new_plan_revision_id=revision_row.id,
            )
        )
        head.current_revision = patch.new_plan.revision
        head.current_plan_hash = patch.new_plan.plan_hash
        if commit:
            await self._session.commit()
        return receipt

    async def get_current(self, *, root_job_id: UUID) -> OrchestrationPlanRevision:
        head = await self._session.get(PlanHeadRow, root_job_id)
        if head is None:
            raise PlanNotFoundError("root Job has no plan")
        revision = await self._get_revision(root_job_id, head.current_revision)
        if revision.plan_hash != head.current_plan_hash:
            raise PlanConflictError("plan head hash does not match the immutable revision")
        return revision

    async def list_ready_frontier(self, *, root_job_id: UUID) -> tuple[TaskNode, ...]:
        head = await self._session.get(PlanHeadRow, root_job_id)
        if head is None:
            raise PlanNotFoundError("root Job has no plan")
        revision = await self._get_revision_row(root_job_id, head.current_revision)
        dependency = aliased(PlanTaskRow)
        unresolved_dependency = exists(
            select(PlanTaskEdgeRow.id)
            .join(dependency, dependency.id == PlanTaskEdgeRow.from_task_id)
            .where(
                PlanTaskEdgeRow.plan_revision_id == revision.id,
                PlanTaskEdgeRow.to_task_id == PlanTaskRow.id,
                PlanTaskEdgeRow.kind == TaskEdgeKind.DEPENDS_ON.value,
                dependency.status != TaskStatus.SUCCEEDED.value,
            )
        )
        rows = list(
            await self._session.scalars(
                select(PlanTaskRow)
                .where(
                    PlanTaskRow.plan_revision_id == revision.id,
                    PlanTaskRow.status.in_((TaskStatus.PLANNED.value, TaskStatus.READY.value)),
                    ~unresolved_dependency,
                )
                .order_by(PlanTaskRow.depth, PlanTaskRow.logical_key, PlanTaskRow.id)
            )
        )
        return tuple(self._node_from_row(row) for row in rows)

    async def bind_task_job(
        self,
        *,
        root_job_id: UUID,
        logical_key: str,
        dispatched_job_id: UUID,
        commit: bool = True,
    ) -> None:
        """Atomically connect a planned node to an existing durable child Job."""
        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root_job_id).with_for_update()
        )
        if head is None:
            raise PlanNotFoundError("root Job has no plan")
        task = await self._session.scalar(
            select(PlanTaskRow)
            .where(
                PlanTaskRow.plan_revision_id
                == select(PlanRevisionRow.id)
                .where(
                    PlanRevisionRow.root_job_id == root_job_id,
                    PlanRevisionRow.revision == head.current_revision,
                )
                .scalar_subquery(),
                PlanTaskRow.logical_key == logical_key,
            )
            .with_for_update()
        )
        child = await self._session.get(JobRow, dispatched_job_id)
        if task is None or child is None or child.parent_job_id != root_job_id:
            raise PlanConflictError("task-to-child binding is stale or invalid")
        if task.dispatched_job_id == child.id:
            if commit:
                await self._session.commit()
            return
        if task.status not in {TaskStatus.PLANNED.value, TaskStatus.READY.value}:
            raise PlanConflictError("task-to-child binding is stale or invalid")
        if task.dispatched_job_id is not None:
            raise PlanConflictError("task-to-child binding is stale or invalid")
        task.dispatched_job_id = child.id
        task.status = TaskStatus.DISPATCHED.value
        if commit:
            await self._session.commit()

    async def refresh_frontier(self, *, root_job_id: UUID) -> tuple[TaskNode, ...]:
        """Refresh task projections from Job state and mark impossible dependencies blocked."""
        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root_job_id).with_for_update()
        )
        if head is None:
            raise PlanNotFoundError("root Job has no plan")
        revision = await self._get_revision_row(root_job_id, head.current_revision, lock=True)
        tasks = list(
            await self._session.scalars(
                select(PlanTaskRow)
                .where(PlanTaskRow.plan_revision_id == revision.id)
                .order_by(PlanTaskRow.depth, PlanTaskRow.logical_key, PlanTaskRow.id)
                .with_for_update()
            )
        )
        job_ids = [item.dispatched_job_id for item in tasks if item.dispatched_job_id is not None]
        jobs = (
            list(await self._session.scalars(select(JobRow).where(JobRow.id.in_(job_ids))))
            if job_ids
            else []
        )
        job_status = {job.id: job.status for job in jobs}
        status_by_id: dict[UUID, str] = {}
        for task in tasks:
            status = task.status
            if task.dispatched_job_id is not None:
                runtime_status = job_status.get(task.dispatched_job_id)
                status = (
                    {
                        JobStatus.QUEUED.value: TaskStatus.DISPATCHED.value,
                        JobStatus.RUNNING.value: TaskStatus.RUNNING.value,
                        JobStatus.SUCCEEDED.value: TaskStatus.SUCCEEDED.value,
                        JobStatus.FAILED.value: TaskStatus.FAILED.value,
                        JobStatus.CANCELLED.value: TaskStatus.CANCELLED.value,
                    }.get(runtime_status, status)
                    if runtime_status is not None
                    else status
                )
            status_by_id[task.id] = status

        edges = list(
            await self._session.scalars(
                select(PlanTaskEdgeRow).where(
                    PlanTaskEdgeRow.plan_revision_id == revision.id,
                    PlanTaskEdgeRow.kind == TaskEdgeKind.DEPENDS_ON.value,
                )
            )
        )
        predecessors: dict[UUID, list[str]] = {task.id: [] for task in tasks}
        for edge in edges:
            predecessors[edge.to_task_id].append(status_by_id[edge.from_task_id])
        for task in tasks:
            if status_by_id[task.id] in {TaskStatus.PLANNED.value, TaskStatus.READY.value}:
                dependency_statuses = predecessors[task.id]
                if any(
                    status
                    in {
                        TaskStatus.FAILED.value,
                        TaskStatus.CANCELLED.value,
                        TaskStatus.SUPERSEDED.value,
                        TaskStatus.BLOCKED.value,
                    }
                    for status in dependency_statuses
                ):
                    status_by_id[task.id] = TaskStatus.BLOCKED.value
                elif all(status == TaskStatus.SUCCEEDED.value for status in dependency_statuses):
                    status_by_id[task.id] = TaskStatus.READY.value
                else:
                    status_by_id[task.id] = TaskStatus.PLANNED.value
            if task.status != status_by_id[task.id]:
                task.status = status_by_id[task.id]

        await self._session.commit()
        return tuple(
            self._node_from_row(task)
            for task in tasks
            if status_by_id[task.id] == TaskStatus.READY.value
        )

    async def _lock_current_root(self, claim: JobClaim) -> JobRow:
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
            raise PlanConflictError("root claim, project revision, or basis is stale")
        return root

    def _assert_plan_matches_root(self, *, plan: OrchestrationPlanRevision, root: JobRow) -> None:
        if plan.root_job_id != str(root.id):
            raise PlanConflictError("plan does not belong to the claimed root Job")
        if plan.basis_hash != root.basis_hash:
            raise PlanConflictError("plan basis hash is stale")

    async def _insert_revision(
        self,
        *,
        plan: OrchestrationPlanRevision,
        root: JobRow,
    ) -> PlanRevisionRow:
        row = PlanRevisionRow(
            id=uuid4(),
            root_job_id=root.id,
            revision=plan.revision,
            parent_revision=plan.parent_revision,
            basis_hash=plan.basis_hash,
            basis_project_revision=root.basis_project_revision,
            reason=plan.reason,
            evidence_refs=list(plan.evidence_refs),
            planner_profile_id=plan.planner_profile_id,
            planner_profile_revision=plan.planner_profile_revision,
            plan_hash=plan.plan_hash,
        )
        self._session.add(row)
        await self._session.flush()
        task_ids: dict[str, UUID] = {}
        for node in plan.nodes:
            task_id = uuid4()
            task_ids[node.logical_key] = task_id
            self._session.add(
                PlanTaskRow(
                    id=task_id,
                    plan_revision_id=row.id,
                    logical_key=node.logical_key,
                    objective=node.objective,
                    mode=node.mode,
                    role_key=node.role_key,
                    profile_id=node.profile_id,
                    profile_revision=node.profile_revision,
                    budget_ref=node.budget_ref,
                    status=node.status.value,
                    depth=node.depth,
                    input_refs=list(node.input_refs),
                    success_criteria=list(node.success_criteria),
                    stop_criteria=list(node.stop_criteria),
                )
            )
        await self._session.flush()
        for edge in plan.edges:
            self._session.add(
                PlanTaskEdgeRow(
                    id=uuid4(),
                    plan_revision_id=row.id,
                    from_task_id=task_ids[edge.from_key],
                    to_task_id=task_ids[edge.to_key],
                    kind=edge.kind.value,
                )
            )
        await self._session.flush()
        return row

    async def _get_revision(
        self,
        root_job_id: UUID,
        revision: int,
        *,
        lock: bool = False,
    ) -> OrchestrationPlanRevision:
        row = await self._get_revision_row(root_job_id, revision, lock=lock)
        tasks = list(
            await self._session.scalars(
                select(PlanTaskRow)
                .where(PlanTaskRow.plan_revision_id == row.id)
                .order_by(PlanTaskRow.logical_key, PlanTaskRow.id)
            )
        )
        task_by_id = {item.id: item for item in tasks}
        edge_rows = list(
            await self._session.scalars(
                select(PlanTaskEdgeRow)
                .where(PlanTaskEdgeRow.plan_revision_id == row.id)
                .order_by(
                    PlanTaskEdgeRow.kind, PlanTaskEdgeRow.from_task_id, PlanTaskEdgeRow.to_task_id
                )
            )
        )
        edges: list[TaskEdge] = []
        for edge in edge_rows:
            source = task_by_id.get(edge.from_task_id)
            target = task_by_id.get(edge.to_task_id)
            if source is None or target is None:
                raise PlanConflictError("plan edge points outside its revision")
            edges.append(
                TaskEdge(
                    from_key=source.logical_key,
                    to_key=target.logical_key,
                    kind=TaskEdgeKind(edge.kind),
                )
            )
        return OrchestrationPlanRevision(
            root_job_id=str(row.root_job_id),
            revision=row.revision,
            parent_revision=row.parent_revision,
            basis_hash=row.basis_hash,
            reason=row.reason,
            evidence_refs=tuple(row.evidence_refs),
            planner_profile_id=row.planner_profile_id,
            planner_profile_revision=row.planner_profile_revision,
            plan_hash=row.plan_hash,
            nodes=tuple(self._node_from_row(task) for task in tasks),
            edges=tuple(edges),
        )

    async def _get_revision_row(
        self,
        root_job_id: UUID,
        revision: int,
        *,
        lock: bool = False,
    ) -> PlanRevisionRow:
        statement = select(PlanRevisionRow).where(
            PlanRevisionRow.root_job_id == root_job_id,
            PlanRevisionRow.revision == revision,
        )
        if lock:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        if row is None:
            raise PlanNotFoundError("plan revision not found")
        return row

    @staticmethod
    def _node_from_row(row: PlanTaskRow) -> TaskNode:
        return TaskNode(
            logical_key=row.logical_key,
            objective=row.objective,
            mode=row.mode,
            role_key=row.role_key,
            profile_id=row.profile_id,
            profile_revision=row.profile_revision,
            budget_ref=row.budget_ref,
            status=TaskStatus(row.status),
            depth=row.depth,
            input_refs=tuple(row.input_refs),
            success_criteria=tuple(row.success_criteria),
            stop_criteria=tuple(row.stop_criteria),
        )

    @staticmethod
    def _receipt_from_row(row: ReplanReceiptRow, *, new_revision: int) -> ReplanReceipt:
        return ReplanReceipt(
            root_job_id=row.root_job_id,
            parent_attempt_id=row.parent_attempt_id,
            parent_claim_generation=row.parent_claim_generation,
            basis_hash=row.basis_hash,
            base_revision=row.base_revision,
            new_revision=new_revision,
            patch_hash=row.patch_hash,
        )


def build_revision_from_patch(
    *,
    base: OrchestrationPlanRevision,
    patch_kind: PlanPatchKind,
    trigger: str,
    new_nodes: tuple[TaskNode, ...] = (),
    new_edges: tuple[TaskEdge, ...] = (),
    retired_keys: tuple[str, ...] = (),
    reason: str = "",
) -> OrchestrationPlanRevision:
    """Deterministically produce the next revision from a plan patch.

    This is a pure helper: it computes the canonical hash of the resulting plan so
    the caller can build a PlanPatchProposal to pass to apply_patch().

    - *new_nodes* / *new_edges* are appended; retired keys are dropped.
    - Retired nodes that already reached dispatched/succeeded/failed remain superseded.
    """

    if patch_kind is PlanPatchKind.CONTRACT and not retired_keys:
        raise ValueError("contract patch requires at least one retired key")
    if patch_kind is PlanPatchKind.EXPAND and not new_nodes:
        raise ValueError("expand patch requires at least one new node")

    existing_keys = {node.logical_key for node in base.nodes}
    retired_set = set(retired_keys)
    for key in retired_set:
        if key not in existing_keys:
            raise ValueError(f"retired key {key!r} does not exist in the base plan")

    new_keys = {node.logical_key for node in new_nodes}
    if new_keys & existing_keys:
        raise ValueError(f"new node keys collide with existing: {new_keys & existing_keys}")
    if new_keys & retired_set:
        raise ValueError(f"new node keys collide with retired: {new_keys & retired_set}")

    retired_nodes = [
        node
        for node in base.nodes
        if node.logical_key in retired_set
        and node.status
        in {TaskStatus.DISPATCHED, TaskStatus.RUNNING, TaskStatus.SUCCEEDED, TaskStatus.FAILED}
    ]
    kept = [node for node in base.nodes if node.logical_key not in retired_set]
    superseded = tuple(
        node.model_copy(update={"status": TaskStatus.SUPERSEDED}) for node in retired_nodes
    )
    fresh_nodes = kept + list(superseded) + list(new_nodes)
    # Drop any edge that references a retired key, so no dangling edges remain.
    edge_dropped = any(
        edge.from_key in retired_set or edge.to_key in retired_set
        for edge in base.edges
    )
    node_keys = {node.logical_key for node in fresh_nodes}
    fresh_edges = tuple(
        edge for edge in base.edges
        if edge.from_key not in retired_set and edge.to_key not in retired_set
    ) + new_edges

    # Normalize node depths only when edges were removed, because the
    # validator enforces depth == dependency depth.  Pure EXPAND keeps
    # original depths intact.
    if edge_dropped:
        adjacency: dict[str, set[str]] = {key: set() for key in node_keys}
        indegree: dict[str, int] = {key: 0 for key in node_keys}
        for edge in fresh_edges:
            if edge.to_key not in adjacency[edge.from_key]:
                adjacency[edge.from_key].add(edge.to_key)
                indegree[edge.to_key] += 1
        graph_depth: dict[str, int] = {key: 0 for key in node_keys}
        frontier = [key for key in node_keys if indegree[key] == 0]
        while frontier:
            current = frontier.pop()
            for successor in adjacency[current]:
                graph_depth[successor] = max(graph_depth[successor], graph_depth[current] + 1)
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    frontier.append(successor)
        normalized: list[TaskNode] = []
        for node in fresh_nodes:
            d = graph_depth.get(node.logical_key, 0)
            normalized.append(
                node if node.depth == d else node.model_copy(update={"depth": d})
            )
        final_nodes = tuple(normalized)
    else:
        final_nodes = tuple(fresh_nodes)

    return OrchestrationPlanRevision(
        root_job_id=base.root_job_id,
        revision=base.revision + 1,
        parent_revision=base.revision,
        basis_hash=base.basis_hash,
        reason=reason or f"patch {patch_kind.value}: {trigger}",
        planner_profile_id=base.planner_profile_id,
        planner_profile_revision=base.planner_profile_revision,
        nodes=final_nodes,
        edges=fresh_edges,
    )
