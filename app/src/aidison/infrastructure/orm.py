from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    pass


class ProjectRow(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("event_sequence >= 0", name="event_sequence_nonnegative"),
        ForeignKeyConstraint(
            ["id", "active_requirement_revision_id"],
            ["requirement_revisions.project_id", "requirement_revisions.id"],
            name="fk_project_active_requirement",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["id", "active_blueprint_id"],
            ["project_blueprints.project_id", "project_blueprints.id"],
            name="fk_project_active_blueprint",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["id", "active_solution_version_id"],
            ["solution_versions.project_id", "solution_versions.id"],
            name="fk_project_active_solution",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_requirement_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    active_blueprint_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    active_solution_version_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    active_spend_budget_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RequirementRevisionRow(Base):
    __tablename__ = "requirement_revisions"
    __table_args__ = (
        UniqueConstraint("project_id", "revision", name="uq_requirement_project_revision"),
        UniqueConstraint("project_id", "id", name="uq_requirement_project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ModuleRow(Base):
    __tablename__ = "modules"
    __table_args__ = (
        UniqueConstraint("requirement_revision_id", "key", name="uq_module_requirement_key"),
        UniqueConstraint("project_id", "id", name="uq_module_project_id"),
        ForeignKeyConstraint(
            ["project_id", "requirement_revision_id"],
            ["requirement_revisions.project_id", "requirement_revisions.id"],
            name="fk_module_project_requirement",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["lineage_id"],
            ["module_lineages.id"],
            name="fk_module_lineage",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        Index("ix_modules_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    requirement_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    lineage_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ModuleLineageRow(Base):
    __tablename__ = "module_lineages"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_module_lineage_project_id"),
        Index("ix_module_lineages_project_key", "project_id", "stable_key"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    split_from_lineage_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    merged_into_lineage_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModuleWorkstreamRow(Base):
    __tablename__ = "module_workstreams"
    __table_args__ = (
        UniqueConstraint("project_id", "module_lineage_id", name="uq_workstream_project_lineage"),
        Index("ix_module_workstreams_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    module_lineage_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("module_lineages.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    memory_manifest_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_project_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_basis_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    optimistic_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModuleMemoryItemRow(Base):
    __tablename__ = "module_memory_items"
    __table_args__ = (
        UniqueConstraint(
            "workstream_id",
            "kind",
            "stable_key",
            "basis_hash",
            "content_hash",
            name="uq_module_memory_dedup",
        ),
        Index("ix_module_memory_workstream_status_key", "workstream_id", "status", "stable_key"),
        Index(
            "ix_module_memory_active_freshness",
            "freshness_deadline",
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    workstream_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("module_workstreams.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(240), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    artifact_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_result_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    applicability: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    freshness_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectBlueprintRow(Base):
    __tablename__ = "project_blueprints"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_blueprint_project_version"),
        UniqueConstraint("project_id", "id", name="uq_blueprint_project_id"),
        ForeignKeyConstraint(
            ["project_id", "requirement_revision_id"],
            ["requirement_revisions.project_id", "requirement_revisions.id"],
            name="fk_blueprint_project_requirement",
            ondelete="RESTRICT",
        ),
        Index("ix_project_blueprints_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    requirement_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ModuleConfigurationRow(Base):
    __tablename__ = "module_configurations"
    __table_args__ = (
        UniqueConstraint("module_id", "revision", name="uq_module_configuration_revision"),
        UniqueConstraint("project_id", "id", name="uq_module_configuration_project_id"),
        ForeignKeyConstraint(
            ["project_id", "module_id"],
            ["modules.project_id", "modules.id"],
            name="fk_module_configuration_project_module",
            ondelete="RESTRICT",
        ),
        Index("ix_module_configurations_project_module", "project_id", "module_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    module_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SelectionLockRow(Base):
    __tablename__ = "selection_locks"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_selection_lock_project_id"),
        ForeignKeyConstraint(
            ["project_id", "module_id"],
            ["modules.project_id", "modules.id"],
            name="fk_selection_lock_project_module",
            ondelete="RESTRICT",
        ),
        Index("ix_selection_locks_project_active", "project_id", "active"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    module_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class AdjustmentBatchRow(Base):
    __tablename__ = "adjustment_batches"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_adjustment_batch_project_id"),
        Index("ix_adjustment_batches_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class UserAdjustmentRow(Base):
    __tablename__ = "user_adjustments"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_user_adjustment_project_id"),
        ForeignKeyConstraint(
            ["project_id", "module_id"],
            ["modules.project_id", "modules.id"],
            name="fk_user_adjustment_project_module",
            ondelete="RESTRICT",
        ),
        Index("ix_user_adjustments_project_batch", "project_id", "batch_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    module_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    batch_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class DraftHistoryEntryRow(Base):
    __tablename__ = "draft_history_entries"
    __table_args__ = (
        UniqueConstraint("project_id", "sequence", name="uq_draft_history_project_sequence"),
        UniqueConstraint("project_id", "id", name="uq_draft_history_project_id"),
        Index("ix_draft_history_entries_project", "project_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SolutionSnapshotRow(Base):
    __tablename__ = "solution_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_solution_snapshot_project_id"),
        Index("ix_solution_snapshots_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProjectReshapeProposalRow(Base):
    __tablename__ = "project_reshape_proposals"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_reshape_proposal_project_id"),
        Index("ix_reshape_proposals_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class RequirementsChangeProposalRow(Base):
    __tablename__ = "requirements_change_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'applied', 'rejected', 'superseded')", name="status"
        ),
        UniqueConstraint("project_id", "id", name="uq_requirements_change_project_id"),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_requirements_change_project",
            ondelete="RESTRICT",
        ),
        Index("ix_requirements_change_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ChangeImpactPreviewRow(Base):
    __tablename__ = "change_impact_previews"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_change_impact_preview_project_id"),
        ForeignKeyConstraint(
            ["project_id", "batch_id"],
            ["adjustment_batches.project_id", "adjustment_batches.id"],
            name="fk_change_impact_preview_project_batch",
            ondelete="RESTRICT",
        ),
        Index("ix_change_impact_previews_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    batch_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class EvidenceBindingRow(Base):
    __tablename__ = "evidence_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "module_id"],
            ["modules.project_id", "modules.id"],
            name="fk_evidence_project_module",
            ondelete="RESTRICT",
        ),
        Index("ix_evidence_project_module", "project_id", "module_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    module_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class CandidateRow(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "module_id"],
            ["modules.project_id", "modules.id"],
            name="fk_candidate_project_module",
            ondelete="RESTRICT",
        ),
        Index("ix_candidates_project_module", "project_id", "module_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    module_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class CompatibilityFindingRow(Base):
    __tablename__ = "compatibility_findings"
    __table_args__ = (Index("ix_compatibility_project", "project_id"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class DecisionRequestRow(Base):
    __tablename__ = "decision_requests"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_decision_project_id"),
        Index("ix_decisions_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SolutionVersionRow(Base):
    __tablename__ = "solution_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_solution_project_version"),
        UniqueConstraint("project_id", "id", name="uq_solution_project_id"),
        ForeignKeyConstraint(
            ["project_id", "solution_proposal_id"],
            ["solution_proposals.project_id", "solution_proposals.id"],
            name="fk_solution_project_proposal",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "requirement_revision_id"],
            ["requirement_revisions.project_id", "requirement_revisions.id"],
            name="fk_solution_project_requirement",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "approved_decision_id"],
            ["decision_requests.project_id", "decision_requests.id"],
            name="fk_solution_project_decision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "previous_version_id"],
            ["solution_versions.project_id", "solution_versions.id"],
            name="fk_solution_project_previous",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    requirement_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    approved_decision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    solution_proposal_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    previous_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ProjectRevisionManifestRow(Base):
    __tablename__ = "project_revision_manifests"
    __table_args__ = (
        UniqueConstraint("project_id", "revision", name="uq_project_manifest_project_revision"),
        UniqueConstraint("project_id", "id", name="uq_project_manifest_project_id"),
        ForeignKeyConstraint(
            ["project_id", "requirement_revision_id"],
            ["requirement_revisions.project_id", "requirement_revisions.id"],
            name="fk_project_manifest_requirement",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["project_id", "blueprint_id"],
            ["project_blueprints.project_id", "project_blueprints.id"],
            name="fk_project_manifest_blueprint",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["project_id", "solution_version_id"],
            ["solution_versions.project_id", "solution_versions.id"],
            name="fk_project_manifest_solution",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["parent_revision_id"],
            ["project_revision_manifests.id"],
            name="fk_project_manifest_parent",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        Index("ix_project_revision_manifests_project_revision", "project_id", "revision"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    requirement_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    blueprint_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    solution_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    change_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    change_summary: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SolutionProposalRow(Base):
    __tablename__ = "solution_proposals"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_solution_proposal_project_id"),
        ForeignKeyConstraint(
            ["project_id", "decision_id"],
            ["decision_requests.project_id", "decision_requests.id"],
            name="fk_solution_proposal_project_decision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "requirement_revision_id"],
            ["requirement_revisions.project_id", "requirement_revisions.id"],
            name="fk_solution_proposal_project_requirement",
            ondelete="RESTRICT",
        ),
        Index("ix_solution_proposals_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    decision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    requirement_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ExecutionPlanProposalRow(Base):
    """One user-resolvable authorization proposal for a bounded agent run."""

    __tablename__ = "execution_plan_proposals"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_execution_plan_project_id"),
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name="status",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_execution_plan_project",
            ondelete="RESTRICT",
        ),
        Index("ix_execution_plan_proposals_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ObservationRow(Base):
    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_observation_project_id"),
        ForeignKeyConstraint(
            ["project_id", "solution_version_id"],
            ["solution_versions.project_id", "solution_versions.id"],
            name="fk_observation_project_solution",
            ondelete="RESTRICT",
        ),
        Index("ix_observations_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    solution_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ImpactAnalysisRow(Base):
    __tablename__ = "impact_analyses"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_impact_project_id"),
        ForeignKeyConstraint(
            ["project_id", "observation_id"],
            ["observations.project_id", "observations.id"],
            name="fk_impact_project_observation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "base_solution_version_id"],
            ["solution_versions.project_id", "solution_versions.id"],
            name="fk_impact_project_solution",
            ondelete="RESTRICT",
        ),
        Index("ix_impacts_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    observation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    base_solution_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class PatchSetRow(Base):
    __tablename__ = "patch_sets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "impact_analysis_id"],
            ["impact_analyses.project_id", "impact_analyses.id"],
            name="fk_patch_project_impact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "base_solution_version_id"],
            ["solution_versions.project_id", "solution_versions.id"],
            name="fk_patch_project_solution",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    impact_analysis_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, unique=True
    )
    base_solution_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class CommandReceiptRow(Base):
    __tablename__ = "command_receipts"

    idempotency_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DomainEventRow(Base):
    __tablename__ = "domain_events"
    __table_args__ = (
        CheckConstraint("project_seq >= 1", name="sequence_positive"),
        CheckConstraint("schema_version >= 0", name="schema_version_nonnegative"),
        CheckConstraint(
            "aggregate_version IS NULL OR aggregate_version >= 1",
            name="aggregate_version_positive",
        ),
        CheckConstraint(
            "schema_version = 0 OR ("
            "aggregate_type IS NOT NULL AND aggregate_id IS NOT NULL AND "
            "aggregate_version IS NOT NULL AND payload_hash IS NOT NULL AND "
            "correlation_id IS NOT NULL AND actor IS NOT NULL AND "
            "source_component IS NOT NULL AND artifact_refs IS NOT NULL)",
            name="versioned_event_metadata_complete",
        ),
        UniqueConstraint("project_id", "project_seq", name="uq_event_project_sequence"),
        UniqueConstraint("event_id", name="uq_event_id"),
        Index("ix_events_project_cursor", "project_id", "project_seq"),
        Index(
            "uq_events_aggregate_version",
            "project_id",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            unique=True,
            postgresql_where=text("aggregate_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    project_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aggregate_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    aggregate_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    aggregate_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), server_default=func.now()
    )
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    causation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_component: Mapped[str | None] = mapped_column(String(100), nullable=True)
    artifact_refs: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EventReplaySnapshotRow(Base):
    """Immutable verified accelerator for a versioned aggregate event stream."""

    __tablename__ = "event_replay_snapshots"
    __table_args__ = (
        CheckConstraint("aggregate_version >= 1", name="aggregate_version_positive"),
        CheckConstraint("project_seq >= 1", name="project_seq_positive"),
        CheckConstraint(
            "char_length(state_hash) = 64", name="state_hash_length"
        ),
        UniqueConstraint(
            "project_id",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "reducer_version",
            name="uq_replay_snapshot_aggregate_version",
        ),
        Index(
            "ix_replay_snapshot_latest",
            "project_id",
            "aggregate_type",
            "aggregate_id",
            "reducer_version",
            "aggregate_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    project_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    reducer_version: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('present', 'missing', 'corrupt', 'quarantined', 'expired')",
            name="status",
        ),
        CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
        UniqueConstraint(
            "project_id",
            "agent_run_id",
            "kind",
            "content_hash",
            name="uq_artifact_agent_run_kind_hash",
        ),
        Index("ix_artifacts_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    agent_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentRunRow(Base):
    """Thin Control-plane row for a LangGraph-owned AgentRun.

    It deliberately does not contain graph branches, tasks, joins, or checkpoint
    payloads. Those remain owned by LangGraph's dedicated Saver schema.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled')",
            name="status",
        ),
        CheckConstraint("basis_project_revision >= 1", name="basis_revision_positive"),
        CheckConstraint("current_generation >= 0", name="generation_nonnegative"),
        CheckConstraint("lifecycle_event_version >= 0", name="lifecycle_event_version_nonnegative"),
        UniqueConstraint("idempotency_key", name="uq_agent_run_idempotency_key"),
        Index("ix_agent_runs_claim", "status", "lease_expires_at", "created_at"),
        Index("ix_agent_runs_project", "project_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    basis_project_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_binding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    run_contract_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    coverage_contract_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    current_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lifecycle_event_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    admitted_checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRunControlRequestRow(Base):
    """Durable user control request; it is not a GraphState or worker lease."""

    __tablename__ = "agent_run_control_requests"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('pause', 'runtime_steering', 'basis_steering')", name="control_kind"
        ),
        CheckConstraint(
            "status IN ('requested', 'acknowledged', 'rejected')", name="control_status"
        ),
        UniqueConstraint("idempotency_key", name="uq_agent_run_control_request_idempotency"),
        Index("ix_agent_run_control_requests_run", "agent_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRunBudgetAccountRow(Base):
    """New-runtime cost envelope; intentionally independent from legacy jobs."""

    __tablename__ = "agent_run_budget_accounts"
    __table_args__ = (
        CheckConstraint("token_cap >= 0", name="token_cap_nonnegative"),
        CheckConstraint("tool_call_cap >= 0", name="tool_cap_nonnegative"),
        CheckConstraint("token_reserved >= 0", name="token_reserved_nonnegative"),
        CheckConstraint("tool_calls_reserved >= 0", name="tool_reserved_nonnegative"),
        CheckConstraint("token_consumed >= 0", name="token_consumed_nonnegative"),
        CheckConstraint("tool_calls_consumed >= 0", name="tool_consumed_nonnegative"),
        CheckConstraint(
            "token_reserved + token_consumed <= token_cap",
            name="token_usage_within_cap",
        ),
        CheckConstraint(
            "tool_calls_reserved + tool_calls_consumed <= tool_call_cap",
            name="tool_usage_within_cap",
        ),
        CheckConstraint("state IN ('open', 'closed', 'cancelled')", name="state"),
        UniqueConstraint("agent_run_id", name="uq_agent_run_budget_account_run"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    token_cap: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tool_call_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    token_reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tool_calls_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_consumed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tool_calls_consumed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRunBudgetOperationRow(Base):
    """One reserve-before-dispatch external call for a LangGraph AgentRun."""

    __tablename__ = "agent_run_budget_operations"
    __table_args__ = (
        CheckConstraint("claim_generation >= 1", name="generation_positive"),
        CheckConstraint("kind IN ('model', 'tool')", name="kind"),
        CheckConstraint(
            "state IN ('reserved', 'dispatched', 'settled', 'released', 'ambiguous')",
            name="state",
        ),
        CheckConstraint("reserved_tokens >= 0", name="reserved_tokens_nonnegative"),
        CheckConstraint("reserved_tool_calls >= 0", name="reserved_tools_nonnegative"),
        CheckConstraint("consumed_tokens >= 0", name="consumed_tokens_nonnegative"),
        CheckConstraint("consumed_tool_calls >= 0", name="consumed_tools_nonnegative"),
        CheckConstraint(
            "consumed_tokens <= reserved_tokens", name="consumed_tokens_within_reservation"
        ),
        CheckConstraint(
            "consumed_tool_calls <= reserved_tool_calls",
            name="consumed_tools_within_reservation",
        ),
        UniqueConstraint("idempotency_key", name="uq_agent_run_budget_operation_idempotency"),
        Index("ix_agent_run_budget_operations_account", "account_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agent_run_budget_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    logical_step: Mapped[str] = mapped_column(String(200), nullable=False)
    physical_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    consumed_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_request_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    request_artifact_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    response_artifact_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    normalized_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRunEffectRow(Base):
    """New-runtime external-write ledger; approval remains an Application concern."""

    __tablename__ = "agent_run_effects"
    __table_args__ = (
        CheckConstraint("claim_generation >= 1", name="generation_positive"),
        CheckConstraint("event_version >= 0", name="event_version_nonnegative"),
        CheckConstraint(
            "state IN ('prepared', 'dispatched', 'succeeded', 'failed', 'ambiguous')",
            name="state",
        ),
        UniqueConstraint("idempotency_key", name="uq_agent_run_effect_idempotency"),
        UniqueConstraint(
            "provider", "external_idempotency_key", name="uq_agent_run_effect_external_idempotency"
        ),
        Index("ix_agent_run_effects_run", "agent_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    effect_kind: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    external_idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_artifact_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    event_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    provider_effect_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    response_artifact_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    failure_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    normalized_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconciliation_artifact_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRunResultRow(Base):
    """Immutable ResultEnvelope projection for a LangGraph AgentRun."""

    __tablename__ = "agent_run_results"
    __table_args__ = (
        CheckConstraint("event_version >= 0", name="event_version_nonnegative"),
        UniqueConstraint("agent_run_id", "id", name="uq_agent_run_result_identity"),
        UniqueConstraint(
            "agent_run_id",
            "task_id",
            "manifest_hash",
            name="uq_agent_run_result_payload",
        ),
        Index("ix_agent_run_results_run", "agent_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentRunResultAdmissionRow(Base):
    """One durable Control verdict per immutable AgentRun result."""

    __tablename__ = "agent_run_result_admissions"
    __table_args__ = (
        UniqueConstraint("result_id", name="uq_agent_run_result_admission_result"),
        ForeignKeyConstraint(
            ["agent_run_id", "result_id"],
            ["agent_run_results.agent_run_id", "agent_run_results.id"],
            name="fk_agent_result_admission_result",
            ondelete="RESTRICT",
        ),
        Index("ix_agent_run_result_admissions_run", "agent_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    result_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentRunDecisionRow(Base):
    """Durable user-decision inbox entry for a non-canonical ProposalManifest."""

    __tablename__ = "agent_run_decisions"
    __table_args__ = (
        CheckConstraint("event_version >= 0", name="event_version_nonnegative"),
        UniqueConstraint("agent_run_id", name="uq_agent_run_decision_run"),
        Index("ix_agent_run_decisions_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_manifest_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    proposal_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    answer: Mapped[str | None] = mapped_column(String(40), nullable=True)
    event_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ── V1 Shopping ────────────────────────────────────────────────────────────


class OfferSnapshotRow(Base):
    __tablename__ = "offer_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_offer_snapshot_project_id"),
        ForeignKeyConstraint(
            ["project_id", "solution_version_id"],
            ["solution_versions.project_id", "solution_versions.id"],
            name="fk_offer_snapshot_project_solution",
            ondelete="RESTRICT",
        ),
        Index("ix_offer_snapshots_project_solution", "project_id", "solution_version_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    solution_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    bom_line_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_offer_id: Mapped[str] = mapped_column(String(500), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PurchaseProposalRow(Base):
    __tablename__ = "purchase_proposals"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_purchase_proposal_project_id"),
        ForeignKeyConstraint(
            ["project_id", "solution_version_id"],
            ["solution_versions.project_id", "solution_versions.id"],
            name="fk_purchase_proposal_project_solution",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "offer_snapshot_id"],
            ["offer_snapshots.project_id", "offer_snapshots.id"],
            name="fk_purchase_proposal_project_offer",
            ondelete="RESTRICT",
        ),
        Index("ix_purchase_proposals_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    solution_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    offer_snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EffectApprovalRow(Base):
    __tablename__ = "effect_approvals"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_effect_approval_project_id"),
        ForeignKeyConstraint(
            ["project_id", "target_ref"],
            ["purchase_proposals.project_id", "purchase_proposals.id"],
            name="fk_effect_approval_project_proposal",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('requested', 'approved', 'denied', 'expired', 'consumed')",
            name="effect_approval_status",
        ),
        CheckConstraint(
            "expires_at > requested_at",
            name="effect_approval_expiry_after_request",
        ),
        CheckConstraint(
            "status = payload->>'status'",
            name="effect_approval_payload_status",
        ),
        Index("ix_effect_approvals_project_status", "project_id", "status"),
        Index(
            "uq_effect_approvals_live_scope",
            "project_id",
            "scope_hash",
            unique=True,
            postgresql_where=text("status IN ('requested', 'approved')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    effect_kind: Mapped[str] = mapped_column(String(100), nullable=False)
    target_ref: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CheckoutHandoffRow(Base):
    __tablename__ = "checkout_handoffs"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_checkout_handoff_project_id"),
        ForeignKeyConstraint(
            ["project_id", "proposal_id"],
            ["purchase_proposals.project_id", "purchase_proposals.id"],
            name="fk_checkout_handoff_project_proposal",
            ondelete="RESTRICT",
        ),
        Index("ix_checkout_handoffs_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InvocationRecordingRow(Base):
    """Secret-free prepared/terminal recording for replayable provider/tool effects."""

    __tablename__ = "invocation_recordings"
    __table_args__ = (
        CheckConstraint("kind IN ('model', 'tool')", name="kind"),
        CheckConstraint("status IN ('pending', 'succeeded', 'ambiguous')", name="status"),
        UniqueConstraint("idempotency_key", name="uq_invocation_recordings_idempotency_key"),
        Index("ix_invocation_recordings_agent_run", "agent_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    agent_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    producer_attempt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    operation_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    response_artifact_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── Spend Budget ────────────────────────────────────────────────────────────


class SpendBudgetProposalRow(Base):
    __tablename__ = "spend_budget_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'applied', 'rejected', 'superseded')",
            name="status",
        ),
        UniqueConstraint("project_id", "id", name="uq_spend_budget_proposal_project_id"),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_spend_budget_proposal_project",
            ondelete="RESTRICT",
        ),
        Index("ix_spend_budget_proposals_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SpendBudgetRevisionRow(Base):
    __tablename__ = "spend_budget_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        UniqueConstraint(
            "project_id",
            "revision",
            name="uq_spend_budget_revision_project_revision",
        ),
        UniqueConstraint("project_id", "id", name="uq_spend_budget_revision_project_id"),
        ForeignKeyConstraint(
            ["project_id", "proposal_id"],
            ["spend_budget_proposals.project_id", "spend_budget_proposals.id"],
            name="fk_spend_budget_revision_project_proposal",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_spend_budget_revisions_project_status",
            "project_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SpendBudgetImpactPreviewRow(Base):
    __tablename__ = "spend_budget_impact_previews"
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_spend_budget_impact_preview_project_id"),
        ForeignKeyConstraint(
            ["project_id", "proposal_id"],
            ["spend_budget_proposals.project_id", "spend_budget_proposals.id"],
            name="fk_spend_budget_impact_preview_project_proposal",
            ondelete="RESTRICT",
        ),
        Index("ix_spend_budget_impact_previews_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


# ── Project-scoped Conversation ──────────────────────────────────────────────


class ConversationSessionRow(Base):
    __tablename__ = "conversation_sessions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'closed')", name="status"),
        UniqueConstraint("project_id", "id", name="uq_conversation_session_project_id"),
        Index(
            "uq_conversation_session_active_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConversationTurnRow(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("role IN ('user', 'assistant')", name="role"),
        CheckConstraint("turn_type IN ('message', 'clarification', 'proposal')", name="turn_type"),
        UniqueConstraint("session_id", "sequence", name="uq_conversation_turn_session_sequence"),
        Index(
            "uq_conversation_turn_idempotency_key",
            "session_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_conversation_turn_in_reply_to",
            "in_reply_to_turn_id",
            unique=True,
            postgresql_where=text("in_reply_to_turn_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversation_sessions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_profile: Mapped[str] = mapped_column(String(200), nullable=False, default="human")
    turn_type: Mapped[str] = mapped_column(String(40), nullable=False, default="message")
    idempotency_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    in_reply_to_turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversation_turns.id", ondelete="RESTRICT"),
        nullable=True,
    )
    is_fallback: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContextSummaryRow(Base):
    __tablename__ = "context_summaries"
    __table_args__ = (
        CheckConstraint(
            "source_end_sequence >= source_start_sequence",
            name="source_sequence_range",
        ),
        CheckConstraint("status IN ('active', 'superseded', 'tombstoned')", name="status"),
        UniqueConstraint("project_id", "id", name="uq_context_summary_project_id"),
        UniqueConstraint(
            "session_id",
            "source_start_sequence",
            "source_end_sequence",
            "basis_hash",
            name="uq_context_summary_source_basis",
        ),
        ForeignKeyConstraint(
            ["project_id", "session_id"],
            ["conversation_sessions.project_id", "conversation_sessions.id"],
            name="fk_context_summary_project_session",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_context_summaries_project_session_active",
            "project_id",
            "session_id",
            "status",
            "source_end_sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_start_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_event_cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConversationClarificationRow(Base):
    __tablename__ = "conversation_clarifications"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('usage', 'requirement', 'constraint', 'module', 'selection', "
            "'budget', 'resource', 'skill', 'other')",
            name="kind",
        ),
        CheckConstraint("status IN ('pending', 'resolved', 'expired')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    turn_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversation_turns.id", ondelete="RESTRICT"),
        nullable=False,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    resolved_by_turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversation_turns.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConversationActionProposalRow(Base):
    __tablename__ = "conversation_action_proposals"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('rewrite_requirements', 'add_module', 'start_research', "
            "'change_selection', 'reshape_project', 'change_spend_budget', "
            "'restore_solution_snapshot')",
            name="kind",
        ),
        CheckConstraint("status IN ('proposed', 'accepted', 'rejected', 'expired')", name="status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    turn_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversation_turns.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="proposed")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_turn_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversation_turns.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
