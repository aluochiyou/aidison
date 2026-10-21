"""link result admissions to attempt results

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-10 05:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make each runtime admission a projection of at most one attempt result."""
    op.add_column(
        "result_admissions",
        sa.Column("result_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_result_admissions_result_id_attempt_results"),
        "result_admissions",
        "attempt_results",
        ["result_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_result_admissions_result_id"),
        "result_admissions",
        ["result_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_result_admissions_result_id"),
        "result_admissions",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_result_admissions_result_id_attempt_results"),
        "result_admissions",
        type_="foreignkey",
    )
    op.drop_column("result_admissions", "result_id")
