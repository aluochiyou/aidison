from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from aidison.application.ports import DomainStore
from aidison.domain.events import (
    DomainEventMetadata,
    ExecutionPlanEventType,
    execution_plan_event_id,
)
from aidison.domain.models import (
    AdjustmentBatch,
    AdjustmentBatchStatus,
    BlueprintStatus,
    BomItem,
    Candidate,
    ChangeImpactPreview,
    CompatibilityFinding,
    CompatibilityStatus,
    DecisionOption,
    DecisionRequest,
    DecisionStatus,
    DraftHistoryEntry,
    DraftHistoryKind,
    EvidenceBinding,
    ExecutionPlanProposal,
    ExecutionPlanStatus,
    ImpactAnalysis,
    ImpactedPendingRef,
    ImpactStatus,
    Module,
    ModuleConfiguration,
    ModuleConfigurationStatus,
    ModuleLineage,
    ModuleLineageChange,
    ModuleLineageOperation,
    ModulePatch,
    ModuleSelection,
    ModuleWorkstream,
    ModuleWorkstreamStatus,
    Observation,
    PatchSet,
    Project,
    ProjectBlueprint,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProjectStage,
    ProposedModule,
    RequirementRevision,
    RequirementsChangeProposal,
    RequirementsChangeProposalStatus,
    RequirementStatus,
    SelectionLock,
    SolutionDependency,
    SolutionPlanStep,
    SolutionProposal,
    SolutionProposalStatus,
    SolutionSnapshot,
    SolutionVersion,
    SpendBudgetCostItem,
    SpendBudgetImpactClassification,
    SpendBudgetImpactLine,
    SpendBudgetImpactPreview,
    SpendBudgetProposal,
    SpendBudgetProposalStatus,
    SpendBudgetRevision,
    UserAdjustment,
    UserAdjustmentKind,
)
from aidison.solution.contracts import SolutionChangeKind, SolutionChangeSet
from aidison.solution.impact import (
    StructuralImpactPartition,
    build_impact_change_plan,
    traverse_solution_dependencies,
)


class DomainNotFoundError(RuntimeError):
    pass


class DomainConflictError(RuntimeError):
    pass


class PreconditionFailedError(DomainConflictError):
    pass


def _canonical_json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC) if value.tzinfo is not None else value
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported value in canonical payload: {type(value).__name__}")


def canonical_hash(*values: object) -> str:
    payload = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_json_default,
    )
    return sha256(payload.encode()).hexdigest()


