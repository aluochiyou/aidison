"""add_restore_solution_snapshot_action_kind

Revision ID: c1d2e3f4a5b6
Revises: b9c0d1e2f3a4
Create Date: 2026-08-11 23:00:00.000000

Add ``restore_solution_snapshot`` to the conversation_action_proposals.kind
CHECK constraint.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE conversation_action_proposals"
        " DROP CONSTRAINT IF EXISTS ck_conversation_action_proposals_kind"
    )
    op.execute(
        "ALTER TABLE conversation_action_proposals"
        " ADD CONSTRAINT ck_conversation_action_proposals_kind CHECK ("
        "kind IN ('rewrite_requirements', 'add_module', 'start_research', "
        "'change_selection', 'reshape_project', 'change_spend_budget', "
        "'restore_solution_snapshot'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE conversation_action_proposals"
        " DROP CONSTRAINT IF EXISTS ck_conversation_action_proposals_kind"
    )
    op.execute(
        "ALTER TABLE conversation_action_proposals"
        " ADD CONSTRAINT ck_conversation_action_proposals_kind CHECK ("
        "kind IN ('rewrite_requirements', 'add_module', 'start_research', "
        "'change_selection', 'reshape_project', 'change_spend_budget'))"
    )
