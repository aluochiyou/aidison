"""PostgreSQL integration tests for SpendBudget domain core."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from aidison.application.service import ProjectApplication
from aidison.domain.models import (
    SpendBudgetCostItem,
    SpendBudgetImpactClassification,
    SpendBudgetProposalStatus,
)
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.store import PostgresDomainStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_spend_budget_full_lifecycle_in_postgres() -> None:
    """Propose → apply → supersede → query through the PostgreSQL store."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = str(uuid4())

    # 1. Propose a budget.
    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)

        project = await app.create_project(
            name="SpendBudget PG test",
            goal="Verify PostgreSQL persistence",
            idempotency_key=f"{key_prefix}:project",
        )
        assert project.revision == 1

        proposal = await app.propose_spend_budget(
            project_id=project.id,
            amount="1500.00",
            currency="CNY",
            summary="Integration test budget",
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:propose",
        )
        assert proposal.status is SpendBudgetProposalStatus.PROPOSED

        # Idempotent re-read.
        duplicate = await app.propose_spend_budget(
            project_id=project.id,
            amount="1500.00",
            currency="CNY",
            summary="Integration test budget",
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:propose",
        )
        assert duplicate.id == proposal.id

    # 2. New transaction: resolve to APPLIED.
    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)

        project_current = await store.get_project(project.id)
        assert project_current is not None and project_current.revision == 1

        revision = await app.resolve_spend_budget(
            proposal_id=proposal.id,
            decision=SpendBudgetProposalStatus.APPLIED,
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:resolve",
        )
        assert revision.revision == 1
        assert revision.amount == "1500.00"
        assert revision.currency == "CNY"

        p = await store.get_project(project.id)
        assert p is not None
        assert p.active_spend_budget_revision_id == revision.id

        active = await store.get_active_spend_budget(project.id)
        assert active is not None
        assert active.id == revision.id

    # 3. Supersede with a second budget.
    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)

        p_cur = await store.get_project(project.id)
        assert p_cur is not None
        assert p_cur.revision == 2  # advanced by resolve

        proposal_2 = await app.propose_spend_budget(
            project_id=project.id,
            amount="2000.00",
            currency="CNY",
            summary="Superseding budget",
            expected_project_revision=2,
            idempotency_key=f"{key_prefix}:propose2",
        )
        revision_2 = await app.resolve_spend_budget(
            proposal_id=proposal_2.id,
            decision=SpendBudgetProposalStatus.APPLIED,
            expected_project_revision=2,
            idempotency_key=f"{key_prefix}:resolve2",
        )
        assert revision_2.revision == 2

        p_final = await store.get_project(project.id)
        assert p_final is not None
        assert p_final.active_spend_budget_revision_id == revision_2.id

        # The prior revision must now be superseded.
        prior = await store.get_spend_budget_revision(revision.id)
        assert prior is not None
        assert prior.status is SpendBudgetProposalStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_spend_budget_impact_preview_in_postgres() -> None:
    """Preview computation is persisted and queryable."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = str(uuid4())

    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)

        project = await app.create_project(
            name="Preview PG test",
            goal="Verify preview persistence",
            idempotency_key=f"{key_prefix}:project",
        )
        proposal = await app.propose_spend_budget(
            project_id=project.id,
            amount="1000.00",
            currency="USD",
            summary="Preview test budget",
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:propose",
        )
        preview = await app.preview_spend_budget_impact(
            proposal_id=proposal.id,
            cost_items=[
                SpendBudgetCostItem(
                    ref="line-1",
                    amount="500.00",
                    currency="USD",
                    observed_at=datetime.now(UTC),
                ),
                SpendBudgetCostItem(
                    ref="line-2",
                    amount="1200.00",
                    currency="USD",
                    observed_at=datetime.now(UTC),
                ),
                SpendBudgetCostItem(
                    ref="line-3",
                    amount="",
                    currency="USD",
                    observed_at=datetime.now(UTC),
                ),
            ],
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:preview",
        )
        assert len(preview.lines) == 3
        by_ref = {line.line_ref: line.classification for line in preview.lines}
        assert by_ref["line-1"] is SpendBudgetImpactClassification.WITHIN
        assert by_ref["line-2"] is SpendBudgetImpactClassification.OVER
        assert by_ref["line-3"] is SpendBudgetImpactClassification.UNKNOWN

        stored = await store.get_spend_budget_impact_preview(preview.id)
        assert stored is not None
        assert stored.id == preview.id

        previews = await store.list_spend_budget_impact_previews(project.id)
        assert len(previews) == 1
        assert previews[0].id == preview.id


@pytest.mark.asyncio
async def test_spend_budget_idempotency_across_transactions() -> None:
    """Idempotency receipts survive across separate PostgreSQL transactions."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = str(uuid4())

    proposal_id = None

    # First transaction: propose.
    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)
        project = await app.create_project(
            name="Idempotency PG test",
            goal="Cross-tx idempotency",
            idempotency_key=f"{key_prefix}:project",
        )
        proposal = await app.propose_spend_budget(
            project_id=project.id,
            amount="777.00",
            currency="EUR",
            summary="Cross-tx idempotent proposal",
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:propose",
        )
        proposal_id = proposal.id

    # Second transaction: same idempotency key returns the same proposal.
    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)
        duplicate = await app.propose_spend_budget(
            project_id=project.id,
            amount="777.00",
            currency="EUR",
            summary="Cross-tx idempotent proposal",
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:propose",
        )
        assert duplicate.id == proposal_id


