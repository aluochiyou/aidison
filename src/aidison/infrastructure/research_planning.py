from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import aliased

from aidison.infrastructure.orm import (
    AttemptResultRow,
    PlanGapRow,
    PlanHeadRow,
    PlanTaskRow,
)
from aidison.infrastructure.planning import (
    PlanConflictError,
    PlanNotFoundError,
    PostgresPlanStore,
)
from aidison.research.planning import GapStatus, ResearchGap
from aidison.runtime.contracts import ResultDisposition


class PostgresResearchPlanStore(PostgresPlanStore):
    """Research gap persistence layered on the business-neutral plan store."""

    async def record_gap(self, *, gap: ResearchGap) -> ResearchGap:
        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == gap.root_job_id)
        )
        if head is None:
            raise PlanNotFoundError("root Job has no plan")

        revision = await self._get_revision_row(gap.root_job_id, head.current_revision)
        if gap.plan_revision != revision.revision:
            raise PlanConflictError(
                f"gap plan_revision {gap.plan_revision} does not match current head "
                f"revision {revision.revision}"
            )

        existing = await self._session.scalar(
            select(PlanGapRow).where(
                PlanGapRow.root_job_id == gap.root_job_id,
                PlanGapRow.plan_revision_id == revision.id,
                PlanGapRow.gap_hash == gap.gap_hash,
            )
        )
        if existing is not None:
            return ResearchGap.model_validate(
                {
                    **existing.payload,
                    "root_job_id": existing.root_job_id,
                    "plan_revision": revision.revision,
                    "source_result_id": existing.source_result_id,
                    "gap_hash": existing.gap_hash,
                    "status": existing.status,
                    "priority": existing.priority,
                }
            )

        task = await self._session.scalar(
            select(PlanTaskRow).where(
                PlanTaskRow.plan_revision_id == revision.id,
                PlanTaskRow.logical_key == gap.task_logical_key,
            )
        )
        if task is None:
            raise PlanConflictError("gap references a task that does not belong to this revision")
        if gap.source_result_id is not None:
            source = await self._session.get(AttemptResultRow, gap.source_result_id)
            if (
                source is None
                or source.disposition != ResultDisposition.ELIGIBLE.value
                or source.result_hash != gap.source_result_hash
                or task.dispatched_job_id != source.job_id
            ):
                raise PlanConflictError("gap source is not an eligible result for this plan task")

        self._session.add(
            PlanGapRow(
                id=uuid4(),
                root_job_id=gap.root_job_id,
                plan_revision_id=revision.id,
                source_task_id=task.id,
                source_result_id=gap.source_result_id,
                gap_hash=gap.gap_hash,
                status=GapStatus.OPEN.value,
                priority=gap.priority,
                payload=gap.model_dump(mode="json", exclude={"status", "priority"}),
            )
        )
        await self._session.flush()
        return gap

    async def list_open_gaps(
        self,
        *,
        root_job_id: UUID,
        min_priority: int = 0,
    ) -> tuple[ResearchGap, ...]:
        head = await self._session.get(PlanHeadRow, root_job_id)
        if head is None:
            raise PlanNotFoundError("root Job has no plan")
        revision = await self._get_revision_row(root_job_id, head.current_revision)
        task_alias = aliased(PlanTaskRow)
        rows = list(
            await self._session.scalars(
                select(PlanGapRow)
                .join(task_alias, task_alias.id == PlanGapRow.source_task_id)
                .where(
                    PlanGapRow.root_job_id == root_job_id,
                    PlanGapRow.plan_revision_id == revision.id,
                    PlanGapRow.status == GapStatus.OPEN.value,
                )
                .order_by(PlanGapRow.created_at)
            )
        )
        gaps: list[ResearchGap] = []
        for row in rows:
            if row.priority < min_priority:
                continue
            gaps.append(
                ResearchGap(
                    root_job_id=row.root_job_id,
                    task_logical_key=row.payload["task_logical_key"],
                    plan_revision=revision.revision,
                    source_result_id=row.source_result_id,
                    source_result_hash=row.payload.get("source_result_hash"),
                    gap_hash=row.gap_hash,
                    category=row.payload["category"],
                    description=row.payload["description"],
                    module_refs=tuple(row.payload.get("module_refs", ())),
                    evidence_refs=tuple(row.payload.get("evidence_refs", ())),
                    status=GapStatus(row.status),
                    priority=row.priority,
                    bound=row.payload.get("bound", 1),
                )
            )
        return tuple(gaps)

    async def resolve_gaps(
        self,
        *,
        root_job_id: UUID,
        gap_hashes: tuple[str, ...],
        status: GapStatus,
    ) -> int:
        if not gap_hashes:
            return 0
        if status == GapStatus.OPEN:
            raise ValueError("resolve_gaps must target a non-OPEN status")
        head = await self._session.scalar(
            select(PlanHeadRow).where(PlanHeadRow.root_job_id == root_job_id).with_for_update()
        )
        if head is None:
            raise PlanNotFoundError("root Job has no plan")
        await self._get_revision_row(root_job_id, head.current_revision, lock=True)
        result = await self._session.execute(
            update(PlanGapRow)
            .where(
                PlanGapRow.root_job_id == root_job_id,
                PlanGapRow.gap_hash.in_(gap_hashes),
                PlanGapRow.status == GapStatus.OPEN.value,
            )
            .values(status=status.value)
        )
        await self._session.flush()
        return int(cast(Any, result).rowcount)
