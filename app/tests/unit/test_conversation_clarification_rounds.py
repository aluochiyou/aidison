"""Focused unit tests for the pre-approval clarification round policy.

Covers the acceptance points of the coverage-driven clarification slice:
- the five requirement facts (usage, budget, resources, skill, constraints),
  with only usage and hard constraints blocking the initial draft
- coverage derived from persisted clarifications and merged with the model's
  controlled declaration (so items explicit in the project description can be
  skipped without being asked)
- a small batch of independent clarifications per model response, targeting
  distinct still-missing items
- no duplicate questions (normalized) and no stacking while one is pending
- convergence to a sanitized initial draft that contains no modules, unknowns,
  module skeletons, candidate solutions, or open research questions
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from aidison.application.conversation import (
    _INITIAL_REQUIREMENTS_DRAFT_MARKER,
    ConversationActionProposalDraft,
    ConversationApplication,
    ConversationClarificationCoverageItem,
    ConversationClarificationDraft,
    ConversationConflictError,
    ConversationCoverageDeclaration,
    ConversationCoverageStatus,
    ConversationModelResponse,
    _build_conversation_system_prompt,
    _coverage_from_persisted,
    _merge_coverage,
    _normalize_clarification_question,
)
from aidison.domain.models import (
    ConversationActionProposal,
    ConversationActionProposalKind,
    ConversationClarification,
    ConversationClarificationKind,
    ConversationClarificationStatus,
    ConversationSession,
    ConversationTurn,
    ConversationTurnRole,
    Project,
    RequirementRevision,
    RequirementStatus,
)
from tests.fakes import InMemoryDomainStore


def _make_project(
    store: InMemoryDomainStore, *, approved: bool = False
) -> tuple[Project, ConversationSession]:
    project = Project(id=uuid4(), name="拾取机械臂", goal="做一台能自动拾取物料的机械臂")
    if approved:
        revision = RequirementRevision(
            project_id=project.id,
            revision=1,
            status=RequirementStatus.APPROVED,
            goal="做一台能自动拾取物料的机械臂",
            approved_at=datetime.now(UTC),
        )
        store.requirements[revision.id] = revision
        project = project.model_copy(update={"active_requirement_revision_id": revision.id})
    store.projects[project.id] = project
    session = ConversationSession(project_id=project.id)
    store.conversation_sessions[session.id] = session
    return project, session


def _question(
    text: str,
    kind: ConversationClarificationKind = ConversationClarificationKind.OTHER,
) -> ConversationClarificationDraft:
    return ConversationClarificationDraft(question=text, kind=kind)


def _draft_proposal() -> ConversationActionProposalDraft:
    return ConversationActionProposalDraft(
        kind=ConversationActionProposalKind.REWRITE_REQUIREMENTS,
        summary="初始需求草案",
        proposed_payload={
            "goal": "做一台能自动拾取物料的机械臂",
            "hard_constraints": [],
            "preferences": [],
            "available_resources": ["已有机械臂底座"],
            "usage_context": "用于流水线自动分拣物料",
            "budget_context": "总成本控制在 2 万元以内",
            "skill_context": "团队具备机械与电气基础，无 AI 经验",
            "unknowns": ["需要多少推力的电机？"],
            "modules": [
                {
                    "key": "picker",
                    "name": "拾取模块",
                    "responsibility": "自动拾取物料",
                }
            ],
        },
    )


def _start_research_proposal() -> ConversationActionProposalDraft:
    return ConversationActionProposalDraft(
        kind=ConversationActionProposalKind.START_RESEARCH,
        summary="开始研究",
        proposed_payload={"objective": "调研机械臂方案"},
    )


def _covered(
    *,
    usage: bool = True,
    budget: bool = True,
    resources: bool = True,
    skill: bool = True,
    constraints: bool = True,
) -> ConversationCoverageDeclaration:
    def _status(flag: bool) -> ConversationCoverageStatus:
        return ConversationCoverageStatus.COVERED if flag else ConversationCoverageStatus.MISSING

    return ConversationCoverageDeclaration(
        usage=_status(usage),
        budget=_status(budget),
        resources=_status(resources),
        skill=_status(skill),
        constraints=_status(constraints),
    )


def _response(
    *,
    reply: str = "好的",
    coverage: ConversationCoverageDeclaration | None = None,
    clarifications: tuple[ConversationClarificationDraft, ...] = (),
    action_proposals: tuple[ConversationActionProposalDraft, ...] = (),
) -> ConversationModelResponse:
    return ConversationModelResponse(
        reply=reply,
        coverage=coverage,
        clarifications=clarifications,
        action_proposals=action_proposals,
    )


def _persist_store_turn(
    store: InMemoryDomainStore, session_id: UUID, sequence: int
) -> ConversationTurn:
    turn = ConversationTurn(
        session_id=session_id,
        sequence=sequence,
        role=ConversationTurnRole.ASSISTANT,
        content="请问预算是多少？",
    )
    store.conversation_turns[turn.id] = turn
    return turn


def _clarifications_on(
    store: InMemoryDomainStore, turn_id: UUID
) -> list[ConversationClarification]:
    return [c for c in store.conversation_clarifications.values() if c.turn_id == turn_id]


def _proposals_on(store: InMemoryDomainStore, turn_id: UUID) -> list[ConversationActionProposal]:
    return [p for p in store.conversation_action_proposals.values() if p.turn_id == turn_id]


class TestNormalizedQuestionKey:
    def test_collapses_whitespace_punctuation_and_case(self) -> None:
        assert _normalize_clarification_question("请问 预算是多少？") == (
            _normalize_clarification_question("请问预算是多少？")
        )
        assert _normalize_clarification_question("What is the budget?") == (
            _normalize_clarification_question("what   is   the budget")
        )

    def test_nfkc_folds_fullwidth_variants(self) -> None:
        assert _normalize_clarification_question("预算１００元") == (
            _normalize_clarification_question("预算100元")
        )

    def test_all_punctuation_normalizes_to_empty(self) -> None:
        assert _normalize_clarification_question("？？？　！") == ""


class TestCoverageVocabulary:
    def test_five_items_and_their_kinds_are_in_the_prompt(self) -> None:
        prompt = _build_conversation_system_prompt()
        for item in (
            ConversationClarificationCoverageItem.USAGE,
            ConversationClarificationCoverageItem.BUDGET,
            ConversationClarificationCoverageItem.RESOURCES,
            ConversationClarificationCoverageItem.SKILL,
            ConversationClarificationCoverageItem.CONSTRAINTS,
        ):
            assert item.value in prompt
        assert "kind=usage" in prompt
        assert "kind=budget" in prompt
        assert "kind=resource" in prompt
        assert "kind=skill" in prompt
        assert "kind=constraint" in prompt

    def test_coverage_declaration_has_all_five_items_defaulting_to_missing(self) -> None:
        declaration = ConversationCoverageDeclaration()
        assert declaration.usage is ConversationCoverageStatus.MISSING
        assert declaration.budget is ConversationCoverageStatus.MISSING
        assert declaration.resources is ConversationCoverageStatus.MISSING
        assert declaration.skill is ConversationCoverageStatus.MISSING
        assert declaration.constraints is ConversationCoverageStatus.MISSING

    def test_prompt_controls_determination_before_the_question(self) -> None:
        prompt = _build_conversation_system_prompt()
        assert "FIRST determine coverage" in prompt
        assert "ask one natural-language question by default" in prompt

    def test_prompt_forbids_chain_of_thought(self) -> None:
        prompt = _build_conversation_system_prompt()
        assert "Do not include chain-of-thought, thinking, or reasoning text" in prompt

    def test_prompt_skips_items_already_explicit_in_the_description(self) -> None:
        prompt = _build_conversation_system_prompt()
        assert "the project description already states it explicitly" in prompt
        assert "Never ask about an item already covered by the description" in prompt

    def test_prompt_forbids_repeating_already_asked_questions(self) -> None:
        prompt = _build_conversation_system_prompt()
        assert "never repeat a question that was already asked or answered" in prompt

    def test_prompt_distinguishes_required_boundary_from_optional_context(self) -> None:
        prompt = _build_conversation_system_prompt()
        assert "usage and constraints are required" in prompt
        assert "optional unknowns" in prompt
        assert "never modules, module skeletons" in prompt
        assert "research phase after the plan is approved" in prompt


class TestCoverageFromPersisted:
    @pytest.mark.asyncio
    async def test_all_five_items_reported_as_missing_when_empty(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        coverage = await _coverage_from_persisted(store, session.id)
        assert list(coverage.keys()) == list(ConversationClarificationCoverageItem)
        assert set(coverage.values()) == {"missing"}

    @pytest.mark.asyncio
    async def test_resolved_clarification_marks_its_item_covered(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        assistant = _persist_store_turn(store, session.id, 1)
        store.conversation_clarifications[uuid4()] = ConversationClarification(
            turn_id=assistant.id,
            question="预算是多少？",
            kind=ConversationClarificationKind.BUDGET,
            status=ConversationClarificationStatus.RESOLVED,
            resolved_by_turn_id=uuid4(),
        )
        coverage = await _coverage_from_persisted(store, session.id)
        assert coverage[ConversationClarificationCoverageItem.BUDGET] == "covered"

    @pytest.mark.asyncio
    async def test_pending_clarification_marks_its_item_pending(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        assistant = _persist_store_turn(store, session.id, 1)
        store.conversation_clarifications[uuid4()] = ConversationClarification(
            turn_id=assistant.id,
            question="有什么硬约束？",
            kind=ConversationClarificationKind.CONSTRAINT,
        )
        coverage = await _coverage_from_persisted(store, session.id)
        assert coverage[ConversationClarificationCoverageItem.CONSTRAINTS] == "pending"


class TestMergeCoverage:
    def test_declaration_covers_items_missing_from_persistence(self) -> None:
        merged = _merge_coverage(
            _covered(),
            {item: "missing" for item in ConversationClarificationCoverageItem},
        )
        assert set(merged.values()) == {"covered"}

    def test_pending_evidence_wins_over_a_covered_declaration(self) -> None:
        merged = _merge_coverage(
            _covered(),
            {
                item: "covered"
                if item is not ConversationClarificationCoverageItem.BUDGET
                else "pending"
                for item in ConversationClarificationCoverageItem
            },
        )
        assert merged[ConversationClarificationCoverageItem.BUDGET] == "pending"

    def test_resolved_evidence_wins_over_a_missing_declaration(self) -> None:
        merged = _merge_coverage(
            _covered(budget=False),
            {
                item: "covered"
                if item is ConversationClarificationCoverageItem.BUDGET
                else "missing"
                for item in ConversationClarificationCoverageItem
            },
        )
        assert merged[ConversationClarificationCoverageItem.BUDGET] == "covered"


class TestSmallClarificationBatchPerResponse:
    @pytest.mark.asyncio
    async def test_three_model_questions_persist_the_first_two_distinct_items(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="请补充信息。",
                coverage=_covered(usage=False, budget=False, skill=False, constraints=False),
                clarifications=(
                    _question("使用场景是什么？", ConversationClarificationKind.USAGE),
                    _question("预算是多少？", ConversationClarificationKind.BUDGET),
                    _question("有什么硬约束？", ConversationClarificationKind.CONSTRAINT),
                ),
            ),
        )

        persisted = _clarifications_on(store, turn.id)
        assert [item.question for item in persisted] == [
            "使用场景是什么？",
            "预算是多少？",
        ]
        assert [item.kind for item in persisted] == [
            ConversationClarificationKind.USAGE,
            ConversationClarificationKind.BUDGET,
        ]

    @pytest.mark.asyncio
    async def test_identical_questions_in_one_response_collapse_to_one(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="请补充信息。",
                coverage=_covered(budget=False),
                clarifications=(
                    _question("预算是多少？", ConversationClarificationKind.BUDGET),
                    _question("预算是多少？", ConversationClarificationKind.BUDGET),
                    _question("预算是多少？", ConversationClarificationKind.BUDGET),
                ),
            ),
        )

        persisted = _clarifications_on(store, turn.id)
        assert len(persisted) == 1


class TestPendingClarificationBlocksStacking:
    @pytest.mark.asyncio
    async def test_pending_question_suppresses_new_question_and_initial_draft(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        assistant = _persist_store_turn(store, session.id, 1)
        store.conversation_clarifications[uuid4()] = ConversationClarification(
            turn_id=assistant.id,
            question="预算是多少？",
            kind=ConversationClarificationKind.BUDGET,
        )
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="还想再确认一点。",
                coverage=_covered(),
                clarifications=(
                    _question("预算上限是多少？", ConversationClarificationKind.BUDGET),
                ),
                action_proposals=(_draft_proposal(),),
            ),
        )

        assert _clarifications_on(store, turn.id) == []
        assert _proposals_on(store, turn.id) == []

    @pytest.mark.asyncio
    async def test_free_message_during_pending_batch_does_not_invoke_model(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        model = MagicMock()
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: model)
        assistant = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="请确认两个互不依赖的事实。",
                coverage=_covered(usage=False, budget=False),
                clarifications=(
                    _question("使用场景是什么？", ConversationClarificationKind.USAGE),
                    _question("预算是多少？", ConversationClarificationKind.BUDGET),
                ),
            ),
        )
        assert len(_clarifications_on(store, assistant.id)) == 2

        result = await app.post_message(
            project_id=project.id,
            session_id=session.id,
            content="我还想补充一点设计思路。",
            idempotency_key="clarification-batch:free-message",
        )

        model.bind.assert_not_called()
        assert result["assistant_turn"].model_profile == "clarification-batch-notice"
        assert "还有 2 个待确认的问题" in result["assistant_turn"].content
        assert len(await store.list_active_clarifications(session.id)) == 2

    @pytest.mark.asyncio
    async def test_answering_one_question_in_a_batch_does_not_reinvoke_the_model(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        model = MagicMock()
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: model)
        assistant = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="请确认两个互不依赖的事实。",
                coverage=_covered(usage=False, budget=False),
                clarifications=(
                    _question("使用场景是什么？", ConversationClarificationKind.USAGE),
                    _question("预算是多少？", ConversationClarificationKind.BUDGET),
                ),
            ),
        )
        questions = _clarifications_on(store, assistant.id)
        assert len(questions) == 2

        result = await app.resolve_clarification_and_continue(
            project_id=project.id,
            clarification_id=questions[0].id,
            response="用于室内分拣。",
            idempotency_key="clarification-batch:first-answer",
        )

        assert result["assistant_turn"].model_profile == "clarification-batch-ack"
        assert "还有 1 个" in result["assistant_turn"].content
        model.bind.assert_not_called()
        pending = await store.list_active_clarifications(session.id)
        assert [item.id for item in pending] == [questions[1].id]


class TestMissingCoverageAndInitialDraft:
    @pytest.mark.asyncio
    async def test_declared_missing_item_holds_draft_and_persists_question(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="还差预算信息。",
                coverage=_covered(budget=False),
                clarifications=(
                    _question("预算范围大概多少？", ConversationClarificationKind.BUDGET),
                ),
                action_proposals=(_draft_proposal(),),
            ),
        )

        persisted = _clarifications_on(store, turn.id)
        assert len(persisted) == 1
        assert persisted[0].kind is ConversationClarificationKind.BUDGET
        assert _proposals_on(store, turn.id) == []

    @pytest.mark.asyncio
    async def test_missing_optional_coverage_without_question_persists_draft_with_unknowns(
        self,
    ) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="信息已足够，给出初始需求草案。",
                coverage=_covered(budget=False, resources=False, skill=False),
                action_proposals=(_draft_proposal(),),
            ),
        )

        [draft] = _proposals_on(store, turn.id)
        assert draft.proposed_payload["unknowns"] == (
            "预算范围尚未确认",
            "可用资源尚未确认",
            "相关技能与能力边界尚未确认",
        )

    @pytest.mark.asyncio
    async def test_missing_required_coverage_without_question_holds_draft(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="信息已足够，给出初始需求草案。",
                coverage=_covered(constraints=False),
                action_proposals=(_draft_proposal(),),
            ),
        )

        assert _proposals_on(store, turn.id) == []

    @pytest.mark.asyncio
    async def test_question_about_an_already_covered_item_is_never_persisted(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="还想确认一下预算。",
                coverage=_covered(),
                clarifications=(_question("预算是多少？", ConversationClarificationKind.BUDGET),),
                action_proposals=(_draft_proposal(),),
            ),
        )

        assert _clarifications_on(store, turn.id) == []
        assert _proposals_on(store, turn.id) == []


class TestConvergenceToInitialDraft:
    @pytest.mark.asyncio
    async def test_all_covered_persists_unique_draft_with_marker(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="信息已足够，给出初始需求草案。",
                coverage=_covered(),
                action_proposals=(_draft_proposal(),),
            ),
        )

        persisted = _proposals_on(store, turn.id)
        assert len(persisted) == 1
        assert persisted[0].kind is ConversationActionProposalKind.REWRITE_REQUIREMENTS
        assert persisted[0].proposed_payload.get(_INITIAL_REQUIREMENTS_DRAFT_MARKER) is True
        assert _clarifications_on(store, turn.id) == []

    @pytest.mark.asyncio
    async def test_initial_phase_drops_non_draft_actions_before_coverage_converges(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="还需要确认预算。",
                coverage=_covered(budget=False),
                clarifications=(
                    _question("预算是多少？", ConversationClarificationKind.BUDGET),
                ),
                action_proposals=(_start_research_proposal(), _draft_proposal()),
            ),
        )

        assert _clarifications_on(store, turn.id)
        assert _proposals_on(store, turn.id) == []

    @pytest.mark.asyncio
    async def test_initial_phase_allows_only_rewrite_draft_after_coverage_converges(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="信息已足够。",
                coverage=_covered(),
                action_proposals=(_start_research_proposal(), _draft_proposal()),
            ),
        )

        proposals = _proposals_on(store, turn.id)
        assert [proposal.kind for proposal in proposals] == [
            ConversationActionProposalKind.REWRITE_REQUIREMENTS
        ]

    @pytest.mark.asyncio
    async def test_active_clarifications_are_isolated_by_session(self) -> None:
        store = InMemoryDomainStore()
        _, first_session = _make_project(store)
        _, second_session = _make_project(store)
        first_turn = _persist_store_turn(store, first_session.id, 1)
        second_turn = _persist_store_turn(store, second_session.id, 1)
        first_question = ConversationClarification(
            turn_id=first_turn.id,
            question="预算是多少？",
            kind=ConversationClarificationKind.BUDGET,
        )
        second_question = ConversationClarification(
            turn_id=second_turn.id,
            question="使用场景是什么？",
            kind=ConversationClarificationKind.USAGE,
        )
        store.conversation_clarifications[first_question.id] = first_question
        store.conversation_clarifications[second_question.id] = second_question

        assert [item.id for item in await store.list_active_clarifications(first_session.id)] == [
            first_question.id
        ]
        assert [item.id for item in await store.list_active_clarifications(second_session.id)] == [
            second_question.id
        ]

    @pytest.mark.asyncio
    async def test_draft_payload_contains_no_modules_and_server_owned_unknowns(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="信息已足够，给出初始需求草案。",
                coverage=_covered(),
                action_proposals=(_draft_proposal(),),
            ),
        )

        persisted = _proposals_on(store, turn.id)[0]
        payload = persisted.proposed_payload
        assert "modules" not in payload
        assert payload["unknowns"] == ()
        assert payload["goal"] == "做一台能自动拾取物料的机械臂"
        assert set(payload) <= {
            _INITIAL_REQUIREMENTS_DRAFT_MARKER,
            "goal",
            "hard_constraints",
            "preferences",
            "available_resources",
            "unknowns",
            "usage_context",
            "budget_context",
            "skill_context",
            "summary",
        }

    @pytest.mark.asyncio
    async def test_coverage_by_description_alone_is_enough_for_the_draft(self) -> None:
        # None of the five items has a persisted clarification: the model
        # declares them all covered because the project description states them.
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="项目描述已经覆盖全部信息。",
                coverage=_covered(),
                action_proposals=(_draft_proposal(),),
            ),
        )

        assert _proposals_on(store, turn.id) != []
        assert _clarifications_on(store, turn.id) == []

    @pytest.mark.asyncio
    async def test_mixing_question_with_draft_keeps_question_and_drops_draft(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="还差一个信息。",
                coverage=_covered(constraints=False),
                clarifications=(
                    _question("有什么硬性约束？", ConversationClarificationKind.CONSTRAINT),
                ),
                action_proposals=(_draft_proposal(),),
            ),
        )

        clarifications = _clarifications_on(store, turn.id)
        assert len(clarifications) == 1
        assert _proposals_on(store, turn.id) == []

    @pytest.mark.asyncio
    async def test_only_invalid_questions_suppresses_draft_too(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="？？？",
                coverage=_covered(),
                clarifications=(_question("？？？"),),
                action_proposals=(_draft_proposal(),),
            ),
        )

        assert _clarifications_on(store, turn.id) == []
        assert _proposals_on(store, turn.id) == []


class TestInitialDraftAcceptIsRejected:
    @pytest.mark.asyncio
    async def test_module_less_draft_is_rejected_not_payload_validated(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="信息已足够，给出初始需求草案。",
                coverage=_covered(),
                action_proposals=(_draft_proposal(),),
            ),
        )

        persisted = _proposals_on(store, turn.id)[0]
        with pytest.raises(
            ConversationConflictError, match="confirmed through the requirements form"
        ):
            await app.accept_action_proposal(
                proposal_id=persisted.id,
                project_id=project.id,
                expected_project_revision=1,
            )


class TestClarifiedContextFields:
    @pytest.mark.asyncio
    async def test_draft_preserves_clarified_context_and_strips_research_content(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="信息已足够，给出初始需求草案。",
                coverage=_covered(),
                action_proposals=(_draft_proposal(),),
            ),
        )

        payload = _proposals_on(store, turn.id)[0].proposed_payload
        assert payload["usage_context"] == "用于流水线自动分拣物料"
        assert payload["budget_context"] == "总成本控制在 2 万元以内"
        assert payload["skill_context"] == "团队具备机械与电气基础，无 AI 经验"
        assert payload["available_resources"] == ["已有机械臂底座"]
        assert "modules" not in payload
        assert payload["unknowns"] == ()

    @pytest.mark.asyncio
    async def test_rewrite_with_budget_context_creates_no_spend_budget_side_effect(self) -> None:
        # Budget context is only a requirement constraint: accepting the rewrite
        # must not create a SpendBudgetRevision, proposal, or any other artifact.
        store = InMemoryDomainStore()
        project, session = _make_project(store, approved=True)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="建议调整需求。",
                action_proposals=(
                    ConversationActionProposalDraft(
                        kind=ConversationActionProposalKind.REWRITE_REQUIREMENTS,
                        summary="保留预算约束",
                        proposed_payload={
                            "goal": "做一台能自动拾取物料的机械臂",
                            "hard_constraints": [],
                            "preferences": [],
                            "available_resources": [],
                            "budget_context": "总成本控制在 2 万元以内",
                            "modules": [
                                {
                                    "key": "picker",
                                    "name": "拾取模块",
                                    "responsibility": "自动拾取物料",
                                }
                            ],
                        },
                    ),
                ),
            ),
        )

        persisted = _proposals_on(store, turn.id)[0]
        assert persisted.kind is ConversationActionProposalKind.REWRITE_REQUIREMENTS
        assert store.spend_budget_proposals == {}
        assert store.spend_budget_revisions == {}

    @pytest.mark.asyncio
    async def test_rejected_initial_draft_accept_does_not_touch_spend_budget(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="信息已足够，给出初始需求草案。",
                coverage=_covered(),
                action_proposals=(_draft_proposal(),),
            ),
        )
        persisted = _proposals_on(store, turn.id)[0]
        with pytest.raises(
            ConversationConflictError, match="confirmed through the requirements form"
        ):
            await app.accept_action_proposal(
                proposal_id=persisted.id,
                project_id=project.id,
                expected_project_revision=1,
            )
        assert store.spend_budget_proposals == {}
        assert store.spend_budget_revisions == {}


class TestResolvedHistoryDeduplication:
    @pytest.mark.asyncio
    async def test_resolved_question_with_new_question_persists_only_new(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        assistant = _persist_store_turn(store, session.id, 1)
        store.conversation_clarifications[uuid4()] = ConversationClarification(
            turn_id=assistant.id,
            question="预算是多少？",
            kind=ConversationClarificationKind.BUDGET,
            status=ConversationClarificationStatus.RESOLVED,
            resolved_by_turn_id=uuid4(),
        )
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="还想再确认一点。",
                coverage=_covered(skill=False),
                clarifications=(
                    _question("预算是多少？", ConversationClarificationKind.BUDGET),
                    _question("你掌握哪些相关技能？", ConversationClarificationKind.SKILL),
                ),
                action_proposals=(_draft_proposal(),),
            ),
        )

        persisted = _clarifications_on(store, turn.id)
        assert len(persisted) == 1
        assert persisted[0].question == "你掌握哪些相关技能？"
        assert _proposals_on(store, turn.id) == []

    @pytest.mark.asyncio
    async def test_all_resolved_questions_suppress_question_and_draft(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store)
        assistant = _persist_store_turn(store, session.id, 1)
        store.conversation_clarifications[uuid4()] = ConversationClarification(
            turn_id=assistant.id,
            question="预算是多少？",
            kind=ConversationClarificationKind.BUDGET,
            status=ConversationClarificationStatus.RESOLVED,
            resolved_by_turn_id=uuid4(),
        )
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="还是需要确认预算。",
                coverage=_covered(budget=False),
                clarifications=(_question("预算是多少？", ConversationClarificationKind.BUDGET),),
                action_proposals=(_draft_proposal(),),
            ),
        )

        assert _clarifications_on(store, turn.id) == []
        assert _proposals_on(store, turn.id) == []


class TestPostApprovalPhaseUnchanged:
    @pytest.mark.asyncio
    async def test_approved_requirements_keep_original_multi_clarification_behavior(self) -> None:
        store = InMemoryDomainStore()
        project, session = _make_project(store, approved=True)
        app = ConversationApplication(store_factory=lambda: store, model_factory=lambda: None)

        turn = await app._persist_model_response(
            store,
            project.id,
            session.id,
            _response(
                reply="继续。",
                clarifications=(
                    _question("你想先看哪个模块？"),
                    _question("需要重新研究吗？"),
                ),
            ),
        )

        persisted = _clarifications_on(store, turn.id)
        assert len(persisted) == 2
