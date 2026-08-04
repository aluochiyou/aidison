from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

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
    active_solution_version_id: Mapped[UUID | None] = mapped_column(
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
        Index("ix_modules_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    requirement_revision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
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
        UniqueConstraint("project_id", "project_seq", name="uq_event_project_sequence"),
        Index("ix_events_project_cursor", "project_id", "project_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    project_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
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
            "attempt_id",
            "kind",
            "content_hash",
            name="uq_artifact_attempt_kind_hash",
        ),
        Index("ix_artifacts_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("attempts.id", ondelete="RESTRICT"), nullable=False
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


class AgentProfileRevisionRow(Base):
    __tablename__ = "agent_profile_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("token_cap > 0", name="token_cap_positive"),
        CheckConstraint("tool_call_cap >= 0", name="tool_cap_nonnegative"),
        CheckConstraint("concurrency_cap >= 1", name="concurrency_cap_positive"),
        CheckConstraint("timeout_seconds >= 1", name="timeout_positive"),
        UniqueConstraint("profile_id", "definition_hash", name="uq_profile_definition"),
    )

    profile_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(1_000), nullable=False)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    output_schema_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    token_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_call_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentProfileActiveRow(Base):
    __tablename__ = "agent_profile_active"
    __table_args__ = (
        CheckConstraint("active_revision >= 1", name="revision_positive"),
        ForeignKeyConstraint(
            ["profile_id", "active_revision"],
            ["agent_profile_revisions.profile_id", "agent_profile_revisions.revision"],
            name="fk_active_profile_revision",
            ondelete="RESTRICT",
        ),
    )

    profile_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    active_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("profile_revision >= 1", name="profile_revision_positive"),
        CheckConstraint("basis_project_revision >= 1", name="basis_revision_positive"),
        CheckConstraint("current_generation >= 0", name="generation_nonnegative"),
        ForeignKeyConstraint(
            ["profile_id", "profile_revision"],
            ["agent_profile_revisions.profile_id", "agent_profile_revisions.revision"],
            name="fk_job_profile_revision",
            ondelete="RESTRICT",
        ),
        Index("ix_jobs_project_status", "project_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    request_key: Mapped[str | None] = mapped_column(String(300), nullable=True, unique=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    parent_job_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(200), nullable=False)
    profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    basis_project_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    current_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobProfileBindingRow(Base):
    __tablename__ = "job_profile_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["profile_id", "profile_revision"],
            ["agent_profile_revisions.profile_id", "agent_profile_revisions.revision"],
            name="fk_job_binding_profile_revision",
            ondelete="RESTRICT",
        ),
    )

    root_job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), primary_key=True
    )
    role_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(200), nullable=False)
    profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BudgetAccountRow(Base):
    __tablename__ = "budget_accounts"
    __table_args__ = (
        CheckConstraint("token_cap >= 0", name="token_cap_nonnegative"),
        CheckConstraint("tool_call_cap >= 0", name="tool_cap_nonnegative"),
        CheckConstraint(
            "token_committed >= 0 AND token_committed <= token_cap",
            name="token_committed_within_cap",
        ),
        CheckConstraint(
            "tool_calls_committed >= 0 AND tool_calls_committed <= tool_call_cap",
            name="tool_committed_within_cap",
        ),
        CheckConstraint(
            "status IN ('open', 'closed', 'cancelled', 'legacy_unknown')",
            name="status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    root_job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), unique=True
    )
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    token_cap: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tool_call_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    token_committed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tool_calls_committed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BudgetAllocationRow(Base):
    __tablename__ = "budget_allocations"
    __table_args__ = (
        CheckConstraint("owner_kind IN ('parent', 'child', 'join', 'audit')", name="owner_kind"),
        CheckConstraint("token_grant >= 0", name="token_grant_nonnegative"),
        CheckConstraint("tool_call_grant >= 0", name="tool_grant_nonnegative"),
        CheckConstraint("token_reserved >= 0", name="token_reserved_nonnegative"),
        CheckConstraint("tool_calls_reserved >= 0", name="tool_reserved_nonnegative"),
        CheckConstraint("token_consumed >= 0", name="token_consumed_nonnegative"),
        CheckConstraint("tool_calls_consumed >= 0", name="tool_consumed_nonnegative"),
        CheckConstraint(
            "token_reserved + token_consumed <= token_grant",
            name="token_usage_within_grant",
        ),
        CheckConstraint(
            "tool_calls_reserved + tool_calls_consumed <= tool_call_grant",
            name="tool_usage_within_grant",
        ),
        CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="status"),
        UniqueConstraint("account_id", "owner_kind", "owner_ref", name="uq_budget_owner"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("budget_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    owner_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_ref: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    token_grant: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tool_call_grant: Mapped[int] = mapped_column(Integer, nullable=False)
    token_reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tool_calls_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_consumed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tool_calls_consumed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BudgetOperationRow(Base):
    __tablename__ = "budget_operations"
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
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    allocation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("budget_allocations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("attempts.id", ondelete="RESTRICT"), nullable=False
    )
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    logical_step: Mapped[str] = mapped_column(String(200), nullable=False)
    physical_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_or_tool: Mapped[str] = mapped_column(String(200), nullable=False)
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


class AttemptRow(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        CheckConstraint("number >= 1", name="number_positive"),
        CheckConstraint("claim_generation >= 1", name="generation_positive"),
        UniqueConstraint("job_id", "number", name="uq_attempt_job_number"),
        UniqueConstraint("job_id", "claim_generation", name="uq_attempt_job_generation"),
        Index("ix_attempts_lease", "status", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    normalized_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DelegationRow(Base):
    __tablename__ = "delegations"
    __table_args__ = (
        CheckConstraint("parent_claim_generation >= 1", name="generation_positive"),
        CheckConstraint("profile_revision >= 1", name="profile_revision_positive"),
        ForeignKeyConstraint(
            ["profile_id", "profile_revision"],
            ["agent_profile_revisions.profile_id", "agent_profile_revisions.revision"],
            name="fk_delegation_profile_revision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("idempotency_key", name="uq_delegation_idempotency_key"),
        UniqueConstraint("child_job_id", name="uq_delegation_child_job"),
        UniqueConstraint(
            "parent_attempt_id", "graph_step_id", "shard_key", name="uq_delegation_shard"
        ),
        Index("ix_delegations_parent", "parent_job_id", "parent_attempt_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    parent_job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    parent_attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("attempts.id", ondelete="RESTRICT"), nullable=False
    )
    parent_claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    join_group_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("join_groups.id", ondelete="RESTRICT"), nullable=False
    )
    child_job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    graph_step_id: Mapped[str] = mapped_column(String(200), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(200), nullable=False)
    profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    shard_key: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JoinGroupRow(Base):
    __tablename__ = "join_groups"
    __table_args__ = (
        CheckConstraint("parent_claim_generation >= 1", name="generation_positive"),
        CheckConstraint("basis_project_revision >= 1", name="basis_revision_positive"),
        CheckConstraint("expected_count BETWEEN 1 AND 2", name="expected_count_v0"),
        UniqueConstraint("parent_attempt_id", "graph_step_id", name="uq_join_parent_step"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    parent_job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    parent_attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("attempts.id", ondelete="RESTRICT"), nullable=False
    )
    parent_claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    graph_step_id: Mapped[str] = mapped_column(String(200), nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    basis_project_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class JoinReceiptRow(Base):
    __tablename__ = "join_receipts"

    join_group_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("join_groups.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    parent_claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AttemptResultRow(Base):
    __tablename__ = "attempt_results"
    __table_args__ = (
        CheckConstraint("claim_generation >= 1", name="generation_positive"),
        CheckConstraint(
            "disposition IN ('eligible', 'quarantined')",
            name="disposition",
        ),
        UniqueConstraint("attempt_id", "result_hash", name="uq_attempt_result_hash"),
        Index("ix_attempt_results_disposition", "disposition"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("attempts.id", ondelete="RESTRICT"), nullable=False
    )
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
