from __future__ import annotations

from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from aidison.solution.contracts import SolutionContract, SolutionRiskClass
from aidison.solution.draft_admission import (
    ApprovedModuleCandidate,
    SolutionDraftAdmissionError,
    SolutionDraftAdmissionInput,
    validate_solution_draft_material,
)
from aidison.solution.payloads import SolutionCompositionPayload


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _contract(run_id: UUID) -> SolutionContract:
    return SolutionContract(
        solution_run_id=run_id,
        project_id=uuid4(),
        basis_hash=_hash("basis"),
        basis_project_revision=1,
        requirement_refs=("requirement://1",),
        module_revision_refs=("module://1",),
        accepted_decision_refs=("decision://1/option/approve",),
        admitted_evidence_refs=("evidence-binding://1",),
        constraint_set_ref="artifact://constraints/1",
        interface_contract_refs=(),
        coverage_contract_ref="artifact://coverage/1",
        risk_class=SolutionRiskClass.STANDARD,
        allowed_tool_ids=(),
        budget_ref="budget://1",
        verification_policy_ref="policy://verification/1",
        completion_policy_ref="policy://completion/1",
    )


def _payload(module_id: UUID, candidate_id: UUID, evidence_id: UUID) -> SolutionCompositionPayload:
    return SolutionCompositionPayload.model_validate(
        {
            "elements": [
                {
                    "element_key": "power.pack",
                    "module_id": str(module_id),
                    "responsibility": "Supply bounded power.",
                    "selected_candidate_id": str(candidate_id),
                    "evidence_binding_ids": [str(evidence_id)],
                }
            ],
            "interfaces": [],
        }
    )


def test_draft_admission_accepts_exact_approved_material() -> None:
    run_id, module_id, candidate_id, evidence_id = uuid4(), uuid4(), uuid4(), uuid4()
    payload = _payload(module_id, candidate_id, evidence_id)

    accepted = validate_solution_draft_material(
        SolutionDraftAdmissionInput(
            contract=_contract(run_id),
            approved_candidates=(
                ApprovedModuleCandidate(module_id=module_id, candidate_id=candidate_id),
            ),
            approved_evidence_ids=(evidence_id,),
            payload=payload,
        )
    )

    assert accepted == payload


def test_draft_admission_rejects_candidate_or_evidence_that_decision_did_not_authorize() -> None:
    run_id, module_id, candidate_id, evidence_id = uuid4(), uuid4(), uuid4(), uuid4()
    input = SolutionDraftAdmissionInput(
        contract=_contract(run_id),
        approved_candidates=(
            ApprovedModuleCandidate(module_id=module_id, candidate_id=candidate_id),
        ),
        approved_evidence_ids=(evidence_id,),
        payload=_payload(module_id, uuid4(), evidence_id),
    )
    with pytest.raises(SolutionDraftAdmissionError, match="outside the approved decision"):
        validate_solution_draft_material(input)

    input = input.model_copy(update={"payload": _payload(module_id, candidate_id, uuid4())})
    with pytest.raises(SolutionDraftAdmissionError, match="outside the approved decision"):
        validate_solution_draft_material(input)
