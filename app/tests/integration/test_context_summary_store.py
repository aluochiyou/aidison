"""PostgreSQL integration coverage for project-scoped derived context summaries."""

from __future__ import annotations

import os
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from aidison.application.conversation import ConversationApplication, ConversationNotFoundError
from aidison.application.service import ProjectApplication
from aidison.domain.models import ConversationTurn, ConversationTurnRole
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.store import PostgresDomainStore

pytestmark = pytest.mark.integration


def _model_factory() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_context_summary_is_project_scoped_and_survives_a_new_session() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with factory() as session:
            store = PostgresDomainStore(session)
            project_app = ProjectApplication(store)
            project_a = await project_app.create_project(
                name="Context summary project A",
                goal="Prove summary isolation",
                idempotency_key=f"context-summary:a:{uuid4()}",
            )
            project_b = await project_app.create_project(
                name="Context summary project B",
                goal="Must not see A summary",
                idempotency_key=f"context-summary:b:{uuid4()}",
            )
            conv = ConversationApplication(
                store_factory=lambda: store,
                model_factory=_model_factory,
            )
            session_a = await conv.ensure_active_session(project_a.id)
            session_b = await conv.ensure_active_session(project_b.id)
            await store.add_conversation_turn(
                ConversationTurn(
                    session_id=session_a.id,
                    sequence=1,
                    role=ConversationTurnRole.USER,
                    content="Old requirement detail",
                    idempotency_key=f"context-summary:turn:{uuid4()}",
                )
            )
            await store.add_conversation_turn(
                ConversationTurn(
                    session_id=session_a.id,
                    sequence=2,
                    role=ConversationTurnRole.ASSISTANT,
                    content="Old answer",
                    model_profile="deepseek-v4-pro",
                )
            )
            await session.commit()

            summary = await conv.record_context_summary(
                project_id=project_a.id,
                session_id=session_a.id,
                source_start_sequence=1,
                source_end_sequence=2,
                content="A 已经澄清旧需求。",
                summarizer_profile="deepseek-v4-pro",
                idempotency_key=f"context-summary:record:{uuid4()}",
            )
            assert summary.project_id == project_a.id

            assert await store.list_active_context_summaries(project_a.id, session_a.id) == (
                summary,
            )
            assert await store.list_active_context_summaries(project_b.id, session_b.id) == ()
            with pytest.raises(ConversationNotFoundError):
                await conv.record_context_summary(
                    project_id=project_a.id,
                    session_id=session_b.id,
                    source_start_sequence=1,
                    source_end_sequence=1,
                    content="Cross-project summary",
                    summarizer_profile="deepseek-v4-pro",
                    idempotency_key=f"context-summary:cross:{uuid4()}",
                )
    finally:
        await engine.dispose()
