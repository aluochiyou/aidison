"""add project revision manifests and module lineages

Revision ID: w0a1b2c3d4e5
Revises: v9a1b2c3d4e5
Create Date: 2026-09-15 10:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "w0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "v9a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _manifest_hash(*, project_id: UUID, revision_number: int, requirement_id: UUID | None,
                   blueprint_id: UUID | None, solution_id: UUID | None) -> str:
    payload = {
        "project_id": str(project_id),
        "revision": revision_number,
        "parent_revision_id": None,
        "requirement_revision_id": str(requirement_id) if requirement_id is not None else None,
        "blueprint_id": str(blueprint_id) if blueprint_id is not None else None,
        "solution_version_id": str(solution_id) if solution_id is not None else None,
        "change_kind": "baseline",
        "change_summary": "Baseline manifest created during Spec-0013 expand migration",
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def upgrade() -> None:
    op.create_table(
        "module_lineages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stable_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "id", name="uq_module_lineage_project_id"),
    )
    op.create_index(
        "ix_module_lineages_project_key", "module_lineages", ["project_id", "stable_key"]
    )
    op.add_column("modules", sa.Column("lineage_id", postgresql.UUID(as_uuid=True), nullable=True))

    # A prior module revision does not prove semantic continuity with another
    # row that happens to use the same key. Give every historical row its own
    # safe baseline lineage; explicit rename/split/merge commands can link
    # future revisions without guessing history.
    op.execute(
        """
        INSERT INTO module_lineages (id, project_id, stable_key, display_name, created_at)
        SELECT id, project_id, key, COALESCE(payload->>'name', key), CURRENT_TIMESTAMP
        FROM modules
        """
    )
    op.execute("UPDATE modules SET lineage_id = id WHERE lineage_id IS NULL")
    op.create_foreign_key(
        "fk_module_lineage",
        "modules",
        "module_lineages",
        ["lineage_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "project_revision_manifests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requirement_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("blueprint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("solution_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("change_kind", sa.String(length=40), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "revision", name="uq_project_manifest_project_revision"),
        sa.UniqueConstraint("project_id", "id", name="uq_project_manifest_project_id"),
        sa.ForeignKeyConstraint(
            ["project_id", "requirement_revision_id"],
            ["requirement_revisions.project_id", "requirement_revisions.id"],
            name="fk_project_manifest_requirement",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "blueprint_id"],
            ["project_blueprints.project_id", "project_blueprints.id"],
            name="fk_project_manifest_blueprint",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "solution_version_id"],
            ["solution_versions.project_id", "solution_versions.id"],
            name="fk_project_manifest_solution",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_foreign_key(
        "fk_project_manifest_parent",
        "project_revision_manifests",
        "project_revision_manifests",
        ["parent_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_project_revision_manifests_project_revision",
        "project_revision_manifests",
        ["project_id", "revision"],
    )

    projects = sa.table(
        "projects",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("revision", sa.Integer()),
        sa.column("active_requirement_revision_id", postgresql.UUID(as_uuid=True)),
        sa.column("active_blueprint_id", postgresql.UUID(as_uuid=True)),
        sa.column("active_solution_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    rows = op.get_bind().execute(
        sa.select(
            projects.c.id,
            projects.c.revision,
            projects.c.active_requirement_revision_id,
            projects.c.active_blueprint_id,
            projects.c.active_solution_version_id,
            projects.c.updated_at,
        )
    ).mappings()
    manifest_table = sa.table(
        "project_revision_manifests",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("project_id", postgresql.UUID(as_uuid=True)),
        sa.column("revision", sa.Integer()),
        sa.column("parent_revision_id", postgresql.UUID(as_uuid=True)),
        sa.column("requirement_revision_id", postgresql.UUID(as_uuid=True)),
        sa.column("blueprint_id", postgresql.UUID(as_uuid=True)),
        sa.column("solution_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("change_kind", sa.String()),
        sa.column("change_summary", sa.Text()),
        sa.column("content_hash", sa.String()),
        sa.column("approved_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    baseline_rows = [
        {
            # Existing Project IDs are globally unique and safe as a one-time
            # baseline manifest identity. Future manifests use generated IDs.
            "id": row["id"],
            "project_id": row["id"],
            "revision": row["revision"],
            "parent_revision_id": None,
            "requirement_revision_id": row["active_requirement_revision_id"],
            "blueprint_id": row["active_blueprint_id"],
            "solution_version_id": row["active_solution_version_id"],
            "change_kind": "baseline",
            "change_summary": "Baseline manifest created during Spec-0013 expand migration",
            "content_hash": _manifest_hash(
                project_id=row["id"],
                revision_number=row["revision"],
                requirement_id=row["active_requirement_revision_id"],
                blueprint_id=row["active_blueprint_id"],
                solution_id=row["active_solution_version_id"],
            ),
            "approved_at": row["updated_at"] or now,
        }
        for row in rows
    ]
    if baseline_rows:
        op.bulk_insert(manifest_table, baseline_rows)


def downgrade() -> None:
    op.drop_index(
        "ix_project_revision_manifests_project_revision",
        table_name="project_revision_manifests",
    )
    op.drop_constraint(
        "fk_project_manifest_parent", "project_revision_manifests", type_="foreignkey"
    )
    op.drop_table("project_revision_manifests")
    op.drop_constraint("fk_module_lineage", "modules", type_="foreignkey")
    op.drop_column("modules", "lineage_id")
    op.drop_index("ix_module_lineages_project_key", table_name="module_lineages")
    op.drop_table("module_lineages")
