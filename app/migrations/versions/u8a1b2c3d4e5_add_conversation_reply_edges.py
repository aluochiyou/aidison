"""add durable conversation reply edges

Revision ID: u8a1b2c3d4e5
Revises: t7a1b2c3d4e5
Create Date: 2026-09-12 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "u8a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "t7a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_turns",
        sa.Column("in_reply_to_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversation_turns_in_reply_to_turn_id_conversation_turns",
        "conversation_turns",
        "conversation_turns",
        ["in_reply_to_turn_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_conversation_turn_in_reply_to",
        "conversation_turns",
        ["in_reply_to_turn_id"],
        unique=True,
        postgresql_where=sa.text("in_reply_to_turn_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversation_turn_in_reply_to", table_name="conversation_turns")
    op.drop_constraint(
        "fk_conversation_turns_in_reply_to_turn_id_conversation_turns",
        "conversation_turns",
        type_="foreignkey",
    )
    op.drop_column("conversation_turns", "in_reply_to_turn_id")