@pytest.mark.asyncio
async def test_spend_budget_revision_rows_persisted() -> None:
    """SpendBudgetRevision rows are durably written to PostgreSQL."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = str(uuid4())

    revision_id: UUID | None = None

    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)
        project = await app.create_project(
            name="Revision persistence test",
            goal="Verify revision rows",
            idempotency_key=f"{key_prefix}:project",
        )
        proposal = await app.propose_spend_budget(
            project_id=project.id,
            amount="999.99",
            currency="JPY",
            summary="Revision row test",
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:propose",
        )
        revision = await app.resolve_spend_budget(
            proposal_id=proposal.id,
            decision=SpendBudgetProposalStatus.APPLIED,
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:resolve",
        )
        revision_id = revision.id

    # Verify the revision row exists in a separate session.
    async with factory() as session:
        store = PostgresDomainStore(session)
        stored = await store.get_spend_budget_revision(revision_id)
        assert stored is not None
        assert stored.amount == "999.99"
        assert stored.currency == "JPY"
        assert stored.revision == 1


@pytest.mark.asyncio
async def test_spend_budget_rejected_does_not_persist_as_active() -> None:
    """A rejected proposal must leave no active budget."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = str(uuid4())

    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)
        project = await app.create_project(
            name="Reject PG test",
            goal="Verify rejection",
            idempotency_key=f"{key_prefix}:project",
        )
        proposal = await app.propose_spend_budget(
            project_id=project.id,
            amount="50.00",
            currency="USD",
            summary="Rejected PG",
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:propose",
        )
        await app.resolve_spend_budget(
            proposal_id=proposal.id,
            decision=SpendBudgetProposalStatus.REJECTED,
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:reject",
        )

        active = await store.get_active_spend_budget(project.id)
        assert active is None

        p = await store.get_project(project.id)
        assert p is not None
        assert p.active_spend_budget_revision_id is None


@pytest.mark.asyncio
async def test_second_application_pointer_based_read_in_postgres() -> None:
    """After a second budget is applied, ``get_active_spend_budget`` must
    return the new revision via ``Project.active_spend_budget_revision_id``,
    and the first revision must be superseded but still queryable."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = str(uuid4())

    r1_id: UUID | None = None

    # First application
    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)

        project = await app.create_project(
            name="Pointer PG test",
            goal="Verify pointer-based active budget read",
            idempotency_key=f"{key_prefix}:project",
        )
        p1 = await app.propose_spend_budget(
            project_id=project.id,
            amount="500.00",
            currency="CNY",
            summary="First budget",
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:p1",
        )
        r1 = await app.resolve_spend_budget(
            proposal_id=p1.id,
            decision=SpendBudgetProposalStatus.APPLIED,
            expected_project_revision=1,
            idempotency_key=f"{key_prefix}:r1",
        )
        assert r1.revision == 1
        r1_id = r1.id

        # get_active_spend_budget must return first revision
        active = await store.get_active_spend_budget(project.id)
        assert active is not None
        assert active.id == r1.id

    # Second application in a separate transaction
    async with factory() as session:
        store = PostgresDomainStore(session)
        app = ProjectApplication(store)

        p_cur = await store.get_project(project.id)
        assert p_cur is not None

        p2 = await app.propose_spend_budget(
            project_id=project.id,
            amount="1200.00",
            currency="CNY",
            summary="Second budget",
            expected_project_revision=p_cur.revision,
            idempotency_key=f"{key_prefix}:p2",
        )
        r2 = await app.resolve_spend_budget(
            proposal_id=p2.id,
            decision=SpendBudgetProposalStatus.APPLIED,
            expected_project_revision=p_cur.revision,
            idempotency_key=f"{key_prefix}:r2",
        )
        assert r2.revision == 2
        assert r2.amount == "1200.00"

        # get_active_spend_budget must return the SECOND revision via pointer
        active = await store.get_active_spend_budget(project.id)
        assert active is not None
        assert active.id == r2.id
        assert active.status is SpendBudgetProposalStatus.APPLIED

        # First revision superseded and still queryable
        first = await store.get_spend_budget_revision(r1_id)
        assert first is not None
        assert first.status is SpendBudgetProposalStatus.SUPERSEDED
        assert first.amount == "500.00"

        # Project pointer must be the new revision
        p_final = await store.get_project(project.id)
        assert p_final is not None
        assert p_final.active_spend_budget_revision_id == r2.id

        # list_spend_budget_revisions returns both in order
        all_revs = await store.list_spend_budget_revisions(project.id)
        assert len(all_revs) == 2
        assert [r.revision for r in all_revs] == [1, 2]