class ProjectApplication:
    def __init__(self, store: DomainStore) -> None:
        self._store = store

    async def create_project(
        self,
        *,
        name: str,
        goal: str,
        idempotency_key: str,
    ) -> Project:
        payload_hash = canonical_hash("create_project", name, goal)
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            project = await self._store.get_project(UUID(receipt))
            if project is None:
                raise DomainConflictError("command receipt references a missing project")
            return project

        project = Project(name=name, goal=goal)
        await self._store.add_project(project)
        await self._store.append_event(
            project.id,
            "project.created",
            {"project_id": str(project.id), "revision": project.revision},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(project.id),
        )
        await self._store.commit()
        return project

    async def propose_execution_plan(
        self,
        *,
        proposal: ExecutionPlanProposal,
        expected_project_revision: int,
        idempotency_key: str,
        guard_revision_before_claim: bool = False,
    ) -> ExecutionPlanProposal:
        # A caller may reconstruct an equivalent proposal after a crash. Its
        # storage identity and creation timestamp are deliberately volatile,
        # while scope_hash covers the complete executable business contract.
        # Hash the latter so the same idempotency key deterministically replays
        # the committed plan instead of producing a false payload conflict.
        payload_hash = canonical_hash("propose_execution_plan", proposal.scope_hash)
        project: Project | None = None
        if guard_revision_before_claim:
            # Stable internal-key callers may require the project revision CAS
            # to run before the idempotent claim, so a stale
            # If-Match is rejected even when a same-basis plan already exists.
            project = await self._required_project(proposal.project_id)
            if project.revision != expected_project_revision:
                raise PreconditionFailedError("project revision is stale")
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            existing = await self._store.get_execution_plan_proposal(UUID(receipt))
            if existing is None:
                raise DomainConflictError("command receipt references missing execution plan")
            return existing

        if project is None:
            project = await self._required_project(proposal.project_id)
            if project.revision != expected_project_revision:
                raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("requirements must be approved before execution planning")
        modules = await self._active_modules(project)
        current_basis = canonical_hash(project.active_requirement_revision_id, modules)
        if proposal.basis_hash != current_basis:
            raise PreconditionFailedError("execution plan basis is stale")
        if proposal.status is not ExecutionPlanStatus.PROPOSED:
            raise DomainConflictError("new execution plan must be proposed")

        await self._store.add_execution_plan_proposal(proposal)
        event_payload: dict[str, object] = {"plan": proposal.model_dump(mode="json")}
        await self._store.append_event(
            project.id,
            ExecutionPlanEventType.PROPOSED.value,
            event_payload,
            DomainEventMetadata.for_payload(
                schema_version=1,
                aggregate_type="execution_plan",
                aggregate_id=proposal.id,
                aggregate_version=1,
                payload=event_payload,
                event_id=execution_plan_event_id(proposal.id, 1),
                correlation_id=proposal.id,
                actor="application_service",
                source_component="project_application",
                occurred_at=proposal.created_at,
            ),
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
        await self._store.commit()
        return proposal

    async def resolve_execution_plan(
        self,
        *,
        proposal_id: UUID,
        decision: ExecutionPlanStatus,
        scope_hash: str,
        expected_project_revision: int,
        idempotency_key: str,
        commit: bool = True,
    ) -> ExecutionPlanProposal:
        if decision not in {ExecutionPlanStatus.APPROVED, ExecutionPlanStatus.REJECTED}:
            raise DomainConflictError("execution plan decision must be approved or rejected")
        payload_hash = canonical_hash(
            "resolve_execution_plan", proposal_id, decision, scope_hash, expected_project_revision
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            existing = await self._store.get_execution_plan_proposal(UUID(receipt))
            if existing is None:
                raise DomainConflictError("command receipt references missing execution plan")
            return existing

        proposal = await self._store.get_execution_plan_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("execution plan proposal not found")
        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if proposal.status is not ExecutionPlanStatus.PROPOSED or proposal.scope_hash != scope_hash:
            raise PreconditionFailedError("execution plan is stale or already resolved")
        resolved = proposal.model_copy(
            update={"status": decision, "resolved_at": datetime.now(UTC)}
        )
        await self._store.update_execution_plan_proposal(
            resolved, expected_status=ExecutionPlanStatus.PROPOSED
        )
        if resolved.resolved_at is None or resolved.scope_hash is None:
            raise DomainConflictError("resolved execution plan is missing lifecycle metadata")
        resolved_event_payload: dict[str, object] = {
            "execution_plan_id": str(resolved.id),
            "scope_hash": resolved.scope_hash,
            "status": resolved.status.value,
            "resolved_at": resolved.resolved_at.isoformat(),
        }
        await self._store.append_event(
            project.id,
            ExecutionPlanEventType.RESOLVED.value,
            resolved_event_payload,
            DomainEventMetadata.for_payload(
                schema_version=1,
                aggregate_type="execution_plan",
                aggregate_id=resolved.id,
                aggregate_version=2,
                payload=resolved_event_payload,
                event_id=execution_plan_event_id(resolved.id, 2),
                correlation_id=resolved.id,
                causation_id=execution_plan_event_id(resolved.id, 1),
                actor="application_service",
                source_component="project_application",
                occurred_at=resolved.resolved_at,
            ),
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(resolved.id))
        if commit:
            await self._store.commit()
        return resolved

    async def create_selection_lock(
        self,
        *,
        project_id: UUID,
        module_id: UUID,
        candidate_id: UUID | None,
        reason: str,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> SelectionLock:
        payload_hash = canonical_hash(
            "create_selection_lock",
            project_id,
            module_id,
            candidate_id,
            reason,
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            lock = await self._store.get_selection_lock(UUID(receipt))
            if lock is None:
                raise DomainConflictError("command receipt references missing selection lock")
            return lock

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        await self._require_active_module(project, module_id)
        if candidate_id is not None:
            candidate = next(
                (
                    item
                    for item in await self._store.list_candidates(project_id)
                    if item.id == candidate_id and item.module_id == module_id
                ),
                None,
            )
            if candidate is None:
                raise DomainConflictError("selection lock candidate does not belong to the module")
        lock = SelectionLock(
            project_id=project_id,
            module_id=module_id,
            candidate_id=candidate_id,
            reason=reason,
        )
        await self._store.add_selection_lock(lock)
        await self._append_draft_history(project=project, kind=DraftHistoryKind.LOCK)
        await self._advance_project(project)
        await self._store.append_event(
            project_id,
            "module.locked",
            {"selection_lock_id": str(lock.id), "module_id": str(module_id)},
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(lock.id))
        await self._store.commit()
        return lock

    async def release_selection_lock(
        self,
        *,
        lock_id: UUID,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> SelectionLock:
        payload_hash = canonical_hash("release_selection_lock", lock_id, expected_project_revision)
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            lock = await self._store.get_selection_lock(UUID(receipt))
            if lock is None:
                raise DomainConflictError("command receipt references missing selection lock")
            return lock

        lock = await self._store.get_selection_lock(lock_id)
        if lock is None:
            raise DomainNotFoundError("selection lock not found")
        project = await self._required_project(lock.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if not lock.active:
            raise PreconditionFailedError("selection lock is already released")
        released = lock.model_copy(update={"active": False, "unlocked_at": datetime.now(UTC)})
        await self._store.update_selection_lock(released, expected_active=True)
        await self._append_draft_history(project=project, kind=DraftHistoryKind.UNLOCK)
        await self._advance_project(project)
        await self._store.append_event(
            project.id,
            "module.unlocked",
            {"selection_lock_id": str(lock.id), "module_id": str(lock.module_id)},
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(lock.id))
        await self._store.commit()
        return released

    async def record_user_adjustment(
        self,
        *,
        project_id: UUID,
        module_id: UUID,
        kind: UserAdjustmentKind,
        target: dict[str, Any],
        batch_window_seconds: float,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> UserAdjustment:
        if batch_window_seconds <= 0:
            raise DomainConflictError("adjustment batch window must be positive")
        payload_hash = canonical_hash(
            "record_user_adjustment",
            project_id,
            module_id,
            kind,
            target,
            batch_window_seconds,
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            adjustment_id = UUID(receipt)
            adjustment = next(
                (
                    item
                    for item in await self._store.list_user_adjustments(project_id)
                    if item.id == adjustment_id
                ),
                None,
            )
            if adjustment is None:
                raise DomainConflictError("command receipt references missing user adjustment")
            return adjustment

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        await self._require_active_module(project, module_id)
        await self._validate_adjustment_target(
            project_id=project_id,
            module_id=module_id,
            kind=kind,
            target=target,
        )
        await self._enforce_selection_locks(
            project_id=project_id, module_id=module_id, kind=kind, target=target
        )

        now = datetime.now(UTC)
        batch = next(
            (
                item
                for item in await self._store.list_adjustment_batches(project_id)
                if item.status is AdjustmentBatchStatus.OPEN
                and item.window_closes_at is not None
                and item.window_closes_at >= now
            ),
            None,
        )
        if batch is None:
            batch = AdjustmentBatch(
                project_id=project_id,
                window_opened_at=now,
                window_closes_at=now + timedelta(seconds=batch_window_seconds),
            )
            await self._store.add_adjustment_batch(batch)

        adjustment = UserAdjustment(
            project_id=project_id,
            module_id=module_id,
            batch_id=batch.id,
            kind=kind,
            target=target,
            created_at=now,
        )
        current = await self._active_module_configuration(project_id, module_id)
        configuration = self._next_module_configuration(current, adjustment)
        if current is not None:
            superseded = current.model_copy(
                update={
                    "status": ModuleConfigurationStatus.SUPERSEDED,
                    "superseded_by_id": configuration.id,
                }
            )
            await self._store.update_module_configuration(
                superseded, expected_status=ModuleConfigurationStatus.ACTIVE
            )
        await self._store.add_module_configuration(configuration)
        await self._store.add_user_adjustment(adjustment)
        updated_batch = batch.model_copy(
            update={
                "adjustment_ids": (*batch.adjustment_ids, adjustment.id),
                "affected_module_ids": tuple(
                    sorted({*batch.affected_module_ids, module_id}, key=str)
                ),
            }
        )
        # The first adjustment creates and fills the batch in the same transaction;
        # later adjustments CAS the same OPEN batch.
        await self._store.update_adjustment_batch(
            updated_batch, expected_status=AdjustmentBatchStatus.OPEN
        )
        await self._append_draft_history(
            project=project,
            kind=DraftHistoryKind.ADJUSTMENT,
            adjustment_ids=(adjustment.id,),
        )
        await self._advance_project(project)
        await self._store.append_event(
            project_id,
            "adjustment.recorded",
            {
                "adjustment_id": str(adjustment.id),
                "module_id": str(module_id),
                "batch_id": str(batch.id),
                "configuration_id": str(configuration.id),
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(adjustment.id))
        await self._store.commit()
        return adjustment

    async def flush_adjustment_batch(
        self,
        *,
        batch_id: UUID,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> AdjustmentBatch:
        batch, _ = await self.flush_adjustment_batch_with_preview(
            batch_id=batch_id,
            expected_project_revision=expected_project_revision,
            idempotency_key=idempotency_key,
        )
        return batch

    async def flush_adjustment_batch_with_preview(
        self,
        *,
        batch_id: UUID,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> tuple[AdjustmentBatch, ChangeImpactPreview | None]:
        """Flush one adjustment batch and atomically persist its impact preview.

        A non-empty batch has exactly one preview, stored in the same database
        transaction as the flushed batch and both idempotency receipts.  This
        deliberately avoids a committed ``FLUSHED`` receipt without a preview
        after a process crash.
        """
        payload_hash = canonical_hash("flush_adjustment_batch", batch_id, expected_project_revision)
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            batch = await self._store.get_adjustment_batch(UUID(receipt))
            if batch is None:
                raise DomainConflictError("command receipt references missing adjustment batch")
            replayed_preview = await self._flushed_batch_preview_for_replay(
                batch=batch,
                expected_project_revision=expected_project_revision,
            )
            return batch, replayed_preview

        batch = await self._store.get_adjustment_batch(batch_id)
        if batch is None:
            raise DomainNotFoundError("adjustment batch not found")
        project = await self._required_project(batch.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if batch.status is not AdjustmentBatchStatus.OPEN:
            raise PreconditionFailedError("adjustment batch is already flushed")
        flushed = batch.model_copy(
            update={"status": AdjustmentBatchStatus.FLUSHED, "flushed_at": datetime.now(UTC)}
        )
        await self._store.update_adjustment_batch(
            flushed, expected_status=AdjustmentBatchStatus.OPEN
        )
        await self._advance_project(project)
        await self._store.append_event(
            project.id,
            "adjustment.batch_flushed",
            {
                "adjustment_batch_id": str(batch.id),
                "affected_module_count": len(batch.affected_module_ids),
            },
        )
        preview: ChangeImpactPreview | None = None
        if batch.adjustment_ids:
            preview_key = f"flush-preview:{batch.id}"
            preview_payload_hash = canonical_hash(
                "generate_change_impact_preview",
                batch.id,
                project.revision + 1,
            )
            existing_preview_ref = await self._store.claim_command(
                preview_key, preview_payload_hash
            )
            if existing_preview_ref is not None:
                raise DomainConflictError(
                    "open adjustment batch already has an impact preview receipt"
                )
            preview = await self._persist_change_impact_preview(
                batch=flushed,
                project=project.model_copy(update={"revision": project.revision + 1}),
                idempotency_key=preview_key,
                payload_hash=preview_payload_hash,
            )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(batch.id))
        await self._store.commit()
        return flushed, preview

    async def generate_change_impact_preview(
        self,
        *,
        batch_id: UUID,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> ChangeImpactPreview:
        """Compute and store a read-only impact analysis for one adjustment batch.

        This command never rewrites module configurations, selection locks,
        revisions, budgets or approvals. It only records the direct/transitive
        module dependency impact, implicated selection locks and invalidated
        pending research/recommendation artifacts, so a later approved
        ExecutionPlan can drive any follow-up re-research.
        """
        payload_hash = canonical_hash(
            "generate_change_impact_preview", batch_id, expected_project_revision
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            preview = await self._store.get_change_impact_preview(UUID(receipt))
            if preview is None:
                raise DomainConflictError(
                    "command receipt references missing change impact preview"
                )
            return preview

        batch = await self._store.get_adjustment_batch(batch_id)
        if batch is None:
            raise DomainNotFoundError("adjustment batch not found")
        project = await self._required_project(batch.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        return await self._persist_change_impact_preview(
            batch=batch,
            project=project,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            commit=True,
        )

    async def _flushed_batch_preview_for_replay(
        self,
        *,
        batch: AdjustmentBatch,
        expected_project_revision: int,
    ) -> ChangeImpactPreview | None:
        if not batch.adjustment_ids:
            return None
        preview_key = f"flush-preview:{batch.id}"
        preview_payload_hash = canonical_hash(
            "generate_change_impact_preview",
            batch.id,
            expected_project_revision + 1,
        )
        preview_ref = await self._store.claim_command(preview_key, preview_payload_hash)
        if preview_ref is None:
            raise DomainConflictError(
                "flushed adjustment batch is missing its required impact preview"
            )
        preview = await self._store.get_change_impact_preview(UUID(preview_ref))
        if preview is None:
            raise DomainConflictError(
                "impact preview receipt references a missing change impact preview"
            )
        return preview

    async def _persist_change_impact_preview(
        self,
        *,
        batch: AdjustmentBatch,
        project: Project,
        idempotency_key: str,
        payload_hash: str,
        commit: bool = False,
    ) -> ChangeImpactPreview:
        """Persist a preview without claiming its command or changing project facts."""
        adjustments = tuple(await self._store.list_user_adjustments(project.id, batch_id=batch.id))
        if not adjustments:
            raise DomainConflictError("cannot analyze an empty adjustment batch")
        modules = await self._active_modules(project)
        direct_hints = tuple(dict.fromkeys(item.module_id for item in adjustments))
        if not set(direct_hints) <= {item.id for item in modules}:
            raise DomainConflictError("adjustment batch references inactive modules")
        direct, transitive, affected, unaffected = self._impact_partition(modules, direct_hints)
        affected_set = set(affected)
        affected_locks = tuple(
            lock.id
            for lock in await self._store.list_selection_locks(project.id)
            if lock.active and lock.module_id in affected_set
        )
        invalidated = await self._collect_invalidated_refs(project, affected_set)
        preview = ChangeImpactPreview(
            project_id=project.id,
            batch_id=batch.id,
            adjustment_ids=tuple(item.id for item in adjustments),
            basis_project_revision=project.revision,
            basis_hash=canonical_hash(batch.id, adjustments, project.revision),
            direct_affected_module_ids=direct,
            transitive_affected_module_ids=transitive,
            affected_module_ids=affected,
            unaffected_module_ids=unaffected,
            affected_selection_lock_ids=affected_locks,
            invalidated_refs=invalidated,
            summary=(
                f"用户对 {len(adjustments)} 项调整的影响预览：直接影响 "
                f"{len(direct)} 个模块，传递影响 {len(transitive)} 个模块，"
                f"涉及 {len(affected_locks)} 个选型锁，失效 {len(invalidated)} 项"
                "待处理建议。本预览只读，后续深入研究需由已批准的执行计划驱动。"
            ),
        )
        await self._store.add_change_impact_preview(preview)
        await self._store.append_event(
            project.id,
            "change_impact_preview.generated",
            {
                "change_impact_preview_id": str(preview.id),
                "adjustment_batch_id": str(batch.id),
                "direct_affected_count": len(direct),
                "transitive_affected_count": len(transitive),
                "invalidated_ref_count": len(invalidated),
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(preview.id))
        if commit:
            await self._store.commit()
        return preview

    async def _collect_invalidated_refs(
        self,
        project: Project,
        affected_module_ids: set[UUID],
    ) -> tuple[ImpactedPendingRef, ...]:
        """Find pending research/recommendation artifacts made stale by a change."""
        refs: list[ImpactedPendingRef] = []

        for decision in await self._store.list_decision_requests(project.id):
            if decision.status is not DecisionStatus.PENDING:
                continue
            if set(decision.affected_module_ids) & affected_module_ids:
                refs.append(
                    ImpactedPendingRef(
                        kind="decision",
                        entity_id=decision.id,
                        summary=decision.question,
                        reason="待确认选择所依据的模块已被用户调整影响",
                    )
                )

        for impact in await self._store.list_impact_analyses(project.id):
            if impact.status is not ImpactStatus.PROPOSED:
                continue
            if set(impact.affected_module_ids) & affected_module_ids:
                refs.append(
                    ImpactedPendingRef(
                        kind="impact",
                        entity_id=impact.id,
                        summary=impact.summary,
                        reason="现场影响分析的模块范围与本次调整重叠",
                    )
                )

        for proposal in await self._store.list_solution_proposals(project.id):
            if proposal.status is not SolutionProposalStatus.PROPOSED:
                continue
            if {item.module_id for item in proposal.module_selections} & affected_module_ids:
                refs.append(
                    ImpactedPendingRef(
                        kind="solution_proposal",
                        entity_id=proposal.id,
                        summary=proposal.artifact_ref,
                        reason="待确认方案的选型覆盖了本次调整影响的模块",
                    )
                )

        for reshape in await self._store.list_project_reshape_proposals(project.id):
            if reshape.status is not ProjectReshapeStatus.PROPOSED:
                continue
            if set(reshape.affected_module_ids) & affected_module_ids:
                refs.append(
                    ImpactedPendingRef(
                        kind="reshape",
                        entity_id=reshape.id,
                        summary=reshape.summary,
                        reason="待应用的结构草案与本次调整影响的模块重叠",
                    )
                )

        for requirements_change in await self._store.list_requirements_change_proposals(project.id):
            if requirements_change.status is not RequirementsChangeProposalStatus.PROPOSED:
                continue
            refs.append(
                ImpactedPendingRef(
                    kind="requirements_change",
                    entity_id=requirements_change.id,
                    summary=requirements_change.summary,
                    reason="待应用的需求/结构变更与本次调整并存，需人工复核",
                )
            )

        modules = await self._active_modules(project)
        current_basis = canonical_hash(
            project.active_requirement_revision_id,
            modules,
        )
        for plan in await self._store.list_execution_plan_proposals(project.id):
            if plan.status is not ExecutionPlanStatus.PROPOSED:
                continue
            if plan.basis_hash != current_basis:
                refs.append(
                    ImpactedPendingRef(
                        kind="execution_plan",
                        entity_id=plan.id,
                        summary=plan.objective,
                        reason="执行计划基线与当前活跃需求/模块不一致，已失效",
                    )
                )
        return tuple(refs)

    async def save_solution_snapshot(
        self,
        *,
        project_id: UUID,
        label: str,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> SolutionSnapshot:
        payload_hash = canonical_hash(
            "save_solution_snapshot", project_id, label, expected_project_revision
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            snapshot = await self._store.get_solution_snapshot(UUID(receipt))
            if snapshot is None:
                raise DomainConflictError("command receipt references missing solution snapshot")
            return snapshot

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        configurations = await self._active_configurations(project)
        snapshot = SolutionSnapshot(
            project_id=project_id,
            label=label,
            blueprint_id=project.active_blueprint_id,
            module_configuration_hashes={
                str(configuration.module_id): self._module_configuration_state_hash(configuration)
                for configuration in configurations
            },
        )
        await self._store.add_solution_snapshot(snapshot)
        await self._append_draft_history(
            project=project,
            kind=DraftHistoryKind.SNAPSHOT_SAVE,
        )
        await self._advance_project(project)
        await self._store.append_event(
            project_id,
            "draft.snapshot_saved",
            {"solution_snapshot_id": str(snapshot.id), "label": snapshot.label},
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(snapshot.id))
        await self._store.commit()
        return snapshot

    async def restore_solution_snapshot(
        self,
        *,
        snapshot_id: UUID,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> SolutionSnapshot:
        payload_hash = canonical_hash(
            "restore_solution_snapshot", snapshot_id, expected_project_revision
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            snapshot = await self._store.get_solution_snapshot(UUID(receipt))
            if snapshot is None:
                raise DomainConflictError("command receipt references missing solution snapshot")
            return snapshot

        snapshot = await self._store.get_solution_snapshot(snapshot_id)
        if snapshot is None:
            raise DomainNotFoundError("solution snapshot not found")
        project = await self._required_project(snapshot.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if snapshot.blueprint_id != project.active_blueprint_id:
            raise PreconditionFailedError("solution snapshot belongs to a different blueprint")

        active_modules = await self._active_modules(project)
        current_configurations = await self._active_configurations(project)
        current_by_module = {item.module_id: item for item in current_configurations}
        historical_by_hash: dict[str, ModuleConfiguration] = {}
        for module in active_modules:
            configurations = await self._store.list_module_configurations(project.id, module.id)
            for configuration in configurations:
                historical_by_hash.setdefault(
                    self._module_configuration_state_hash(configuration), configuration
                )

        snapshot_module_ids = {
            UUID(module_id) for module_id in snapshot.module_configuration_hashes
        }
        if not snapshot_module_ids <= {module.id for module in active_modules}:
            raise PreconditionFailedError("solution snapshot references inactive modules")

        # ── Phase A: validate every historical hash and collect replacements ──
        planned: list[tuple[UUID, ModuleConfiguration, ModuleConfiguration | None]] = []
        for module_id, configuration_hash in snapshot.module_configuration_hashes.items():
            source = historical_by_hash.get(configuration_hash)
            if source is None or source.module_id != UUID(module_id):
                raise PreconditionFailedError("solution snapshot configuration is unavailable")
            current = current_by_module.get(source.module_id)
            if (
                current is not None
                and self._module_configuration_state_hash(current) == configuration_hash
            ):
                continue
            replacement = ModuleConfiguration(
                project_id=project.id,
                module_id=source.module_id,
                revision=1 if current is None else current.revision + 1,
                base_snapshot_hash=configuration_hash,
                options=dict(source.options),
            )
            planned.append((source.module_id, replacement, current))

        # ── Phase B: enforce SelectionLocks before any write ──
        for planned_module_id, replacement, _current in planned:
            snapshot_candidate = replacement.options.get("selected_candidate_id")
            if snapshot_candidate is None:
                # No selection change — non-selection parameter restore is always safe.
                continue
            try:
                snapshot_candidate_id = UUID(str(snapshot_candidate))
            except (TypeError, ValueError):
                continue
            for lock in await self._store.list_selection_locks(
                project_id=project.id, module_id=planned_module_id
            ):
                if not lock.active:
                    continue
                # Wildcard lock (candidate_id=None) blocks any selection change.
                # A specific lock blocks only mismatched candidates.
                if lock.candidate_id is None or lock.candidate_id != snapshot_candidate_id:
                    raise DomainConflictError(
                        f"snapshot restore would change module {planned_module_id} "
                        f"selection to {snapshot_candidate_id}, "
                        f"but an active SelectionLock prevents it"
                    )

        # ── Phase C: apply validated replacements ──
        for _module_id, replacement, current in planned:
            if current is not None:
                await self._store.update_module_configuration(
                    current.model_copy(
                        update={
                            "status": ModuleConfigurationStatus.SUPERSEDED,
                            "superseded_by_id": replacement.id,
                        }
                    ),
                    expected_status=ModuleConfigurationStatus.ACTIVE,
                )
            await self._store.add_module_configuration(replacement)

        await self._append_draft_history(
            project=project,
            kind=DraftHistoryKind.SNAPSHOT_RESTORE,
        )
        await self._advance_project(project)
        await self._store.append_event(
            project.id,
            "draft.snapshot_restored",
            {"solution_snapshot_id": str(snapshot.id), "label": snapshot.label},
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(snapshot.id))
        await self._store.commit()
        return snapshot

    async def propose_project_reshape(
        self,
        *,
        proposal: ProjectReshapeProposal,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> ProjectReshapeProposal:
        payload_hash = canonical_hash("propose_project_reshape", proposal)
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            existing = await self._store.get_project_reshape_proposal(UUID(receipt))
            if existing is None:
                raise DomainConflictError("command receipt references missing reshape proposal")
            return existing

        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if proposal.status is not ProjectReshapeStatus.PROPOSED:
            raise DomainConflictError("new project reshape must be proposed")
        if proposal.basis_blueprint_id != project.active_blueprint_id:
            raise PreconditionFailedError("project reshape basis is stale")
        if proposal.basis_blueprint_id is None:
            if project.active_requirement_revision_id is None:
                raise DomainConflictError(
                    "initial project structure requires approved requirements"
                )
            pending_initial = [
                item
                for item in await self._store.list_project_reshape_proposals(project.id)
                if item.status is ProjectReshapeStatus.PROPOSED and item.basis_blueprint_id is None
            ]
            if pending_initial:
                raise DomainConflictError("an initial module structure is already awaiting review")
        await self._validate_reshape_proposal(project, proposal)
        await self._store.add_project_reshape_proposal(proposal)
        await self._store.append_event(
            project.id,
            "project.reshape_proposed",
            {"project_reshape_proposal_id": str(proposal.id)},
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
        await self._store.commit()
        return proposal

    async def resolve_project_reshape(
        self,
        *,
        proposal_id: UUID,
        decision: ProjectReshapeStatus,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> ProjectReshapeProposal:
        if decision not in {ProjectReshapeStatus.APPLIED, ProjectReshapeStatus.REJECTED}:
            raise DomainConflictError("project reshape can only be applied or rejected")
        payload_hash = canonical_hash(
            "resolve_project_reshape", proposal_id, decision, expected_project_revision
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            proposal = await self._store.get_project_reshape_proposal(UUID(receipt))
            if proposal is None:
                raise DomainConflictError("command receipt references missing reshape proposal")
            return proposal

        proposal = await self._store.get_project_reshape_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("project reshape proposal not found")
        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if proposal.status is not ProjectReshapeStatus.PROPOSED:
            raise PreconditionFailedError("project reshape is already resolved")
        if proposal.basis_blueprint_id != project.active_blueprint_id:
            raise PreconditionFailedError("project reshape basis is stale")

        resolved = proposal.model_copy(
            update={"status": decision, "resolved_at": datetime.now(UTC)}
        )
        if decision is ProjectReshapeStatus.REJECTED:
            await self._store.update_project_reshape_proposal(
                resolved, expected_status=ProjectReshapeStatus.PROPOSED
            )
            await self._store.append_event(
                project.id,
                "project.reshape_rejected",
                {"project_reshape_proposal_id": str(proposal.id)},
            )
            await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
            await self._store.commit()
            return resolved

        await self._validate_reshape_proposal(project, proposal)
        is_initial_structure = project.active_blueprint_id is None
        if is_initial_structure:
            requirement_revision_id = project.active_requirement_revision_id
            if requirement_revision_id is None:
                raise DomainConflictError(
                    "initial project structure requires approved requirements"
                )
            prior: ProjectBlueprint | None = None
        else:
            blueprint_id = project.active_blueprint_id
            if blueprint_id is None:  # pragma: no cover - narrowed by is_initial_structure.
                raise DomainConflictError("active project blueprint is unavailable")
            prior = await self._store.get_project_blueprint(blueprint_id)
            if prior is None:
                raise DomainConflictError("active project blueprint is unavailable")
            requirement_revision_id = prior.requirement_revision_id
        active_modules = await self._active_modules(project)
        relationship_only = (
            (proposal.dependency_edges is not None or proposal.engineering_couplings is not None)
            and not proposal.new_modules
        )
        retained = (
            list(active_modules)
            if relationship_only
            else [
                item for item in active_modules if item.id in set(proposal.unchanged_module_ids)
            ]
        )
        new_modules = self._materialize_reshape_modules(
            project=project,
            requirement_revision_id=requirement_revision_id,
            retained_modules=retained,
            drafts=proposal.new_modules,
        )
        new_modules = await self._attach_reshape_module_lineages(
            new_modules,
            replaced_modules=tuple(
                item for item in active_modules if item.id in set(proposal.affected_module_ids)
            ),
            lineage_changes=proposal.lineage_changes,
        )
        all_modules = (*retained, *new_modules)
        if relationship_only:
            if prior is None:  # pragma: no cover - API forbids initial relationship-only reshape.
                raise DomainConflictError("relationship reshape requires an active blueprint")
            next_dependency_edges = (
                proposal.dependency_edges
                if proposal.dependency_edges is not None
                else prior.dependency_edges
            )
            next_engineering_couplings = (
                proposal.engineering_couplings
                if proposal.engineering_couplings is not None
                else prior.engineering_couplings
            )
        else:
            next_dependency_edges = tuple(
                (dependency_id, item.id)
                for item in all_modules
                for dependency_id in item.dependency_ids
            )
            next_engineering_couplings = ()
        blueprint = ProjectBlueprint(
            project_id=project.id,
            requirement_revision_id=requirement_revision_id,
            version=len(await self._store.list_project_blueprints(project.id)) + 1,
            module_ids=tuple(item.id for item in all_modules),
            dependency_edges=next_dependency_edges,
            engineering_couplings=next_engineering_couplings,
            status=BlueprintStatus.ACTIVE,
            applied_reshape_proposal_id=proposal.id,
        )
        await self._store.add_modules(new_modules)
        await self._store.add_project_blueprint(blueprint)
        await self._synchronize_module_workstream_lifecycle(
            project_id=project.id,
            active_modules=all_modules,
        )
        if prior is not None:
            await self._store.update_project_blueprint(
                prior.model_copy(update={"status": BlueprintStatus.SUPERSEDED}),
                expected_status=BlueprintStatus.ACTIVE,
            )
        await self._store.update_project_reshape_proposal(
            resolved, expected_status=ProjectReshapeStatus.PROPOSED
        )
        await self._store.update_project(
            project.model_copy(
                update={
                    "goal": proposal.target_goal,
                    "active_blueprint_id": blueprint.id,
                    "stage": ProjectStage.RESEARCH,
                    "revision": project.revision + 1,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.append_event(
            project.id,
            "project.reshaped",
            {
                "project_reshape_proposal_id": str(proposal.id),
                "blueprint_id": str(blueprint.id),
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
        await self._store.commit()
        return resolved

    async def propose_requirements_change(
        self,
        *,
        proposal: RequirementsChangeProposal,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> RequirementsChangeProposal:
        """Record a reviewable structure & requirements rewrite proposal.

        This never writes a RequirementRevision, Module or Blueprint. The
        project only advances when the user explicitly applies the proposal
        through ``resolve_requirements_change``.
        """
        payload_hash = canonical_hash("propose_requirements_change", proposal)
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            existing = await self._store.get_requirements_change_proposal(UUID(receipt))
            if existing is None:
                raise DomainConflictError("command receipt references missing requirements change")
            return existing

        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None or project.active_blueprint_id is None:
            raise DomainConflictError("requirements changes require a confirmed module structure")
        if proposal.status is not RequirementsChangeProposalStatus.PROPOSED:
            raise DomainConflictError("new requirements change proposal must be proposed")
        if proposal.basis_requirement_revision_id != project.active_requirement_revision_id:
            raise PreconditionFailedError("requirements change basis is stale")
        if proposal.basis_blueprint_id != project.active_blueprint_id:
            raise PreconditionFailedError("requirements change blueprint basis is stale")
        self._validate_proposed_module_set(proposal.modules)
        await self._store.add_requirements_change_proposal(proposal)
        await self._store.append_event(
            project.id,
            "requirements_change.proposed",
            {"requirements_change_proposal_id": str(proposal.id)},
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
        await self._store.commit()
        return proposal

    async def resolve_requirements_change(
        self,
        *,
        proposal_id: UUID,
        decision: RequirementsChangeProposalStatus,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> RequirementsChangeProposal:
        if decision not in {
            RequirementsChangeProposalStatus.APPLIED,
            RequirementsChangeProposalStatus.REJECTED,
        }:
            raise DomainConflictError("requirements change can only be applied or rejected")
        payload_hash = canonical_hash(
            "resolve_requirements_change",
            proposal_id,
            decision,
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            proposal = await self._store.get_requirements_change_proposal(UUID(receipt))
            if proposal is None:
                raise DomainConflictError("command receipt references missing requirements change")
            return proposal

        proposal = await self._store.get_requirements_change_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("requirements change proposal not found")
        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None or project.active_blueprint_id is None:
            raise DomainConflictError("requirements changes require a confirmed module structure")
        if proposal.status is not RequirementsChangeProposalStatus.PROPOSED:
            raise PreconditionFailedError("requirements change is already resolved")
        if proposal.basis_requirement_revision_id != project.active_requirement_revision_id:
            raise PreconditionFailedError("requirements change basis is stale")
        if proposal.basis_blueprint_id != project.active_blueprint_id:
            raise PreconditionFailedError("requirements change blueprint basis is stale")

        resolved = proposal.model_copy(
            update={"status": decision, "resolved_at": datetime.now(UTC)}
        )
        if decision is RequirementsChangeProposalStatus.REJECTED:
            await self._store.update_requirements_change_proposal(
                resolved, expected_status=RequirementsChangeProposalStatus.PROPOSED
            )
            await self._store.append_event(
                project.id,
                "requirements_change.rejected",
                {"requirements_change_proposal_id": str(proposal.id)},
            )
            await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
            await self._store.commit()
            return resolved

        # Apply: replace the requirement revision, module graph and blueprint in
        # ONE transaction with ONE revision bump. A frozen solution is never
        # silently replaced; that path stays with Observation → ImpactAnalysis.
        if project.active_solution_version_id is not None:
            raise DomainConflictError(
                "a frozen solution must be revised through observation impact analysis"
            )
        self._validate_proposed_module_set(proposal.modules)
        requirement, _ = await self._write_approved_requirements(
            project=project,
            goal=proposal.target_goal,
            hard_constraints=proposal.hard_constraints,
            preferences=proposal.preferences,
            available_resources=proposal.available_resources,
            usage_context=proposal.usage_context,
            budget_context=proposal.budget_context,
            skill_context=proposal.skill_context,
            unknowns=proposal.unknowns,
            modules=tuple(item.model_dump() for item in proposal.modules),
            update_goal=True,
        )
        await self._store.update_requirements_change_proposal(
            resolved, expected_status=RequirementsChangeProposalStatus.PROPOSED
        )
        await self._store.append_event(
            project.id,
            "requirements_change.applied",
            {
                "requirements_change_proposal_id": str(proposal.id),
                "requirement_revision_id": str(requirement.id),
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
        await self._store.commit()
        return resolved

    async def approve_requirements(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        goal: str,
        hard_constraints: Sequence[str],
        preferences: Sequence[str],
        available_resources: Sequence[str],
        usage_context: str = "",
        budget_context: str = "",
        skill_context: str = "",
        unknowns: Sequence[str],
        modules: Sequence[dict[str, Any]],
        idempotency_key: str,
    ) -> tuple[RequirementRevision, tuple[Module, ...]]:
        payload_hash = canonical_hash(
            "approve_requirements",
            project_id,
            expected_project_revision,
            goal,
            hard_constraints,
            preferences,
            available_resources,
            usage_context,
            budget_context,
            skill_context,
            unknowns,
            modules,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            requirement = await self._store.get_requirement_revision(UUID(receipt))
            if requirement is None:
                raise DomainConflictError("command receipt references missing requirements")
            return requirement, tuple(
                await self._store.list_modules(project_id, requirement_revision_id=requirement.id)
            )

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if (
            project.active_requirement_revision_id is not None
            or project.active_blueprint_id is not None
        ):
            raise DomainConflictError(
                "requirements changes must be proposed through requirements-change-proposals"
            )
        if modules:
            raise DomainConflictError(
                "initial requirements cannot include modules; use module discovery"
            )
        requirement, module_entities = await self._write_approved_requirements(
            project=project,
            goal=goal,
            hard_constraints=hard_constraints,
            preferences=preferences,
            available_resources=available_resources,
            usage_context=usage_context,
            budget_context=budget_context,
            skill_context=skill_context,
            unknowns=unknowns,
            modules=modules,
            update_goal=False,
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(requirement.id),
        )
        await self._store.commit()
        return requirement, module_entities

    async def _write_approved_requirements(
        self,
        *,
        project: Project,
        goal: str,
        hard_constraints: Sequence[str],
        preferences: Sequence[str],
        available_resources: Sequence[str],
        usage_context: str = "",
        budget_context: str = "",
        skill_context: str = "",
        unknowns: Sequence[str],
        modules: Sequence[dict[str, Any]],
        update_goal: bool,
    ) -> tuple[RequirementRevision, tuple[Module, ...]]:
        """Write an approved requirement revision and an optional module graph.

        This helper performs NO command receipt claim and NO commit. It is shared
        by ``approve_requirements`` (outer command owns receipt + commit) and by
        ``resolve_requirements_change`` so an applied requirements rewrite stays a
        single transaction with a single project revision bump. When ``update_goal``
        is set, ``Project.goal`` is replaced with the approved requirements goal.

        ``budget_context`` is only a requirement fact; writing it never creates,
        modifies or resolves a SpendBudgetRevision.
        """
        project_id = project.id
        previous = await self._store.list_requirement_revisions(project_id)
        requirement = RequirementRevision(
            project_id=project_id,
            revision=len(previous) + 1,
            status=RequirementStatus.APPROVED,
            goal=goal,
            hard_constraints=tuple(hard_constraints),
            preferences=tuple(preferences),
            available_resources=tuple(available_resources),
            usage_context=usage_context,
            budget_context=budget_context,
            skill_context=skill_context,
            unknowns=tuple(unknowns),
            approved_at=datetime.now(UTC),
        )
        module_drafts = tuple(
            Module(
                project_id=project_id,
                requirement_revision_id=requirement.id,
                key=item["key"],
                name=item["name"],
                responsibility=item["responsibility"],
                acceptance=tuple(item.get("acceptance", ())),
                open_questions=tuple(item.get("open_questions", ())),
            )
            for item in modules
        )
        is_initial_module_discovery = not module_drafts
        if is_initial_module_discovery:
            if (
                project.active_requirement_revision_id is not None
                or project.active_blueprint_id is not None
            ):
                raise DomainConflictError(
                    "requirements changes must keep an existing module structure"
                )
        elif not 1 <= len(module_drafts) <= 8:
            raise DomainConflictError("requirements must contain one to eight modules")
        modules_by_key = {item.key: item for item in module_drafts}
        if len(modules_by_key) != len(module_drafts):
            raise DomainConflictError("module keys must be unique")
        dependencies_by_key = {
            item["key"]: tuple(dict.fromkeys(item.get("dependency_keys", ()))) for item in modules
        }
        self._validate_module_graph(set(modules_by_key), dependencies_by_key)
        module_entities = tuple(
            item.model_copy(
                update={
                    "dependency_ids": tuple(
                        modules_by_key[key].id for key in dependencies_by_key[item.key]
                    )
                }
            )
            for item in module_drafts
        )
        module_entities = await self._attach_fresh_module_lineages(module_entities)
        blueprint = (
            None
            if is_initial_module_discovery
            else ProjectBlueprint(
                project_id=project_id,
                requirement_revision_id=requirement.id,
                version=len(await self._store.list_project_blueprints(project_id)) + 1,
                module_ids=tuple(item.id for item in module_entities),
                dependency_edges=tuple(
                    (dependency_id, item.id)
                    for item in module_entities
                    for dependency_id in item.dependency_ids
                ),
                status=BlueprintStatus.ACTIVE,
            )
        )

        updated_project = project.model_copy(
            update={
                "goal": goal if update_goal else project.goal,
                "stage": (
                    ProjectStage.REQUIREMENTS
                    if is_initial_module_discovery
                    else ProjectStage.RESEARCH
                ),
                "revision": project.revision + 1,
                "active_requirement_revision_id": requirement.id,
                "active_blueprint_id": blueprint.id if blueprint is not None else None,
                "updated_at": datetime.now(UTC),
            }
        )
        if project.active_blueprint_id is not None:
            current_blueprint = await self._store.get_project_blueprint(project.active_blueprint_id)
            if current_blueprint is None:
                raise DomainConflictError("project references a missing active blueprint")
            await self._store.update_project_blueprint(
                current_blueprint.model_copy(update={"status": BlueprintStatus.SUPERSEDED}),
                expected_status=BlueprintStatus.ACTIVE,
            )
        await self._store.update_project(
            project=updated_project,
            expected_revision=project.revision,
        )
        await self._store.add_requirement_revision(requirement)
        if module_entities:
            await self._store.add_modules(module_entities)
        if blueprint is not None:
            await self._store.add_project_blueprint(blueprint)
        await self._store.append_event(
            project_id,
            "requirements.approved",
            {"requirement_id": str(requirement.id), "module_count": len(module_entities)},
        )
        if blueprint is not None:
            await self._store.append_event(
                project_id,
                "blueprint.created",
                {"blueprint_id": str(blueprint.id), "version": blueprint.version},
            )
        return requirement, module_entities

    async def _attach_fresh_module_lineages(
        self, modules: Sequence[Module]
    ) -> tuple[Module, ...]:
        """Attach a new stable identity only when this command creates a module.

        Existing module IDs keep their persisted lineage. A reshape currently
        has no explicit rename/split/merge declaration, so reusing a lineage
        for a replacement based only on a matching display name or key would
        be an unsafe semantic guess. That richer operation is deliberately a
        later, reviewable command.
        """

        fresh = tuple(item for item in modules if item.lineage_id is None)
        if not fresh:
            return tuple(modules)
        lineages = tuple(
            ModuleLineage(
                project_id=item.project_id,
                stable_key=item.key,
                display_name=item.name,
            )
            for item in fresh
        )
        await self._store.add_module_lineages(lineages)
        await self._store.add_module_workstreams(
            tuple(
                ModuleWorkstream(
                    project_id=lineage.project_id,
                    module_lineage_id=lineage.id,
                )
                for lineage in lineages
            )
        )
        lineage_by_module_id = {
            module.id: lineage.id for module, lineage in zip(fresh, lineages, strict=True)
        }
        return tuple(
            item.model_copy(update={"lineage_id": lineage_by_module_id[item.id]})
            if item.id in lineage_by_module_id
            else item
            for item in modules
        )

    async def _attach_reshape_module_lineages(
        self,
        modules: Sequence[Module],
        *,
        replaced_modules: Sequence[Module],
        lineage_changes: Sequence[ModuleLineageChange],
    ) -> tuple[Module, ...]:
        """Apply only user-approved lineage assertions for a replacement shape.

        ``Module`` rows are immutable revision records.  A rename therefore
        creates a new row (and may use a new revision-local ``key``) while
        preserving the old logical ``lineage_id`` and stable lineage key.
        Nothing in this helper infers continuity from matching text.
        """

        if not lineage_changes:
            attached = await self._attach_fresh_module_lineages(modules)
            await self._retire_removed_module_lineages(
                removed_modules=replaced_modules,
                reused_lineage_ids=set(),
            )
            return attached

        active_by_id = {item.id: item for item in replaced_modules}
        draft_by_key = {item.key: item for item in modules}
        existing_lineages = {
            item.id: item
            for item in await self._store.list_module_lineages(replaced_modules[0].project_id)
        }
        changes_by_target = {item.target_new_module_key: item for item in lineage_changes}
        source_operations: dict[UUID, list[ModuleLineageChange]] = {}
        for change in lineage_changes:
            for source_id in change.source_module_ids:
                source_operations.setdefault(source_id, []).append(change)

        created: list[ModuleLineage] = []
        lineage_by_target_key: dict[str, UUID] = {}
        updates: dict[UUID, ModuleLineage] = {}
        now = datetime.now(UTC)

        def source_lineage(source_id: UUID) -> ModuleLineage:
            lineage_id = active_by_id[source_id].lineage_id
            if lineage_id is None:  # Defensive; proposal admission validates this first.
                raise DomainConflictError("reshape lineage source has no stable identity")
            return existing_lineages[lineage_id]

        for target_key, module in draft_by_key.items():
            lineage_change = changes_by_target.get(target_key)
            if lineage_change is None:
                lineage = ModuleLineage(
                    project_id=module.project_id,
                    stable_key=module.key,
                    display_name=module.name,
                )
                created.append(lineage)
                lineage_by_target_key[target_key] = lineage.id
                continue

            source_lineages = tuple(
                source_lineage(source_id) for source_id in lineage_change.source_module_ids
            )
            if lineage_change.operation is ModuleLineageOperation.RENAME:
                lineage = source_lineages[0].model_copy(update={"display_name": module.name})
                updates[lineage.id] = lineage
                lineage_by_target_key[target_key] = lineage.id
            elif lineage_change.operation is ModuleLineageOperation.SPLIT:
                lineage = ModuleLineage(
                    project_id=module.project_id,
                    stable_key=module.key,
                    display_name=module.name,
                    split_from_lineage_id=source_lineages[0].id,
                )
                created.append(lineage)
                lineage_by_target_key[target_key] = lineage.id
            else:
                lineage = ModuleLineage(
                    project_id=module.project_id,
                    stable_key=module.key,
                    display_name=module.name,
                )
                created.append(lineage)
                lineage_by_target_key[target_key] = lineage.id
                for source in source_lineages:
                    updates[source.id] = source.model_copy(
                        update={"merged_into_lineage_id": lineage.id, "retired_at": now}
                    )

        for source_id, operations in source_operations.items():
            if operations[0].operation is ModuleLineageOperation.SPLIT:
                source = source_lineage(source_id)
                updates[source.id] = source.model_copy(update={"retired_at": now})

        reused_lineage_ids = {
            source_lineage(change.source_module_ids[0]).id
            for change in lineage_changes
            if change.operation is ModuleLineageOperation.RENAME
        }
        await self._retire_removed_module_lineages(
            removed_modules=replaced_modules,
            reused_lineage_ids=reused_lineage_ids,
            already_updated=updates,
            now=now,
        )
        if created:
            await self._store.add_module_lineages(created)
            await self._store.add_module_workstreams(
                tuple(
                    ModuleWorkstream(
                        project_id=lineage.project_id,
                        module_lineage_id=lineage.id,
                    )
                    for lineage in created
                )
            )
        for lineage in updates.values():
            await self._store.update_module_lineage(lineage)
        return tuple(
            item.model_copy(update={"lineage_id": lineage_by_target_key[item.key]})
            for item in modules
        )

    async def _retire_removed_module_lineages(
        self,
        *,
        removed_modules: Sequence[Module],
        reused_lineage_ids: set[UUID],
        already_updated: dict[UUID, ModuleLineage] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Retire a lineage whose active module was explicitly replaced.

        This is a factual lifecycle transition, not a semantic lineage guess.
        A rename is the sole operation that preserves an active lineage.
        """

        if not removed_modules:
            return
        lineages = {
            item.id: item
            for item in await self._store.list_module_lineages(removed_modules[0].project_id)
        }
        updates = already_updated if already_updated is not None else {}
        retirement_time = now or datetime.now(UTC)
        for module in removed_modules:
            if module.lineage_id is None or module.lineage_id in reused_lineage_ids:
                continue
            lineage = updates.get(module.lineage_id, lineages[module.lineage_id])
            if lineage.retired_at is None:
                updates[lineage.id] = lineage.model_copy(update={"retired_at": retirement_time})
        if already_updated is None:
            for lineage in updates.values():
                await self._store.update_module_lineage(lineage)

    async def _synchronize_module_workstream_lifecycle(
        self,
        *,
        project_id: UUID,
        active_modules: Sequence[Module],
    ) -> None:
        """Retire a workstream only when its lineage leaves the active shape."""

        active_lineage_ids = {
            item.lineage_id for item in active_modules if item.lineage_id is not None
        }
        for workstream in await self._store.list_module_workstreams(project_id):
            if (
                workstream.module_lineage_id not in active_lineage_ids
                and workstream.status is not ModuleWorkstreamStatus.RETIRED
            ):
                await self._store.update_module_workstream(
                    workstream.model_copy(
                        update={
                            "status": ModuleWorkstreamStatus.RETIRED,
                            "optimistic_revision": workstream.optimistic_revision + 1,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                )

    async def submit_research_proposal(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        evidence: Sequence[EvidenceBinding],
        candidates: Sequence[Candidate],
        findings: Sequence[CompatibilityFinding],
        decision_question: str,
        decision_options: Sequence[DecisionOption],
        idempotency_key: str,
    ) -> DecisionRequest:
        payload_hash = canonical_hash(
            "submit_research_proposal",
            project_id,
            expected_project_revision,
            evidence,
            candidates,
            findings,
            decision_question,
            decision_options,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            decision = await self._store.get_decision_request(UUID(receipt))
            if decision is None:
                raise DomainConflictError("command receipt references a missing decision")
            return decision

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("requirements must be approved before research")
        for evidence_item in evidence:
            if evidence_item.project_id != project_id:
                raise DomainConflictError("proposal contains another project's data")
        for candidate_item in candidates:
            if candidate_item.project_id != project_id:
                raise DomainConflictError("proposal contains another project's data")
        for finding_item in findings:
            if finding_item.project_id != project_id:
                raise DomainConflictError("proposal contains another project's data")
        candidate_ids = {item.id for item in candidates}
        evidence_ids = {item.id for item in evidence}
        for option in decision_options:
            if option.legacy_unbound:
                raise DomainConflictError("new decisions cannot contain legacy unbound options")
            if not set(option.candidate_ids).issubset(candidate_ids):
                raise DomainConflictError("decision option references an unknown candidate")
            if not set(option.evidence_binding_ids).issubset(evidence_ids):
                raise DomainConflictError("decision option references unknown evidence")

        modules = await self._store.list_modules(
            project_id,
            requirement_revision_id=project.active_requirement_revision_id,
        )
        basis_hash = canonical_hash(
            project.active_requirement_revision_id,
            modules,
            evidence,
            candidates,
            findings,
        )
        decision = DecisionRequest(
            project_id=project_id,
            basis_hash=basis_hash,
            question=decision_question,
            options=tuple(decision_options),
            affected_module_ids=tuple(module.id for module in modules),
        )
        await self._store.update_project(
            project=project.model_copy(
                update={
                    "stage": ProjectStage.DECIDING,
                    "revision": project.revision + 1,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.add_evidence_bindings(evidence)
        await self._store.add_candidates(candidates)
        await self._store.add_compatibility_findings(findings)
        await self._store.add_decision_request(decision)
        await self._store.append_event(
            project_id,
            "decision.required",
            {"decision_id": str(decision.id), "basis_hash": basis_hash},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(decision.id),
        )
        await self._store.commit()
        return decision

    async def resolve_decision(
        self,
        *,
        decision_id: UUID,
        expected_project_revision: int,
        selected_option_id: str,
        basis_hash: str,
        idempotency_key: str,
    ) -> DecisionRequest:
        payload_hash = canonical_hash(
            "resolve_decision",
            decision_id,
            expected_project_revision,
            selected_option_id,
            basis_hash,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            decision = await self._store.get_decision_request(UUID(receipt))
            if decision is None:
                raise DomainConflictError("command receipt references a missing decision")
            return decision

        decision = await self._store.get_decision_request(decision_id)
        if decision is None:
            raise DomainNotFoundError("decision not found")
        if decision.status is not DecisionStatus.PENDING:
            raise DomainConflictError("decision is already resolved")
        if decision.basis_hash != basis_hash:
            raise PreconditionFailedError("decision basis is stale")
        project = await self._required_project(decision.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        resolved = DecisionRequest.model_validate(
            decision.model_copy(
                update={
                    "status": DecisionStatus.APPROVED,
                    "selected_option_id": selected_option_id,
                    "resolved_at": datetime.now(UTC),
                }
            ).model_dump()
        )
        await self._store.update_project(
            project=project.model_copy(
                update={"revision": project.revision + 1, "updated_at": datetime.now(UTC)}
            ),
            expected_revision=project.revision,
        )
        await self._store.update_decision_request(resolved)
        await self._store.append_event(
            decision.project_id,
            "decision.resolved",
            {"decision_id": str(decision.id), "selected_option_id": selected_option_id},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(decision.id),
        )
        await self._store.commit()
        return resolved

    async def freeze_solution(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        solution_proposal_id: UUID,
        basis_hash: str,
        idempotency_key: str,
    ) -> SolutionVersion:
        payload_hash = canonical_hash(
            "freeze_solution",
            project_id,
            expected_project_revision,
            solution_proposal_id,
            basis_hash,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            solution = await self._store.get_solution_version(UUID(receipt))
            if solution is None:
                raise DomainConflictError("command receipt references a missing solution")
            return solution

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        proposal = await self._store.get_solution_proposal(solution_proposal_id)
        if proposal is None or proposal.project_id != project_id:
            raise DomainNotFoundError("solution proposal not found")
        if proposal.status is not SolutionProposalStatus.PROPOSED:
            raise DomainConflictError("solution proposal is already resolved")
        if proposal.basis_hash != basis_hash:
            raise PreconditionFailedError("solution proposal basis is stale")
        decision = await self._store.get_decision_request(proposal.decision_id)
        if decision is None or decision.project_id != project_id:
            raise DomainNotFoundError("decision not found")
        if decision.status is not DecisionStatus.APPROVED:
            raise DomainConflictError("solution requires an approved decision")
        findings = {
            item.id: item for item in await self._store.list_compatibility_findings(project_id)
        }
        if any(
            findings[item_id].status is CompatibilityStatus.INCOMPATIBLE
            for item_id in proposal.compatibility_finding_ids
        ):
            raise DomainConflictError("incompatible finding blocks solution freeze")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("project has no approved requirements")
        if proposal.requirement_revision_id != project.active_requirement_revision_id:
            raise PreconditionFailedError("solution proposal requirements are stale")
        existing = await self._store.list_solution_versions(project_id)
        solution = SolutionVersion(
            project_id=project_id,
            version=len(existing) + 1,
            requirement_revision_id=project.active_requirement_revision_id,
            basis_hash=proposal.basis_hash,
            module_snapshots=tuple(
                {
                    **item.model_dump(mode="json"),
                    "snapshot_hash": canonical_hash(item),
                }
                for item in proposal.module_selections
            ),
            evidence_binding_ids=proposal.evidence_binding_ids,
            compatibility_finding_ids=proposal.compatibility_finding_ids,
            dependencies=proposal.dependencies,
            dependency_projection_complete=proposal.dependency_projection_complete,
            bom=tuple(item.model_dump(mode="json") for item in proposal.bom),
            implementation_steps=tuple(
                item.model_dump(mode="json") for item in proposal.implementation_steps
            ),
            verification_steps=tuple(
                item.model_dump(mode="json") for item in proposal.verification_steps
            ),
            approved_decision_id=decision.id,
            solution_proposal_id=proposal.id,
        )
        approved_proposal = proposal.model_copy(
            update={
                "status": SolutionProposalStatus.APPROVED,
                "resolved_at": datetime.now(UTC),
            }
        )
        await self._store.update_project(
            project=project.model_copy(
                update={
                    "stage": ProjectStage.APPROVED,
                    "revision": project.revision + 1,
                    "active_solution_version_id": solution.id,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.add_solution_version(solution)
        await self._store.update_solution_proposal(approved_proposal)
        await self._store.append_event(
            project_id,
            "solution.frozen",
            {"solution_id": str(solution.id), "version": solution.version},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(solution.id),
        )
        await self._store.commit()
        return solution

    async def submit_solution_proposal(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        decision_id: UUID,
        module_selections: Sequence[ModuleSelection],
        evidence_binding_ids: Sequence[UUID],
        compatibility_finding_ids: Sequence[UUID],
        dependencies: Sequence[SolutionDependency] = (),
        dependency_projection_complete: bool = False,
        bom: Sequence[BomItem],
        implementation_steps: Sequence[SolutionPlanStep],
        verification_steps: Sequence[SolutionPlanStep],
        risks: Sequence[str],
        unknowns: Sequence[str],
        consequences: Sequence[str],
        artifact_ref: str,
        profile_id: str,
        profile_revision: int,
        idempotency_key: str,
    ) -> SolutionProposal:
        proposal_basis = canonical_hash(
            decision_id,
            module_selections,
            evidence_binding_ids,
            compatibility_finding_ids,
            dependencies,
            dependency_projection_complete,
            bom,
            implementation_steps,
            verification_steps,
            risks,
            unknowns,
            consequences,
            artifact_ref,
            profile_id,
            profile_revision,
        )
        payload_hash = canonical_hash(
            "submit_solution_proposal",
            project_id,
            expected_project_revision,
            proposal_basis,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            proposal = await self._store.get_solution_proposal(UUID(receipt))
            if proposal is None:
                raise DomainConflictError("command receipt references a missing solution proposal")
            return proposal

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("solution proposal requires approved requirements")
        decision = await self._store.get_decision_request(decision_id)
        if decision is None or decision.project_id != project_id:
            raise DomainNotFoundError("decision not found")
        if decision.status is not DecisionStatus.APPROVED:
            raise DomainConflictError("solution proposal requires an approved decision")

        modules = tuple(
            await self._store.list_modules(
                project_id,
                requirement_revision_id=project.active_requirement_revision_id,
            )
        )
        module_ids = {item.id for item in modules}
        selections = tuple(module_selections)
        if {item.module_id for item in selections} != module_ids or len(selections) != len(
            module_ids
        ):
            raise DomainConflictError("solution proposal must select every active module once")

        candidates = {item.id: item for item in await self._store.list_candidates(project_id)}
        evidence = {item.id: item for item in await self._store.list_evidence_bindings(project_id)}
        findings = {
            item.id: item for item in await self._store.list_compatibility_findings(project_id)
        }
        proposal_evidence_ids = tuple(dict.fromkeys(evidence_binding_ids))
        proposal_finding_ids = tuple(dict.fromkeys(compatibility_finding_ids))
        proposal_dependencies = tuple(dependencies)
        if not set(proposal_evidence_ids) <= set(evidence):
            raise DomainConflictError("solution proposal references unknown evidence")
        if not set(proposal_finding_ids) <= set(findings):
            raise DomainConflictError("solution proposal references unknown compatibility findings")
        for selection in selections:
            candidate = candidates.get(selection.candidate_id)
            if (
                candidate is None
                or candidate.module_id != selection.module_id
                or candidate.name != selection.candidate_name
            ):
                raise DomainConflictError("solution selection does not match a project candidate")
            if not set(selection.evidence_binding_ids) <= set(candidate.evidence_binding_ids):
                raise DomainConflictError("solution selection has unbound evidence")

        dependency_keys = {
            (
                item.producer_module_id,
                item.consumer_module_id,
                item.kind,
                item.interface_key,
            )
            for item in proposal_dependencies
        }
        if len(dependency_keys) != len(proposal_dependencies):
            raise DomainConflictError("solution dependencies must be unique")
        if any(
            item.producer_module_id not in module_ids or item.consumer_module_id not in module_ids
            for item in proposal_dependencies
        ):
            raise DomainConflictError("solution dependency references a module outside the project")
        proposal_evidence_refs = {f"evidence-binding://{item}" for item in proposal_evidence_ids}
        if any(
            not set(item.evidence_refs) <= proposal_evidence_refs for item in proposal_dependencies
        ):
            raise DomainConflictError(
                "solution dependency references evidence outside the proposal"
            )

        selected_pairs = {(item.module_id, item.candidate_id) for item in selections}
        line_ids: set[str] = set()
        for item in bom:
            if item.line_id in line_ids:
                raise DomainConflictError("BOM line IDs must be unique")
            line_ids.add(item.line_id)
            if (item.module_id, item.candidate_id) not in selected_pairs:
                raise DomainConflictError("BOM item is not bound to a selected candidate")
            if not set(item.evidence_binding_ids) <= set(proposal_evidence_ids):
                raise DomainConflictError("BOM item references evidence outside the proposal")

        implementation = tuple(implementation_steps)
        verification = tuple(verification_steps)
        for step in (*implementation, *verification):
            if not set(step.module_ids) <= module_ids:
                raise DomainConflictError("solution step references a module outside the project")
        verification_scope = {module_id for step in verification for module_id in step.module_ids}
        needs_verification = {
            module_id
            for finding_id in proposal_finding_ids
            if findings[finding_id].status
            in {CompatibilityStatus.UNKNOWN, CompatibilityStatus.NEEDS_TEST}
            for module_id in findings[finding_id].module_ids
        }
        if not needs_verification <= verification_scope:
            raise DomainConflictError("unknown or needs_test findings require verification steps")

        proposal = SolutionProposal(
            project_id=project_id,
            decision_id=decision.id,
            requirement_revision_id=project.active_requirement_revision_id,
            basis_hash=proposal_basis,
            module_selections=selections,
            evidence_binding_ids=proposal_evidence_ids,
            compatibility_finding_ids=proposal_finding_ids,
            dependencies=proposal_dependencies,
            dependency_projection_complete=dependency_projection_complete,
            bom=tuple(bom),
            implementation_steps=implementation,
            verification_steps=verification,
            risks=tuple(risks),
            unknowns=tuple(unknowns),
            consequences=tuple(consequences),
            artifact_ref=artifact_ref,
            profile_id=profile_id,
            profile_revision=profile_revision,
        )
        await self._store.update_project(
            project=project.model_copy(
                update={"revision": project.revision + 1, "updated_at": datetime.now(UTC)}
            ),
            expected_revision=project.revision,
        )
        await self._store.add_solution_proposal(proposal)
        await self._store.append_event(
            project_id,
            "solution.proposed",
            {"solution_proposal_id": str(proposal.id), "basis_hash": proposal.basis_hash},
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(proposal.id),
        )
        await self._store.commit()
        return proposal

    async def submit_observation(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        statement: str,
        affected_module_ids: Sequence[UUID],
        idempotency_key: str,
    ) -> Observation:
        payload_hash = canonical_hash(
            "submit_observation",
            project_id,
            expected_project_revision,
            statement,
            affected_module_ids,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            observation = await self._store.get_observation(UUID(receipt))
            if observation is None:
                raise DomainConflictError("command receipt references a missing observation")
            return observation

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_solution_version_id is None:
            raise DomainConflictError("observation requires an active solution")
        solution = await self._store.get_solution_version(project.active_solution_version_id)
        if solution is None:
            raise DomainConflictError("active solution is missing")
        modules = await self._store.list_modules(
            project_id,
            requirement_revision_id=project.active_requirement_revision_id,
        )
        module_ids = {module.id for module in modules}
        direct_affected = tuple(dict.fromkeys(affected_module_ids))
        if not direct_affected or not set(direct_affected) <= module_ids:
            raise DomainConflictError("observation must reference project modules")
        observation = Observation(
            project_id=project_id,
            solution_version_id=solution.id,
            statement=statement,
            affected_module_hints=direct_affected,
        )
        await self._store.update_project(
            project=project.model_copy(
                update={
                    "stage": ProjectStage.REVISING,
                    "revision": project.revision + 1,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.add_observation(observation)
        await self._store.append_event(
            project_id,
            "observation.recorded",
            {
                "observation_id": str(observation.id),
                "direct_affected_count": len(direct_affected),
            },
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(observation.id),
        )
        await self._store.commit()
        return observation

    async def submit_impact_analysis(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        observation_id: UUID,
        module_patches: Sequence[ModulePatch],
        stale_evidence_binding_ids: Sequence[UUID],
        replacement_bom_items: Sequence[BomItem],
        replacement_implementation_steps: Sequence[SolutionPlanStep],
        replacement_verification_steps: Sequence[SolutionPlanStep],
        summary: str,
        risks: Sequence[str],
        artifact_ref: str,
        profile_id: str,
        profile_revision: int,
        idempotency_key: str,
    ) -> ImpactAnalysis:
        proposal_basis = canonical_hash(
            observation_id,
            module_patches,
            stale_evidence_binding_ids,
            replacement_bom_items,
            replacement_implementation_steps,
            replacement_verification_steps,
            summary,
            risks,
            artifact_ref,
            profile_id,
            profile_revision,
        )
        payload_hash = canonical_hash(
            "submit_impact_analysis",
            project_id,
            expected_project_revision,
            proposal_basis,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            impact = await self._store.get_impact_analysis(UUID(receipt))
            if impact is None:
                raise DomainConflictError("command receipt references a missing impact")
            return impact

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        observation = await self._store.get_observation(observation_id)
        if observation is None or observation.project_id != project_id:
            raise DomainNotFoundError("observation not found")
        if project.active_solution_version_id != observation.solution_version_id:
            raise PreconditionFailedError("observation base is no longer the active solution")
        base = await self._store.get_solution_version(observation.solution_version_id)
        if base is None:
            raise DomainConflictError("observation base solution is missing")
        change_set = SolutionChangeSet(
            change_set_id=observation.id,
            project_id=project_id,
            base_solution_version_id=base.id,
            base_solution_basis_hash=base.basis_hash,
            changed_module_ids=observation.affected_module_hints,
            change_kinds=(SolutionChangeKind.OBSERVATION,),
            before_refs=tuple(f"artifact://{item}" for item in observation.artifact_ids),
            trigger_ref=f"observation://{observation.id}",
        )
        if base.dependency_projection_complete:
            partition = traverse_solution_dependencies(
                change_set=change_set,
                solution=base,
            )
            direct = partition.direct_module_ids
            transitive = partition.transitive_module_ids
            affected = partition.affected_module_ids
            unaffected = partition.unaffected_module_ids
            dependency_paths = partition.paths
        else:
            # Compatibility bridge for pre-R6 frozen versions.  New
            # ``dependency_projection_complete`` solutions never consult the
            # mutable module graph, including when they intentionally have no
            # interface dependencies.
            modules = tuple(
                await self._store.list_modules(
                    project_id,
                    requirement_revision_id=project.active_requirement_revision_id,
                )
            )
            direct, transitive, affected, unaffected = self._impact_partition(
                modules,
                observation.affected_module_hints,
            )
            dependency_paths = ()
            partition = StructuralImpactPartition(
                change_set_id=change_set.change_set_id,
                direct_module_ids=direct,
                transitive_module_ids=transitive,
                affected_module_ids=affected,
                unaffected_module_ids=unaffected,
                paths=dependency_paths,
            )
        impact_plan = build_impact_change_plan(
            change_set=change_set,
            partition=partition,
        )
        affected_set = set(affected)
        patches = tuple(module_patches)
        if not patches or len({item.module_id for item in patches}) != len(patches):
            raise DomainConflictError("impact proposal requires unique module patches")
        if not {item.module_id for item in patches} <= affected_set:
            raise DomainConflictError("impact proposal cannot patch unaffected modules")

        snapshots = {UUID(str(item["module_id"])): item for item in base.module_snapshots}
        candidates = {item.id: item for item in await self._store.list_candidates(project_id)}
        evidence = {item.id: item for item in await self._store.list_evidence_bindings(project_id)}
        for patch in patches:
            snapshot = snapshots.get(patch.module_id)
            candidate = candidates.get(patch.replacement.candidate_id)
            if snapshot is None or snapshot.get("snapshot_hash") != patch.base_snapshot_hash:
                raise PreconditionFailedError("impact patch base snapshot is stale")
            if (
                candidate is None
                or candidate.module_id != patch.module_id
                or candidate.name != patch.replacement.candidate_name
            ):
                raise DomainConflictError("impact patch does not match a project candidate")
            if not set(patch.replacement.evidence_binding_ids) <= set(
                candidate.evidence_binding_ids
            ):
                raise DomainConflictError("impact patch has unbound evidence")

        stale_ids = tuple(dict.fromkeys(stale_evidence_binding_ids))
        if not set(stale_ids) <= set(evidence) or any(
            evidence[item_id].module_id not in affected_set for item_id in stale_ids
        ):
            raise DomainConflictError("stale evidence must belong to affected project modules")
        replacement_pairs = {(item.module_id, item.replacement.candidate_id) for item in patches}
        replacement_bom = tuple(replacement_bom_items)
        if any(
            item.module_id not in affected_set
            or (item.module_id, item.candidate_id) not in replacement_pairs
            or not set(item.evidence_binding_ids)
            <= set(candidates[item.candidate_id].evidence_binding_ids)
            for item in replacement_bom
        ):
            raise DomainConflictError("replacement BOM must bind affected patch candidates")
        implementation = tuple(replacement_implementation_steps)
        verification = tuple(replacement_verification_steps)
        if (
            not implementation
            or not verification
            or any(
                not set(step.module_ids) or not set(step.module_ids) <= affected_set
                for step in (*implementation, *verification)
            )
        ):
            raise DomainConflictError("replacement steps must stay inside affected modules")

        impact = ImpactAnalysis(
            project_id=project_id,
            observation_id=observation.id,
            base_solution_version_id=base.id,
            basis_hash=proposal_basis,
            direct_affected_module_ids=direct,
            transitive_affected_module_ids=transitive,
            affected_module_ids=affected,
            unaffected_module_ids=unaffected,
            dependency_paths=dependency_paths,
            classifications=impact_plan.classifications,
            stale_evidence_binding_ids=stale_ids,
            module_patches=patches,
            replacement_bom_items=replacement_bom,
            replacement_implementation_steps=implementation,
            replacement_verification_steps=verification,
            summary=summary,
            risks=tuple(risks),
            artifact_ref=artifact_ref,
            profile_id=profile_id,
            profile_revision=profile_revision,
        )
        await self._store.update_project(
            project=project.model_copy(
                update={"revision": project.revision + 1, "updated_at": datetime.now(UTC)}
            ),
            expected_revision=project.revision,
        )
        await self._store.add_impact_analysis(impact)
        await self._store.append_event(
            project_id,
            "impact.proposed",
            {
                "impact_id": str(impact.id),
                "direct_affected_count": len(direct),
                "transitive_affected_count": len(transitive),
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(impact.id))
        await self._store.commit()
        return impact

    async def approve_impact_and_patch(
        self,
        *,
        impact_id: UUID,
        expected_project_revision: int,
        basis_hash: str,
        idempotency_key: str,
    ) -> tuple[PatchSet, SolutionVersion]:
        payload_hash = canonical_hash(
            "approve_impact_and_patch",
            impact_id,
            expected_project_revision,
            basis_hash,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            patch_set = await self._store.get_patch_set(UUID(receipt))
            if patch_set is None:
                raise DomainConflictError("command receipt references a missing patch set")
            solution_id = self._revision_solution_id(patch_set.id)
            solution = await self._store.get_solution_version(solution_id)
            if solution is None:
                raise DomainConflictError("command receipt references a missing solution")
            return patch_set, solution

        impact = await self._store.get_impact_analysis(impact_id)
        if impact is None:
            raise DomainNotFoundError("impact analysis not found")
        if impact.status is not ImpactStatus.PROPOSED:
            raise DomainConflictError("impact analysis is already resolved")
        if impact.basis_hash != basis_hash:
            raise PreconditionFailedError("impact proposal basis is stale")
        if impact.artifact_ref is None or impact.profile_id is None or not impact.module_patches:
            raise DomainConflictError("legacy impact analysis cannot be strictly approved")
        base = await self._store.get_solution_version(impact.base_solution_version_id)
        if base is None:
            raise PreconditionFailedError("impact base is missing")
        project = await self._required_project(impact.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_solution_version_id != base.id:
            raise PreconditionFailedError("impact base is no longer the active solution")
        patch_set = PatchSet(
            project_id=impact.project_id,
            impact_analysis_id=impact.id,
            base_solution_version_id=base.id,
            base_solution_basis_hash=base.basis_hash,
            typed_module_patches=impact.module_patches,
            replacement_bom_items=impact.replacement_bom_items,
            replacement_implementation_steps=impact.replacement_implementation_steps,
            replacement_verification_steps=impact.replacement_verification_steps,
        )
        affected_set = set(impact.affected_module_ids)
        if not {item.module_id for item in patch_set.typed_module_patches} <= affected_set:
            raise DomainConflictError("patch set cannot touch unaffected modules")
        replacements = {item.module_id: item for item in patch_set.typed_module_patches}
        patched_set = set(replacements)
        snapshots = tuple(
            (
                {
                    **replacements[UUID(str(snapshot["module_id"]))].replacement.model_dump(
                        mode="json"
                    ),
                    "snapshot_hash": canonical_hash(
                        replacements[UUID(str(snapshot["module_id"]))].replacement
                    ),
                }
                if UUID(str(snapshot["module_id"])) in replacements
                else snapshot
            )
            for snapshot in base.module_snapshots
        )
        bom = tuple(
            item for item in base.bom if UUID(str(item["module_id"])) not in patched_set
        ) + tuple(item.model_dump(mode="json") for item in patch_set.replacement_bom_items)

        def merge_steps(
            base_steps: Sequence[dict[str, Any]],
            replacements: Sequence[SolutionPlanStep],
        ) -> tuple[dict[str, Any], ...]:
            replacement_scope = {
                module_id for step in replacements for module_id in step.module_ids
            }
            preserved = tuple(
                item
                for item in base_steps
                if set(UUID(str(value)) for value in item.get("module_ids", ())).isdisjoint(
                    replacement_scope
                )
            )
            return preserved + tuple(item.model_dump(mode="json") for item in replacements)

        implementation = merge_steps(
            base.implementation_steps,
            patch_set.replacement_implementation_steps,
        )
        verification = merge_steps(
            base.verification_steps,
            patch_set.replacement_verification_steps,
        )
        stale_evidence = set(impact.stale_evidence_binding_ids)
        replacement_evidence = {
            evidence_id
            for patch in patch_set.typed_module_patches
            for evidence_id in patch.replacement.evidence_binding_ids
        }
        next_solution = SolutionVersion.model_validate(
            base.model_copy(
                update={
                    "id": self._revision_solution_id(patch_set.id),
                    "version": base.version + 1,
                    "basis_hash": canonical_hash(base.basis_hash, patch_set),
                    "module_snapshots": snapshots,
                    "evidence_binding_ids": tuple(
                        dict.fromkeys(
                            (
                                *(
                                    item
                                    for item in base.evidence_binding_ids
                                    if item not in stale_evidence
                                ),
                                *replacement_evidence,
                            )
                        )
                    ),
                    "bom": bom,
                    "implementation_steps": implementation,
                    "verification_steps": verification,
                    "previous_version_id": base.id,
                    "created_at": datetime.now(UTC),
                }
            ).model_dump()
        )
        approved_impact = impact.model_copy(
            update={"status": ImpactStatus.APPROVED, "resolved_at": datetime.now(UTC)}
        )
        await self._store.update_project(
            project=project.model_copy(
                update={
                    "stage": ProjectStage.APPROVED,
                    "revision": project.revision + 1,
                    "active_solution_version_id": next_solution.id,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.add_patch_set(patch_set)
        await self._store.update_impact_analysis(approved_impact)
        await self._store.add_solution_version(next_solution)
        await self._store.append_event(
            impact.project_id,
            "solution.revised",
            {
                "solution_id": str(next_solution.id),
                "previous_solution_id": str(base.id),
                "version": next_solution.version,
                "affected_module_ids": [str(item) for item in impact.affected_module_ids],
                "reused_module_ids": [str(item) for item in impact.unaffected_module_ids],
            },
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(patch_set.id),
        )
        await self._store.commit()
        return patch_set, next_solution

    @staticmethod
    def _impact_partition(
        modules: Sequence[Module],
        direct_module_ids: Sequence[UUID],
    ) -> tuple[tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...]]:
        module_ids = {item.id for item in modules}
        direct_set = set(direct_module_ids)
        if not direct_set or not direct_set <= module_ids:
            raise DomainConflictError("impact must reference active project modules")
        affected_set = set(direct_set)
        changed = True
        while changed:
            changed = False
            for module in modules:
                if module.id not in affected_set and affected_set.intersection(
                    module.dependency_ids
                ):
                    affected_set.add(module.id)
                    changed = True
        ordered = tuple(sorted(modules, key=lambda item: item.key))
        direct = tuple(item.id for item in ordered if item.id in direct_set)
        transitive = tuple(
            item.id for item in ordered if item.id in affected_set and item.id not in direct_set
        )
        affected = tuple(item.id for item in ordered if item.id in affected_set)
        unaffected = tuple(item.id for item in ordered if item.id not in affected_set)
        return direct, transitive, affected, unaffected

    @staticmethod
    def _revision_solution_id(patch_set_id: UUID) -> UUID:
        return UUID(bytes=sha256(str(patch_set_id).encode()).digest()[:16])

    async def _active_modules(self, project: Project) -> tuple[Module, ...]:
        if project.active_blueprint_id is not None:
            blueprint = await self._store.get_project_blueprint(project.active_blueprint_id)
            if blueprint is None or blueprint.status is not BlueprintStatus.ACTIVE:
                raise DomainConflictError("active project blueprint is unavailable")
            modules_by_id = {item.id: item for item in await self._store.list_modules(project.id)}
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
                    "active project blueprint references a missing module"
                ) from exc
        return ()

    async def _active_configurations(self, project: Project) -> tuple[ModuleConfiguration, ...]:
        configurations: list[ModuleConfiguration] = []
        for module in await self._active_modules(project):
            configuration = await self._active_module_configuration(project.id, module.id)
            if configuration is not None:
                configurations.append(configuration)
        return tuple(sorted(configurations, key=lambda item: str(item.module_id)))

    async def _append_draft_history(
        self,
        *,
        project: Project,
        kind: DraftHistoryKind,
        adjustment_ids: tuple[UUID, ...] = (),
    ) -> DraftHistoryEntry:
        history = await self._store.list_draft_history_entries(project.id)
        parent = history[-1] if history else None
        state_hash = canonical_hash(
            project.active_blueprint_id,
            await self._active_configurations(project),
            tuple(
                sorted(
                    (
                        item
                        for item in await self._store.list_selection_locks(project.id)
                        if item.active
                    ),
                    key=lambda item: str(item.id),
                )
            ),
        )
        entry = DraftHistoryEntry(
            project_id=project.id,
            sequence=1 if parent is None else parent.sequence + 1,
            parent_entry_id=None if parent is None else parent.id,
            kind=kind,
            adjustment_ids=adjustment_ids,
            draft_state_hash=state_hash,
        )
        await self._store.add_draft_history_entry(entry)
        return entry

    async def _advance_project(self, project: Project) -> Project:
        updated = project.model_copy(
            update={"revision": project.revision + 1, "updated_at": datetime.now(UTC)}
        )
        await self._store.update_project(project=updated, expected_revision=project.revision)
        return updated

    async def _require_active_module(self, project: Project, module_id: UUID) -> Module:
        module = next(
            (item for item in await self._active_modules(project) if item.id == module_id),
            None,
        )
        if module is None:
            raise DomainConflictError("module is not active in the current project blueprint")
        return module

    async def _validate_adjustment_target(
        self,
        *,
        project_id: UUID,
        module_id: UUID,
        kind: UserAdjustmentKind,
        target: dict[str, Any],
    ) -> None:
        if kind in {UserAdjustmentKind.SET_OPTION, UserAdjustmentKind.SET_PARAMETER}:
            if not isinstance(target.get("key"), str) or not target["key"].strip():
                raise DomainConflictError(
                    "option and parameter adjustments require a non-empty key"
                )
            if "value" not in target:
                raise DomainConflictError("option and parameter adjustments require a value")
            return
        if kind is UserAdjustmentKind.SELECT_CANDIDATE:
            try:
                candidate_id = UUID(str(target["candidate_id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise DomainConflictError(
                    "candidate adjustment requires a valid candidate_id"
                ) from exc
            if not any(
                candidate.id == candidate_id and candidate.module_id == module_id
                for candidate in await self._store.list_candidates(project_id)
            ):
                raise DomainConflictError("candidate adjustment does not belong to the module")
            return
        if kind is UserAdjustmentKind.ADD_CUSTOM_CANDIDATE:
            if not isinstance(target.get("name"), str) or not target["name"].strip():
                raise DomainConflictError("custom candidate adjustment requires a name")
            return
        raise DomainConflictError("unsupported user adjustment kind")

    async def _enforce_selection_locks(
        self,
        *,
        project_id: UUID,
        module_id: UUID,
        kind: UserAdjustmentKind,
        target: dict[str, Any],
    ) -> None:
        if kind is not UserAdjustmentKind.SELECT_CANDIDATE:
            return
        candidate_id = UUID(str(target["candidate_id"]))
        for lock in await self._store.list_selection_locks(
            project_id=project_id,
            module_id=module_id,
        ):
            if not lock.active:
                continue
            if lock.candidate_id is None or lock.candidate_id != candidate_id:
                raise DomainConflictError("candidate selection is protected by an active user lock")

    async def _active_module_configuration(
        self, project_id: UUID, module_id: UUID
    ) -> ModuleConfiguration | None:
        active = [
            item
            for item in await self._store.list_module_configurations(project_id, module_id)
            if item.status is ModuleConfigurationStatus.ACTIVE
        ]
        if len(active) > 1:
            raise DomainConflictError("module has more than one active configuration revision")
        return active[0] if active else None

    @staticmethod
    def _module_configuration_state_hash(configuration: ModuleConfiguration) -> str:
        """Stable user-facing configuration fingerprint, independent of lifecycle metadata."""
        return canonical_hash(configuration.module_id, configuration.options)

    @staticmethod
    def _next_module_configuration(
        current: ModuleConfiguration | None,
        adjustment: UserAdjustment,
    ) -> ModuleConfiguration:
        options = {} if current is None else dict(current.options)
        if adjustment.kind in {UserAdjustmentKind.SET_OPTION, UserAdjustmentKind.SET_PARAMETER}:
            options[str(adjustment.target["key"])] = adjustment.target["value"]
        elif adjustment.kind is UserAdjustmentKind.SELECT_CANDIDATE:
            options["selected_candidate_id"] = str(adjustment.target["candidate_id"])
        else:
            options["custom_candidate"] = dict(adjustment.target)
        return ModuleConfiguration(
            project_id=adjustment.project_id,
            module_id=adjustment.module_id,
            revision=1 if current is None else current.revision + 1,
            base_snapshot_hash=None if current is None else canonical_hash(current),
            options=options,
        )

    async def _required_project(self, project_id: UUID) -> Project:
        project = await self._store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        return project

    @staticmethod
    def _validate_proposed_module_set(modules: Sequence[ProposedModule]) -> None:
        """Validate a complete proposed module graph (unique keys, no cycles)."""
        keys = [item.key for item in modules]
        if len(keys) != len(set(keys)):
            raise DomainConflictError("proposed module keys must be unique")
        dependencies_by_key = {
            item.key: tuple(dict.fromkeys(item.dependency_keys)) for item in modules
        }
        ProjectApplication._validate_module_graph(set(keys), dependencies_by_key)

    @staticmethod
    def _validate_module_graph(
        module_keys: set[str],
        dependencies_by_key: dict[str, tuple[str, ...]],
    ) -> None:
        for key, dependencies in dependencies_by_key.items():
            unknown = set(dependencies) - module_keys
            if unknown:
                raise DomainConflictError(
                    f"module {key} has unknown dependencies: {sorted(unknown)}"
                )
            if key in dependencies:
                raise DomainConflictError(f"module {key} cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise DomainConflictError("module dependencies contain a cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in dependencies_by_key[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in sorted(module_keys):
            visit(key)

    async def _validate_reshape_proposal(
        self, project: Project, proposal: ProjectReshapeProposal
    ) -> None:
        active_modules = await self._active_modules(project)
        active_ids = {item.id for item in active_modules}
        retained_ids = set(proposal.unchanged_module_ids)
        affected_ids = set(proposal.affected_module_ids)
        if not retained_ids <= active_ids or not affected_ids <= active_ids:
            raise DomainConflictError("project reshape references inactive modules")
        if retained_ids | affected_ids != active_ids:
            raise DomainConflictError("project reshape must classify every active module")
        relationship_only = (
            (proposal.dependency_edges is not None or proposal.engineering_couplings is not None)
            and not proposal.new_modules
        )
        if relationship_only:
            if proposal.lineage_changes:
                raise DomainConflictError("relationship-only reshape cannot assert module lineage")
            edge_ids = proposal.dependency_edges or ()
            if any(
                source not in active_ids or target not in active_ids
                for source, target in edge_ids
            ):
                raise DomainConflictError("reshape dependency edges reference inactive modules")
            by_id = {item.id: item for item in active_modules}
            dependencies_by_key: dict[str, tuple[str, ...]] = {
                item.key: () for item in active_modules
            }
            for source_id, target_id in edge_ids:
                target = by_id[target_id]
                dependencies_by_key[target.key] = (
                    *dependencies_by_key[target.key],
                    by_id[source_id].key,
                )
            self._validate_module_graph(set(dependencies_by_key), dependencies_by_key)
            active_lineage_ids = {item.lineage_id for item in active_modules}
            couplings = proposal.engineering_couplings or ()
            if any(
                edge.source_lineage_id not in active_lineage_ids
                or edge.target_lineage_id not in active_lineage_ids
                for edge in couplings
            ):
                raise DomainConflictError("engineering couplings reference inactive lineages")
            return
        if not retained_ids and not proposal.new_modules:
            raise DomainConflictError("project reshape cannot remove every module")
        existing_keys = {item.key for item in active_modules}
        new_keys = {item.key for item in proposal.new_modules}
        if new_keys & existing_keys:
            raise DomainConflictError("reshape new module keys must not reuse existing keys")
        self._validate_reshape_lineage_changes(
            proposal=proposal,
            active_modules=active_modules,
            affected_ids=affected_ids,
        )
        retained = [item for item in active_modules if item.id in retained_ids]
        retained_keys = {item.key for item in retained}
        module_keys = retained_keys | new_keys
        dependencies_by_key = {
            item.key: tuple(
                next(candidate.key for candidate in active_modules if candidate.id == dependency_id)
                for dependency_id in item.dependency_ids
            )
            for item in retained
        }
        dependencies_by_key.update(
            {item.key: item.dependency_keys for item in proposal.new_modules}
        )
        self._validate_module_graph(module_keys, dependencies_by_key)

    @staticmethod
    def _validate_reshape_lineage_changes(
        *,
        proposal: ProjectReshapeProposal,
        active_modules: Sequence[Module],
        affected_ids: set[UUID],
    ) -> None:
        if not proposal.lineage_changes:
            return
        active_by_id = {item.id: item for item in active_modules}
        target_keys = {item.key for item in proposal.new_modules}
        source_operations: dict[UUID, list[ModuleLineageChange]] = {}
        for change in proposal.lineage_changes:
            if change.target_new_module_key not in target_keys:
                raise DomainConflictError("reshape lineage target is not a new module")
            for source_id in change.source_module_ids:
                source = active_by_id.get(source_id)
                if source is None or source_id not in affected_ids:
                    raise DomainConflictError(
                        "reshape lineage source must be an affected active module"
                    )
                if source.lineage_id is None:
                    raise DomainConflictError("reshape lineage source has no stable identity")
                source_operations.setdefault(source_id, []).append(change)

        for operations in source_operations.values():
            if len(operations) > 1 and any(
                item.operation is not ModuleLineageOperation.SPLIT for item in operations
            ):
                raise DomainConflictError(
                    "only one source module may be reused by multiple split operations"
                )

    @staticmethod
    def _materialize_reshape_modules(
        *,
        project: Project,
        requirement_revision_id: UUID,
        retained_modules: Sequence[Module],
        drafts: Sequence[ProposedModule],
    ) -> tuple[Module, ...]:
        by_key = {item.key: item for item in retained_modules}
        new_modules = tuple(
            Module(
                project_id=project.id,
                requirement_revision_id=requirement_revision_id,
                key=draft.key,
                name=draft.name,
                responsibility=draft.responsibility,
                acceptance=draft.acceptance,
                open_questions=draft.open_questions,
            )
            for draft in drafts
        )
        by_key.update({item.key: item for item in new_modules})
        return tuple(
            item.model_copy(
                update={"dependency_ids": tuple(by_key[key].id for key in draft.dependency_keys)}
            )
            for item, draft in zip(new_modules, drafts, strict=True)
        )

    # ── Spend Budget ──────────────────────────────────────────────────────

    @staticmethod
    def _validate_decimal(value: str, field_name: str) -> Decimal:
        if not value or not value.strip():
            raise DomainConflictError(f"{field_name} must be a non-empty decimal string")
        try:
            amount = Decimal(value.strip())
        except InvalidOperation as exc:
            raise DomainConflictError(f"{field_name} is not a valid decimal") from exc
        if amount <= 0:
            raise DomainConflictError(f"{field_name} must be positive")
        return amount

    @staticmethod
    def _validate_currency(currency: str) -> str:
        cleaned = currency.strip().upper()
        if len(cleaned) != 3 or not cleaned.isalpha():
            raise DomainConflictError("currency must be a 3-letter ISO 4217 code")
        return cleaned

    async def propose_spend_budget(
        self,
        *,
        project_id: UUID,
        amount: str,
        currency: str,
        summary: str,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> SpendBudgetProposal:
        payload_hash = canonical_hash(
            "propose_spend_budget",
            project_id,
            amount,
            currency,
            summary,
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            proposal = await self._store.get_spend_budget_proposal(UUID(receipt))
            if proposal is None:
                raise DomainConflictError(
                    "command receipt references a missing spend budget proposal"
                )
            return proposal

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        self._validate_decimal(amount, "amount")
        self._validate_currency(currency)

        proposal = SpendBudgetProposal(
            project_id=project_id,
            amount=amount.strip(),
            currency=currency.strip().upper(),
            summary=summary,
            basis_project_revision=expected_project_revision,
        )
        await self._store.add_spend_budget_proposal(proposal)
        await self._store.append_event(
            project_id,
            "spend_budget.proposed",
            {
                "spend_budget_proposal_id": str(proposal.id),
                "amount": proposal.amount,
                "currency": proposal.currency,
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
        await self._store.commit()
        return proposal

    async def resolve_spend_budget(
        self,
        *,
        proposal_id: UUID,
        decision: SpendBudgetProposalStatus,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> SpendBudgetRevision:
        if decision not in {
            SpendBudgetProposalStatus.APPLIED,
            SpendBudgetProposalStatus.REJECTED,
        }:
            raise DomainConflictError("spend budget can only be applied or rejected")
        payload_hash = canonical_hash(
            "resolve_spend_budget",
            proposal_id,
            decision,
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            # APPLIED: receipt stores the revision UUID.
            revision = await self._store.get_spend_budget_revision(UUID(receipt))
            if revision is not None:
                return revision
            # REJECTED: receipt stores the proposal UUID; reconstruct from proposal.
            # Only REJECTED proposals are accepted here — an APPLIED proposal whose
            # revision is missing is a corruption and must raise.
            if decision is SpendBudgetProposalStatus.REJECTED:
                proposal = await self._store.get_spend_budget_proposal(UUID(receipt))
                if proposal is not None and proposal.status is SpendBudgetProposalStatus.REJECTED:
                    return SpendBudgetRevision(
                        project_id=proposal.project_id,
                        proposal_id=proposal.id,
                        revision=0,
                        amount=proposal.amount,
                        currency=proposal.currency,
                        status=proposal.status,
                    )
            raise DomainConflictError(
                "command receipt references a missing spend budget resolution"
            )

        proposal = await self._store.get_spend_budget_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("spend budget proposal not found")
        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if proposal.status is not SpendBudgetProposalStatus.PROPOSED:
            raise PreconditionFailedError("spend budget proposal is already resolved")

        resolved = proposal.model_copy(
            update={"status": decision, "resolved_at": datetime.now(UTC)}
        )
        await self._store.update_spend_budget_proposal(
            resolved, expected_status=SpendBudgetProposalStatus.PROPOSED
        )

        if decision is SpendBudgetProposalStatus.REJECTED:
            await self._store.append_event(
                project.id,
                "spend_budget.rejected",
                {"spend_budget_proposal_id": str(proposal.id)},
            )
            await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
            await self._store.commit()
            # Return a rejected revision-like payload via the receipt; the caller
            # receives a SpendBudgetRevision with the proposal's amount/currency.
            revision = SpendBudgetRevision(
                project_id=proposal.project_id,
                proposal_id=proposal.id,
                revision=0,
                amount=proposal.amount,
                currency=proposal.currency,
                status=decision,
            )
            return revision

        # APPLIED: create an immutable SpendBudgetRevision and activate it.
        previous_revisions = await self._store.list_spend_budget_revisions(project.id)
        applied_count = sum(
            1 for r in previous_revisions if r.status == SpendBudgetProposalStatus.APPLIED
        )
        next_revision = applied_count + 1

        # Supersede the prior active revision if one exists.
        prior_active = await self._store.get_active_spend_budget(project.id)
        if prior_active is not None:
            superseded = prior_active.model_copy(
                update={"status": SpendBudgetProposalStatus.SUPERSEDED}
            )
            await self._store.update_spend_budget_revision(
                superseded,
                expected_status=SpendBudgetProposalStatus.APPLIED,
            )

        revision = SpendBudgetRevision(
            project_id=project.id,
            proposal_id=proposal.id,
            revision=next_revision,
            amount=proposal.amount,
            currency=proposal.currency,
        )
        await self._store.add_spend_budget_revision(revision)
        await self._store.update_project(
            project=project.model_copy(
                update={
                    "revision": project.revision + 1,
                    "active_spend_budget_revision_id": revision.id,
                    "updated_at": datetime.now(UTC),
                }
            ),
            expected_revision=project.revision,
        )
        await self._store.append_event(
            project.id,
            "spend_budget.resolved",
            {
                "spend_budget_proposal_id": str(proposal.id),
                "spend_budget_revision_id": str(revision.id),
                "amount": revision.amount,
                "currency": revision.currency,
                "revision": revision.revision,
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(revision.id))
        await self._store.commit()
        return revision

    async def preview_spend_budget_impact(
        self,
        *,
        proposal_id: UUID,
        cost_items: Sequence[SpendBudgetCostItem],
        expected_project_revision: int,
        idempotency_key: str,
    ) -> SpendBudgetImpactPreview:
        """Compute a deterministic, read-only budget-impact classification.

        The preview never rewrites SelectionLock, candidates, research results,
        freeze plans, or external product state.  Each cost item receives
        exactly one classification.
        """
        payload_hash = canonical_hash(
            "preview_spend_budget_impact",
            proposal_id,
            tuple(cost_items),
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            preview = await self._store.get_spend_budget_impact_preview(UUID(receipt))
            if preview is None:
                raise DomainConflictError(
                    "command receipt references a missing spend budget impact preview"
                )
            return preview

        proposal = await self._store.get_spend_budget_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("spend budget proposal not found")
        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")

        budget_amount = self._validate_decimal(proposal.amount, "budget amount")
        budget_currency = proposal.currency

        lines: list[SpendBudgetImpactLine] = []
        for item in cost_items:
            classification, reason = self._classify_cost_item(
                item=item,
                budget_amount=budget_amount,
                budget_currency=budget_currency,
            )
            lines.append(
                SpendBudgetImpactLine(
                    line_ref=item.ref,
                    amount=item.amount,
                    currency=item.currency,
                    classification=classification,
                    reason=reason,
                )
            )

        within_count = sum(
            1 for line in lines if line.classification is SpendBudgetImpactClassification.WITHIN
        )
        over_count = sum(
            1 for line in lines if line.classification is SpendBudgetImpactClassification.OVER
        )
        unknown_count = sum(
            1 for line in lines if line.classification is SpendBudgetImpactClassification.UNKNOWN
        )

        preview = SpendBudgetImpactPreview(
            project_id=project.id,
            proposal_id=proposal.id,
            basis_project_revision=project.revision,
            budget_amount=proposal.amount,
            budget_currency=budget_currency,
            lines=tuple(lines),
            summary=(
                f"预算 {budget_currency} {proposal.amount} 对 "
                f"{len(lines)} 项成本的影响：{within_count} 项在预算内，"
                f"{over_count} 项超出预算，{unknown_count} 项无法确定。"
                "本预览只读，不修改选型锁、候选、研究结果或冻结方案。"
            ),
        )
        await self._store.add_spend_budget_impact_preview(preview)
        await self._store.append_event(
            project.id,
            "spend_budget.preview_generated",
            {
                "spend_budget_impact_preview_id": str(preview.id),
                "proposal_id": str(proposal.id),
                "within_count": within_count,
                "over_count": over_count,
                "unknown_count": unknown_count,
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(preview.id))
        await self._store.commit()
        return preview

    @staticmethod
    def _classify_cost_item(
        *,
        item: SpendBudgetCostItem,
        budget_amount: Decimal,
        budget_currency: str,
    ) -> tuple[SpendBudgetImpactClassification, str]:
        # Missing price
        if not item.amount or not item.amount.strip():
            return (
                SpendBudgetImpactClassification.UNKNOWN,
                f"{item.ref}: 价格缺失，无法比较",
            )
        # Currency mismatch
        item_currency = item.currency.strip().upper()
        if item_currency != budget_currency:
            return (
                SpendBudgetImpactClassification.UNKNOWN,
                f"{item.ref}: 币种 {item_currency} 与预算币种 {budget_currency} 不匹配",
            )
        # Stale / unobserved
        if item.observed_at is None:
            return (
                SpendBudgetImpactClassification.UNKNOWN,
                f"{item.ref}: 价格未记录观测时间，视为过期",
            )
        # Known, same-currency comparison
        try:
            cost = Decimal(item.amount.strip())
        except InvalidOperation:
            return (
                SpendBudgetImpactClassification.UNKNOWN,
                f"{item.ref}: 价格格式无效",
            )
        if cost <= 0:
            return (
                SpendBudgetImpactClassification.UNKNOWN,
                f"{item.ref}: 价格必须为正数",
            )
        if cost <= budget_amount:
            return (
                SpendBudgetImpactClassification.WITHIN,
                f"{item.ref}: {item_currency} {item.amount} "
                f"≤ 预算 {budget_currency} {budget_amount}",
            )
        return (
            SpendBudgetImpactClassification.OVER,
            f"{item.ref}: {item_currency} {item.amount} > 预算 {budget_currency} {budget_amount}",
        )
