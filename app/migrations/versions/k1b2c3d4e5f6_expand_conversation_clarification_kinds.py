"""expand_conversation_clarification_kinds

Revision ID: k1b2c3d4e5f6
Revises: j0a1b2c3d4e5
Create Date: 2026-08-12 12:00:00.000000

The pre-approval clarification rounds collect usage/budget/resources/skill/
constraints facts, which map to the clarification kinds ``usage``, ``resource``
and ``skill``. The original CHECK only allowed the six legacy kinds, so a model
reply emitting one of the new kinds failed persistence with IntegrityError even
after the user turn had already committed. This migration widens the CHECK to
the full kind vocabulary and keeps the ORM and the schema consistent.

The downgrade is fail-closed: if any row already uses one of the new kinds it
refuses to restore the old constraint instead of silently dropping data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "k1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "j0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "ck_conversation_clarifications_kind"
_OLD_KINDS = "kind IN ('requirement', 'constraint', 'module', 'selection', 'budget', 'other')"
_NEW_KINDS = (
    "kind IN ('usage', 'requirement', 'constraint', 'module', 'selection', "
    "'budget', 'resource', 'skill', 'other')"
)


def upgrade() -> None:
    op.drop_constraint(op.f(_CONSTRAINT_NAME), "conversation_clarifications", type_="check")
    op.create_check_constraint(op.f(_CONSTRAINT_NAME), "conversation_clarifications", _NEW_KINDS)


def downgrade() -> None:
    bind = op.get_bind()
    incompatible = bind.execute(
        sa.text(
            "SELECT count(*) FROM conversation_clarifications "
            "WHERE kind IN ('usage', 'resource', 'skill')"
        )
    ).scalar()
    if incompatible:
        raise RuntimeError(
            "cannot downgrade ck_conversation_clarifications_kind: "
            f"{incompatible} rows use usage/resource/skill kinds; refusing to drop data"
        )
    op.drop_constraint(op.f(_CONSTRAINT_NAME), "conversation_clarifications", type_="check")
    op.create_check_constraint(op.f(_CONSTRAINT_NAME), "conversation_clarifications", _OLD_KINDS)
