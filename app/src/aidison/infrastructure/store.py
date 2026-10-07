from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar, cast
from uuid import UUID, uuid4

import sqlalchemy
from pydantic import BaseModel
from sqlalchemy import CursorResult, Select, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.application.ports import (
    DomainStore,
    DuplicateCommandError,
    OptimisticConcurrencyError,
)
from aidison.application.service import DomainConflictError, DomainNotFoundError
from aidison.domain.events import (
    DomainEventMetadata,
    StoredDomainEvent,
    canonical_payload_hash,
)
from aidison.domain.models import (
    AdjustmentBatch,
    AdjustmentBatchStatus,
    BlueprintStatus,
    Candidate,
    ChangeImpactPreview,
    CheckoutHandoff,
    CompatibilityFinding,
    ContextSummary,
    ContextSummaryStatus,
    ConversationActionProposal,
    ConversationActionProposalKind,
    ConversationActionProposalStatus,
    ConversationClarification,
    ConversationClarificationKind,
    ConversationClarificationStatus,
    ConversationSession,
    ConversationSessionStatus,
    ConversationTurn,
    ConversationTurnRole,
    ConversationTurnType,
    DecisionRequest,
    DecisionStatus,
    DraftHistoryEntry,
    EffectApproval,
    EffectApprovalStatus,
    EvidenceBinding,
    ExecutionPlanProposal,
    ExecutionPlanStatus,
    ImpactAnalysis,
    ImpactStatus,
    Module,
    ModuleConfiguration,
    ModuleConfigurationStatus,
    ModuleLineage,
    ModuleMemoryItem,
    ModuleMemoryItemKind,
    ModuleMemoryItemStatus,
    ModuleWorkstream,
    ModuleWorkstreamStatus,
    Observation,
    OfferSnapshot,
    PatchSet,
    Project,
    ProjectBlueprint,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProjectRevisionChangeKind,
    ProjectRevisionManifest,
    ProjectStage,
    PurchaseProposal,
    RequirementRevision,
    RequirementsChangeProposal,
    RequirementsChangeProposalStatus,
    SelectionLock,
    SolutionProposal,
    SolutionProposalStatus,
    SolutionSnapshot,
    SolutionVersion,
    SpendBudgetImpactPreview,
    SpendBudgetProposal,
    SpendBudgetProposalStatus,
    SpendBudgetRevision,
    UserAdjustment,
)
from aidison.infrastructure.orm import (
    AdjustmentBatchRow,
    CandidateRow,
    ChangeImpactPreviewRow,
    CheckoutHandoffRow,
    CommandReceiptRow,
    CompatibilityFindingRow,
    ContextSummaryRow,
    ConversationActionProposalRow,
    ConversationClarificationRow,
    ConversationSessionRow,
    ConversationTurnRow,
    DecisionRequestRow,
    DomainEventRow,
    DraftHistoryEntryRow,
    EffectApprovalRow,
    EvidenceBindingRow,
    ExecutionPlanProposalRow,
    ImpactAnalysisRow,
    ModuleConfigurationRow,
    ModuleLineageRow,
    ModuleMemoryItemRow,
    ModuleRow,
    ModuleWorkstreamRow,
    ObservationRow,
    OfferSnapshotRow,
    PatchSetRow,
    ProjectBlueprintRow,
    ProjectReshapeProposalRow,
    ProjectRevisionManifestRow,
    ProjectRow,
    PurchaseProposalRow,
    RequirementRevisionRow,
    RequirementsChangeProposalRow,
    SelectionLockRow,
    SolutionProposalRow,
    SolutionSnapshotRow,
    SolutionVersionRow,
    SpendBudgetImpactPreviewRow,
    SpendBudgetProposalRow,
    SpendBudgetRevisionRow,
    UserAdjustmentRow,
)
from aidison.infrastructure.signals import publish_domain_event_signal

ModelT = TypeVar("ModelT", bound=BaseModel)


class PersistenceCorruptionError(RuntimeError):
    """Raised when typed identity columns disagree with the canonical JSON payload."""


def _payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _decode(model_type: type[ModelT], payload: dict[str, Any]) -> ModelT:
    return model_type.model_validate(payload)


def _assert_identity(
    *,
    entity_name: str,
    row_id: UUID,
    row_project_id: UUID,
    payload_id: UUID,
    payload_project_id: UUID,
) -> None:
    if row_id != payload_id or row_project_id != payload_project_id:
        raise PersistenceCorruptionError(f"{entity_name} typed identity differs from payload")


