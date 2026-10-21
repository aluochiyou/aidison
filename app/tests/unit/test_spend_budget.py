"""Unit tests for SpendBudget domain core."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aidison.application.service import (
    DomainConflictError,
    PreconditionFailedError,
    ProjectApplication,
)
from aidison.domain.models import (
    Project,
    SpendBudgetCostItem,
    SpendBudgetImpactClassification,
    SpendBudgetProposal,
    SpendBudgetProposalStatus,
)
from tests.fakes import InMemoryDomainStore


async def _project_with_budget(
    store: InMemoryDomainStore,
    *,
    idempotency_prefix: str = "sb",
) -> tuple[ProjectApplication, Project]:
    app = ProjectApplication(store)
    project = await app.create_project(
        name="SpendBudget fixture",
        goal="Test governed spend budgets",
        idempotency_key=f"{idempotency_prefix}:project",
    )
    return app, project


@pytest.mark.asyncio
async def test_propose_spend_budget_creates_proposal() -> None:
    """A proposal is created and persisted without changing the project."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="800.00",
        currency="CNY",
        summary="Set budget to 800 CNY",
        expected_project_revision=1,
        idempotency_key="sb:propose",
    )
    assert proposal.status is SpendBudgetProposalStatus.PROPOSED
    assert proposal.amount == "800.00"
    assert proposal.currency == "CNY"
    assert proposal.basis_project_revision == 1

    # Project must not be modified by a proposal.
    current = await store.get_project(project.id)
    assert current is not None
    assert current.revision == 1
    assert current.active_spend_budget_revision_id is None


@pytest.mark.asyncio
async def test_propose_spend_budget_is_idempotent() -> None:
    """Same idempotency key returns the same proposal."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    first = await app.propose_spend_budget(
        project_id=project.id,
        amount="500.00",
        currency="USD",
        summary="Test idempotency",
        expected_project_revision=1,
        idempotency_key="sb:idempotent",
    )
    second = await app.propose_spend_budget(
        project_id=project.id,
        amount="500.00",
        currency="USD",
        summary="Test idempotency",
        expected_project_revision=1,
        idempotency_key="sb:idempotent",
    )
    assert second.id == first.id
    assert second.amount == first.amount


@pytest.mark.asyncio
async def test_propose_spend_budget_rejects_stale_revision() -> None:
    """Proposal with stale expected_project_revision must fail."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    with pytest.raises(PreconditionFailedError, match="stale"):
        await app.propose_spend_budget(
            project_id=project.id,
            amount="100.00",
            currency="EUR",
            summary="Wrong revision",
            expected_project_revision=99,
            idempotency_key="sb:stale",
        )


@pytest.mark.asyncio
async def test_propose_spend_budget_rejects_invalid_amount() -> None:
    """Non-decimal or non-positive amounts must be rejected."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    for bad_amount in ("", "abc", "-5.00", "0.00"):
        with pytest.raises(DomainConflictError, match="amount"):
            await app.propose_spend_budget(
                project_id=project.id,
                amount=bad_amount,
                currency="USD",
                summary="Invalid amount",
                expected_project_revision=1,
                idempotency_key=f"sb:bad-amount:{bad_amount}",
            )


@pytest.mark.asyncio
async def test_propose_spend_budget_rejects_invalid_currency() -> None:
    """Non-ISO-4217 currency codes must be rejected."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    for bad_currency in ("", "US", "USDD", "123"):
        with pytest.raises(DomainConflictError, match="currency"):
            await app.propose_spend_budget(
                project_id=project.id,
                amount="100.00",
                currency=bad_currency,
                summary="Invalid currency",
                expected_project_revision=1,
                idempotency_key=f"sb:bad-ccy:{bad_currency}",
            )


