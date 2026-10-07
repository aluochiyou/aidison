"""Conversation accept-path governance: rewrite and selection never apply directly."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from aidison.application.conversation import (
    ConversationApplication,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationPreconditionFailedError,
    _build_conversation_context,
)
from aidison.application.service import ProjectApplication
from aidison.domain.models import (
    Candidate,
    ConversationActionProposal,
    ConversationActionProposalKind,
    ConversationTurn,
    ConversationTurnRole,
    Module,
    RequirementRevision,
    RequirementsChangeProposalStatus,
    ResearchStrategyProposal,
    ResearchStrategyTask,
    SolutionSnapshot,
    UserAdjustmentKind,
)
from aidison.research.defaults import (
    RESEARCH_DEFAULT_MAX_DURATION_SECONDS,
    RESEARCH_DEFAULT_MAX_TOKEN_BUDGET,
)
from tests.fakes import InMemoryDomainStore, bootstrap_initial_modules


def _noop_model_factory() -> MagicMock:
    model = MagicMock()
    model.bind = MagicMock()
    return model


async def _strategy_planner(
    *,
    objective: str,
    requirement: RequirementRevision,
    scope_modules: Sequence[Module],
    research_depth: str = "standard",
    source_strategy: str | None = None,
) -> ResearchStrategyProposal:
    del requirement, research_depth
    tasks = tuple(
        ResearchStrategyTask(
            task_key=f"{module.key}.research",
            title=f"Research {module.name}",
            objective=f"Find source-backed constraints for {module.name}: {objective}",
            module_ids=(module.id,),
            depends_on_task_keys=(() if index == 0 else (f"{scope_modules[0].key}.research",)),
            priority="must",
            expected_outputs=("evidence", "candidate"),
            stop_conditions=("Record an evidence-backed option or an explicit gap.",),
        )
        for index, module in enumerate(scope_modules)
    )
    return ResearchStrategyProposal(
        summary="Fixture strategy follows the selected modules in dependency order.",
        scope_module_ids=tuple(module.id for module in scope_modules),
        tasks=tasks,
        source_strategy=source_strategy or "primary",
    )


async def _project_and_conv(store: InMemoryDomainStore) -> tuple[object, ConversationApplication]:
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Conv governance fixture",
        goal="Verify conversation accept boundaries",
        idempotency_key="conv-gov:project",
    )
    conv = ConversationApplication(
        store_factory=lambda: store,
        model_factory=_noop_model_factory,
        research_strategy_planner=_strategy_planner,
    )
    return project, conv


async def _proposal(
    store: InMemoryDomainStore,
    project_id: object,
    conv: ConversationApplication,
    kind: ConversationActionProposalKind,
    payload: dict,
) -> ConversationActionProposal:
    session = await conv.ensure_active_session(project_id)
    turn = ConversationTurn(
        session_id=session.id,
        sequence=1,
        role=ConversationTurnRole.ASSISTANT,
        content="Suggestion ready.",
        model_profile="deepseek-v4-pro",
    )
    await store.add_conversation_turn(turn)
    proposal = ConversationActionProposal(
        turn_id=turn.id,
        kind=kind,
        summary="Conversation suggestion",
        proposed_payload=payload,
    )
    await store.add_conversation_action_proposal(proposal)
    return proposal


@pytest.mark.asyncio
async def test_rewrite_requirements_accept_creates_unapplied_proposal() -> None:
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    project, _ = await bootstrap_initial_modules(
        store=store,
        application=ProjectApplication(store),
        project=project,
        modules=({"key": "core", "name": "Core", "responsibility": "Core logic"},),
        idempotency_key_prefix="rewrite-requirements",
    )
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.REWRITE_REQUIREMENTS,
        {
            "goal": "A rewritten project goal",
            "hard_constraints": ["cheap"],
            "modules": [{"key": "core", "name": "Core", "responsibility": "Core logic"}],
        },
    )
    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=project.revision,
    )
    assert result["result_ref"].startswith("requirements_change:")

    # A reviewable proposal exists and nothing was approved yet.
    stored = await store.get_requirements_change_proposal(
        UUID(result["result_ref"].removeprefix("requirements_change:"))
    )
    assert stored is not None
    assert stored.status is RequirementsChangeProposalStatus.PROPOSED


@pytest.mark.asyncio
async def test_rewrite_preserves_clarified_context_without_budget_side_effect() -> None:
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    project, _ = await bootstrap_initial_modules(
        store=store,
        application=ProjectApplication(store),
        project=project,
        modules=({"key": "core", "name": "Core", "responsibility": "Core logic"},),
        idempotency_key_prefix="rewrite-context",
    )
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.REWRITE_REQUIREMENTS,
        {
            "goal": "A rewritten project goal",
            "hard_constraints": ["cheap"],
            "available_resources": ["已有底座"],
            "usage_context": "用于流水线分拣",
            "budget_context": "成本控制在 2 万元以内",
            "skill_context": "团队具备机械与电气基础",
            "modules": [{"key": "core", "name": "Core", "responsibility": "Core logic"}],
        },
    )
    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=project.revision,
    )
    assert result["result_ref"].startswith("requirements_change:")

    stored = await store.get_requirements_change_proposal(
        UUID(result["result_ref"].removeprefix("requirements_change:"))
    )
    assert stored is not None
    assert stored.usage_context == "用于流水线分拣"
    assert stored.budget_context == "成本控制在 2 万元以内"
    assert stored.skill_context == "团队具备机械与电气基础"
    assert stored.available_resources == ("已有底座",)

    # Budget here is only a requirement constraint: accepting the rewrite must
    # never create a SpendBudgetRevision, proposal, or any other side effect.
    assert store.spend_budget_proposals == {}
    assert store.spend_budget_revisions == {}
    assert stored.target_goal == "A rewritten project goal"
    assert len(await store.list_requirement_revisions(project.id)) == 1
    assert len(await store.list_modules(project.id)) == 1
    project_after = await store.get_project(project.id)
    assert project_after is not None and project_after.revision == project.revision
    assert "requirements_change.proposed" in {event[2] for event in store.events}
    assert [event[2] for event in store.events].count("requirements.approved") == 1


@pytest.mark.asyncio
async def test_change_selection_accept_records_adjustment_and_preview() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, conv = await _project_and_conv(store)
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "power", "name": "Power", "responsibility": "Supply power"},),
        idempotency_key_prefix="conv-gov:selection",
    )
    module = modules[0]
    candidate = Candidate(
        project_id=project.id,
        module_id=module.id,
        name="Preferred battery",
        description="User preferred battery route",
    )
    await store.add_candidates((candidate,))

    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.CHANGE_SELECTION,
        {"module_id": str(module.id), "candidate_id": str(candidate.id)},
    )
    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=project.revision,
    )
    assert result["result_ref"].startswith("adjustment:")

    adjustments = await store.list_user_adjustments(project.id)
    assert len(adjustments) == 1
    assert adjustments[0].kind is UserAdjustmentKind.SELECT_CANDIDATE
    previews = await store.list_change_impact_previews(project.id)
    assert len(previews) == 1
    assert previews[0].affected_module_ids == (module.id,)
    assert "change_impact_preview.generated" in {event[2] for event in store.events}


@pytest.mark.asyncio
async def test_change_selection_locked_module_is_rejected() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, conv = await _project_and_conv(store)
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "power", "name": "Power", "responsibility": "Supply power"},),
        idempotency_key_prefix="conv-gov:selection-locked",
    )
    module = modules[0]
    locked = Candidate(
        project_id=project.id,
        module_id=module.id,
        name="Locked battery",
        description="Locked route",
    )
    alternate = Candidate(
        project_id=project.id,
        module_id=module.id,
        name="Alternate battery",
        description="Alternative route",
    )
    await store.add_candidates((locked, alternate))
    await app.create_selection_lock(
        project_id=project.id,
        module_id=module.id,
        candidate_id=locked.id,
        reason="Keep the locked route",
        expected_project_revision=project.revision,
        idempotency_key="conv-gov:lock",
    )
    current = await store.get_project(project.id)
    assert current is not None

    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.CHANGE_SELECTION,
        {"module_id": str(module.id), "candidate_id": str(alternate.id)},
    )
    with pytest.raises(ConversationConflictError, match="prevents changing this module"):
        await conv.accept_action_proposal(
            proposal_id=proposal.id,
            project_id=project.id,
            expected_project_revision=current.revision,
        )
    # The proposal stays proposed and nothing was written.
    stored = await store.get_conversation_action_proposal(proposal.id)
    assert stored is not None and stored.status.value == "proposed"
    assert len(await store.list_user_adjustments(project.id)) == 0


@pytest.mark.asyncio
async def test_context_uses_newest_snapshots_in_stable_created_time_order() -> None:
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    session = await conv.ensure_active_session(project.id)
    baseline = datetime(2026, 1, 1, tzinfo=UTC)

    # Insert in reverse chronology to ensure the assembler does not depend on
    # dict/database return order or UUID ordering.
    for index in range(6, 0, -1):
        await store.add_solution_snapshot(
            SolutionSnapshot(
                project_id=project.id,
                label=f"snapshot-{index}",
                created_at=baseline + timedelta(minutes=index),
            )
        )

    context = await _build_conversation_context(store, project.id, session.id, max_chars=8_000)
    snapshot_section = context.split("Recent snapshots", maxsplit=1)[1].split("Budget:", 1)[0]
    assert "snapshot-6" in snapshot_section
    assert "snapshot-2" in snapshot_section
    assert "snapshot-1" not in snapshot_section
    assert snapshot_section.index("snapshot-6") < snapshot_section.index("snapshot-5")


@pytest.mark.asyncio
async def test_context_never_exceeds_its_hard_character_budget() -> None:
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    session = await conv.ensure_active_session(project.id)
    await store.add_conversation_turn(
        ConversationTurn(
            session_id=session.id,
            sequence=1,
            role=ConversationTurnRole.USER,
            content="x" * 1_000,
            idempotency_key="context-hard-budget",
        )
    )

    context = await _build_conversation_context(store, project.id, session.id, max_chars=200)
    assert len(context) <= 200


@pytest.mark.asyncio
async def test_recorded_summary_replaces_its_source_turns_in_context() -> None:
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    session = await conv.ensure_active_session(project.id)
    for sequence, role, content in (
        (1, ConversationTurnRole.USER, "old user question"),
        (2, ConversationTurnRole.ASSISTANT, "old assistant answer"),
        (3, ConversationTurnRole.USER, "new user question"),
    ):
        await store.add_conversation_turn(
            ConversationTurn(
                session_id=session.id,
                sequence=sequence,
                role=role,
                content=content,
                idempotency_key=f"summary-turn-{sequence}"
                if role is ConversationTurnRole.USER
                else None,
                model_profile="deepseek-v4-pro"
                if role is ConversationTurnRole.ASSISTANT
                else "human",
            )
        )

    summary = await conv.record_context_summary(
        project_id=project.id,
        session_id=session.id,
        source_start_sequence=1,
        source_end_sequence=2,
        content="用户确认旧问题已经解决。",
        summarizer_profile="deepseek-v4-pro",
        idempotency_key="context-summary:one",
    )
    replay = await conv.record_context_summary(
        project_id=project.id,
        session_id=session.id,
        source_start_sequence=1,
        source_end_sequence=2,
        content="用户确认旧问题已经解决。",
        summarizer_profile="deepseek-v4-pro",
        idempotency_key="context-summary:one",
    )
    assert replay.id == summary.id

    context = await _build_conversation_context(
        store, project.id, session.id, max_turns=20, max_chars=8_000
    )
    assert "Context summary (turns 1-2): 用户确认旧问题已经解决。" in context
    assert "old user question" not in context
    assert "old assistant answer" not in context
    assert "new user question" in context


@pytest.mark.asyncio
async def test_context_summary_cannot_target_another_projects_session() -> None:
    store = InMemoryDomainStore()
    project_a, conv = await _project_and_conv(store)
    project_b = await ProjectApplication(store).create_project(
        name="Other context project",
        goal="Must remain isolated",
        idempotency_key="context-summary:other-project",
    )
    session_b = await conv.ensure_active_session(project_b.id)

    with pytest.raises(ConversationNotFoundError, match="conversation session not found"):
        await conv.record_context_summary(
            project_id=project_a.id,
            session_id=session_b.id,
            source_start_sequence=1,
            source_end_sequence=1,
            content="must not cross projects",
            summarizer_profile="deepseek-v4-pro",
            idempotency_key="context-summary:cross-project",
        )


@pytest.mark.asyncio
async def test_restore_solution_snapshot_accept_restores_config() -> None:
    """Accepting a restore_solution_snapshot proposal calls
    restore_solution_snapshot on the domain layer."""
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)

    # Approve requirements so we have modules for snapshot
    app = ProjectApplication(store)
    project, _ = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "mcu", "name": "MCU", "responsibility": "Control"},),
        idempotency_key_prefix="rs-test",
    )

    # Save a known state as snapshot
    saved = await app.save_solution_snapshot(
        project_id=project.id,
        label="Baseline",
        expected_project_revision=project.revision,
        idempotency_key="rs-test:save",
    )

    proposal = await _proposal(
        store,
        project.id,
        conv,
        kind=ConversationActionProposalKind.RESTORE_SOLUTION_SNAPSHOT,
        payload={"snapshot_id": str(saved.id)},
    )
    current = await store.get_project(project.id)
    assert current is not None

    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=current.revision,
    )
    assert result["result_ref"].startswith("solution_snapshot:")
    assert result["result_ref"] == f"solution_snapshot:{saved.id}"

    # Proposal was marked accepted
    stored = await store.get_conversation_action_proposal(proposal.id)
    assert stored is not None
    assert str(stored.status) == "accepted"
    assert stored.resolution_turn_id is not None


@pytest.mark.asyncio
async def test_restore_solution_snapshot_stale_revision_rejected() -> None:
    """Stale If-Match on restore_solution_snapshot accept returns 412."""
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)

    app = ProjectApplication(store)
    project, _ = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "mcu", "name": "MCU", "responsibility": "Control"},),
        idempotency_key_prefix="rs-stale",
    )
    saved = await app.save_solution_snapshot(
        project_id=project.id,
        label="Baseline",
        expected_project_revision=project.revision,
        idempotency_key="rs-stale:save",
    )
    proposal = await _proposal(
        store,
        project.id,
        conv,
        kind=ConversationActionProposalKind.RESTORE_SOLUTION_SNAPSHOT,
        payload={"snapshot_id": str(saved.id)},
    )

    with pytest.raises(ConversationPreconditionFailedError, match="stale"):
        await conv.accept_action_proposal(
            proposal_id=proposal.id,
            project_id=project.id,
            expected_project_revision=999,
        )


@pytest.mark.asyncio
async def test_restore_solution_snapshot_rejects_cross_project_snapshot_without_side_effects() -> (
    None
):
    """A conversation may restore only a snapshot owned by its own project."""
    store = InMemoryDomainStore()
    project_a, conv = await _project_and_conv(store)
    app = ProjectApplication(store)
    project_b = await app.create_project(
        name="Other project",
        goal="Must never be restored from project A conversation",
        idempotency_key="rs-cross:project-b",
    )

    for project, key in ((project_a, "a"), (project_b, "b")):
        project, _ = await bootstrap_initial_modules(
            store=store,
            application=app,
            project=project,
            modules=({"key": "mcu", "name": "MCU", "responsibility": "Control"},),
            idempotency_key_prefix=f"rs-cross:{key}",
        )
        await app.save_solution_snapshot(
            project_id=project.id,
            label=f"{key} baseline",
            expected_project_revision=project.revision,
            idempotency_key=f"rs-cross:snapshot:{key}",
        )

    foreign_snapshot = (await store.list_solution_snapshots(project_b.id))[0]
    proposal = await _proposal(
        store,
        project_a.id,
        conv,
        ConversationActionProposalKind.RESTORE_SOLUTION_SNAPSHOT,
        {"snapshot_id": str(foreign_snapshot.id)},
    )
    project_a_before = await store.get_project(project_a.id)
    project_b_before = await store.get_project(project_b.id)
    assert project_a_before is not None and project_b_before is not None
    event_count_before = len(store.events)

    with pytest.raises(
        ConversationConflictError,
        match="solution snapshot is unavailable for this project",
    ):
        await conv.accept_action_proposal(
            proposal_id=proposal.id,
            project_id=project_a.id,
            expected_project_revision=project_a_before.revision,
        )

    stored = await store.get_conversation_action_proposal(proposal.id)
    project_a_after = await store.get_project(project_a.id)
    project_b_after = await store.get_project(project_b.id)
    assert stored is not None and stored.status.value == "proposed"
    assert stored.resolution_turn_id is None
    assert project_a_after == project_a_before
    assert project_b_after == project_b_before
    assert len(store.events) == event_count_before
    assert f"conversation.accept:{proposal.id}" not in store.receipts

    missing = await _proposal(
        store,
        project_a.id,
        conv,
        ConversationActionProposalKind.RESTORE_SOLUTION_SNAPSHOT,
        {"snapshot_id": str(uuid4())},
    )
    with pytest.raises(
        ConversationConflictError,
        match="solution snapshot is unavailable for this project",
    ):
        await conv.accept_action_proposal(
            proposal_id=missing.id,
            project_id=project_a.id,
            expected_project_revision=project_a_before.revision,
        )
    missing_after = await store.get_conversation_action_proposal(missing.id)
    assert missing_after is not None and missing_after.status.value == "proposed"
    assert missing_after.resolution_turn_id is None
    assert len(store.events) == event_count_before
    assert f"conversation.accept:{missing.id}" not in store.receipts


@pytest.mark.asyncio
async def test_restore_solution_snapshot_rejects_extra_payload_fields() -> None:
    """Extra keys in payload must be rejected at validation time."""
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    proposal = await _proposal(
        store,
        project.id,
        conv,
        kind=ConversationActionProposalKind.RESTORE_SOLUTION_SNAPSHOT,
        payload={
            "snapshot_id": "00000000-0000-0000-0000-000000000001",
            "unauthorized": "field",
        },
    )
    current = await store.get_project(project.id)
    assert current is not None

    with pytest.raises(ValueError, match="unexpected keys"):
        await conv.accept_action_proposal(
            proposal_id=proposal.id,
            project_id=project.id,
            expected_project_revision=current.revision,
        )


# ── reject_action_proposal ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_action_proposal_persists_rejected_without_side_effects() -> None:
    """Rejecting a spend-budget proposal executes no command and touches nothing."""
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.CHANGE_SPEND_BUDGET,
        {"amount": "800.00", "currency": "CNY", "summary": "Raise the cap"},
    )
    before = await store.get_project(project.id)
    assert before is not None

    result = await conv.reject_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=before.revision,
        idempotency_key="reject:unit",
    )
    assert result["result_ref"] == f"rejected:{proposal.id}"

    stored = await store.get_conversation_action_proposal(proposal.id)
    assert stored is not None
    assert str(stored.status) == "rejected"
    assert stored.resolution_turn_id is not None
    assert stored.resolved_at is not None

    # No command was executed: no budget proposal, no project revision bump.
    assert len(await store.list_spend_budget_proposals(project.id)) == 0
    after = await store.get_project(project.id)
    assert after is not None and after.revision == before.revision

    # A system resolution turn was recorded in the proposal's session.
    turn = await store.get_conversation_turn(proposal.turn_id)
    assert turn is not None
    turns = await store.list_conversation_turns(turn.session_id)
    assert turns[-1].content.startswith("Action proposal")
    assert turns[-1].model_profile == "system"

    assert "conversation.action_proposal_rejected" in {event[2] for event in store.events}
    assert f"conversation.reject:{project.id}:{proposal.id}:reject:unit" in store.receipts


@pytest.mark.asyncio
async def test_reject_action_proposal_replays_idempotently() -> None:
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {"objective": "Compare options"},
    )
    current = await store.get_project(project.id)
    assert current is not None

    first = await conv.reject_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=current.revision,
        idempotency_key="reject:replay",
    )
    events_after_first = len(store.events)
    replay = await conv.reject_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=current.revision,
        idempotency_key="reject:replay",
    )
    assert replay["result_ref"] == first["result_ref"] == f"rejected:{proposal.id}"
    assert str(replay["proposal"].status) == "rejected"
    # Replay adds no duplicate resolution turn, event, or receipt.
    assert len(store.events) == events_after_first
    turn = await store.get_conversation_turn(proposal.turn_id)
    assert turn is not None
    turns = [
        t
        for t in await store.list_conversation_turns(turn.session_id)
        if t.model_profile == "system"
    ]
    assert len(turns) == 1
    assert f"conversation.reject:{project.id}:{proposal.id}:reject:replay" in store.receipts


@pytest.mark.asyncio
async def test_reject_different_key_on_settled_proposal_conflicts() -> None:
    """Same key replays the REJECTED receipt; a different key then conflicts."""
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {"objective": "Compare"},
    )
    current = await store.get_project(project.id)
    assert current is not None

    first = await conv.reject_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=current.revision,
        idempotency_key="reject:key-a",
    )
    assert first["result_ref"] == f"rejected:{proposal.id}"

    # Replay with the same key but a different (stale) If-Match revision still
    # returns the existing REJECTED result: the command hash does not include
    # the revision, so the receipt is authoritative.
    replay = await conv.reject_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=999,
        idempotency_key="reject:key-a",
    )
    assert replay["result_ref"] == f"rejected:{proposal.id}"

    # A different key on the now-settled proposal conflicts and writes nothing.
    with pytest.raises(ConversationConflictError, match="not in proposed state"):
        await conv.reject_action_proposal(
            proposal_id=proposal.id,
            project_id=project.id,
            expected_project_revision=999,
            idempotency_key="reject:key-b",
        )

    turn = await store.get_conversation_turn(proposal.turn_id)
    assert turn is not None
    system_turns = [
        t
        for t in await store.list_conversation_turns(turn.session_id)
        if t.model_profile == "system"
    ]
    assert len(system_turns) == 1
    assert f"conversation.reject:{project.id}:{proposal.id}:reject:key-b" not in store.receipts


@pytest.mark.asyncio
async def test_reject_action_proposal_requires_proposed_state() -> None:
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    project, _ = await bootstrap_initial_modules(
        store=store,
        application=ProjectApplication(store),
        project=project,
        modules=({"key": "core", "name": "Core", "responsibility": "Core logic"},),
        idempotency_key_prefix="reject-proposed-state",
    )
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.REWRITE_REQUIREMENTS,
        {
            "goal": "A rewritten project goal",
            "modules": [{"key": "core", "name": "Core", "responsibility": "Core logic"}],
        },
    )
    current = await store.get_project(project.id)
    assert current is not None
    await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=current.revision,
    )

    with pytest.raises(ConversationConflictError, match="not in proposed state"):
        await conv.reject_action_proposal(
            proposal_id=proposal.id,
            project_id=project.id,
            expected_project_revision=current.revision,
            idempotency_key="reject:settled",
        )


@pytest.mark.asyncio
async def test_reject_action_proposal_is_project_scoped() -> None:
    store = InMemoryDomainStore()
    project_a, conv = await _project_and_conv(store)
    project_b = await ProjectApplication(store).create_project(
        name="Other reject project",
        goal="Must stay isolated",
        idempotency_key="reject-scope:project-b",
    )
    proposal = await _proposal(
        store,
        project_b.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {"objective": "Compare"},
    )
    current_a = await store.get_project(project_a.id)
    assert current_a is not None

    with pytest.raises(ConversationConflictError, match="does not belong to project"):
        await conv.reject_action_proposal(
            proposal_id=proposal.id,
            project_id=project_a.id,
            expected_project_revision=current_a.revision,
            idempotency_key="reject:scoped",
        )
    stored = await store.get_conversation_action_proposal(proposal.id)
    assert stored is not None and stored.status.value == "proposed"


@pytest.mark.asyncio
async def test_reject_action_proposal_guards_stale_revision() -> None:
    store = InMemoryDomainStore()
    project, conv = await _project_and_conv(store)
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {"objective": "Compare"},
    )

    with pytest.raises(ConversationPreconditionFailedError, match="stale"):
        await conv.reject_action_proposal(
            proposal_id=proposal.id,
            project_id=project.id,
            expected_project_revision=999,
            idempotency_key="reject:stale",
        )
    stored = await store.get_conversation_action_proposal(proposal.id)
    assert stored is not None and stored.status.value == "proposed"
    assert f"conversation.reject:{project.id}:{proposal.id}:reject:stale" not in store.receipts


@pytest.mark.asyncio
async def test_reject_action_proposal_does_not_touch_selection_lock() -> None:
    """Rejecting a change_selection proposal never runs the adjustment command."""
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, conv = await _project_and_conv(store)
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "power", "name": "Power", "responsibility": "Supply power"},),
        idempotency_key_prefix="reject-lock",
    )
    module = modules[0]
    locked = Candidate(
        project_id=project.id,
        module_id=module.id,
        name="Locked battery",
        description="Locked route",
    )
    await store.add_candidates((locked,))
    await app.create_selection_lock(
        project_id=project.id,
        module_id=module.id,
        candidate_id=locked.id,
        reason="Keep the locked route",
        expected_project_revision=project.revision,
        idempotency_key="reject-lock:lock",
    )
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.CHANGE_SELECTION,
        {"module_id": str(module.id), "candidate_id": str(locked.id)},
    )
    current = await store.get_project(project.id)
    assert current is not None

    # Reject succeeds while the lock is active because no command runs; the
    # lock stays intact and no adjustment is recorded.
    result = await conv.reject_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=current.revision,
        idempotency_key="reject:lock",
    )
    assert result["result_ref"] == f"rejected:{proposal.id}"
    locks = await store.list_selection_locks(project.id)
    assert all(lock.active for lock in locks)
    assert len(await store.list_user_adjustments(project.id)) == 0
    after = await store.get_project(project.id)
    assert after is not None and after.revision == current.revision


# ── scoped start_research (re-research specific modules) ───────────────────


async def _project_with_modules(
    store: InMemoryDomainStore, *, module_keys: tuple[str, ...]
) -> tuple[object, ConversationApplication, list[object]]:
    app = ProjectApplication(store)
    project, conv = await _project_and_conv(store)
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=tuple(
            {"key": key, "name": key.capitalize(), "responsibility": f"{key} logic"}
            for key in module_keys
        ),
        idempotency_key_prefix=f"conv-gov:research:{module_keys}",
    )
    return project, conv, list(modules)


@pytest.mark.asyncio
async def test_start_research_module_ids_restrict_execution_plan_scope() -> None:
    """Accepting a scoped re-research request creates an approval-gated plan."""
    store = InMemoryDomainStore()
    project, conv, modules = await _project_with_modules(store, module_keys=("power", "mcu"))
    target = modules[0]
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {
            "module_ids": [str(target.id)],
            "objective": "Re-research only the power module",
        },
    )
    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=project.revision,
    )
    assert result["result_ref"].startswith("execution_plan:")

    plan = await store.get_execution_plan_proposal(
        UUID(result["result_ref"].removeprefix("execution_plan:"))
    )
    assert plan is not None
    assert plan.status.value == "proposed"
    assert plan.objective == "Re-research only the power module"
    assert plan.max_concurrency == 1
    assert plan.research_strategy is not None
    assert plan.work_summary == (
        plan.research_strategy.summary,
        *plan.research_strategy.decision_notes,
    )
    assert "decompose active modules" not in " ".join(plan.work_summary)
    assert "execution_plan.proposed" in {event[2] for event in store.events}
    assert "conversation.action_proposal_accepted" in {event[2] for event in store.events}


@pytest.mark.asyncio
async def test_start_research_without_module_ids_covers_all_active_modules() -> None:
    """Omitting module_ids keeps the existing whole-project research behavior."""
    store = InMemoryDomainStore()
    project, conv, modules = await _project_with_modules(store, module_keys=("power", "mcu"))
    assert len(modules) == 2
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {},
    )
    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=project.revision,
    )
    plan = await store.get_execution_plan_proposal(
        UUID(result["result_ref"].removeprefix("execution_plan:"))
    )
    assert plan is not None
    assert plan.max_concurrency == 2


@pytest.mark.asyncio
async def test_start_research_defaults_deep_conversation_plan_to_independent_verification() -> None:
    """A chat proposal without an explicit override retains the deep-plan safety default."""
    store = InMemoryDomainStore()
    project, conv, modules = await _project_with_modules(store, module_keys=("power", "mcu"))
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {"objective": "Find a well-supported architecture"},
    )

    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=project.revision,
    )
    plan = await store.get_execution_plan_proposal(
        UUID(result["result_ref"].removeprefix("execution_plan:"))
    )

    assert plan is not None
    assert plan.research_depth == "deep"
    assert plan.requires_independent_verification is True
    assert plan.max_token_budget == RESEARCH_DEFAULT_MAX_TOKEN_BUDGET
    assert plan.max_duration_seconds == RESEARCH_DEFAULT_MAX_DURATION_SECONDS


@pytest.mark.asyncio
async def test_start_research_honors_explicit_chat_opt_out_of_independent_verification() -> None:
    """The deep default is a default, not an override of an explicit user constraint."""
    store = InMemoryDomainStore()
    project, conv, modules = await _project_with_modules(store, module_keys=("power", "mcu"))
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {
            "objective": "Find a quick directional answer",
            "research_depth": "deep",
            "requires_independent_verification": False,
        },
    )

    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=project.revision,
    )
    plan = await store.get_execution_plan_proposal(
        UUID(result["result_ref"].removeprefix("execution_plan:"))
    )

    assert plan is not None
    assert plan.requires_independent_verification is False
    assert plan.max_token_budget == RESEARCH_DEFAULT_MAX_TOKEN_BUDGET
    assert plan.max_duration_seconds == RESEARCH_DEFAULT_MAX_DURATION_SECONDS


@pytest.mark.asyncio
async def test_start_research_can_set_governed_plan_limits_from_conversation() -> None:
    """Conversation may propose plan limits, but acceptance still only creates a plan."""
    store = InMemoryDomainStore()
    project, conv, modules = await _project_with_modules(
        store, module_keys=("power", "mcu", "structure")
    )
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {
            "module_ids": [str(module.id) for module in modules[:2]],
            "objective": "Cross-check the power and controller modules",
            "max_concurrency": 2,
            "max_token_budget": 12_000,
            "max_duration_seconds": 3_600,
            "requires_independent_verification": True,
            "research_depth": "deep",
        },
    )

    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=project.revision,
    )
    plan = await store.get_execution_plan_proposal(
        UUID(result["result_ref"].removeprefix("execution_plan:"))
    )
    assert plan is not None
    assert plan.status.value == "proposed"
    assert plan.max_concurrency == 2
    assert plan.max_token_budget == 12_000
    assert plan.max_duration_seconds == 3_600
    assert plan.requires_independent_verification is True
    assert plan.research_depth == "deep"
    assert plan.research_strategy is not None
    assert len(plan.research_strategy.tasks) == 2


@pytest.mark.asyncio
async def test_start_research_unknown_module_id_rejected_without_effects() -> None:
    """A scoped re-research request cannot reference inactive or foreign modules."""
    store = InMemoryDomainStore()
    project, conv, _modules = await _project_with_modules(store, module_keys=("power",))
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {"module_ids": [str(uuid4())], "objective": "Re-research"},
    )
    with pytest.raises(ConversationConflictError, match="inactive or unknown modules"):
        await conv.accept_action_proposal(
            proposal_id=proposal.id,
            project_id=project.id,
            expected_project_revision=project.revision,
        )
    stored = await store.get_conversation_action_proposal(proposal.id)
    assert stored is not None and stored.status.value == "proposed"
    assert stored.resolution_turn_id is None
    assert await store.list_execution_plan_proposals(project.id) == []


@pytest.mark.asyncio
async def test_start_research_rejects_invalid_module_ids_payload() -> None:
    """Non-UUID or duplicate module_ids fail secondary payload validation (422)."""
    store = InMemoryDomainStore()
    project, conv, _modules = await _project_with_modules(store, module_keys=("power",))

    non_uuid = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {"module_ids": ["not-a-uuid"]},
    )
    with pytest.raises(ValueError, match="valid UUIDs"):
        await conv.accept_action_proposal(
            proposal_id=non_uuid.id,
            project_id=project.id,
            expected_project_revision=project.revision,
        )
    assert await store.list_execution_plan_proposals(project.id) == []

    same_id = str(uuid4())
    duplicate = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {"module_ids": [same_id, same_id]},
    )
    with pytest.raises(ValueError, match="must be unique"):
        await conv.accept_action_proposal(
            proposal_id=duplicate.id,
            project_id=project.id,
            expected_project_revision=project.revision,
        )
    for proposal in (non_uuid, duplicate):
        stored = await store.get_conversation_action_proposal(proposal.id)
        assert stored is not None and stored.status.value == "proposed"
    assert await store.list_execution_plan_proposals(project.id) == []


@pytest.mark.asyncio
async def test_start_research_rejects_invalid_plan_limit_payload() -> None:
    store = InMemoryDomainStore()
    project, conv, _modules = await _project_with_modules(store, module_keys=("power",))
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.START_RESEARCH,
        {"max_token_budget": "12000"},
    )

    with pytest.raises(ValueError, match="max_token_budget must be an integer"):
        await conv.accept_action_proposal(
            proposal_id=proposal.id,
            project_id=project.id,
            expected_project_revision=project.revision,
        )
    assert await store.list_execution_plan_proposals(project.id) == []

@pytest.mark.asyncio
async def test_reshape_project_can_propose_module_relationships_from_conversation() -> None:
    """A relationship edit stays a reviewable reshape, never a live project edit."""
    store = InMemoryDomainStore()
    project, conv, modules = await _project_with_modules(
        store, module_keys=("power", "mcu")
    )
    proposal = await _proposal(
        store,
        project.id,
        conv,
        ConversationActionProposalKind.RESHAPE_PROJECT,
        {
            "target_goal": "Keep the existing project goal",
            "summary": "Make the controller depend on the power module",
            "affected_module_ids": [str(module.id) for module in modules],
            "new_modules": [],
            "dependency_edges": [
                {
                    "source_module_id": str(modules[0].id),
                    "target_module_id": str(modules[1].id),
                }
            ],
        },
    )

    result = await conv.accept_action_proposal(
        proposal_id=proposal.id,
        project_id=project.id,
        expected_project_revision=project.revision,
    )
    reshape_id = UUID(result["result_ref"].removeprefix("reshape_proposal:"))
    reshape = await store.get_project_reshape_proposal(reshape_id)
    assert reshape is not None
    assert reshape.status.value == "proposed"
    assert reshape.dependency_edges == ((modules[0].id, modules[1].id),)
    current = await store.get_project(project.id)
    assert current is not None and current.revision == project.revision


@pytest.mark.asyncio
async def test_context_exposes_module_and_candidate_ids_for_governed_actions() -> None:
    """The bounded context lets the model reference real ids for proposals."""
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, conv = await _project_and_conv(store)
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "power", "name": "Power", "responsibility": "Supply power"},),
        idempotency_key_prefix="conv-gov:ctx",
    )
    module = modules[0]
    candidate = Candidate(
        project_id=project.id,
        module_id=module.id,
        name="Preferred battery",
        description="User preferred battery route",
    )
    await store.add_candidates((candidate,))

    session = await conv.ensure_active_session(project.id)
    context = await _build_conversation_context(store, project.id, session.id, max_chars=8_000)
    assert f"Power(power)[{module.id}]" in context
    assert f"power::{candidate.id}: Preferred battery" in context
