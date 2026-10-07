"""Focused integration tests for CHANGE_SPEND_BUDGET conversation action proposal.

Covers two minimal acceptance chains:
1. Model proposes → accept → SpendBudgetProposal(PROPOSED), no revision bump,
   no runtime job, workspace shows review_spend_budget/READY_TO_REVIEW;
   resolve applied → active revision points to new budget.
2. Stale If-Match → 412 on accept and resolve; Idempotency-Key replay
   produces same result_ref without duplicate proposals/revisions/events.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from tests.integration.support import (
    approve_requirements_and_apply_initial_modules,
    module_discovery_model_factory,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def key_prefix() -> str:
    return str(uuid4())


# ── Model factories ───────────────────────────────────────────────────────────


def _budget_model_factory() -> BaseChatModel:
    """Conversation model that emits a CHANGE_SPEND_BUDGET action proposal."""
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "reply": "Let me set a budget for the project.",
                    "action_proposals": [
                        {
                            "kind": "change_spend_budget",
                            "summary": "Set CNY 5000 budget",
                            "proposed_payload": {
                                "amount": "5000",
                                "currency": "CNY",
                                "summary": "Total project research budget",
                            },
                        }
                    ],
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _client(
    engine: object, factory: object, model_factory: object = _budget_model_factory
) -> httpx.AsyncClient:
    api = create_app(
        factory,
        conversation_model_factory=model_factory,
        module_discovery_model_factory=module_discovery_model_factory,
    )
    transport = httpx.ASGITransport(app=api)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _headers(key_prefix: str, suffix: str, **extra: str) -> dict[str, str]:
    return {"Idempotency-Key": f"{key_prefix}:{suffix}", **extra}


async def _create_project(client: httpx.AsyncClient, key_prefix: str) -> str:
    r = await client.post(
        "/api/projects",
        json={"name": "Budget Conversation Test", "goal": "Verify spend budget"},
        headers=_headers(key_prefix, "project"),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _approve_requirements(
    client: httpx.AsyncClient, key_prefix: str, project_id: str
) -> None:
    await approve_requirements_and_apply_initial_modules(
        client,
        project_id=project_id,
        goal="Verify spend budget flow",
        idempotency_key_prefix=f"{key_prefix}:reqs",
    )


async def _get_project_revision(
    client: httpx.AsyncClient, project_id: str
) -> int:
    r = await client.get(f"/api/projects/{project_id}")
    assert r.status_code == 200, r.text
    return r.json()["revision"]


# ── Chain 1: happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_spend_budget_accept_then_resolve_full_chain(
    key_prefix: str,
) -> None:
    """Model proposes CHANGE_SPEND_BUDGET; accept creates PROPOSED plan with
    no side-effects; resolve applied bumps revision and sets active budget.

    Verifies:
    - Accept: SpendBudgetProposal(PROPOSED), project.revision unchanged,
      active_spend_budget is null, no runtime job.
    - Workspace before resolve: review_spend_budget / READY_TO_REVIEW.
    - Resolve applied: active_spend_budget_revision_id set, revision bumped,
      snapshot contains spend_budget revision and proposal list.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory, _budget_model_factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)
            rev_before_accept = await _get_project_revision(client, project_id)

            # 1. Post a message so the model emits the proposal.
            r = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Set a CNY 5000 budget for this project."},
                headers=_headers(key_prefix, "budget-msg"),
            )
            assert r.status_code == 200, r.text

            # 2. Retrieve the generated proposal from the snapshot.
            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            conv = snapshot.json()["conversation"]
            open_proposals = conv["open_action_proposals"]
            assert len(open_proposals) == 1
            assert open_proposals[0]["kind"] == "change_spend_budget"
            proposal_id = open_proposals[0]["id"]

            # 3. Accept the conversation proposal.
            r = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal_id}/accept",
                headers={
                    **_headers(key_prefix, "accept-budget"),
                    "If-Match": f'"{rev_before_accept}"',
                },
            )
            assert r.status_code == 200, r.text
            accept_body = r.json()
            result_ref = accept_body["result_ref"]
            assert result_ref.startswith("spend_budget:")
            budget_proposal_id = result_ref.removeprefix("spend_budget:")
            accepted_proposal = accept_body["proposal"]
            assert accepted_proposal["status"] == "accepted"

            # 4. Verify: SpendBudgetProposal is PROPOSED (not applied).
            async with factory() as session:
                from aidison.infrastructure.store import PostgresDomainStore

                store = PostgresDomainStore(session)
                budget = await store.get_spend_budget_proposal(
                    UUID(budget_proposal_id)
                )
                assert budget is not None
                assert budget.status.value == "proposed"

                # project.revision unchanged
                project = await store.get_project(UUID(project_id))
                assert project is not None
                assert project.revision == rev_before_accept

                # active_spend_budget is null (not applied yet)
                active = await store.get_active_spend_budget(UUID(project_id))
                assert active is None

                # no modern AgentRun was created
                agent_run_count = await session.scalar(
                    text("SELECT count(*) FROM agent_runs WHERE project_id = :pid"),
                    {"pid": project_id},
                )
                assert agent_run_count == 0

            # 5. Workspace before resolve: review_spend_budget / READY_TO_REVIEW.
            ws = await client.get(f"/api/projects/{project_id}/workspace")
            assert ws.status_code == 200, ws.text
            ws_body = ws.json()
            assert ws_body["attention"]["state"] == "ready_to_review"
            actions = ws_body["next_actions"]
            budget_action = next(
                (a for a in actions if a["kind"] == "review_spend_budget"), None
            )
            assert budget_action is not None, (
                f"expected review_spend_budget action in workspace, got {actions}"
            )

            # 6. Resolve the spend budget proposal as applied.
            rev_before_resolve = await _get_project_revision(client, project_id)
            r = await client.post(
                f"/api/spend-budget-proposals/{budget_proposal_id}/resolve",
                json={"decision": "applied"},
                headers={
                    **_headers(key_prefix, "resolve-budget"),
                    "If-Match": f'"{rev_before_resolve}"',
                },
            )
            assert r.status_code == 200, r.text
            resolved = r.json()
            revision = resolved["spend_budget_revision"]
            assert revision["amount"] == "5000"
            assert revision["currency"] == "CNY"
            assert revision["status"] == "applied"
            assert revision["revision"] >= 1

            # 7. Verify: active_spend_budget_revision_id now set, revision bumped.
            async with factory() as session:
                from aidison.infrastructure.store import PostgresDomainStore

                store = PostgresDomainStore(session)
                project = await store.get_project(UUID(project_id))
                assert project is not None
                assert project.revision == rev_before_resolve + 1
                assert project.active_spend_budget_revision_id is not None

                active = await store.get_active_spend_budget(UUID(project_id))
                assert active is not None
                assert active.amount == "5000"
                assert active.currency == "CNY"

            # 8. Snapshot contains spend_budget revision and proposals.
            snapshot2 = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot2.status_code == 200, snapshot2.text
            snap = snapshot2.json()
            assert snap["spend_budget"] is not None
            assert snap["spend_budget"]["amount"] == "5000"
            assert len(snap["spend_budget_proposals"]) >= 1
    finally:
        await engine.dispose()