@pytest.mark.asyncio
async def test_resolve_spend_budget_applied_activates_revision() -> None:
    """Applying a proposal creates a SpendBudgetRevision and updates the project."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="800.00",
        currency="CNY",
        summary="Set CNY budget",
        expected_project_revision=1,
        idempotency_key="sb:propose-cny",
    )

    revision = await app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=1,
        idempotency_key="sb:resolve-cny",
    )
    assert revision.revision == 1
    assert revision.amount == "800.00"
    assert revision.currency == "CNY"
    assert revision.status is SpendBudgetProposalStatus.APPLIED

    # Project must now reference the active revision.
    current = await store.get_project(project.id)
    assert current is not None
    assert current.revision == 2
    assert current.active_spend_budget_revision_id == revision.id

    # Proposal must be marked applied.
    resolved_proposal = await store.get_spend_budget_proposal(proposal.id)
    assert resolved_proposal is not None
    assert resolved_proposal.status is SpendBudgetProposalStatus.APPLIED


@pytest.mark.asyncio
async def test_resolve_spend_budget_rejected_does_not_activate() -> None:
    """Rejecting a proposal must not create an active revision."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="300.00",
        currency="USD",
        summary="Rejected budget",
        expected_project_revision=1,
        idempotency_key="sb:propose-reject",
    )

    revision = await app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.REJECTED,
        expected_project_revision=1,
        idempotency_key="sb:reject",
    )
    assert revision.status is SpendBudgetProposalStatus.REJECTED
    assert revision.revision == 0  # rejected revisions use 0

    # Project must NOT have an active budget.
    current = await store.get_project(project.id)
    assert current is not None
    assert current.active_spend_budget_revision_id is None

    # Proposal must be marked rejected.
    resolved_proposal = await store.get_spend_budget_proposal(proposal.id)
    assert resolved_proposal is not None
    assert resolved_proposal.status is SpendBudgetProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_resolve_spend_budget_supersedes_prior_active() -> None:
    """A new applied revision supersedes the prior active one."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    # First budget: 500 CNY
    p1 = await app.propose_spend_budget(
        project_id=project.id,
        amount="500.00",
        currency="CNY",
        summary="First budget",
        expected_project_revision=1,
        idempotency_key="sb:p1",
    )
    r1 = await app.resolve_spend_budget(
        proposal_id=p1.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=1,
        idempotency_key="sb:r1",
    )
    assert r1.revision == 1

    # Project revision is now 2.
    current = await store.get_project(project.id)
    assert current is not None and current.revision == 2

    # Second budget: 800 CNY supersedes the first.
    p2 = await app.propose_spend_budget(
        project_id=project.id,
        amount="800.00",
        currency="CNY",
        summary="Second budget",
        expected_project_revision=2,
        idempotency_key="sb:p2",
    )
    r2 = await app.resolve_spend_budget(
        proposal_id=p2.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=2,
        idempotency_key="sb:r2",
    )
    assert r2.revision == 2

    # Prior active revision must be superseded.
    prior = await store.get_spend_budget_revision(r1.id)
    assert prior is not None
    assert prior.status is SpendBudgetProposalStatus.SUPERSEDED

    # New revision must be active.
    current = await store.get_project(project.id)
    assert current is not None
    assert current.active_spend_budget_revision_id == r2.id


@pytest.mark.asyncio
async def test_resolve_spend_budget_cas_prevents_double_resolve() -> None:
    """Resolving an already-resolved proposal must fail (CAS)."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="600.00",
        currency="JPY",
        summary="CAS test",
        expected_project_revision=1,
        idempotency_key="sb:cas:p",
    )
    await app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=1,
        idempotency_key="sb:cas:r1",
    )
    with pytest.raises(PreconditionFailedError, match="already resolved"):
        await app.resolve_spend_budget(
            proposal_id=proposal.id,
            decision=SpendBudgetProposalStatus.REJECTED,
            expected_project_revision=2,  # project revision advanced after first resolve
            idempotency_key="sb:cas:r2",
        )


