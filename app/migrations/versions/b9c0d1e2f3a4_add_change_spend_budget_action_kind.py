"""add_change_spend_budget_action_kind

Revision ID: b9c0d1e2f3a4
Revises: a7b8c9d0e1f2
Create Date: 2026-08-11 22:00:00.000000

Add ``change_spend_budget`` to the conversation_action_proposals.kind
CHECK constraint so the conversation layer can persist budget proposals.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
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


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "ALTER TABLE conversation_action_proposals"
        " DROP CONSTRAINT IF EXISTS ck_conversation_action_proposals_kind"
    )
    op.execute(
        "ALTER TABLE conversation_action_proposals"
        " ADD CONSTRAINT ck_conversation_action_proposals_kind CHECK ("
        "kind IN ('rewrite_requirements', 'add_module', 'start_research', "
        "'change_selection', 'reshape_project'))"
    )
