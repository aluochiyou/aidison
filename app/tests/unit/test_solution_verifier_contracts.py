from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from aidison.solution.verifier import (
    InterfaceVerifierOutcome,
    InterfaceVerifierPayload,
    JsonModeInterfaceVerifier,
    validate_interface_verifier_payload,
)


def test_verifier_payload_is_scoped_to_one_interface_and_allowed_evidence() -> None:
    interface_id = uuid4()
    accepted = validate_interface_verifier_payload(
        payload=InterfaceVerifierPayload(
            interface_id=interface_id,
            outcome=InterfaceVerifierOutcome.CONFIRMED,
            summary="The cited source explicitly covers the stated voltage range.",
            evidence_refs=("evidence-binding://allowed",),
        ),
        expected_interface_id=interface_id,
        allowed_evidence_refs=("evidence-binding://allowed",),
    )

    assert accepted.outcome is InterfaceVerifierOutcome.CONFIRMED


def test_verifier_payload_cannot_invent_evidence_or_confirm_without_evidence() -> None:
    interface_id = uuid4()
    with pytest.raises(ValueError, match="outside its task envelope"):
        validate_interface_verifier_payload(
            payload=InterfaceVerifierPayload(
                interface_id=interface_id,
                outcome=InterfaceVerifierOutcome.CONFLICTED,
                summary="The evidence conflicts.",
                evidence_refs=("evidence-binding://invented",),
            ),
            expected_interface_id=interface_id,
            allowed_evidence_refs=("evidence-binding://allowed",),
        )
    with pytest.raises(ValidationError, match="requires evidence"):
        InterfaceVerifierPayload(
            interface_id=interface_id,
            outcome=InterfaceVerifierOutcome.CONFIRMED,
            summary="Unsupported confirmation.",
        )


@pytest.mark.asyncio
async def test_json_mode_interface_verifier_makes_one_raw_json_call() -> None:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=(
                '{"interface_id":"00000000-0000-0000-0000-000000000001",'
                '"outcome":"needs_more_evidence","summary":"Need a primary specification."}'
            )
        )
    )
    model.bind.return_value = bound

    raw_json = await JsonModeInterfaceVerifier(model).verify(
        instruction="Verify only the supplied electrical interface.",
    )

    assert '"outcome":"needs_more_evidence"' in raw_json
    model.bind.assert_called_once_with(response_format={"type": "json_object"})
    bound.ainvoke.assert_awaited_once()
