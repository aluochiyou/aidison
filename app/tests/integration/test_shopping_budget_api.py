"""PostgreSQL HTTP integration coverage for read-only shopping budget routes.

Covers POST /api/projects/{project_id}/shopping/budget-summary and GET
/api/projects/{project_id}/shopping/recommendations/{bom_line_id} against a
minimal legitimate project (approved requirements, active SolutionVersion,
applied SpendBudget, offer snapshots). Both routes must be pure reads: the
shopping provider is never called and no AgentRun / purchase proposal / effect
approval / checkout handoff rows are
created.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.api.app import create_app
from aidison.application.service import ProjectApplication
from aidison.domain.models import (
    DecisionOption,
    DecisionRequest,
    OfferSnapshot,
    Project,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProposedModule,
    SolutionVersion,
    SpendBudgetProposalStatus,
    SpendBudgetRevision,
)
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import (
    AgentRunRow,
    CheckoutHandoffRow,
    EffectApprovalRow,
    PurchaseProposalRow,
)
from aidison.infrastructure.store import PostgresDomainStore
from aidison.operations.fixture import FakeShoppingProvider

pytestmark = pytest.mark.integration

_BOM_LINE_ID = "power"
_BUDGET_AMOUNT = "250.00"
_BUDGET_CURRENCY = "CNY"


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _offer(
    solution: SolutionVersion,
    *,
    offer_id: str,
    unit_price: str,
    observed_at: datetime,
    expires_at: datetime | None = None,
    currency: str = _BUDGET_CURRENCY,
    shipping: str | None = "20.00",
    tax: str | None = "10.00",
) -> OfferSnapshot:
    return OfferSnapshot(
        project_id=solution.project_id,
        solution_version_id=solution.id,
        bom_line_id=_BOM_LINE_ID,
        provider="fake-desktop",
        provider_offer_id=offer_id,
        title=f"Offer {offer_id}",
        availability="in_stock",
        unit_price=unit_price,
        currency=currency,
        shipping_estimate=shipping,
        tax_estimate=tax,
        region="CN",
        quantity_available=10,
        product_url=f"https://example.com/item?id={offer_id}",
        snapshot_hash=_hash(f"offer:{offer_id}:{unit_price}:{observed_at.isoformat()}"),
        provenance="integration-test",
        observed_at=observed_at,
        expires_at=expires_at,
    )


async def _seed_project(
    session: AsyncSession,
) -> tuple[Project, SolutionVersion, SpendBudgetRevision]:
    """Seed the minimal legitimate read-only fixture for one project."""
    store = PostgresDomainStore(session)
    project_app = ProjectApplication(store)

    project = await project_app.create_project(
        name="Shopping read-only fixture",
        goal="Verify read-only budget summary and recommendations",
        idempotency_key=f"shopping-api:project:{uuid4()}",
    )
    requirement, _ = await project_app.approve_requirements(
        project_id=project.id,
        expected_project_revision=1,
        goal="Build a budgeted power supply.",
        hard_constraints=(),
        preferences=(),
        available_resources=(),
        unknowns=(),
        modules=(),
        idempotency_key=f"shopping-api:requirements:{uuid4()}",
    )
    reshape = await project_app.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            target_goal="Build a budgeted power supply.",
            summary="Create the single power module used by this read-only fixture.",
            new_modules=(
                ProposedModule(
                    key=_BOM_LINE_ID,
                    name="Power",
                    responsibility="Supply power",
                ),
            ),
        ),
        expected_project_revision=2,
        idempotency_key=f"shopping-api:reshape-propose:{uuid4()}",
    )
    await project_app.resolve_project_reshape(
        proposal_id=reshape.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=2,
        idempotency_key=f"shopping-api:reshape-apply:{uuid4()}",
    )
    modules = await store.list_modules(project.id, requirement.id)

    decision = DecisionRequest(
        project_id=project.id,
        basis_hash=_hash("fixture-decision"),
        question="Freeze the power solution?",
        options=(
            DecisionOption(
                option_id="approve",
                label="Approve",
                summary="Approve the power solution.",
                legacy_unbound=True,
            ),
            DecisionOption(
                option_id="reject",
                label="Reject",
                summary="Reject the power solution.",
                legacy_unbound=True,
            ),
        ),
        affected_module_ids=tuple(item.id for item in modules),
    )
    solution = SolutionVersion(
        project_id=project.id,
        version=1,
        requirement_revision_id=requirement.id,
        basis_hash=_hash("fixture-solution"),
        module_snapshots=(),
        bom=({"line_id": _BOM_LINE_ID, "name": "Power", "quantity": 1},),
        approved_decision_id=decision.id,
    )
    await store.add_decision_request(decision)
    await store.add_solution_version(solution)

    current = await store.get_project(project.id)
    assert current is not None and current.revision == 3
    await store.update_project(
        current.model_copy(
            update={
                "active_solution_version_id": solution.id,
                "revision": 4,
                "updated_at": datetime.now(UTC),
            }
        ),
        expected_revision=3,
    )

    proposal = await project_app.propose_spend_budget(
        project_id=project.id,
        amount=_BUDGET_AMOUNT,
        currency=_BUDGET_CURRENCY,
        summary="Fixture budget",
        expected_project_revision=4,
        idempotency_key=f"shopping-api:budget-propose:{uuid4()}",
    )
    budget = await project_app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=4,
        idempotency_key=f"shopping-api:budget-resolve:{uuid4()}",
    )
    return project, solution, budget


async def _truncate_and_seed(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[Project, SolutionVersion]:
    async with factory() as session:
        await session.execute(text("TRUNCATE TABLE projects CASCADE"))
        await session.commit()
        project, solution, _budget = await _seed_project(session)
        return project, solution


async def _assert_read_only(
    session: AsyncSession,
    project_id: UUID,
    project_revision: int,
) -> None:
    """The read-only routes must leave no modern runtime / purchase state behind."""
    project = await PostgresDomainStore(session).get_project(project_id)
    assert project is not None
    assert project.revision == project_revision

    assert (
        await session.scalar(
            select(func.count())
            .select_from(AgentRunRow)
            .where(AgentRunRow.project_id == project_id)
        )
        == 0
    )
    assert (
        await session.scalar(
            select(func.count())
            .select_from(PurchaseProposalRow)
            .where(PurchaseProposalRow.project_id == project_id)
        )
        == 0
    )
    assert (
        await session.scalar(
            select(func.count())
            .select_from(EffectApprovalRow)
            .where(EffectApprovalRow.project_id == project_id)
        )
        == 0
    )
    assert (
        await session.scalar(
            select(func.count())
            .select_from(CheckoutHandoffRow)
            .where(CheckoutHandoffRow.project_id == project_id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_budget_summary_within_budget_and_unknown_shipping() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    provider = FakeShoppingProvider()
    api = create_app(factory, shopping_provider=provider)
    transport = httpx.ASGITransport(app=api)
    try:
        project, solution = await _truncate_and_seed(factory)
        now = datetime.now(UTC)
        known = _offer(solution, offer_id="known", unit_price="100.00", observed_at=now)
        unknown_shipping = _offer(
            solution,
            offer_id="unknown-shipping",
            unit_price="100.00",
            observed_at=now,
            shipping=None,
            tax=None,
        )
        async with factory() as session:
            store = PostgresDomainStore(session)
            await store.add_offer_snapshot(known)
            await store.add_offer_snapshot(unknown_shipping)
            await session.commit()

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            within_response = await client.post(
                f"/api/projects/{project.id}/shopping/budget-summary",
                json={
                    "selections": [
                        {"offer_snapshot_id": str(known.id), "quantity": 2}
                    ]
                },
            )
            assert within_response.status_code == 200, within_response.text
            within = within_response.json()
            assert within["classification"] == "within"
            assert within["merchandise_subtotal"] == "200.00"
            assert within["total_amount"] == "230.00"
            assert within["remaining_amount"] == "20.00"
            assert within["budget_amount"] == _BUDGET_AMOUNT
            assert within["lines"][0]["classification"] == "within"
            assert within["lines"][0]["total_amount"] == "230.00"

            unknown_response = await client.post(
                f"/api/projects/{project.id}/shopping/budget-summary",
                json={
                    "selections": [
                        {"offer_snapshot_id": str(unknown_shipping.id), "quantity": 2}
                    ]
                },
            )
            assert unknown_response.status_code == 200, unknown_response.text
            unknown = unknown_response.json()
            assert unknown["classification"] == "unknown"
            assert unknown["merchandise_subtotal"] == "200.00"
            assert unknown["total_amount"] is None
            assert unknown["remaining_amount"] is None
            assert "shipping estimate is unavailable" in unknown["lines"][0]["reason"]

        assert provider.search_called == 0
        assert provider.handoff_create_called == 0
        async with factory() as session:
            await _assert_read_only(session, project.id, project_revision=5)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recommendations_dedupe_and_rank_within_before_over_expired_unknown() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    provider = FakeShoppingProvider()
    api = create_app(factory, shopping_provider=provider)
    transport = httpx.ASGITransport(app=api)
    try:
        project, solution = await _truncate_and_seed(factory)
        now = datetime.now(UTC)
        within = _offer(solution, offer_id="within", unit_price="100.00", observed_at=now)
        within_old = _offer(
            solution,
            offer_id="within",
            unit_price="90.00",
            observed_at=now - timedelta(minutes=1),
        )
        over = _offer(solution, offer_id="over", unit_price="300.00", observed_at=now)
        expired = _offer(
            solution,
            offer_id="expired",
            unit_price="50.00",
            observed_at=now - timedelta(days=1),
            expires_at=now - timedelta(minutes=1),
            shipping=None,
            tax=None,
        )
        usd = _offer(
            solution,
            offer_id="usd",
            unit_price="200.00",
            observed_at=now,
            currency="USD",
        )
        async with factory() as session:
            store = PostgresDomainStore(session)
            for snapshot in (within, within_old, over, expired, usd):
                await store.add_offer_snapshot(snapshot)
            await session.commit()

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/projects/{project.id}/shopping/recommendations/{_BOM_LINE_ID}"
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["bom_line_id"] == _BOM_LINE_ID
            assert payload["budget_amount"] == _BUDGET_AMOUNT

            items = payload["items"]
            assert [item["offer_snapshot_id"] for item in items] == [
                str(within.id),
                str(over.id),
                str(expired.id),
                str(usd.id),
            ]
            assert [item["listed_price_classification"] for item in items] == [
                "within",
                "over",
                "unknown",
                "unknown",
            ]
            assert items[0]["final_total_known"] is True
            assert items[2]["final_total_known"] is False

            # The stale copy of the same provider offer is dropped (dedup).
            assert str(within_old.id) not in {item["offer_snapshot_id"] for item in items}
            assert len({item["offer_snapshot_id"] for item in items}) == len(items)

        assert provider.search_called == 0
        assert provider.handoff_create_called == 0
        async with factory() as session:
            await _assert_read_only(session, project.id, project_revision=5)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cross_project_selection_and_missing_bom_line_are_rejected() -> None:
    """Invalid budget-summary selections are rejected; unknown bom lines return no items.

    A budget-summary selection referencing an offer snapshot owned by another project
    must be refused (domain conflict) without advancing either project's revision and
    without creating any AgentRun / purchase proposal / effect approval / checkout handoff.
    Requesting recommendations for a bom_line that is not part of the project's active
    solution BOM is a legitimate "no recommendations" result: HTTP 200 with an empty
    items list, and still no runtime / purchase / effect / checkout rows.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    provider = FakeShoppingProvider()
    api = create_app(factory, shopping_provider=provider)
    transport = httpx.ASGITransport(app=api)
    try:
        project_a, _solution_a = await _truncate_and_seed(factory)
        async with factory() as session:
            project_b, solution_b, _budget_b = await _seed_project(session)
            foreign = _offer(
                solution_b,
                offer_id="foreign",
                unit_price="100.00",
                observed_at=datetime.now(UTC),
            )
            await PostgresDomainStore(session).add_offer_snapshot(foreign)
            await session.commit()

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            cross_response = await client.post(
                f"/api/projects/{project_a.id}/shopping/budget-summary",
                json={
                    "selections": [
                        {"offer_snapshot_id": str(foreign.id), "quantity": 1}
                    ]
                },
            )
            assert cross_response.status_code == 409, cross_response.text
            assert cross_response.json()["error"]["code"] == "DOMAIN_CONFLICT"

            missing_response = await client.get(
                f"/api/projects/{project_a.id}/shopping/recommendations/missing-bom-line"
            )
            assert missing_response.status_code == 200, missing_response.text
            assert missing_response.json()["items"] == []

        assert provider.search_called == 0
        assert provider.handoff_create_called == 0
        async with factory() as session:
            await _assert_read_only(session, project_a.id, project_revision=5)
            await _assert_read_only(session, project_b.id, project_revision=5)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_purchase_write_routes_are_gone_and_leave_no_state() -> None:
    """The product exposes shopping research, not cart/checkout side effects.

    Each retired public write route must reject even syntactically valid input
    with the same stable product-boundary response.  This is intentionally an
    HTTP seam test: hiding the controls in the browser would not protect a
    direct client from creating purchase or checkout state.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    provider = FakeShoppingProvider()
    api = create_app(factory, shopping_provider=provider)
    transport = httpx.ASGITransport(app=api)
    try:
        project, solution = await _truncate_and_seed(factory)
        offer = _offer(
            solution,
            offer_id="write-boundary",
            unit_price="100.00",
            observed_at=datetime.now(UTC),
        )
        async with factory() as session:
            await PostgresDomainStore(session).add_offer_snapshot(offer)
            await session.commit()

        headers = {
            "If-Match": '"4"',
            "Idempotency-Key": f"shopping-write-boundary:{uuid4()}",
        }
        absent_purchase = uuid4()
        absent_approval = uuid4()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [
                await client.post(
                    f"/api/projects/{project.id}/purchase-proposals",
                    headers=headers,
                    json={
                        "solution_version_id": str(solution.id),
                        "offer_snapshot_id": str(offer.id),
                        "quantity": 1,
                        "region": "CN",
                        "currency": "CNY",
                        "max_total": "100.00",
                    },
                ),
                await client.post(
                    f"/api/purchase-proposals/{absent_purchase}/confirm-lines",
                    headers=headers,
                    json={"confirmed_line_ids": ["power"]},
                ),
                await client.post(
                    f"/api/purchase-proposals/{absent_purchase}/effect-approvals",
                    headers=headers,
                ),
                await client.post(
                    f"/api/effect-approvals/{absent_approval}/resolve",
                    headers=headers,
                    json={"decision": "approved", "scope_hash": "a" * 64},
                ),
                await client.post(
                    f"/api/purchase-proposals/{absent_purchase}/checkout-handoffs",
                    headers=headers,
                    json={"effect_approval_id": str(absent_approval)},
                ),
            ]

        for response in responses:
            assert response.status_code == 410, response.text
            assert response.json()["error"]["code"] == "POLICY_DENIED"

        assert provider.search_called == 0
        assert provider.handoff_create_called == 0
        async with factory() as session:
            await _assert_read_only(session, project.id, project_revision=5)
    finally:
        await engine.dispose()
