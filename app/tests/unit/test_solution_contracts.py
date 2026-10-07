from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.solution.contracts import (
    SolutionContract,
    SolutionGraphIdentity,
    SolutionRiskClass,
    compile_solution_coverage_contract,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _contract(**changes: object) -> SolutionContract:
    values: dict[str, object] = {
        "solution_run_id": uuid4(),
        "project_id": uuid4(),
        "basis_hash": _hash("basis"),
        "basis_project_revision": 3,
        "requirement_refs": ("requirement://revision/3",),
        "module_revision_refs": ("module://power/revision/1",),
        "accepted_decision_refs": ("decision://motor/approved",),
        "admitted_evidence_refs": ("evidence://motor/current",),
        "constraint_set_ref": "artifact://constraints/1",
        "interface_contract_refs": (),
        "coverage_contract_ref": "artifact://solution-coverage/1",
        "risk_class": SolutionRiskClass.ELEVATED,
        "allowed_tool_ids": ("catalog.read",),
        "budget_ref": "budget://run/1",
        "verification_policy_ref": "policy://verification/elevated",
        "completion_policy_ref": "policy://completion/solution",
    }
    values.update(changes)
    return SolutionContract(**values)


def test_solution_contract_is_content_addressed_and_ref_only() -> None:
    contract = _contract()

    assert contract.content_hash is not None
    assert "requirement_body" not in SolutionContract.model_fields
    assert "evidence_body" not in SolutionContract.model_fields
    assert "checkpoint" not in SolutionContract.model_fields

    same = _contract(
        solution_run_id=contract.solution_run_id,
        project_id=contract.project_id,
    )
    assert same.content_hash == contract.content_hash


def test_solution_contract_rejects_duplicate_or_tampered_references() -> None:
    with pytest.raises(ValidationError, match="module_revision_refs must be unique"):
        _contract(
            module_revision_refs=(
                "module://power/revision/1",
                "module://power/revision/1",
            )
        )

    with pytest.raises(ValidationError, match="content_hash"):
        _contract(content_hash=_hash("not-the-contract"))


def test_solution_identity_binds_exact_contract_not_only_its_artifact_location() -> None:
    contract = _contract()
    identity = SolutionGraphIdentity(
        run_id=contract.solution_run_id,
        basis_hash=contract.basis_hash,
        basis_project_revision=contract.basis_project_revision,
        graph_revision="solution-v1",
        state_schema_version="solution-state-v1",
        solution_contract_ref="artifact://solution-contract/1",
        solution_contract_hash=contract.content_hash,
    )

    assert identity.solution_contract_hash == contract.content_hash


def test_solution_coverage_is_stably_compiled_from_module_references() -> None:
    contract = compile_solution_coverage_contract(
        basis_hash=_hash("basis"),
        requirement_ref="requirement://revision/3",
        modules=(
            ("power", "module://power/revision/3", "power delivery"),
            ("structure", "module://structure/revision/3", "structural support"),
        ),
    )

    assert [item.key for item in contract.keys] == [
        "solution.power.composition",
        "solution.structure.composition",
    ]
    assert contract.content_hash is not None