# ── Chain 2: stale If-Match, idempotency, payload validation ──────────────────


@pytest.mark.asyncio
async def test_change_spend_budget_error_and_idempotency_contracts(
    key_prefix: str,
) -> None:
    """Stale If-Match → 412 on both accept and resolve; Idempotency-Key
    replay does not duplicate proposals/revisions/events; extra payload
    keys and invalid amount/currency are rejected with no domain mutation.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            # Post a message to get a session + assistant turn.
            r = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "What is the budget?"},
                headers=_headers(key_prefix, "setup-msg"),
            )
            assert r.status_code == 200
            sessions = await client.get(
                f"/api/projects/{project_id}/conversation/sessions"
            )
            sid = sessions.json()["sessions"][0]["id"]
            turns = await client.get(
                f"/api/projects/{project_id}/conversation/sessions/{sid}/turns"
            )
            assistant = next(
                (t for t in turns.json()["turns"] if t["role"] == "assistant"), None
            )
            assert assistant is not None

            # ── Manually create a CHANGE_SPEND_BUDGET proposal ──────────
            from aidison.domain.models import (
                ConversationActionProposal,
                ConversationActionProposalKind,
            )
            from aidison.infrastructure.store import PostgresDomainStore

            async with factory() as session:
                store = PostgresDomainStore(session)
                proposal = ConversationActionProposal(
                    turn_id=UUID(assistant["id"]),
                    kind=ConversationActionProposalKind.CHANGE_SPEND_BUDGET,
                    summary="Set CNY 8000 budget",
                    proposed_payload={
                        "amount": "8000",
                        "currency": "CNY",
                        "summary": "Research budget allocation",
                    },
                )
                await store.add_conversation_action_proposal(proposal)
                await session.commit()
                proposal_id = str(proposal.id)

            rev = await _get_project_revision(client, project_id)

            # ── A. Stale If-Match → 412 on accept ──
            r = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal_id}/accept",
                headers={
                    **_headers(key_prefix, "accept-stale"),
                    "If-Match": f'"{rev + 99}"',
                },
            )
            assert r.status_code == 412, (
                f"expected 412 for stale If-Match on accept, got {r.status_code}: {r.text}"
            )

            # ── B. Correct If-Match → 200 ───────────────────────────
            r = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal_id}/accept",
                headers={
                    **_headers(key_prefix, "accept-ok"),
                    "If-Match": f'"{rev}"',
                },
            )
            assert r.status_code == 200, r.text
            accept_body = r.json()
            result_ref = accept_body["result_ref"]
            budget_proposal_id = result_ref.removeprefix("spend_budget:")

            # ── C. Idempotent replay — same Idempotency-Key, same result ──
            replay = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal_id}/accept",
                headers={
                    **_headers(key_prefix, "accept-ok"),
                    "If-Match": f'"{rev}"',
                },
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["result_ref"] == result_ref

            # No duplicate spend budget proposals
            async with factory() as session:
                from aidison.infrastructure.store import PostgresDomainStore

                store = PostgresDomainStore(session)
                all_budget_proposals = await store.list_spend_budget_proposals(
                    UUID(project_id)
                )
                assert len(all_budget_proposals) == 1

            # ── D. Stale If-Match → 412 on resolve ──────────────────
            r = await client.post(
                f"/api/spend-budget-proposals/{budget_proposal_id}/resolve",
                json={"decision": "applied"},
                headers={
                    **_headers(key_prefix, "resolve-stale"),
                    "If-Match": f'"{rev + 99}"',
                },
            )
            assert r.status_code == 412, (
                f"expected 412 for stale If-Match on resolve, got {r.status_code}: {r.text}"
            )

            # ── E. Correct resolve → 200 ────────────────────────────
            rev_before = await _get_project_revision(client, project_id)
            r = await client.post(
                f"/api/spend-budget-proposals/{budget_proposal_id}/resolve",
                json={"decision": "applied"},
                headers={
                    **_headers(key_prefix, "resolve-ok"),
                    "If-Match": f'"{rev_before}"',
                },
            )
            assert r.status_code == 200, r.text
            resolved1 = r.json()

            # ── F. Idempotent replay of resolve ─────────────────────
            replay2 = await client.post(
                f"/api/spend-budget-proposals/{budget_proposal_id}/resolve",
                json={"decision": "applied"},
                headers={
                    **_headers(key_prefix, "resolve-ok"),
                    "If-Match": f'"{rev_before}"',
                },
            )
            assert replay2.status_code == 200, replay2.text
            assert replay2.json() == resolved1

            # No duplicate revisions
            async with factory() as session:
                from aidison.infrastructure.store import PostgresDomainStore

                store = PostgresDomainStore(session)
                all_revisions = await store.list_spend_budget_revisions(
                    UUID(project_id)
                )
                applied = [r for r in all_revisions if r.status.value == "applied"]
                assert len(applied) == 1

                # Project revision bumped exactly once from the resolve.
                project = await store.get_project(UUID(project_id))
                assert project is not None
                assert project.revision == rev_before + 1

            # ── G. Payload validation: extra keys rejected ───────────
            async with factory() as session:
                store = PostgresDomainStore(session)
                bad_proposal = ConversationActionProposal(
                    turn_id=UUID(assistant["id"]),
                    kind=ConversationActionProposalKind.CHANGE_SPEND_BUDGET,
                    summary="Bad payload",
                    proposed_payload={
                        "amount": "100",
                        "currency": "USD",
                        "summary": "ok",
                        "secret_budget_field": "should be rejected",
                    },
                )
                await store.add_conversation_action_proposal(bad_proposal)
                await session.commit()
                bad_id = str(bad_proposal.id)

            r = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{bad_id}/accept",
                headers={
                    **_headers(key_prefix, "accept-extra-keys"),
                    "If-Match": f'"{rev_before + 1}"',
                },
            )
            assert r.status_code == 422, (
                f"expected 422 for extra keys, got {r.status_code}: {r.text}"
            )

            # No domain mutation from failed validation.
            async with factory() as session:
                store = PostgresDomainStore(session)
                still_two = await store.list_spend_budget_proposals(
                    UUID(project_id)
                )
                assert len(still_two) == 1  # only the valid one exists

            # ── H. Payload validation: invalid amount rejected ───────
            async with factory() as session:
                store = PostgresDomainStore(session)
                bad_amt = ConversationActionProposal(
                    turn_id=UUID(assistant["id"]),
                    kind=ConversationActionProposalKind.CHANGE_SPEND_BUDGET,
                    summary="Bad amount",
                    proposed_payload={
                        "amount": "not-a-number",
                        "currency": "CNY",
                        "summary": "invalid",
                    },
                )
                await store.add_conversation_action_proposal(bad_amt)
                await session.commit()
                bad_amt_id = str(bad_amt.id)

            r = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{bad_amt_id}/accept",
                headers={
                    **_headers(key_prefix, "accept-bad-amount"),
                    "If-Match": f'"{rev_before + 1}"',
                },
            )
            assert r.status_code == 422, (
                f"expected 422 for invalid amount, got {r.status_code}: {r.text}"
            )

            # ── I. Payload validation: invalid currency rejected ─────
            async with factory() as session:
                store = PostgresDomainStore(session)
                bad_ccy = ConversationActionProposal(
                    turn_id=UUID(assistant["id"]),
                    kind=ConversationActionProposalKind.CHANGE_SPEND_BUDGET,
                    summary="Bad currency",
                    proposed_payload={
                        "amount": "100",
                        "currency": "INVALID_CCY",
                        "summary": "bad currency code",
                    },
                )
                await store.add_conversation_action_proposal(bad_ccy)
                await session.commit()
                bad_ccy_id = str(bad_ccy.id)

            r = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{bad_ccy_id}/accept",
                headers={
                    **_headers(key_prefix, "accept-bad-currency"),
                    "If-Match": f'"{rev_before + 1}"',
                },
            )
            assert r.status_code == 422, (
                f"expected 422 for invalid currency, got {r.status_code}: {r.text}"
            )
    finally:
        await engine.dispose()
