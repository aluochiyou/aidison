from __future__ import annotations

from uuid import uuid4

import pytest

from aidison.solution.contracts import SolutionContract, SolutionRiskClass
from aidison.solution.draft_admission import (
    ApprovedModuleCandidate,
    SolutionDraftAdmissionInput,
    validate_solution_draft_material,
)
from aidison.solution.normalize import (
    SolutionNormalizationArtifactRefs,
    SolutionNormalizationInput,
    normalize_solution_composition,
)
from aidison.solution.payloads import SolutionCompositionPayload


def _contract() -> SolutionContract:
    return SolutionContract(
        solution_run_id=uuid4(),
        project_id=uuid4(),
        basis_hash="a" * 64,
        basis_project_revision=1,
        requirement_refs=("requirement://1",),
        module_revision_refs=("module://1", "module://2"),
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


def _admitted_payload() -> tuple[SolutionContract, SolutionCompositionPayload]:
    source_module, consumer_module = uuid4(), uuid4()
    source_candidate, consumer_candidate, evidence = uuid4(), uuid4(), uuid4()
    payload = SolutionCompositionPayload.model_validate(
        {
            "elements": [
                {
                    "element_key": "power.source",
                    "module_id": str(source_module),
                    "responsibility": "Supply power.",
                    "selected_candidate_id": str(source_candidate),
                    "output_interface_keys": ["power.feed"],
                    "evidence_binding_ids": [str(evidence)],
                },
                {
                    "element_key": "control.consumer",
                    "module_id": str(consumer_module),
                    "responsibility": "Consume power.",
                    "selected_candidate_id": str(consumer_candidate),
                    "input_interface_keys": ["power.feed"],
                    "evidence_binding_ids": [str(evidence)],
                },
            ],
            "interfaces": [
                {
                    "interface_key": "power.feed",
                    "producer_module_id": str(source_module),
                    "consumer_module_id": str(consumer_module),
                    "interface_type": "electrical",
                    "schema_or_unit": "V",
                    "direction": "source_to_load",
                    "range_or_capacity": "11.1-12.6 V",
                    "evidence_binding_ids": [str(evidence)],
                }
            ],
        }
    )
    contract = _contract()
    admitted = validate_solution_draft_material(
        SolutionDraftAdmissionInput(
            contract=contract,
            approved_candidates=(
                ApprovedModuleCandidate(module_id=source_module, candidate_id=source_candidate),
                ApprovedModuleCandidate(module_id=consumer_module, candidate_id=consumer_candidate),
            ),
            approved_evidence_ids=(evidence,),
            payload=payload,
        )
    )
    return contract, admitted


def test_normalization_converts_admitted_draft_to_closed_typed_element_set() -> None:
    contract, payload = _admitted_payload()

    normalized = normalize_solution_composition(
        SolutionNormalizationInput(
            contract=contract,
            payload=payload,
            artifact_refs=(
                SolutionNormalizationArtifactRefs(
                    element_key="power.source",
                    configuration_ref="artifact://config/source",
                ),
                SolutionNormalizationArtifactRefs(
                    element_key="control.consumer",
                    configuration_ref="artifact://config/consumer",
                ),
            ),
        )
    )

    assert len(normalized.elements) == 2
    assert len(normalized.interfaces) == 1
    assert normalized.interfaces[0].verification_status.value == "unverified"
    assert normalized.elements[0].decision_refs == contract.accepted_decision_refs


def test_normalization_rejects_missing_or_spurious_configuration_artifact_refs() -> None:
    contract, payload = _admitted_payload()
    input = SolutionNormalizationInput(
        contract=contract,
        payload=payload,
        artifact_refs=(
            SolutionNormalizationArtifactRefs(
                element_key="power.source",
                configuration_ref="artifact://config/source",
            ),
        ),
    )

    with pytest.raises(ValueError, match="exactly one configuration Artifact"):
        normalize_solution_composition(input)
