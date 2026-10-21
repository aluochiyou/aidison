from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ExecutionGrant,
    ProposalManifest,
    ResearchResultStatus,
    ResultEnvelope,
    TaskEnvelope,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim, AgentRunKind, utc_now
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.solution.composition_executor import (
    SolutionCompositionExecution,
    SolutionCompositionMaterial,
)
from aidison.solution.contracts import SolutionContract, SolutionRiskClass
from aidison.solution.draft_admission import ApprovedModuleCandidate
from aidison.solution.elements import SolutionElement, SolutionElementSet
from aidison.solution.integration import IntegrationCheckReport, IntegrationCheckStatus
from aidison.solution.proposal_manifest import (
    FrozenSolutionProposal,
    SolutionProposalManifest,
    SolutionProposalReadiness,
)
from aidison.solution.single_task_graph import build_single_task_solution_graph


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class _Executor:
    async def execute(self, **_: object) -> SolutionCompositionExecution:
        run_id = uuid4()
        element = SolutionElement(
            element_key="power.pack",
            module_id=uuid4(),
            responsibility="Supply power.",
            selected_candidate_ref="candidate://1",
            configuration_ref="artifact://configuration/1",
            input_interface_ids=(),
            output_interface_ids=(),
            constraint_refs=("artifact://constraints/1",),
            evidence_refs=(),
            decision_refs=("decision://1",),
            unresolved_refs=(),
        )
        result = ResultEnvelope(
            run_id=run_id,
            task_id=uuid4(),
            basis_hash=_hash("basis"),
            producer_attempt_id=uuid4(),
            producer_generation=1,
            producer_profile_ref="profile://solution/1",
            status=ResearchResultStatus.SUCCEEDED,
            artifact_ref="artifact://normalized/1",
            manifest_hash=_hash("normalized"),
            evidence_refs=(),
            coverage_observation_refs=(),
            unresolved_refs=(),
        )
        return SolutionCompositionExecution(
            raw_artifact_ref="artifact://raw/1",
            normalized_artifact_ref="artifact://normalized/1",
            integration_artifact_ref="artifact://integration/1",
            verifier_plan_artifact_ref="artifact://verifier-plan/1",
            elements=SolutionElementSet(elements=(element,), interfaces=()),
            integration=IntegrationCheckReport(findings=(), outcome=IntegrationCheckStatus.PASSED),
            result=result,
            admission=AdmissionRecord(
                run_id=result.run_id,
                result_id=result.id,
                result_manifest_hash=result.manifest_hash,
                disposition=AdmissionDisposition.ACCEPTED,
                reason_codes=("schema_valid",),
                admitted_ref="admitted://result/1",
            ),
        )


class _ProposalFreezer:
    async def freeze(self, **kwargs: object) -> FrozenSolutionProposal:
        contract = kwargs["contract"]
        assert isinstance(contract, SolutionContract)
        proposal = SolutionProposalManifest(
            run_id=contract.solution_run_id,
            project_id=contract.project_id,
            basis_hash=contract.basis_hash,
            run_contract_ref="artifact://solution-contract/1",
            contract_content_hash=contract.content_hash or _hash("contract"),
            coverage_contract_ref=contract.coverage_contract_ref,
            normalized_elements_ref="artifact://normalized/1",
            normalized_elements_hash=_hash("normalized"),
            integration_report_ref="artifact://integration/1",
            integration_report_hash=_hash("integration"),
            accepted_decision_refs=contract.accepted_decision_refs,
            unresolved_refs=(),
            readiness=SolutionProposalReadiness.READY,
        )
        return FrozenSolutionProposal(
            proposal=proposal,
            review_manifest=ProposalManifest(
                run_id=contract.solution_run_id,
                basis_hash=contract.basis_hash,
                artifact_ref="artifact://proposal/1",
                manifest_hash=_hash("proposal"),
            ),
        )


