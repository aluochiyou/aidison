"""add draft workbench foundations

Revision ID: g8a9b0c1d2e3
Revises: f7a8b9c0d1e2
Create Date: 2026-08-10 08:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g8a9b0c1d2e3"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity_payload_table(
    name: str,
    *,
    extra_columns: list[sa.Column[object]] | None = None,
    constraints: list[sa.Constraint] | None = None,
) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        *(extra_columns or []),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{name}")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f(f"fk_{name}_project_id_projects"),
            ondelete="RESTRICT",
        ),
        *(constraints or []),
    )


def upgrade() -> None:
    _identity_payload_table(
        "project_blueprints",
        extra_columns=[
            sa.Column("requirement_revision_id", sa.UUID(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
        ],
        constraints=[
            sa.UniqueConstraint("project_id", "version", name="uq_blueprint_project_version"),
            sa.UniqueConstraint("project_id", "id", name="uq_blueprint_project_id"),
            sa.ForeignKeyConstraint(
                ["project_id", "requirement_revision_id"],
                ["requirement_revisions.project_id", "requirement_revisions.id"],
                name="fk_blueprint_project_requirement",
                ondelete="RESTRICT",
            ),
        ],
    )
    op.create_index(
        "ix_project_blueprints_project_status",
        "project_blueprints",
        ["project_id", "status"],
        unique=False,
    )
    op.add_column("projects", sa.Column("active_blueprint_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_project_active_blueprint",
        "projects",
        "project_blueprints",
        ["id", "active_blueprint_id"],
        ["project_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )

    _identity_payload_table(
        "module_configurations",
        extra_columns=[
            sa.Column("module_id", sa.UUID(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
        ],
        constraints=[
            sa.UniqueConstraint("module_id", "revision", name="uq_module_configuration_revision"),
            sa.UniqueConstraint("project_id", "id", name="uq_module_configuration_project_id"),
            sa.ForeignKeyConstraint(
                ["project_id", "module_id"],
                ["modules.project_id", "modules.id"],
                name="fk_module_configuration_project_module",
                ondelete="RESTRICT",
            ),
        ],
    )
    op.create_index(
        "ix_module_configurations_project_module",
        "module_configurations",
        ["project_id", "module_id"],
        unique=False,
    )

    _identity_payload_table(
        "selection_locks",
        extra_columns=[
            sa.Column("module_id", sa.UUID(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
        ],
        constraints=[
            sa.UniqueConstraint("project_id", "id", name="uq_selection_lock_project_id"),
            sa.ForeignKeyConstraint(
                ["project_id", "module_id"],
                ["modules.project_id", "modules.id"],
                name="fk_selection_lock_project_module",
                ondelete="RESTRICT",
            ),
        ],
    )
    op.create_index(
        "ix_selection_locks_project_active",
        "selection_locks",
        ["project_id", "active"],
        unique=False,
    )

    _identity_payload_table(
        "adjustment_batches",
        extra_columns=[sa.Column("status", sa.String(length=40), nullable=False)],
        constraints=[
            sa.UniqueConstraint("project_id", "id", name="uq_adjustment_batch_project_id")
        ],
    )
    op.create_index(
        "ix_adjustment_batches_project_status",
        "adjustment_batches",
        ["project_id", "status"],
        unique=False,
    )

    _identity_payload_table(
        "user_adjustments",
        extra_columns=[
            sa.Column("module_id", sa.UUID(), nullable=False),
            sa.Column("batch_id", sa.UUID(), nullable=True),
        ],
        constraints=[
            sa.UniqueConstraint("project_id", "id", name="uq_user_adjustment_project_id"),
            sa.ForeignKeyConstraint(
                ["project_id", "module_id"],
                ["modules.project_id", "modules.id"],
                name="fk_user_adjustment_project_module",
                ondelete="RESTRICT",
            ),
        ],
    )
    op.create_index(
        "ix_user_adjustments_project_batch",
        "user_adjustments",
        ["project_id", "batch_id"],
        unique=False,
    )

    _identity_payload_table(
        "draft_history_entries",
        extra_columns=[sa.Column("sequence", sa.Integer(), nullable=False)],
        constraints=[
            sa.UniqueConstraint("project_id", "sequence", name="uq_draft_history_project_sequence"),
            sa.UniqueConstraint("project_id", "id", name="uq_draft_history_project_id"),
        ],
    )
    op.create_index(
        "ix_draft_history_entries_project",
        "draft_history_entries",
        ["project_id", "sequence"],
        unique=False,
    )

    _identity_payload_table(
        "solution_snapshots",
        constraints=[
            sa.UniqueConstraint("project_id", "id", name="uq_solution_snapshot_project_id")
        ],
    )
    op.create_index("ix_solution_snapshots_project", "solution_snapshots", ["project_id"])

    _identity_payload_table(
        "project_reshape_proposals",
        extra_columns=[sa.Column("status", sa.String(length=40), nullable=False)],
        constraints=[
            sa.UniqueConstraint("project_id", "id", name="uq_reshape_proposal_project_id")
        ],
    )
    op.create_index(
        "ix_reshape_proposals_project_status",
        "project_reshape_proposals",
        ["project_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    for index, table in (
        ("ix_reshape_proposals_project_status", "project_reshape_proposals"),
        ("ix_solution_snapshots_project", "solution_snapshots"),
        ("ix_draft_history_entries_project", "draft_history_entries"),
        ("ix_user_adjustments_project_batch", "user_adjustments"),
        ("ix_adjustment_batches_project_status", "adjustment_batches"),
        ("ix_selection_locks_project_active", "selection_locks"),
        ("ix_module_configurations_project_module", "module_configurations"),
        ("ix_project_blueprints_project_status", "project_blueprints"),
    ):
        op.drop_index(index, table_name=table)
    for table in (
        "project_reshape_proposals",
        "solution_snapshots",
        "draft_history_entries",
        "user_adjustments",
        "adjustment_batches",
        "selection_locks",
        "module_configurations",
    ):
        op.drop_table(table)
    op.drop_constraint("fk_project_active_blueprint", "projects", type_="foreignkey")
    op.drop_column("projects", "active_blueprint_id")
    op.drop_table("project_blueprints")