@pytest.mark.asyncio
async def test_resolve_spend_budget_is_idempotent() -> None:
    """Resolving with the same idempotency key is idempotent."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="700.00",
        currency="GBP",
        summary="Idempotency test",
        expected_project_revision=1,
        idempotency_key="sb:idem:p",
    )
    first = await app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=1,
        idempotency_key="sb:idem:r",
    )
    second = await app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=1,
        idempotency_key="sb:idem:r",
    )
    assert second.id == first.id
    assert second.revision == first.revision


@pytest.mark.asyncio
async def test_preview_spend_budget_impact_within() -> None:
    """Cost items at or below the budget must be classified WITHIN."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="1000.00",
        currency="CNY",
        summary="Preview test budget",
        expected_project_revision=1,
        idempotency_key="sb:prev:p",
    )

    preview = await app.preview_spend_budget_impact(
        proposal_id=proposal.id,
        cost_items=[
            SpendBudgetCostItem(
                ref="bom:lens",
                amount="300.00",
                currency="CNY",
                observed_at=datetime.now(UTC),
            ),
            SpendBudgetCostItem(
                ref="bom:sensor",
                amount="1000.00",
                currency="CNY",
                observed_at=datetime.now(UTC),
            ),
        ],
        expected_project_revision=1,
        idempotency_key="sb:prev:within",
    )
    assert len(preview.lines) == 2
    classifications = {line.line_ref: line.classification for line in preview.lines}
    assert classifications["bom:lens"] is SpendBudgetImpactClassification.WITHIN
    assert classifications["bom:sensor"] is SpendBudgetImpactClassification.WITHIN


@pytest.mark.asyncio
async def test_preview_spend_budget_impact_over() -> None:
    """Cost items exceeding the budget must be classified OVER."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="500.00",
        currency="USD",
        summary="Over budget test",
        expected_project_revision=1,
        idempotency_key="sb:over:p",
    )

    preview = await app.preview_spend_budget_impact(
        proposal_id=proposal.id,
        cost_items=[
            SpendBudgetCostItem(
                ref="bom:cpu",
                amount="600.00",
                currency="USD",
                observed_at=datetime.now(UTC),
            ),
        ],
        expected_project_revision=1,
        idempotency_key="sb:over:preview",
    )
    assert len(preview.lines) == 1
    assert preview.lines[0].classification is SpendBudgetImpactClassification.OVER


@pytest.mark.asyncio
async def test_preview_spend_budget_impact_unknown_missing_price() -> None:
    """Cost items with empty amount must be classified UNKNOWN."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="500.00",
        currency="USD",
        summary="Missing price test",
        expected_project_revision=1,
        idempotency_key="sb:missing:p",
    )

    preview = await app.preview_spend_budget_impact(
        proposal_id=proposal.id,
        cost_items=[
            SpendBudgetCostItem(
                ref="bom:unknown",
                amount="",
                currency="USD",
                observed_at=datetime.now(UTC),
            ),
        ],
        expected_project_revision=1,
        idempotency_key="sb:missing:preview",
    )
    assert preview.lines[0].classification is SpendBudgetImpactClassification.UNKNOWN


@pytest.mark.asyncio
async def test_preview_spend_budget_impact_unknown_currency_mismatch() -> None:
    """Cost items in a different currency must be classified UNKNOWN."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="500.00",
        currency="CNY",
        summary="Currency mismatch test",
        expected_project_revision=1,
        idempotency_key="sb:ccy:p",
    )

    preview = await app.preview_spend_budget_impact(
        proposal_id=proposal.id,
        cost_items=[
            SpendBudgetCostItem(
                ref="bom:usd-part",
                amount="50.00",
                currency="USD",
                observed_at=datetime.now(UTC),
            ),
        ],
        expected_project_revision=1,
        idempotency_key="sb:ccy:preview",
    )
    assert preview.lines[0].classification is SpendBudgetImpactClassification.UNKNOWN


@pytest.mark.asyncio
async def test_preview_spend_budget_impact_unknown_stale() -> None:
    """Cost items without observed_at must be classified UNKNOWN (stale)."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="500.00",
        currency="EUR",
        summary="Stale price test",
        expected_project_revision=1,
        idempotency_key="sb:stale:p",
    )

    preview = await app.preview_spend_budget_impact(
        proposal_id=proposal.id,
        cost_items=[
            SpendBudgetCostItem(
                ref="bom:stale-price",
                amount="100.00",
                currency="EUR",
                observed_at=None,
            ),
        ],
        expected_project_revision=1,
        idempotency_key="sb:stale:preview",
    )
    assert preview.lines[0].classification is SpendBudgetImpactClassification.UNKNOWN