class PostgresDomainStore(DomainStore):
    """Transactional PostgreSQL implementation of Aidison's canonical Domain store.

    One instance is bound to one request/session transaction. `claim_command`
    holds a transaction-scoped advisory lock, so equal idempotency keys cannot
    both execute before the winner commits or rolls back.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_command(self, idempotency_key: str, payload_hash: str) -> str | None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": idempotency_key},
        )
        receipt = await self._session.get(CommandReceiptRow, idempotency_key)
        if receipt is None:
            return None
        if receipt.payload_hash != payload_hash:
            raise DuplicateCommandError("idempotency key was used with another payload")
        return receipt.result_ref

    async def save_command_receipt(
        self,
        idempotency_key: str,
        payload_hash: str,
        result_ref: str,
    ) -> None:
        self._session.add(
            CommandReceiptRow(
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                result_ref=result_ref,
            )
        )

    async def add_project(self, project: Project) -> None:
        self._session.add(
            ProjectRow(
                id=project.id,
                name=project.name,
                goal=project.goal,
                stage=project.stage.value,
                revision=project.revision,
                event_sequence=0,
                active_requirement_revision_id=project.active_requirement_revision_id,
                active_blueprint_id=project.active_blueprint_id,
                active_solution_version_id=project.active_solution_version_id,
                created_at=project.created_at,
                updated_at=project.updated_at,
            )
        )
        # The baseline manifest has an explicit database FK but no SQLAlchemy
        # relationship object. Flush the parent first so later event queries
        # cannot trigger an autoflush that inserts the child before Project.
        await self._session.flush()
        self._session.add(
            ProjectRevisionManifestRow(
                **self._manifest_values(
                    ProjectRevisionManifest(
                        project_id=project.id,
                        revision=project.revision,
                        change_kind=ProjectRevisionChangeKind.BASELINE,
                        change_summary="Project created",
                        approved_at=project.created_at,
                    )
                )
            )
        )

    @staticmethod
    def _manifest_values(manifest: ProjectRevisionManifest) -> dict[str, Any]:
        return {
            "id": manifest.id,
            "project_id": manifest.project_id,
            "revision": manifest.revision,
            "parent_revision_id": manifest.parent_revision_id,
            "requirement_revision_id": manifest.requirement_revision_id,
            "blueprint_id": manifest.blueprint_id,
            "solution_version_id": manifest.solution_version_id,
            "change_kind": manifest.change_kind.value,
            "change_summary": manifest.change_summary,
            "content_hash": manifest.content_hash,
            "approved_at": manifest.approved_at,
        }

    @staticmethod
    def _manifest_from_row(row: ProjectRevisionManifestRow) -> ProjectRevisionManifest:
        return ProjectRevisionManifest(
            id=row.id,
            project_id=row.project_id,
            revision=row.revision,
            parent_revision_id=row.parent_revision_id,
            requirement_revision_id=row.requirement_revision_id,
            blueprint_id=row.blueprint_id,
            solution_version_id=row.solution_version_id,
            change_kind=ProjectRevisionChangeKind(row.change_kind),
            change_summary=row.change_summary,
            content_hash=row.content_hash,
            approved_at=row.approved_at,
        )

    async def list_project_revision_manifests(
        self, project_id: UUID
    ) -> Sequence[ProjectRevisionManifest]:
        rows = (
            await self._session.scalars(
                select(ProjectRevisionManifestRow)
                .where(ProjectRevisionManifestRow.project_id == project_id)
                .order_by(ProjectRevisionManifestRow.revision)
            )
        ).all()
        return tuple(self._manifest_from_row(row) for row in rows)

    async def get_project(self, project_id: UUID) -> Project | None:
        row = await self._session.get(ProjectRow, project_id)
        if row is None:
            return None
        return self._project_from_row(row)

    async def list_projects(self, *, limit: int = 50) -> Sequence[Project]:
        rows = (
            await self._session.scalars(
                select(ProjectRow).order_by(ProjectRow.updated_at.desc()).limit(limit)
            )
        ).all()
        return tuple(self._project_from_row(row) for row in rows)

    @staticmethod
    def _project_from_row(row: ProjectRow) -> Project:
        return Project(
            id=row.id,
            name=row.name,
            goal=row.goal,
            stage=ProjectStage(row.stage),
            revision=row.revision,
            active_requirement_revision_id=row.active_requirement_revision_id,
            active_blueprint_id=row.active_blueprint_id,
            active_solution_version_id=row.active_solution_version_id,
            active_spend_budget_revision_id=row.active_spend_budget_revision_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def get_project_event_sequence(self, project_id: UUID) -> int:
        value = await self._session.scalar(
            select(ProjectRow.event_sequence).where(ProjectRow.id == project_id)
        )
        if value is None:
            raise DomainNotFoundError("project not found")
        return value

    async def update_project(self, project: Project, *, expected_revision: int) -> None:
        previous = await self._session.scalar(
            select(ProjectRevisionManifestRow)
            .where(
                ProjectRevisionManifestRow.project_id == project.id,
                ProjectRevisionManifestRow.revision == expected_revision,
            )
            .with_for_update()
        )
        if previous is None:
            raise PersistenceCorruptionError("project revision is missing its baseline manifest")
        statement = (
            update(ProjectRow)
            .where(ProjectRow.id == project.id, ProjectRow.revision == expected_revision)
            .values(
                name=project.name,
                goal=project.goal,
                stage=project.stage.value,
                revision=project.revision,
                active_requirement_revision_id=project.active_requirement_revision_id,
                active_blueprint_id=project.active_blueprint_id,
                active_solution_version_id=project.active_solution_version_id,
                active_spend_budget_revision_id=project.active_spend_budget_revision_id,
                updated_at=project.updated_at,
            )
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("project revision is stale")
        self._session.add(
            ProjectRevisionManifestRow(
                **self._manifest_values(
                    ProjectRevisionManifest(
                        project_id=project.id,
                        revision=project.revision,
                        parent_revision_id=previous.id,
                        requirement_revision_id=project.active_requirement_revision_id,
                        blueprint_id=project.active_blueprint_id,
                        solution_version_id=project.active_solution_version_id,
                        change_kind=ProjectRevisionChangeKind.PROJECT_UPDATE,
                        change_summary="Canonical project state updated",
                        approved_at=project.updated_at,
                    )
                )
            )
        )

    async def add_requirement_revision(self, requirement: RequirementRevision) -> None:
        self._session.add(
            RequirementRevisionRow(
                id=requirement.id,
                project_id=requirement.project_id,
                revision=requirement.revision,
                payload=_payload(requirement),
            )
        )
        await self._session.flush()

    async def get_requirement_revision(
        self,
        requirement_id: UUID,
    ) -> RequirementRevision | None:
        row = await self._session.get(RequirementRevisionRow, requirement_id)
        if row is None:
            return None
        requirement = _decode(RequirementRevision, row.payload)
        _assert_identity(
            entity_name="RequirementRevision",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=requirement.id,
            payload_project_id=requirement.project_id,
        )
        return requirement

    async def list_requirement_revisions(
        self,
        project_id: UUID,
    ) -> Sequence[RequirementRevision]:
        statement = (
            select(RequirementRevisionRow)
            .where(RequirementRevisionRow.project_id == project_id)
            .order_by(RequirementRevisionRow.revision)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(RequirementRevision, row.payload) for row in rows]

    async def add_modules(self, modules: Sequence[Module]) -> None:
        self._session.add_all(
            [
                ModuleRow(
                    id=module.id,
                    project_id=module.project_id,
                    requirement_revision_id=module.requirement_revision_id,
                    lineage_id=module.lineage_id,
                    key=module.key,
                    payload=_payload(module),
                )
                for module in modules
            ]
        )

    async def add_module_lineages(self, lineages: Sequence[ModuleLineage]) -> None:
        self._session.add_all(
            [
                ModuleLineageRow(
                    id=lineage.id,
                    project_id=lineage.project_id,
                    stable_key=lineage.stable_key,
                    display_name=lineage.display_name,
                    split_from_lineage_id=lineage.split_from_lineage_id,
                    merged_into_lineage_id=lineage.merged_into_lineage_id,
                    retired_at=lineage.retired_at,
                    created_at=lineage.created_at,
                )
                for lineage in lineages
            ]
        )

    async def list_module_lineages(self, project_id: UUID) -> Sequence[ModuleLineage]:
        rows = (
            await self._session.scalars(
                select(ModuleLineageRow)
                .where(ModuleLineageRow.project_id == project_id)
                .order_by(ModuleLineageRow.stable_key, ModuleLineageRow.created_at)
            )
        ).all()
        return tuple(
            ModuleLineage(
                id=row.id,
                project_id=row.project_id,
                stable_key=row.stable_key,
                display_name=row.display_name,
                split_from_lineage_id=row.split_from_lineage_id,
                merged_into_lineage_id=row.merged_into_lineage_id,
                retired_at=row.retired_at,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def update_module_lineage(self, lineage: ModuleLineage) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ModuleLineageRow)
                .where(
                    ModuleLineageRow.id == lineage.id,
                    ModuleLineageRow.project_id == lineage.project_id,
                )
                .values(
                    display_name=lineage.display_name,
                    split_from_lineage_id=lineage.split_from_lineage_id,
                    merged_into_lineage_id=lineage.merged_into_lineage_id,
                    retired_at=lineage.retired_at,
                )
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("module lineage is missing")

    async def add_module_workstreams(self, workstreams: Sequence[ModuleWorkstream]) -> None:
        self._session.add_all(
            [
                ModuleWorkstreamRow(
                    id=workstream.id,
                    project_id=workstream.project_id,
                    module_lineage_id=workstream.module_lineage_id,
                    status=workstream.status.value,
                    memory_manifest_ref=workstream.memory_manifest_ref,
                    last_project_revision=workstream.last_project_revision,
                    last_basis_hash=workstream.last_basis_hash,
                    optimistic_revision=workstream.optimistic_revision,
                    created_at=workstream.created_at,
                    updated_at=workstream.updated_at,
                )
                for workstream in workstreams
            ]
        )

    async def list_module_workstreams(self, project_id: UUID) -> Sequence[ModuleWorkstream]:
        rows = (
            await self._session.scalars(
                select(ModuleWorkstreamRow)
                .where(ModuleWorkstreamRow.project_id == project_id)
                .order_by(ModuleWorkstreamRow.created_at, ModuleWorkstreamRow.id)
            )
        ).all()
        return tuple(
            ModuleWorkstream(
                id=row.id,
                project_id=row.project_id,
                module_lineage_id=row.module_lineage_id,
                status=ModuleWorkstreamStatus(row.status),
                memory_manifest_ref=row.memory_manifest_ref,
                last_project_revision=row.last_project_revision,
                last_basis_hash=row.last_basis_hash,
                optimistic_revision=row.optimistic_revision,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        )

    async def update_module_workstream(self, workstream: ModuleWorkstream) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ModuleWorkstreamRow)
                .where(
                    ModuleWorkstreamRow.id == workstream.id,
                    ModuleWorkstreamRow.project_id == workstream.project_id,
                    ModuleWorkstreamRow.optimistic_revision == workstream.optimistic_revision - 1,
                )
                .values(
                    status=workstream.status.value,
                    memory_manifest_ref=workstream.memory_manifest_ref,
                    last_project_revision=workstream.last_project_revision,
                    last_basis_hash=workstream.last_basis_hash,
                    optimistic_revision=workstream.optimistic_revision,
                    updated_at=workstream.updated_at,
                )
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("module workstream is stale or missing")

    async def add_module_memory_items(self, items: Sequence[ModuleMemoryItem]) -> None:
        if not items:
            return
        await self._session.execute(
            postgresql_insert(ModuleMemoryItemRow)
            .values(
                [
                    {
                        "id": item.id,
                        "workstream_id": item.workstream_id,
                        "kind": item.kind.value,
                        "stable_key": item.stable_key,
                        "basis_hash": item.basis_hash,
                        "content_hash": item.content_hash,
                        "status": item.status.value,
                        "artifact_ref": item.artifact_ref,
                        "evidence_refs": list(item.evidence_refs),
                        "source_result_id": item.source_result_id,
                        "applicability": item.applicability,
                        "freshness_deadline": item.freshness_deadline,
                        "supersedes_id": item.supersedes_id,
                        "created_at": item.created_at,
                    }
                    for item in items
                ]
            )
            .on_conflict_do_nothing(constraint="uq_module_memory_dedup")
        )

    async def list_module_memory_items(
        self,
        workstream_id: UUID,
        *,
        stable_key: str | None = None,
    ) -> Sequence[ModuleMemoryItem]:
        statement = select(ModuleMemoryItemRow).where(
            ModuleMemoryItemRow.workstream_id == workstream_id
        )
        if stable_key is not None:
            statement = statement.where(ModuleMemoryItemRow.stable_key == stable_key)
        rows = (
            await self._session.scalars(
                statement.order_by(ModuleMemoryItemRow.created_at, ModuleMemoryItemRow.id)
            )
        ).all()
        return tuple(
            ModuleMemoryItem(
                id=row.id,
                workstream_id=row.workstream_id,
                kind=ModuleMemoryItemKind(row.kind),
                stable_key=row.stable_key,
                basis_hash=row.basis_hash,
                content_hash=row.content_hash,
                status=ModuleMemoryItemStatus(row.status),
                artifact_ref=row.artifact_ref,
                evidence_refs=tuple(row.evidence_refs),
                source_result_id=row.source_result_id,
                applicability=dict(row.applicability),
                freshness_deadline=row.freshness_deadline,
                supersedes_id=row.supersedes_id,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def assign_module_lineage(self, *, module_id: UUID, lineage_id: UUID) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ModuleRow)
                .where(ModuleRow.id == module_id, ModuleRow.lineage_id.is_(None))
                .values(lineage_id=lineage_id)
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("module lineage is already assigned or missing")

    async def list_modules(
        self,
        project_id: UUID,
        requirement_revision_id: UUID | None = None,
    ) -> Sequence[Module]:
        statement: Select[tuple[ModuleRow]] = select(ModuleRow).where(
            ModuleRow.project_id == project_id
        )
        if requirement_revision_id is not None:
            statement = statement.where(
                ModuleRow.requirement_revision_id == requirement_revision_id
            )
        rows = (await self._session.scalars(statement.order_by(ModuleRow.key))).all()
        return [
            _decode(Module, row.payload).model_copy(update={"lineage_id": row.lineage_id})
            for row in rows
        ]

    async def add_project_blueprint(self, blueprint: ProjectBlueprint) -> None:
        self._session.add(
            ProjectBlueprintRow(
                id=blueprint.id,
                project_id=blueprint.project_id,
                requirement_revision_id=blueprint.requirement_revision_id,
                version=blueprint.version,
                status=blueprint.status.value,
                payload=_payload(blueprint),
            )
        )

    async def get_project_blueprint(self, blueprint_id: UUID) -> ProjectBlueprint | None:
        row = await self._session.get(ProjectBlueprintRow, blueprint_id)
        if row is None:
            return None
        blueprint = _decode(ProjectBlueprint, row.payload)
        _assert_identity(
            entity_name="ProjectBlueprint",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=blueprint.id,
            payload_project_id=blueprint.project_id,
        )
        if (
            row.requirement_revision_id != blueprint.requirement_revision_id
            or row.version != blueprint.version
            or row.status != blueprint.status.value
        ):
            raise PersistenceCorruptionError("ProjectBlueprint typed state differs from payload")
        return blueprint

    async def list_project_blueprints(self, project_id: UUID) -> Sequence[ProjectBlueprint]:
        rows = (
            await self._session.scalars(
                select(ProjectBlueprintRow)
                .where(ProjectBlueprintRow.project_id == project_id)
                .order_by(ProjectBlueprintRow.version)
            )
        ).all()
        return [_decode(ProjectBlueprint, row.payload) for row in rows]

    async def update_project_blueprint(
        self, blueprint: ProjectBlueprint, *, expected_status: BlueprintStatus
    ) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ProjectBlueprintRow)
                .where(
                    ProjectBlueprintRow.id == blueprint.id,
                    ProjectBlueprintRow.status == expected_status.value,
                )
                .values(status=blueprint.status.value, payload=_payload(blueprint))
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("project blueprint status is stale")

    async def add_module_configuration(self, configuration: ModuleConfiguration) -> None:
        self._session.add(
            ModuleConfigurationRow(
                id=configuration.id,
                project_id=configuration.project_id,
                module_id=configuration.module_id,
                revision=configuration.revision,
                status=configuration.status.value,
                payload=_payload(configuration),
            )
        )

    async def list_module_configurations(
        self, project_id: UUID, module_id: UUID
    ) -> Sequence[ModuleConfiguration]:
        rows = (
            await self._session.scalars(
                select(ModuleConfigurationRow)
                .where(
                    ModuleConfigurationRow.project_id == project_id,
                    ModuleConfigurationRow.module_id == module_id,
                )
                .order_by(ModuleConfigurationRow.revision)
            )
        ).all()
        return [_decode(ModuleConfiguration, row.payload) for row in rows]

    async def update_module_configuration(
        self,
        configuration: ModuleConfiguration,
        *,
        expected_status: ModuleConfigurationStatus,
    ) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ModuleConfigurationRow)
                .where(
                    ModuleConfigurationRow.id == configuration.id,
                    ModuleConfigurationRow.status == expected_status.value,
                )
                .values(
                    status=configuration.status.value,
                    payload=_payload(configuration),
                )
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("module configuration status is stale")

    async def add_selection_lock(self, lock: SelectionLock) -> None:
        self._session.add(
            SelectionLockRow(
                id=lock.id,
                project_id=lock.project_id,
                module_id=lock.module_id,
                active=lock.active,
                payload=_payload(lock),
            )
        )

    async def get_selection_lock(self, lock_id: UUID) -> SelectionLock | None:
        row = await self._session.get(SelectionLockRow, lock_id)
        if row is None:
            return None
        lock = _decode(SelectionLock, row.payload)
        _assert_identity(
            entity_name="SelectionLock",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=lock.id,
            payload_project_id=lock.project_id,
        )
        if row.active != lock.active:
            raise PersistenceCorruptionError("SelectionLock typed state differs from payload")
        return lock

    async def list_selection_locks(
        self, project_id: UUID, module_id: UUID | None = None
    ) -> Sequence[SelectionLock]:
        statement = select(SelectionLockRow).where(SelectionLockRow.project_id == project_id)
        if module_id is not None:
            statement = statement.where(SelectionLockRow.module_id == module_id)
        rows = (await self._session.scalars(statement.order_by(SelectionLockRow.id))).all()
        return [_decode(SelectionLock, row.payload) for row in rows]

    async def update_selection_lock(self, lock: SelectionLock, *, expected_active: bool) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(SelectionLockRow)
                .where(
                    SelectionLockRow.id == lock.id,
                    SelectionLockRow.active == expected_active,
                )
                .values(active=lock.active, payload=_payload(lock))
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("selection lock state is stale")

    async def add_user_adjustment(self, adjustment: UserAdjustment) -> None:
        self._session.add(
            UserAdjustmentRow(
                id=adjustment.id,
                project_id=adjustment.project_id,
                module_id=adjustment.module_id,
                batch_id=adjustment.batch_id,
                payload=_payload(adjustment),
            )
        )

    async def list_user_adjustments(
        self, project_id: UUID, batch_id: UUID | None = None
    ) -> Sequence[UserAdjustment]:
        statement = select(UserAdjustmentRow).where(UserAdjustmentRow.project_id == project_id)
        if batch_id is not None:
            statement = statement.where(UserAdjustmentRow.batch_id == batch_id)
        rows = (await self._session.scalars(statement.order_by(UserAdjustmentRow.id))).all()
        return [_decode(UserAdjustment, row.payload) for row in rows]

    async def add_adjustment_batch(self, batch: AdjustmentBatch) -> None:
        self._session.add(
            AdjustmentBatchRow(
                id=batch.id,
                project_id=batch.project_id,
                status=batch.status.value,
                payload=_payload(batch),
            )
        )

    async def get_adjustment_batch(self, batch_id: UUID) -> AdjustmentBatch | None:
        row = await self._session.get(AdjustmentBatchRow, batch_id)
        if row is None:
            return None
        batch = _decode(AdjustmentBatch, row.payload)
        _assert_identity(
            entity_name="AdjustmentBatch",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=batch.id,
            payload_project_id=batch.project_id,
        )
        if row.status != batch.status.value:
            raise PersistenceCorruptionError("AdjustmentBatch typed state differs from payload")
        return batch

    async def list_adjustment_batches(self, project_id: UUID) -> Sequence[AdjustmentBatch]:
        rows = (
            await self._session.scalars(
                select(AdjustmentBatchRow)
                .where(AdjustmentBatchRow.project_id == project_id)
                .order_by(AdjustmentBatchRow.id)
            )
        ).all()
        return [_decode(AdjustmentBatch, row.payload) for row in rows]

    async def update_adjustment_batch(
        self,
        batch: AdjustmentBatch,
        *,
        expected_status: AdjustmentBatchStatus,
    ) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AdjustmentBatchRow)
                .where(
                    AdjustmentBatchRow.id == batch.id,
                    AdjustmentBatchRow.status == expected_status.value,
                )
                .values(status=batch.status.value, payload=_payload(batch))
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("adjustment batch status is stale")

    async def add_draft_history_entry(self, entry: DraftHistoryEntry) -> None:
        self._session.add(
            DraftHistoryEntryRow(
                id=entry.id,
                project_id=entry.project_id,
                sequence=entry.sequence,
                payload=_payload(entry),
            )
        )

    async def list_draft_history_entries(self, project_id: UUID) -> Sequence[DraftHistoryEntry]:
        rows = (
            await self._session.scalars(
                select(DraftHistoryEntryRow)
                .where(DraftHistoryEntryRow.project_id == project_id)
                .order_by(DraftHistoryEntryRow.sequence)
            )
        ).all()
        return [_decode(DraftHistoryEntry, row.payload) for row in rows]

    async def add_solution_snapshot(self, snapshot: SolutionSnapshot) -> None:
        self._session.add(
            SolutionSnapshotRow(
                id=snapshot.id,
                project_id=snapshot.project_id,
                payload=_payload(snapshot),
            )
        )

    async def get_solution_snapshot(self, snapshot_id: UUID) -> SolutionSnapshot | None:
        row = await self._session.get(SolutionSnapshotRow, snapshot_id)
        if row is None:
            return None
        snapshot = _decode(SolutionSnapshot, row.payload)
        _assert_identity(
            entity_name="SolutionSnapshot",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=snapshot.id,
            payload_project_id=snapshot.project_id,
        )
        return snapshot

    async def list_solution_snapshots(self, project_id: UUID) -> Sequence[SolutionSnapshot]:
        rows = (
            await self._session.scalars(
                select(SolutionSnapshotRow)
                .where(SolutionSnapshotRow.project_id == project_id)
                .order_by(SolutionSnapshotRow.id)
            )
        ).all()
        return [_decode(SolutionSnapshot, row.payload) for row in rows]

    async def add_project_reshape_proposal(self, proposal: ProjectReshapeProposal) -> None:
        self._session.add(
            ProjectReshapeProposalRow(
                id=proposal.id,
                project_id=proposal.project_id,
                status=proposal.status.value,
                payload=_payload(proposal),
            )
        )

    async def get_project_reshape_proposal(
        self, proposal_id: UUID
    ) -> ProjectReshapeProposal | None:
        row = await self._session.get(ProjectReshapeProposalRow, proposal_id)
        if row is None:
            return None
        proposal = _decode(ProjectReshapeProposal, row.payload)
        _assert_identity(
            entity_name="ProjectReshapeProposal",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=proposal.id,
            payload_project_id=proposal.project_id,
        )
        if row.status != proposal.status.value:
            raise PersistenceCorruptionError(
                "ProjectReshapeProposal typed state differs from payload"
            )
        return proposal

    async def list_project_reshape_proposals(
        self, project_id: UUID
    ) -> Sequence[ProjectReshapeProposal]:
        rows = (
            await self._session.scalars(
                select(ProjectReshapeProposalRow)
                .where(ProjectReshapeProposalRow.project_id == project_id)
                .order_by(ProjectReshapeProposalRow.id)
            )
        ).all()
        return [_decode(ProjectReshapeProposal, row.payload) for row in rows]

    async def update_project_reshape_proposal(
        self,
        proposal: ProjectReshapeProposal,
        *,
        expected_status: ProjectReshapeStatus,
    ) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ProjectReshapeProposalRow)
                .where(
                    ProjectReshapeProposalRow.id == proposal.id,
                    ProjectReshapeProposalRow.status == expected_status.value,
                )
                .values(status=proposal.status.value, payload=_payload(proposal))
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("project reshape proposal status is stale")

    async def add_requirements_change_proposal(self, proposal: RequirementsChangeProposal) -> None:
        self._session.add(
            RequirementsChangeProposalRow(
                id=proposal.id,
                project_id=proposal.project_id,
                status=proposal.status.value,
                payload=_payload(proposal),
            )
        )

    async def get_requirements_change_proposal(
        self, proposal_id: UUID
    ) -> RequirementsChangeProposal | None:
        row = await self._session.get(RequirementsChangeProposalRow, proposal_id)
        if row is None:
            return None
        proposal = _decode(RequirementsChangeProposal, row.payload)
        _assert_identity(
            entity_name="RequirementsChangeProposal",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=proposal.id,
            payload_project_id=proposal.project_id,
        )
        if row.status != proposal.status.value:
            raise PersistenceCorruptionError(
                "RequirementsChangeProposal typed state differs from payload"
            )
        return proposal

    async def list_requirements_change_proposals(
        self, project_id: UUID
    ) -> Sequence[RequirementsChangeProposal]:
        rows = (
            await self._session.scalars(
                select(RequirementsChangeProposalRow)
                .where(RequirementsChangeProposalRow.project_id == project_id)
                .order_by(RequirementsChangeProposalRow.id)
            )
        ).all()
        return [_decode(RequirementsChangeProposal, row.payload) for row in rows]

    async def update_requirements_change_proposal(
        self,
        proposal: RequirementsChangeProposal,
        *,
        expected_status: RequirementsChangeProposalStatus,
    ) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RequirementsChangeProposalRow)
                .where(
                    RequirementsChangeProposalRow.id == proposal.id,
                    RequirementsChangeProposalRow.status == expected_status.value,
                )
                .values(status=proposal.status.value, payload=_payload(proposal))
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("requirements change proposal status is stale")

    async def add_change_impact_preview(self, preview: ChangeImpactPreview) -> None:
        self._session.add(
            ChangeImpactPreviewRow(
                id=preview.id,
                project_id=preview.project_id,
                batch_id=preview.batch_id,
                basis_hash=preview.basis_hash,
                payload=_payload(preview),
            )
        )

    async def get_change_impact_preview(self, preview_id: UUID) -> ChangeImpactPreview | None:
        row = await self._session.get(ChangeImpactPreviewRow, preview_id)
        if row is None:
            return None
        preview = _decode(ChangeImpactPreview, row.payload)
        _assert_identity(
            entity_name="ChangeImpactPreview",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=preview.id,
            payload_project_id=preview.project_id,
        )
        if row.batch_id != preview.batch_id or row.basis_hash != preview.basis_hash:
            raise PersistenceCorruptionError("ChangeImpactPreview typed state differs from payload")
        return preview

    async def list_change_impact_previews(self, project_id: UUID) -> Sequence[ChangeImpactPreview]:
        rows = (
            await self._session.scalars(
                select(ChangeImpactPreviewRow)
                .where(ChangeImpactPreviewRow.project_id == project_id)
                .order_by(ChangeImpactPreviewRow.id)
            )
        ).all()
        return [_decode(ChangeImpactPreview, row.payload) for row in rows]

    async def add_evidence_bindings(self, bindings: Sequence[EvidenceBinding]) -> None:
        self._session.add_all(
            [
                EvidenceBindingRow(
                    id=binding.id,
                    project_id=binding.project_id,
                    module_id=binding.module_id,
                    payload=_payload(binding),
                )
                for binding in bindings
            ]
        )

    async def list_evidence_bindings(self, project_id: UUID) -> Sequence[EvidenceBinding]:
        statement = (
            select(EvidenceBindingRow)
            .where(EvidenceBindingRow.project_id == project_id)
            .order_by(EvidenceBindingRow.id)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(EvidenceBinding, row.payload) for row in rows]

    async def add_candidates(self, candidates: Sequence[Candidate]) -> None:
        self._session.add_all(
            [
                CandidateRow(
                    id=candidate.id,
                    project_id=candidate.project_id,
                    module_id=candidate.module_id,
                    payload=_payload(candidate),
                )
                for candidate in candidates
            ]
        )

    async def list_candidates(self, project_id: UUID) -> Sequence[Candidate]:
        statement = (
            select(CandidateRow)
            .where(CandidateRow.project_id == project_id)
            .order_by(CandidateRow.id)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(Candidate, row.payload) for row in rows]

    async def add_compatibility_findings(
        self,
        findings: Sequence[CompatibilityFinding],
    ) -> None:
        self._session.add_all(
            [
                CompatibilityFindingRow(
                    id=finding.id,
                    project_id=finding.project_id,
                    payload=_payload(finding),
                )
                for finding in findings
            ]
        )

    async def list_compatibility_findings(
        self,
        project_id: UUID,
    ) -> Sequence[CompatibilityFinding]:
        statement = (
            select(CompatibilityFindingRow)
            .where(CompatibilityFindingRow.project_id == project_id)
            .order_by(CompatibilityFindingRow.id)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(CompatibilityFinding, row.payload) for row in rows]

    async def add_decision_request(self, decision: DecisionRequest) -> None:
        self._session.add(
            DecisionRequestRow(
                id=decision.id,
                project_id=decision.project_id,
                status=decision.status.value,
                basis_hash=decision.basis_hash,
                payload=_payload(decision),
            )
        )

    async def get_decision_request(self, decision_id: UUID) -> DecisionRequest | None:
        row = await self._session.get(DecisionRequestRow, decision_id)
        if row is None:
            return None
        decision = _decode(DecisionRequest, row.payload)
        _assert_identity(
            entity_name="DecisionRequest",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=decision.id,
            payload_project_id=decision.project_id,
        )
        if row.status != decision.status.value or row.basis_hash != decision.basis_hash:
            raise PersistenceCorruptionError("DecisionRequest typed state differs from payload")
        return decision

    async def list_decision_requests(self, project_id: UUID) -> Sequence[DecisionRequest]:
        rows = (
            await self._session.scalars(
                select(DecisionRequestRow)
                .where(DecisionRequestRow.project_id == project_id)
                .order_by(DecisionRequestRow.id)
            )
        ).all()
        return [_decode(DecisionRequest, row.payload) for row in rows]

    async def update_decision_request(self, decision: DecisionRequest) -> None:
        statement = (
            update(DecisionRequestRow)
            .where(
                DecisionRequestRow.id == decision.id,
                DecisionRequestRow.status == DecisionStatus.PENDING.value,
                DecisionRequestRow.basis_hash == decision.basis_hash,
            )
            .values(status=decision.status.value, payload=_payload(decision))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("decision is stale or already resolved")

    async def add_solution_proposal(self, proposal: SolutionProposal) -> None:
        self._session.add(
            SolutionProposalRow(
                id=proposal.id,
                project_id=proposal.project_id,
                decision_id=proposal.decision_id,
                requirement_revision_id=proposal.requirement_revision_id,
                status=proposal.status.value,
                basis_hash=proposal.basis_hash,
                payload=_payload(proposal),
            )
        )

    async def get_solution_proposal(self, proposal_id: UUID) -> SolutionProposal | None:
        row = await self._session.get(SolutionProposalRow, proposal_id)
        if row is None:
            return None
        proposal = _decode(SolutionProposal, row.payload)
        _assert_identity(
            entity_name="SolutionProposal",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=proposal.id,
            payload_project_id=proposal.project_id,
        )
        if row.status != proposal.status.value or row.basis_hash != proposal.basis_hash:
            raise PersistenceCorruptionError("SolutionProposal typed state differs from payload")
        return proposal

    async def list_solution_proposals(self, project_id: UUID) -> Sequence[SolutionProposal]:
        rows = (
            await self._session.scalars(
                select(SolutionProposalRow)
                .where(SolutionProposalRow.project_id == project_id)
                .order_by(SolutionProposalRow.id)
            )
        ).all()
        return [_decode(SolutionProposal, row.payload) for row in rows]

    async def update_solution_proposal(self, proposal: SolutionProposal) -> None:
        statement = (
            update(SolutionProposalRow)
            .where(
                SolutionProposalRow.id == proposal.id,
                SolutionProposalRow.status == SolutionProposalStatus.PROPOSED.value,
                SolutionProposalRow.basis_hash == proposal.basis_hash,
            )
            .values(status=proposal.status.value, payload=_payload(proposal))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("solution proposal is stale or already resolved")

    async def add_execution_plan_proposal(self, proposal: ExecutionPlanProposal) -> None:
        self._session.add(
            ExecutionPlanProposalRow(
                id=proposal.id,
                project_id=proposal.project_id,
                status=proposal.status.value,
                basis_hash=proposal.basis_hash,
                scope_hash=proposal.scope_hash,
                payload=_payload(proposal),
            )
        )

    async def get_execution_plan_proposal(self, proposal_id: UUID) -> ExecutionPlanProposal | None:
        row = await self._session.get(ExecutionPlanProposalRow, proposal_id)
        if row is None:
            return None
        proposal = _decode(ExecutionPlanProposal, row.payload)
        _assert_identity(
            entity_name="ExecutionPlanProposal",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=proposal.id,
            payload_project_id=proposal.project_id,
        )
        if (
            row.status != proposal.status.value
            or row.basis_hash != proposal.basis_hash
            or row.scope_hash != proposal.scope_hash
        ):
            raise PersistenceCorruptionError(
                "ExecutionPlanProposal typed state differs from payload"
            )
        return proposal

    async def list_execution_plan_proposals(
        self, project_id: UUID
    ) -> Sequence[ExecutionPlanProposal]:
        rows = (
            await self._session.scalars(
                select(ExecutionPlanProposalRow)
                .where(ExecutionPlanProposalRow.project_id == project_id)
                # ``created_at`` is immutable typed proposal data.  The
                # workspace treats the newest proposal as the current review
                # candidate, so UUID order would make a newly requested
                # strategy intermittently lose to an older one.
                .order_by(
                    ExecutionPlanProposalRow.payload["created_at"].astext,
                    ExecutionPlanProposalRow.id,
                )
            )
        ).all()
        return [_decode(ExecutionPlanProposal, row.payload) for row in rows]

    async def update_execution_plan_proposal(
        self,
        proposal: ExecutionPlanProposal,
        *,
        expected_status: ExecutionPlanStatus,
    ) -> None:
        statement = (
            update(ExecutionPlanProposalRow)
            .where(
                ExecutionPlanProposalRow.id == proposal.id,
                ExecutionPlanProposalRow.status == expected_status.value,
                ExecutionPlanProposalRow.scope_hash == proposal.scope_hash,
            )
            .values(status=proposal.status.value, payload=_payload(proposal))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("execution plan proposal is stale or already resolved")

    async def add_solution_version(self, solution: SolutionVersion) -> None:
        self._session.add(
            SolutionVersionRow(
                id=solution.id,
                project_id=solution.project_id,
                version=solution.version,
                requirement_revision_id=solution.requirement_revision_id,
                approved_decision_id=solution.approved_decision_id,
                solution_proposal_id=solution.solution_proposal_id,
                previous_version_id=solution.previous_version_id,
                basis_hash=solution.basis_hash,
                payload=_payload(solution),
            )
        )

    async def get_solution_version(self, solution_id: UUID) -> SolutionVersion | None:
        row = await self._session.get(SolutionVersionRow, solution_id)
        if row is None:
            return None
        solution = _decode(SolutionVersion, row.payload)
        _assert_identity(
            entity_name="SolutionVersion",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=solution.id,
            payload_project_id=solution.project_id,
        )
        if row.version != solution.version or row.basis_hash != solution.basis_hash:
            raise PersistenceCorruptionError("SolutionVersion typed state differs from payload")
        return solution

    async def list_solution_versions(self, project_id: UUID) -> Sequence[SolutionVersion]:
        statement = (
            select(SolutionVersionRow)
            .where(SolutionVersionRow.project_id == project_id)
            .order_by(SolutionVersionRow.version)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(SolutionVersion, row.payload) for row in rows]

    async def add_observation(self, observation: Observation) -> None:
        self._session.add(
            ObservationRow(
                id=observation.id,
                project_id=observation.project_id,
                solution_version_id=observation.solution_version_id,
                payload=_payload(observation),
            )
        )
        await self._session.flush()

    async def get_observation(self, observation_id: UUID) -> Observation | None:
        row = await self._session.get(ObservationRow, observation_id)
        if row is None:
            return None
        observation = _decode(Observation, row.payload)
        _assert_identity(
            entity_name="Observation",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=observation.id,
            payload_project_id=observation.project_id,
        )
        return observation

    async def list_observations(self, project_id: UUID) -> Sequence[Observation]:
        statement = (
            select(ObservationRow)
            .where(ObservationRow.project_id == project_id)
            .order_by(ObservationRow.id)
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(Observation, row.payload) for row in rows]

    async def add_impact_analysis(self, impact: ImpactAnalysis) -> None:
        self._session.add(
            ImpactAnalysisRow(
                id=impact.id,
                project_id=impact.project_id,
                status=impact.status.value,
                observation_id=impact.observation_id,
                base_solution_version_id=impact.base_solution_version_id,
                basis_hash=impact.basis_hash,
                payload=_payload(impact),
            )
        )

    async def get_impact_analysis(self, impact_id: UUID) -> ImpactAnalysis | None:
        row = await self._session.get(ImpactAnalysisRow, impact_id)
        if row is None:
            return None
        impact = _decode(ImpactAnalysis, row.payload)
        _assert_identity(
            entity_name="ImpactAnalysis",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=impact.id,
            payload_project_id=impact.project_id,
        )
        if row.status != impact.status.value or row.basis_hash != impact.basis_hash:
            raise PersistenceCorruptionError("ImpactAnalysis typed state differs from payload")
        return impact

    async def list_impact_analyses(self, project_id: UUID) -> Sequence[ImpactAnalysis]:
        rows = (
            await self._session.scalars(
                select(ImpactAnalysisRow)
                .where(ImpactAnalysisRow.project_id == project_id)
                .order_by(ImpactAnalysisRow.id)
            )
        ).all()
        return [_decode(ImpactAnalysis, row.payload) for row in rows]

    async def update_impact_analysis(self, impact: ImpactAnalysis) -> None:
        statement = (
            update(ImpactAnalysisRow)
            .where(
                ImpactAnalysisRow.id == impact.id,
                ImpactAnalysisRow.status == ImpactStatus.PROPOSED.value,
                ImpactAnalysisRow.basis_hash == impact.basis_hash,
            )
            .values(status=impact.status.value, payload=_payload(impact))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("impact is stale or already resolved")

    async def add_patch_set(self, patch_set: PatchSet) -> None:
        self._session.add(
            PatchSetRow(
                id=patch_set.id,
                project_id=patch_set.project_id,
                impact_analysis_id=patch_set.impact_analysis_id,
                base_solution_version_id=patch_set.base_solution_version_id,
                payload=_payload(patch_set),
            )
        )

    async def get_patch_set(self, patch_set_id: UUID) -> PatchSet | None:
        row = await self._session.get(PatchSetRow, patch_set_id)
        if row is None:
            return None
        patch_set = _decode(PatchSet, row.payload)
        _assert_identity(
            entity_name="PatchSet",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=patch_set.id,
            payload_project_id=patch_set.project_id,
        )
        return patch_set

    async def list_patch_sets(self, project_id: UUID) -> Sequence[PatchSet]:
        rows = (
            await self._session.scalars(
                select(PatchSetRow)
                .where(PatchSetRow.project_id == project_id)
                .order_by(PatchSetRow.id)
            )
        ).all()
        return [_decode(PatchSet, row.payload) for row in rows]

    # ── V1 Shopping ────────────────────────────────────────────────────

    async def add_offer_snapshot(self, snapshot: OfferSnapshot) -> None:
        self._session.add(
            OfferSnapshotRow(
                id=snapshot.id,
                project_id=snapshot.project_id,
                solution_version_id=snapshot.solution_version_id,
                bom_line_id=snapshot.bom_line_id,
                provider=snapshot.provider,
                provider_offer_id=snapshot.provider_offer_id,
                snapshot_hash=snapshot.snapshot_hash,
                payload=_payload(snapshot),
                observed_at=snapshot.observed_at,
            )
        )

    async def get_offer_snapshot(self, snapshot_id: UUID) -> OfferSnapshot | None:
        row = await self._session.get(OfferSnapshotRow, snapshot_id)
        if row is None:
            return None
        snapshot = _decode(OfferSnapshot, row.payload)
        _assert_identity(
            entity_name="OfferSnapshot",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=snapshot.id,
            payload_project_id=snapshot.project_id,
        )
        return snapshot

    async def list_offer_snapshots(self, project_id: UUID) -> Sequence[OfferSnapshot]:
        statement = (
            select(OfferSnapshotRow)
            .where(OfferSnapshotRow.project_id == project_id)
            .order_by(OfferSnapshotRow.observed_at.desc())
        )
        rows = (await self._session.scalars(statement)).all()
        return [_decode(OfferSnapshot, row.payload) for row in rows]

    async def add_purchase_proposal(self, proposal: PurchaseProposal) -> None:
        self._session.add(
            PurchaseProposalRow(
                id=proposal.id,
                project_id=proposal.project_id,
                solution_version_id=proposal.solution_version_id,
                offer_snapshot_id=proposal.offer_snapshot_id,
                status=proposal.status.value,
                basis_hash=proposal.basis_hash,
                payload=_payload(proposal),
            )
        )

    async def get_purchase_proposal(self, proposal_id: UUID) -> PurchaseProposal | None:
        row = await self._session.get(PurchaseProposalRow, proposal_id)
        if row is None:
            return None
        proposal = _decode(PurchaseProposal, row.payload)
        _assert_identity(
            entity_name="PurchaseProposal",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=proposal.id,
            payload_project_id=proposal.project_id,
        )
        if row.status != proposal.status.value or row.basis_hash != proposal.basis_hash:
            raise PersistenceCorruptionError("PurchaseProposal typed state differs from payload")
        return proposal

    async def list_purchase_proposals(self, project_id: UUID) -> Sequence[PurchaseProposal]:
        rows = (
            await self._session.scalars(
                select(PurchaseProposalRow)
                .where(PurchaseProposalRow.project_id == project_id)
                .order_by(PurchaseProposalRow.created_at.desc())
            )
        ).all()
        return [_decode(PurchaseProposal, row.payload) for row in rows]

    async def update_purchase_proposal(self, proposal: PurchaseProposal) -> None:
        statement = (
            sqlalchemy.update(PurchaseProposalRow)
            .where(
                PurchaseProposalRow.id == proposal.id,
                PurchaseProposalRow.basis_hash == proposal.basis_hash,
            )
            .values(status=proposal.status.value, payload=_payload(proposal))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("purchase proposal is stale or already updated")

    async def add_effect_approval(self, approval: EffectApproval) -> None:
        self._session.add(
            EffectApprovalRow(
                id=approval.id,
                project_id=approval.project_id,
                effect_kind=approval.effect_kind,
                target_ref=approval.target_ref,
                basis_hash=approval.basis_hash,
                scope_hash=approval.scope_hash,
                constraints=approval.constraints,
                status=approval.status.value,
                payload=_payload(approval),
                requested_at=approval.requested_at,
                expires_at=approval.expires_at,
                resolved_at=approval.resolved_at,
                consumed_at=approval.consumed_at,
            )
        )

    async def get_effect_approval(self, approval_id: UUID) -> EffectApproval | None:
        row = await self._session.get(EffectApprovalRow, approval_id)
        if row is None:
            return None
        approval = _decode(EffectApproval, row.payload)
        _assert_identity(
            entity_name="EffectApproval",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=approval.id,
            payload_project_id=approval.project_id,
        )
        if (
            row.effect_kind != approval.effect_kind
            or row.target_ref != approval.target_ref
            or row.basis_hash != approval.basis_hash
            or row.scope_hash != approval.scope_hash
            or row.constraints != approval.constraints
            or row.status != approval.status.value
            or row.requested_at != approval.requested_at
            or row.expires_at != approval.expires_at
            or row.resolved_at != approval.resolved_at
            or row.consumed_at != approval.consumed_at
        ):
            raise PersistenceCorruptionError("EffectApproval typed state differs from payload")
        return approval

    async def list_effect_approvals(self, project_id: UUID) -> Sequence[EffectApproval]:
        rows = (
            await self._session.scalars(
                select(EffectApprovalRow)
                .where(EffectApprovalRow.project_id == project_id)
                .order_by(EffectApprovalRow.requested_at.desc(), EffectApprovalRow.id)
            )
        ).all()
        return [_decode(EffectApproval, row.payload) for row in rows]

    async def find_live_effect_approval(
        self,
        project_id: UUID,
        scope_hash: str,
    ) -> EffectApproval | None:
        row = await self._session.scalar(
            select(EffectApprovalRow)
            .where(
                EffectApprovalRow.project_id == project_id,
                EffectApprovalRow.scope_hash == scope_hash,
                EffectApprovalRow.status.in_(
                    (
                        EffectApprovalStatus.REQUESTED.value,
                        EffectApprovalStatus.APPROVED.value,
                    )
                ),
            )
            .with_for_update()
        )
        return None if row is None else _decode(EffectApproval, row.payload)

    async def update_effect_approval(
        self,
        approval: EffectApproval,
        *,
        expected_status: EffectApprovalStatus,
    ) -> None:
        statement = (
            sqlalchemy.update(EffectApprovalRow)
            .where(
                EffectApprovalRow.id == approval.id,
                EffectApprovalRow.project_id == approval.project_id,
                EffectApprovalRow.effect_kind == approval.effect_kind,
                EffectApprovalRow.target_ref == approval.target_ref,
                EffectApprovalRow.basis_hash == approval.basis_hash,
                EffectApprovalRow.scope_hash == approval.scope_hash,
                EffectApprovalRow.status == expected_status.value,
            )
            .values(
                status=approval.status.value,
                payload=_payload(approval),
                resolved_at=approval.resolved_at,
                consumed_at=approval.consumed_at,
            )
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("effect approval is stale or terminal")

    async def add_checkout_handoff(self, handoff: CheckoutHandoff) -> None:
        self._session.add(
            CheckoutHandoffRow(
                id=handoff.id,
                project_id=handoff.project_id,
                proposal_id=handoff.proposal_id,
                provider=handoff.provider,
                status=handoff.status.value,
                basis_hash=handoff.basis_hash,
                payload=_payload(handoff),
            )
        )

    async def get_checkout_handoff(self, handoff_id: UUID) -> CheckoutHandoff | None:
        row = await self._session.get(CheckoutHandoffRow, handoff_id)
        if row is None:
            return None
        handoff = _decode(CheckoutHandoff, row.payload)
        _assert_identity(
            entity_name="CheckoutHandoff",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=handoff.id,
            payload_project_id=handoff.project_id,
        )
        if row.status != handoff.status.value or row.basis_hash != handoff.basis_hash:
            raise PersistenceCorruptionError("CheckoutHandoff typed state differs from payload")
        return handoff

    async def list_checkout_handoffs(self, project_id: UUID) -> Sequence[CheckoutHandoff]:
        rows = (
            await self._session.scalars(
                select(CheckoutHandoffRow)
                .where(CheckoutHandoffRow.project_id == project_id)
                .order_by(CheckoutHandoffRow.created_at.desc())
            )
        ).all()
        return [_decode(CheckoutHandoff, row.payload) for row in rows]

    async def update_checkout_handoff(self, handoff: CheckoutHandoff) -> None:
        statement = (
            sqlalchemy.update(CheckoutHandoffRow)
            .where(CheckoutHandoffRow.id == handoff.id)
            .values(status=handoff.status.value, payload=_payload(handoff))
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("checkout handoff is stale or missing")

    async def append_event(
        self,
        project_id: UUID,
        event_type: str,
        payload: dict[str, object],
        metadata: DomainEventMetadata | None = None,
    ) -> int:
        stored_payload = dict(payload)
        if metadata is not None and metadata.payload_hash != canonical_payload_hash(stored_payload):
            raise PersistenceCorruptionError("domain event payload differs from metadata hash")
        sequence_statement = (
            update(ProjectRow)
            .where(ProjectRow.id == project_id)
            .values(event_sequence=ProjectRow.event_sequence + 1)
            .returning(ProjectRow.event_sequence)
        )
        project_sequence = await self._session.scalar(sequence_statement)
        if project_sequence is None:
            raise OptimisticConcurrencyError("project disappeared while appending event")
        self._session.add(
            DomainEventRow(
                project_id=project_id,
                project_seq=project_sequence,
                event_type=event_type,
                payload=stored_payload,
                event_id=metadata.event_id if metadata is not None else uuid4(),
                schema_version=metadata.schema_version if metadata is not None else 0,
                aggregate_type=metadata.aggregate_type if metadata is not None else None,
                aggregate_id=metadata.aggregate_id if metadata is not None else None,
                aggregate_version=(
                    metadata.aggregate_version if metadata is not None else None
                ),
                occurred_at=metadata.occurred_at if metadata is not None else datetime.now(UTC),
                payload_hash=metadata.payload_hash if metadata is not None else None,
                correlation_id=metadata.correlation_id if metadata is not None else None,
                causation_id=metadata.causation_id if metadata is not None else None,
                actor=metadata.actor if metadata is not None else None,
                source_component=metadata.source_component if metadata is not None else None,
                artifact_refs=list(metadata.artifact_refs) if metadata is not None else None,
            )
        )
        await publish_domain_event_signal(
            self._session,
            project_id=project_id,
            project_sequence=project_sequence,
            event_type=event_type,
        )
        return project_sequence

    async def list_events(
        self,
        project_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> Sequence[DomainEventRow]:
        statement = (
            select(DomainEventRow)
            .where(
                DomainEventRow.project_id == project_id,
                DomainEventRow.project_seq > after_sequence,
            )
            .order_by(DomainEventRow.project_seq)
            .limit(limit)
        )
        return (await self._session.scalars(statement)).all()

    async def list_aggregate_events(
        self,
        project_id: UUID,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
    ) -> Sequence[StoredDomainEvent]:
        statement = (
            select(DomainEventRow)
            .where(
                DomainEventRow.project_id == project_id,
                DomainEventRow.aggregate_type == aggregate_type,
                DomainEventRow.aggregate_id == aggregate_id,
                DomainEventRow.schema_version >= 1,
            )
            .order_by(DomainEventRow.project_seq)
        )
        rows = (await self._session.scalars(statement)).all()
        return [
            StoredDomainEvent(
                event_id=row.event_id,
                schema_version=row.schema_version,
                aggregate_type=cast(str, row.aggregate_type),
                aggregate_id=cast(UUID, row.aggregate_id),
                aggregate_version=cast(int, row.aggregate_version),
                occurred_at=row.occurred_at,
                payload_hash=cast(str, row.payload_hash),
                correlation_id=cast(UUID, row.correlation_id),
                causation_id=row.causation_id,
                actor=cast(str, row.actor),
                source_component=cast(str, row.source_component),
                artifact_refs=tuple(row.artifact_refs or ()),
                project_id=row.project_id,
                project_seq=row.project_seq,
                event_type=row.event_type,
                payload=row.payload,
                recorded_at=row.created_at,
            )
            for row in rows
        ]

    # ── Spend Budget ──────────────────────────────────────────────────────

    async def add_spend_budget_proposal(self, proposal: SpendBudgetProposal) -> None:
        self._session.add(
            SpendBudgetProposalRow(
                id=proposal.id,
                project_id=proposal.project_id,
                status=proposal.status.value,
                payload=_payload(proposal),
            )
        )

    async def get_spend_budget_proposal(self, proposal_id: UUID) -> SpendBudgetProposal | None:
        row = await self._session.get(SpendBudgetProposalRow, proposal_id)
        if row is None:
            return None
        proposal = _decode(SpendBudgetProposal, row.payload)
        _assert_identity(
            entity_name="SpendBudgetProposal",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=proposal.id,
            payload_project_id=proposal.project_id,
        )
        if row.status != proposal.status.value:
            raise PersistenceCorruptionError("SpendBudgetProposal typed state differs from payload")
        return proposal

    async def list_spend_budget_proposals(self, project_id: UUID) -> Sequence[SpendBudgetProposal]:
        rows = (
            await self._session.scalars(
                select(SpendBudgetProposalRow)
                .where(SpendBudgetProposalRow.project_id == project_id)
                .order_by(SpendBudgetProposalRow.id)
            )
        ).all()
        return [_decode(SpendBudgetProposal, row.payload) for row in rows]

    async def update_spend_budget_proposal(
        self,
        proposal: SpendBudgetProposal,
        *,
        expected_status: SpendBudgetProposalStatus,
    ) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(SpendBudgetProposalRow)
                .where(
                    SpendBudgetProposalRow.id == proposal.id,
                    SpendBudgetProposalRow.status == expected_status.value,
                )
                .values(status=proposal.status.value, payload=_payload(proposal))
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("spend budget proposal status is stale")

    async def add_spend_budget_revision(self, revision: SpendBudgetRevision) -> None:
        self._session.add(
            SpendBudgetRevisionRow(
                id=revision.id,
                project_id=revision.project_id,
                proposal_id=revision.proposal_id,
                revision=revision.revision,
                status=revision.status.value,
                payload=_payload(revision),
            )
        )

    async def update_spend_budget_revision(
        self,
        revision: SpendBudgetRevision,
        *,
        expected_status: SpendBudgetProposalStatus,
    ) -> None:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(SpendBudgetRevisionRow)
                .where(
                    SpendBudgetRevisionRow.id == revision.id,
                    SpendBudgetRevisionRow.status == expected_status.value,
                )
                .values(status=revision.status.value, payload=_payload(revision))
            ),
        )
        if result.rowcount != 1:
            raise OptimisticConcurrencyError("spend budget revision status is stale")

    async def get_spend_budget_revision(self, revision_id: UUID) -> SpendBudgetRevision | None:
        row = await self._session.get(SpendBudgetRevisionRow, revision_id)
        if row is None:
            return None
        revision = _decode(SpendBudgetRevision, row.payload)
        _assert_identity(
            entity_name="SpendBudgetRevision",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=revision.id,
            payload_project_id=revision.project_id,
        )
        if row.status != revision.status.value:
            raise PersistenceCorruptionError("SpendBudgetRevision typed state differs from payload")
        return revision

    async def list_spend_budget_revisions(self, project_id: UUID) -> Sequence[SpendBudgetRevision]:
        rows = (
            await self._session.scalars(
                select(SpendBudgetRevisionRow)
                .where(SpendBudgetRevisionRow.project_id == project_id)
                .order_by(SpendBudgetRevisionRow.revision)
            )
        ).all()
        return [_decode(SpendBudgetRevision, row.payload) for row in rows]

    async def get_active_spend_budget(self, project_id: UUID) -> SpendBudgetRevision | None:
        """Return the revision pointed to by Project.active_spend_budget_revision_id.

        This is the single source of truth for the active budget; it does NOT
        query by status, so old revisions can safely be marked superseded
        without affecting the active-budget read path.
        """
        project_row = await self._session.get(ProjectRow, project_id)
        if project_row is None or project_row.active_spend_budget_revision_id is None:
            return None
        row = await self._session.get(
            SpendBudgetRevisionRow, project_row.active_spend_budget_revision_id
        )
        if row is None:
            return None
        return _decode(SpendBudgetRevision, row.payload)

    async def add_spend_budget_impact_preview(self, preview: SpendBudgetImpactPreview) -> None:
        self._session.add(
            SpendBudgetImpactPreviewRow(
                id=preview.id,
                project_id=preview.project_id,
                proposal_id=preview.proposal_id,
                payload=_payload(preview),
            )
        )

    async def get_spend_budget_impact_preview(
        self, preview_id: UUID
    ) -> SpendBudgetImpactPreview | None:
        row = await self._session.get(SpendBudgetImpactPreviewRow, preview_id)
        if row is None:
            return None
        preview = _decode(SpendBudgetImpactPreview, row.payload)
        _assert_identity(
            entity_name="SpendBudgetImpactPreview",
            row_id=row.id,
            row_project_id=row.project_id,
            payload_id=preview.id,
            payload_project_id=preview.project_id,
        )
        return preview

    async def list_spend_budget_impact_previews(
        self, project_id: UUID
    ) -> Sequence[SpendBudgetImpactPreview]:
        rows = (
            await self._session.scalars(
                select(SpendBudgetImpactPreviewRow)
                .where(SpendBudgetImpactPreviewRow.project_id == project_id)
                .order_by(SpendBudgetImpactPreviewRow.id)
            )
        ).all()
        return [_decode(SpendBudgetImpactPreview, row.payload) for row in rows]

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    # ── Conversation ─────────────────────────────────────────────────────

    @staticmethod
    def _turn_from_row(row: ConversationTurnRow) -> ConversationTurn:
        return ConversationTurn(
            id=row.id,
            session_id=row.session_id,
            sequence=row.sequence,
            role=ConversationTurnRole(row.role),
            content=row.content,
            model_profile=row.model_profile,
            turn_type=ConversationTurnType(row.turn_type),
            idempotency_key=row.idempotency_key,
            in_reply_to_turn_id=row.in_reply_to_turn_id,
            is_fallback=row.is_fallback,
            created_at=row.created_at,
        )

    @staticmethod
    def _clarification_from_row(row: ConversationClarificationRow) -> ConversationClarification:
        return ConversationClarification(
            id=row.id,
            turn_id=row.turn_id,
            question=row.question,
            kind=ConversationClarificationKind(row.kind),
            status=ConversationClarificationStatus(row.status),
            resolved_by_turn_id=row.resolved_by_turn_id,
            created_at=row.created_at,
        )

    @staticmethod
    def _action_proposal_from_row(
        row: ConversationActionProposalRow,
    ) -> ConversationActionProposal:
        return ConversationActionProposal(
            id=row.id,
            turn_id=row.turn_id,
            kind=ConversationActionProposalKind(row.kind),
            summary=row.summary,
            proposed_payload=row.proposed_payload,
            status=ConversationActionProposalStatus(row.status),
            resolved_at=row.resolved_at,
            resolution_turn_id=row.resolution_turn_id,
            created_at=row.created_at,
        )

    async def add_conversation_session(self, session: ConversationSession) -> None:
        row = ConversationSessionRow(
            id=session.id,
            project_id=session.project_id,
            status=session.status.value,
            created_at=session.created_at,
            closed_at=session.closed_at,
        )
        self._session.add(row)
        await self._session.flush()

    async def get_conversation_session(self, session_id: UUID) -> ConversationSession | None:
        row = await self._session.get(ConversationSessionRow, session_id)
        if row is None:
            return None
        return ConversationSession(
            id=row.id,
            project_id=row.project_id,
            status=ConversationSessionStatus(row.status),
            created_at=row.created_at,
            closed_at=row.closed_at,
        )

    async def list_conversation_sessions(self, project_id: UUID) -> Sequence[ConversationSession]:
        result = await self._session.execute(
            select(ConversationSessionRow)
            .where(ConversationSessionRow.project_id == project_id)
            .order_by(ConversationSessionRow.created_at.desc())
        )
        rows = result.scalars().all()
        return tuple(
            ConversationSession(
                id=row.id,
                project_id=row.project_id,
                status=ConversationSessionStatus(row.status),
                created_at=row.created_at,
                closed_at=row.closed_at,
            )
            for row in rows
        )

    async def get_active_conversation_session(self, project_id: UUID) -> ConversationSession | None:
        result = await self._session.execute(
            select(ConversationSessionRow).where(
                ConversationSessionRow.project_id == project_id,
                ConversationSessionRow.status == "active",
            )
        )
        row = result.scalars().first()
        if row is None:
            return None
        return ConversationSession(
            id=row.id,
            project_id=row.project_id,
            status=ConversationSessionStatus(row.status),
            created_at=row.created_at,
            closed_at=row.closed_at,
        )

    async def close_conversation_session(self, session_id: UUID) -> None:
        row = await self._session.get(ConversationSessionRow, session_id)
        if row is None:
            raise DomainNotFoundError("conversation session not found")
        if row.status != "active":
            raise DomainConflictError("conversation session is not active")
        row.status = "closed"
        row.closed_at = datetime.now(UTC)
        await self._session.flush()

    async def add_conversation_turn(self, turn: ConversationTurn) -> None:
        row = ConversationTurnRow(
            id=turn.id,
            session_id=turn.session_id,
            sequence=turn.sequence,
            role=turn.role.value,
            content=turn.content,
            model_profile=turn.model_profile,
            turn_type=turn.turn_type.value,
            idempotency_key=turn.idempotency_key,
            in_reply_to_turn_id=turn.in_reply_to_turn_id,
            is_fallback=turn.is_fallback,
            created_at=turn.created_at,
        )
        self._session.add(row)
        await self._session.flush()

    async def lock_conversation_session(self, session_id: UUID) -> None:
        """Serialize short conversation writes for one session transaction."""

        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"conversation-session:{session_id}"},
        )

    async def get_conversation_turn_by_idempotency(
        self, session_id: UUID, idempotency_key: str
    ) -> ConversationTurn | None:
        result = await self._session.execute(
            select(ConversationTurnRow).where(
                ConversationTurnRow.session_id == session_id,
                ConversationTurnRow.idempotency_key == idempotency_key,
                ConversationTurnRow.role == "user",
            )
        )
        row = result.scalars().first()
        if row is None:
            return None
        return self._turn_from_row(row)

    async def get_conversation_reply_to_turn(self, turn_id: UUID) -> ConversationTurn | None:
        result = await self._session.execute(
            select(ConversationTurnRow).where(
                ConversationTurnRow.in_reply_to_turn_id == turn_id
            )
        )
        row = result.scalars().first()
        return self._turn_from_row(row) if row is not None else None

    async def get_conversation_turn(self, turn_id: UUID) -> ConversationTurn | None:
        row = await self._session.get(ConversationTurnRow, turn_id)
        if row is None:
            return None
        return self._turn_from_row(row)

    async def list_conversation_turns(
        self,
        session_id: UUID,
        *,
        after: int = 0,
        limit: int = 50,
    ) -> Sequence[ConversationTurn]:
        result = await self._session.execute(
            select(ConversationTurnRow)
            .where(
                ConversationTurnRow.session_id == session_id,
                ConversationTurnRow.sequence > after,
            )
            .order_by(ConversationTurnRow.sequence)
            .limit(limit + 1)
        )
        rows = result.scalars().all()
        return tuple(self._turn_from_row(row) for row in rows[:limit])

    async def get_latest_turn_sequence(self, session_id: UUID) -> int:
        result = await self._session.execute(
            select(sqlalchemy.func.max(ConversationTurnRow.sequence)).where(
                ConversationTurnRow.session_id == session_id
            )
        )
        value = result.scalar()
        return value or 0

    @staticmethod
    def _context_summary_from_row(row: ContextSummaryRow) -> ContextSummary:
        summary = _decode(ContextSummary, row.payload)
        if (
            summary.id != row.id
            or summary.project_id != row.project_id
            or summary.session_id != row.session_id
            or summary.source_start_sequence != row.source_start_sequence
            or summary.source_end_sequence != row.source_end_sequence
            or summary.source_event_cursor != row.source_event_cursor
            or summary.basis_hash != row.basis_hash
            or summary.status.value != row.status
            or summary.created_at != row.created_at
        ):
            raise PersistenceCorruptionError("ContextSummary typed state differs from payload")
        return summary

    async def add_context_summary(self, summary: ContextSummary) -> None:
        row = ContextSummaryRow(
            id=summary.id,
            project_id=summary.project_id,
            session_id=summary.session_id,
            source_start_sequence=summary.source_start_sequence,
            source_end_sequence=summary.source_end_sequence,
            source_event_cursor=summary.source_event_cursor,
            basis_hash=summary.basis_hash,
            status=summary.status.value,
            payload=_payload(summary),
            created_at=summary.created_at,
        )
        self._session.add(row)
        await self._session.flush()

    async def get_context_summary(self, summary_id: UUID) -> ContextSummary | None:
        row = await self._session.get(ContextSummaryRow, summary_id)
        return None if row is None else self._context_summary_from_row(row)

    async def find_context_summary(
        self,
        *,
        session_id: UUID,
        source_start_sequence: int,
        source_end_sequence: int,
        basis_hash: str,
    ) -> ContextSummary | None:
        row = await self._session.scalar(
            select(ContextSummaryRow).where(
                ContextSummaryRow.session_id == session_id,
                ContextSummaryRow.source_start_sequence == source_start_sequence,
                ContextSummaryRow.source_end_sequence == source_end_sequence,
                ContextSummaryRow.basis_hash == basis_hash,
            )
        )
        return None if row is None else self._context_summary_from_row(row)

    async def list_active_context_summaries(
        self, project_id: UUID, session_id: UUID
    ) -> Sequence[ContextSummary]:
        rows = (
            await self._session.scalars(
                select(ContextSummaryRow)
                .where(
                    ContextSummaryRow.project_id == project_id,
                    ContextSummaryRow.session_id == session_id,
                    ContextSummaryRow.status == ContextSummaryStatus.ACTIVE.value,
                )
                .order_by(
                    ContextSummaryRow.source_end_sequence,
                    ContextSummaryRow.source_start_sequence,
                    ContextSummaryRow.id,
                )
            )
        ).all()
        return tuple(self._context_summary_from_row(row) for row in rows)

    async def count_turns_in_session(self, session_id: UUID) -> int:
        result = await self._session.execute(
            select(sqlalchemy.func.count(ConversationTurnRow.id)).where(
                ConversationTurnRow.session_id == session_id
            )
        )
        return result.scalar() or 0

    async def add_conversation_clarification(
        self, clarification: ConversationClarification
    ) -> None:
        row = ConversationClarificationRow(
            id=clarification.id,
            turn_id=clarification.turn_id,
            question=clarification.question,
            kind=clarification.kind.value,
            status=clarification.status.value,
            resolved_by_turn_id=clarification.resolved_by_turn_id,
            created_at=clarification.created_at,
        )
        self._session.add(row)
        await self._session.flush()

    async def list_active_clarifications(
        self, session_id: UUID
    ) -> Sequence[ConversationClarification]:
        result = await self._session.execute(
            select(ConversationClarificationRow)
            .join(
                ConversationTurnRow,
                ConversationClarificationRow.turn_id == ConversationTurnRow.id,
            )
            .where(
                ConversationTurnRow.session_id == session_id,
                ConversationClarificationRow.status == "pending",
            )
            .order_by(ConversationClarificationRow.created_at)
        )
        rows = result.scalars().all()
        return tuple(self._clarification_from_row(row) for row in rows)

    async def list_clarifications(
        self, session_id: UUID
    ) -> Sequence[ConversationClarification]:
        result = await self._session.execute(
            select(ConversationClarificationRow)
            .join(
                ConversationTurnRow,
                ConversationClarificationRow.turn_id == ConversationTurnRow.id,
            )
            .where(ConversationTurnRow.session_id == session_id)
            .order_by(
                ConversationClarificationRow.created_at,
                ConversationClarificationRow.id,
            )
        )
        rows = result.scalars().all()
        return tuple(self._clarification_from_row(row) for row in rows)


    async def get_conversation_clarification(
        self, clarification_id: UUID
    ) -> ConversationClarification | None:
        row = await self._session.get(ConversationClarificationRow, clarification_id)
        if row is None:
            return None
        return self._clarification_from_row(row)

    async def resolve_conversation_clarification(
        self, clarification_id: UUID, resolved_by_turn_id: UUID
    ) -> None:
        row = await self._session.get(ConversationClarificationRow, clarification_id)
        if row is None:
            raise DomainNotFoundError("clarification not found")
        if row.status != "pending":
            raise DomainConflictError("clarification is not pending")
        row.status = "resolved"
        row.resolved_by_turn_id = resolved_by_turn_id
        await self._session.flush()

    async def add_conversation_action_proposal(self, proposal: ConversationActionProposal) -> None:
        row = ConversationActionProposalRow(
            id=proposal.id,
            turn_id=proposal.turn_id,
            kind=proposal.kind.value,
            summary=proposal.summary,
            proposed_payload=proposal.proposed_payload,
            status=proposal.status.value,
            resolved_at=proposal.resolved_at,
            resolution_turn_id=proposal.resolution_turn_id,
            created_at=proposal.created_at,
        )
        self._session.add(row)
        await self._session.flush()

    async def list_active_action_proposals(
        self, session_id: UUID
    ) -> Sequence[ConversationActionProposal]:
        result = await self._session.execute(
            select(ConversationActionProposalRow)
            .join(
                ConversationTurnRow,
                ConversationActionProposalRow.turn_id == ConversationTurnRow.id,
            )
            .where(
                ConversationTurnRow.session_id == session_id,
                ConversationActionProposalRow.status == "proposed",
            )
            .order_by(ConversationActionProposalRow.created_at)
        )
        rows = result.scalars().all()
        return tuple(self._action_proposal_from_row(row) for row in rows)

    async def get_conversation_action_proposal(
        self, proposal_id: UUID
    ) -> ConversationActionProposal | None:
        row = await self._session.get(ConversationActionProposalRow, proposal_id)
        if row is None:
            return None
        return self._action_proposal_from_row(row)

    async def resolve_conversation_action_proposal(
        self,
        proposal_id: UUID,
        *,
        status: str,
        resolved_at: datetime,
        resolution_turn_id: UUID,
    ) -> None:
        row = await self._session.get(ConversationActionProposalRow, proposal_id)
        if row is None:
            raise DomainNotFoundError("action proposal not found")
        if row.status != "proposed":
            raise DomainConflictError("action proposal is not in proposed state")
        row.status = status
        row.resolved_at = resolved_at
        row.resolution_turn_id = resolution_turn_id
        await self._session.flush()