@pytest.mark.asyncio
async def test_solution_graph_exposes_only_public_artifact_refs() -> None:
    basis_hash = _hash("basis")
    run = AgentRun(
        project_id=uuid4(),
        kind=AgentRunKind.SOLUTION,
        idempotency_key="solution-graph-test",
        basis_hash=basis_hash,
        basis_project_revision=1,
        runtime_binding=RuntimeBinding(
            runtime_family=RuntimeFamily.LANGGRAPH_V1,
            runtime_revision="runtime-v1",
            graph_key="solution",
            graph_revision="solution-v1",
            state_schema_version="solution-state-v1",
            profile_binding_ref="profile://solution/1",
            policy_binding_ref="policy://solution/1",
        ),
        thread_id="solution-graph-test",
        run_contract_ref="artifact://solution-contract/1",
    )
    task = TaskEnvelope(
        run_id=run.id,
        task_key="solution.compose",
        basis_hash=basis_hash,
        plan_revision=1,
        capability="solution_composer",
        input_refs=(run.run_contract_ref,),
        dependency_task_ids=(),
        coverage_keys=("solution.power.composition",),
        allowed_tool_ids=(),
        budget_ref="budget://1",
        idempotency_key="solution.compose",
    )
    claim = AgentRunClaim(
        run_id=run.id,
        worker_id="solution-graph-worker",
        generation=1,
        lease_token=uuid4(),
        lease_expires_at=utc_now(),
    )
    grant = ExecutionGrant(
        task_id=task.id,
        attempt_id=uuid4(),
        generation=1,
        lease_token=claim.lease_token,
        deadline_ref="deadline://1",
        idempotency_prefix="solution.compose",
    )
    module_id, candidate_id = uuid4(), uuid4()
    material = SolutionCompositionMaterial(
        contract=SolutionContract(
            solution_run_id=run.id,
            project_id=run.project_id,
            basis_hash=basis_hash,
            basis_project_revision=1,
            requirement_refs=("requirement://1",),
            module_revision_refs=(f"module://{module_id}",),
            accepted_decision_refs=("decision://1",),
            admitted_evidence_refs=(),
            constraint_set_ref="artifact://constraints/1",
            interface_contract_refs=(),
            coverage_contract_ref="artifact://coverage/1",
            risk_class=SolutionRiskClass.STANDARD,
            allowed_tool_ids=(),
            budget_ref="budget://1",
            verification_policy_ref="policy://verification/1",
            completion_policy_ref="policy://completion/1",
        ),
        approved_candidates=(
            ApprovedModuleCandidate(module_id=module_id, candidate_id=candidate_id),
        ),
        approved_evidence_ids=(),
        instruction="Compose a bounded solution.",
    )
    graph = build_single_task_solution_graph(
        checkpointer=InMemorySaver(),
        executor=_Executor(),  # type: ignore[arg-type]
        run=run,
        claim=claim,
        task=task,
        grant=grant,
        material=material,
        proposal_freezer=_ProposalFreezer(),  # type: ignore[arg-type]
    )

    state = await graph.ainvoke(
        {"run_id": str(run.id)},
        {"configurable": {"thread_id": run.thread_id}},
    )

    assert state["run_id"] == str(run.id)
    assert state["raw_artifact_ref"] == "artifact://raw/1"
    assert state["normalized_artifact_ref"] == "artifact://normalized/1"
    assert state["integration_artifact_ref"] == "artifact://integration/1"
    assert state["verifier_plan_artifact_ref"] == "artifact://verifier-plan/1"
    assert state["proposal_readiness"] == "ready"
    assert state["proposal_manifest_ref"] == "artifact://proposal/1"
    assert state["admitted_result_ref"] == "admitted://result/1"
    interrupt = state["__interrupt__"][0]
    assert interrupt.value == {
        "kind": "solution_proposal_review",
        "proposal_manifest_ref": "artifact://proposal/1",
        "proposal_readiness": "ready",
    }