@pytest.mark.asyncio
async def test_preview_spend_budget_impact_does_not_modify_state() -> None:
    """The impact preview must be strictly read-only: no project or lock changes."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="1000.00",
        currency="CNY",
        summary="Read-only preview test",
        expected_project_revision=1,
        idempotency_key="sb:ro:p",
    )

    project_before = await store.get_project(project.id)
    assert project_before is not None

    await app.preview_spend_budget_impact(
        proposal_id=proposal.id,
        cost_items=[
            SpendBudgetCostItem(
                ref="item:1",
                amount="200.00",
                currency="CNY",
                observed_at=datetime.now(UTC),
            ),
        ],
        expected_project_revision=1,
        idempotency_key="sb:ro:preview",
    )

    # Project must be unchanged.
    project_after = await store.get_project(project.id)
    assert project_after is not None
    assert project_after.revision == project_before.revision
    assert project_after.active_spend_budget_revision_id is None

    # Proposal status must be unchanged.
    p = await store.get_spend_budget_proposal(proposal.id)
    assert p is not None
    assert p.status is SpendBudgetProposalStatus.PROPOSED


@pytest.mark.asyncio
async def test_preview_spend_budget_impact_is_idempotent() -> None:
    """Preview is idempotent across duplicate calls."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="1000.00",
        currency="CNY",
        summary="Preview idempotency",
        expected_project_revision=1,
        idempotency_key="sb:pidem:p",
    )

    items = [
        SpendBudgetCostItem(
            ref="item:1",
            amount="200.00",
            currency="CNY",
            observed_at=datetime.now(UTC),
        ),
    ]
    first = await app.preview_spend_budget_impact(
        proposal_id=proposal.id,
        cost_items=items,
        expected_project_revision=1,
        idempotency_key="sb:pidem:preview",
    )
    second = await app.preview_spend_budget_impact(
        proposal_id=proposal.id,
        cost_items=items,
        expected_project_revision=1,
        idempotency_key="sb:pidem:preview",
    )
    assert second.id == first.id


@pytest.mark.asyncio
async def test_spend_budget_proposal_fields_are_immutable() -> None:
    """SpendBudgetProposal uses FrozenModel and cannot be mutated."""
    from uuid import UUID

    proposal = SpendBudgetProposal(
        project_id=UUID("00000000-0000-0000-0000-000000000001"),
        amount="100.00",
        currency="USD",
        summary="Immutable test",
        basis_project_revision=1,
    )
    with pytest.raises(Exception):  # noqa: B017
        proposal.amount = "200.00"


@pytest.mark.asyncio
async def test_get_active_spend_budget_uses_project_pointer() -> None:
    """``get_active_spend_budget`` must read the revision via
    ``Project.active_spend_budget_revision_id``, not by status filtering."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="800.00",
        currency="CNY",
        summary="Pointer test",
        expected_project_revision=1,
        idempotency_key="sb:ptr:p",
    )
    revision = await app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=1,
        idempotency_key="sb:ptr:r",
    )
    active = await store.get_active_spend_budget(project.id)
    assert active is not None
    assert active.id == revision.id

    # Simulate a scenario where the project pointer differs from what a
    # status query would return: mutate the revision status to superseded
    # without updating the pointer.  get_active_spend_budget should STILL
    # return the pointed-to revision, proving it reads the pointer.
    mutated = revision.model_copy(
        update={"status": SpendBudgetProposalStatus.SUPERSEDED}
    )
    await store.update_spend_budget_revision(
        mutated, expected_status=SpendBudgetProposalStatus.APPLIED
    )
    # status-filtered query would now miss it, but pointer-based still works
    active = await store.get_active_spend_budget(project.id)
    assert active is not None
    assert active.id == revision.id
    # confirm the status was indeed changed to superseded
    assert active.status is SpendBudgetProposalStatus.SUPERSEDED


@pytest.mark.asyncio
async def test_second_application_new_revision_active_old_superseded() -> None:
    """After a second budget is applied the new revision is active and the
    first is superseded, both queryable through the store."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    # First budget
    p1 = await app.propose_spend_budget(
        project_id=project.id,
        amount="500.00",
        currency="CNY",
        summary="First",
        expected_project_revision=1,
        idempotency_key="sb:s2:p1",
    )
    r1 = await app.resolve_spend_budget(
        proposal_id=p1.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=1,
        idempotency_key="sb:s2:r1",
    )
    assert r1.revision == 1

    project_v2 = await store.get_project(project.id)
    assert project_v2 is not None

    # Second budget
    p2 = await app.propose_spend_budget(
        project_id=project.id,
        amount="1200.00",
        currency="CNY",
        summary="Second",
        expected_project_revision=project_v2.revision,
        idempotency_key="sb:s2:p2",
    )
    r2 = await app.resolve_spend_budget(
        proposal_id=p2.id,
        decision=SpendBudgetProposalStatus.APPLIED,
        expected_project_revision=project_v2.revision,
        idempotency_key="sb:s2:r2",
    )
    assert r2.revision == 2
    assert r2.amount == "1200.00"

    # get_active_spend_budget must return the SECOND revision
    active = await store.get_active_spend_budget(project.id)
    assert active is not None
    assert active.id == r2.id
    assert active.status is SpendBudgetProposalStatus.APPLIED

    # First revision still queryable and superseded
    first = await store.get_spend_budget_revision(r1.id)
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


