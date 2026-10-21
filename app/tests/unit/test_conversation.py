"""Unit tests for Conversation domain models and invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.application.conversation import (
    ConversationModelResponse,
    _normalize_model_response_payload,
)
from aidison.domain.models import (
    ContextSummary,
    ContextSummaryStatus,
    ConversationActionProposal,
    ConversationActionProposalKind,
    ConversationActionProposalStatus,
    ConversationClarification,
    ConversationClarificationStatus,
    ConversationSession,
    ConversationSessionStatus,
    ConversationTurn,
    ConversationTurnRole,
)


class TestConversationSession:
    def test_default_status_is_active(self) -> None:
        s = ConversationSession(project_id=uuid4())
        assert s.status is ConversationSessionStatus.ACTIVE

    def test_closed_session_requires_timestamp(self) -> None:
        with pytest.raises(ValueError, match="requires closed_at"):
            ConversationSession(
                project_id=uuid4(),
                status=ConversationSessionStatus.CLOSED,
            )

    def test_closed_session_with_timestamp_is_valid(self) -> None:
        s = ConversationSession(
            project_id=uuid4(),
            status=ConversationSessionStatus.CLOSED,
            closed_at=datetime.now(UTC),
        )
        assert s.status is ConversationSessionStatus.CLOSED

    def test_active_session_without_timestamp_is_valid(self) -> None:
        s = ConversationSession(project_id=uuid4())
        assert s.status is ConversationSessionStatus.ACTIVE
        assert s.closed_at is None


class TestConversationTurn:
    def test_user_turn_requires_idempotency_key(self) -> None:
        with pytest.raises(ValueError, match="requires idempotency_key"):
            ConversationTurn(
                session_id=uuid4(),
                sequence=1,
                role=ConversationTurnRole.USER,
                content="hello",
            )

    def test_user_turn_with_key_is_valid(self) -> None:
        t = ConversationTurn(
            session_id=uuid4(),
            sequence=1,
            role=ConversationTurnRole.USER,
            content="hello",
            idempotency_key="msg-1",
        )
        assert t.idempotency_key == "msg-1"

    def test_assistant_turn_without_key_is_valid(self) -> None:
        t = ConversationTurn(
            session_id=uuid4(),
            sequence=2,
            role=ConversationTurnRole.ASSISTANT,
            content="hi",
            model_profile="deepseek-v4-pro",
        )
        assert t.role is ConversationTurnRole.ASSISTANT

    def test_fallback_only_assistant(self) -> None:
        with pytest.raises(ValueError, match="only assistant turns may be marked as fallback"):
            ConversationTurn(
                session_id=uuid4(),
                sequence=1,
                role=ConversationTurnRole.USER,
                content="hello",
                idempotency_key="msg-2",
                is_fallback=True,
            )

    def test_assistant_fallback_is_valid(self) -> None:
        t = ConversationTurn(
            session_id=uuid4(),
            sequence=2,
            role=ConversationTurnRole.ASSISTANT,
            content="Sorry, I could not respond.",
            model_profile="fallback",
            is_fallback=True,
        )
        assert t.is_fallback is True

    def test_turns_are_frozen(self) -> None:
        t = ConversationTurn(
            session_id=uuid4(),
            sequence=1,
            role=ConversationTurnRole.USER,
            content="hello",
            idempotency_key="msg-3",
        )
        with pytest.raises(ValidationError):
            t.content = "modified"  # type: ignore[misc]


class TestContextSummary:
    def test_summary_requires_a_nonempty_contiguous_turn_range(self) -> None:
        with pytest.raises(ValueError, match="end sequence"):
            ContextSummary(
                project_id=uuid4(),
                session_id=uuid4(),
                source_start_sequence=4,
                source_end_sequence=3,
                source_event_cursor=0,
                basis_hash="a" * 64,
                content="A summary",
                summarizer_profile="deepseek-v4-pro",
            )

    def test_tombstoned_summary_requires_a_reason(self) -> None:
        with pytest.raises(ValueError, match="requires tombstone_reason"):
            ContextSummary(
                project_id=uuid4(),
                session_id=uuid4(),
                source_start_sequence=1,
                source_end_sequence=2,
                source_event_cursor=0,
                basis_hash="a" * 64,
                content="A summary",
                summarizer_profile="deepseek-v4-pro",
                status=ContextSummaryStatus.TOMBSTONED,
            )


class TestConversationClarificationResolution:
    def test_resolved_requires_resolved_by_turn_id(self) -> None:
        with pytest.raises(ValueError, match="requires resolved_by_turn_id"):
            ConversationClarification(
                turn_id=uuid4(),
                question="what?",
                status=ConversationClarificationStatus.RESOLVED,
            )

    def test_pending_must_not_have_resolved_by(self) -> None:
        with pytest.raises(ValueError, match="cannot have resolved_by_turn_id"):
            ConversationClarification(
                turn_id=uuid4(),
                question="what?",
                status=ConversationClarificationStatus.PENDING,
                resolved_by_turn_id=uuid4(),
            )


class TestConversationActionProposalResolution:
    def test_proposed_cannot_have_resolved_at(self) -> None:
        with pytest.raises(ValueError, match="cannot have resolved_at"):
            ConversationActionProposal(
                turn_id=uuid4(),
                kind=ConversationActionProposalKind.START_RESEARCH,
                summary="let's search",
                status=ConversationActionProposalStatus.PROPOSED,
                resolved_at=datetime.now(UTC),
            )

    def test_accepted_requires_resolved_at(self) -> None:
        with pytest.raises(ValueError, match="requires resolved_at"):
            ConversationActionProposal(
                turn_id=uuid4(),
                kind=ConversationActionProposalKind.REWRITE_REQUIREMENTS,
                summary="revise",
                status=ConversationActionProposalStatus.ACCEPTED,
            )

    def test_accepted_with_timestamp_is_valid(self) -> None:
        now = datetime.now(UTC)
        p = ConversationActionProposal(
            turn_id=uuid4(),
            kind=ConversationActionProposalKind.ADD_MODULE,
            summary="add motor module",
            status=ConversationActionProposalStatus.ACCEPTED,
            resolved_at=now,
            resolution_turn_id=uuid4(),
        )
        assert p.resolved_at == now


class TestConversationTurnSequenceOrdering:
    def test_sequence_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            ConversationTurn(
                session_id=uuid4(),
                sequence=0,
                role=ConversationTurnRole.USER,
                content="zero",
                idempotency_key="seq-zero",
            )

    def test_sequence_1_is_first_valid(self) -> None:
        t = ConversationTurn(
            session_id=uuid4(),
            sequence=1,
            role=ConversationTurnRole.USER,
            content="first",
            idempotency_key="first-turn",
        )
        assert t.sequence == 1


class TestConversationModelResponseParsing:
    def test_normalizes_known_clarification_kind_aliases(self) -> None:
        response = ConversationModelResponse.model_validate(
            _normalize_model_response_payload(
                {
                    "reply": "请补充一些信息。",
                    "clarifications": [
                        {"question": "使用场景是什么？", "kind": " Context "},
                        {"question": "有哪些安全要求？", "kind": "SAFETY"},
                        {"question": "投入时间如何？", "kind": "skill-and-time"},
                        {
                            "question": "验收标准是什么？",
                            "kind": "acceptance criteria",
                        },
                        {"question": "功能范围是什么？", "kind": "FEATURE"},
                        {
                            "question": "有哪些技术限制？",
                            "kind": "technical-constraint",
                        },
                    ],
                }
            )
        )

        assert [item.kind.value for item in response.clarifications] == [
            "other",
            "constraint",
            "skill",
            "requirement",
            "requirement",
            "constraint",
        ]

    def test_collapses_unknown_non_authoritative_question_label_to_other(self) -> None:
        response = ConversationModelResponse.model_validate(
            _normalize_model_response_payload(
                {
                    "reply": "请补充。",
                    "clarifications": [
                        {"question": "功能范围是什么？", "kind": "functional_scope"},
                        {"question": "还缺什么？"},
                    ],
                }
            )
        )

        assert [item.kind.value for item in response.clarifications] == [
            "other",
            "other",
        ]

    def test_coverage_object_is_normalized_before_strict_validation(self) -> None:
        response = ConversationModelResponse.model_validate(
            _normalize_model_response_payload(
                {
                    "reply": "请确认。",
                    "coverage": {
                        "usage": "covered",
                        "budget": "missing",
                        "resources": "covered",
                        "skill": "maybe",
                        "constraints": "covered",
                        "unexpected": "covered",
                    },
                }
            )
        )
        assert response.coverage is not None
        assert response.coverage.usage.value == "covered"
        assert response.coverage.budget.value == "missing"
        assert response.coverage.resources.value == "covered"
        assert response.coverage.skill.value == "missing"
        assert response.coverage.constraints.value == "covered"

    def test_missing_coverage_defaults_to_none(self) -> None:
        response = ConversationModelResponse.model_validate({"reply": "你好。"})
        assert response.coverage is None

    def test_partial_coverage_object_defaults_missing_items(self) -> None:
        response = ConversationModelResponse.model_validate(
            _normalize_model_response_payload(
                {"reply": "请确认。", "coverage": {"budget": "covered"}}
            )
        )
        assert response.coverage is not None
        assert response.coverage.budget.value == "covered"
        assert response.coverage.usage.value == "missing"
        assert response.coverage.resources.value == "missing"
        assert response.coverage.skill.value == "missing"
        assert response.coverage.constraints.value == "missing"
