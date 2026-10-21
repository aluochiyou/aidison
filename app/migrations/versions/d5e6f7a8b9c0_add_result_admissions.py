"""add result admission ledger

Revision ID: d5e6f7a8b9c0
Revises: c3d4e5f6a7b8
Create Date: 2026-08-10 04:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "result_admissions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("handoff_id", sa.UUID(), nullable=False),
        sa.Column("result_ref", sa.String(length=4000), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("receipt_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('admitted', 'rejected', 'quarantined')",
            name=op.f("ck_result_admissions_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_result_admissions")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_result_admissions_key_hash")),
    )
    op.create_index(
        "ix_result_admissions_handoff_basis",
        "result_admissions",
        ["handoff_id", "basis_hash"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_result_admissions_handoff_basis", table_name="result_admissions")
    op.drop_table("result_admissions")