@pytest.mark.asyncio
async def test_resolve_spend_budget_rejected_is_idempotent() -> None:
    """Rejected resolutions must replay correctly via idempotency receipt.

    The REJECTED branch stores the proposal UUID (not a revision UUID) in
    the command receipt.  On replay the service must fall back to
    reconstructing the revision from the resolved proposal.
    """
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store)

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="300.00",
        currency="USD",
        summary="Rejected idempotency test",
        expected_project_revision=1,
        idempotency_key="sb:rej-idem:p",
    )
    first = await app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.REJECTED,
        expected_project_revision=1,
        idempotency_key="sb:rej-idem:r",
    )
    assert first.status is SpendBudgetProposalStatus.REJECTED
    assert first.revision == 0

    # Idempotent replay must return the same reconstructed result.
    second = await app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.REJECTED,
        expected_project_revision=1,
        idempotency_key="sb:rej-idem:r",
    )
    assert second.status is SpendBudgetProposalStatus.REJECTED
    assert second.proposal_id == first.proposal_id
    assert second.amount == first.amount
    assert second.currency == first.currency

    # The proposal must remain rejected.
    p = await store.get_spend_budget_proposal(proposal.id)
    assert p is not None
    assert p.status is SpendBudgetProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_resolve_rejected_replay_rejects_non_rejected_proposal() -> None:
    """If a replay receipt references a proposal that is no longer REJECTED,
    the idempotency path must still raise.  Only genuinely REJECTED proposals
    may bypass the revision lookup."""
    store = InMemoryDomainStore()
    app, project = await _project_with_budget(store, idempotency_prefix="sb:strict")

    proposal = await app.propose_spend_budget(
        project_id=project.id,
        amount="100.00",
        currency="USD",
        summary="Will be tampered",
        expected_project_revision=1,
        idempotency_key="sb:strict:p",
    )
    # First: real rejection.
    first = await app.resolve_spend_budget(
        proposal_id=proposal.id,
        decision=SpendBudgetProposalStatus.REJECTED,
        expected_project_revision=1,
        idempotency_key="sb:strict:r",
    )
    assert first.status is SpendBudgetProposalStatus.REJECTED

    # Tamper: set the proposal back to PROPOSED in the store.
    tampered = proposal.model_copy(
        update={"status": SpendBudgetProposalStatus.PROPOSED}
    )
    store.spend_budget_proposals[proposal.id] = tampered

    # Idempotent replay must now fail because the proposal is not REJECTED.
    with pytest.raises(DomainConflictError, match="missing spend budget resolution"):
        await app.resolve_spend_budget(
            proposal_id=proposal.id,
            decision=SpendBudgetProposalStatus.REJECTED,
            expected_project_revision=1,
            idempotency_key="sb:strict:r",
        )
