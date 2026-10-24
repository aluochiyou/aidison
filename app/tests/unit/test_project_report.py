from datetime import UTC, datetime
from uuid import uuid4

from aidison.application.project_report import render_project_report
from aidison.domain.models import (
    Candidate,
    DecisionOption,
    DecisionRequest,
    DecisionStatus,
    EvidenceBinding,
    EvidenceStatus,
    Module,
    Project,
    ProjectStage,
    RequirementRevision,
    RequirementStatus,
    SolutionVersion,
)


def test_report_exports_only_frozen_solution_and_bound_evidence() -> None:
    project_id = uuid4()
    requirement_id = uuid4()
    module = Module(
        project_id=project_id,
        requirement_revision_id=requirement_id,
        key="flight_control",
        name="飞控",
        responsibility="保持姿态稳定",
    )
    evidence = EvidenceBinding(
        project_id=project_id,
        module_id=module.id,
        claim="控制器支持目标接口",
        source_url="https://example.com/spec",
        snapshot_hash="a" * 64,
        span_text="规格原文片段",
        status=EvidenceStatus.SUPPORTED,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    candidate = Candidate(
        project_id=project_id,
        module_id=module.id,
        name="FC-A",
        description="候选飞控",
        evidence_binding_ids=(evidence.id,),
    )
    decision = DecisionRequest(
        project_id=project_id,
        basis_hash="b" * 64,
        question="采用哪款飞控？",
        options=(
            DecisionOption(
                option_id="fc-a",
                label="FC-A",
                summary="证据完整",
                candidate_ids=(candidate.id,),
                evidence_binding_ids=(evidence.id,),
            ),
            DecisionOption(
                option_id="fc-b",
                label="FC-B",
                summary="备用方案",
                candidate_ids=(candidate.id,),
                evidence_binding_ids=(evidence.id,),
            ),
        ),
        status=DecisionStatus.APPROVED,
        selected_option_id="fc-a",
        resolved_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    solution = SolutionVersion(
        project_id=project_id,
        version=1,
        requirement_revision_id=requirement_id,
        basis_hash="b" * 64,
        module_snapshots=(
            {
                "module_id": module.id,
                "candidate_id": candidate.id,
                "candidate_name": candidate.name,
                "rationale": "证据完整",
                "evidence_binding_ids": [evidence.id],
            },
        ),
        evidence_binding_ids=(evidence.id,),
        approved_decision_id=decision.id,
    )
    report = render_project_report(
        project=Project(
            id=project_id,
            name="抛投无人机",
            goal="安全完成抛投",
            stage=ProjectStage.APPROVED,
            active_requirement_revision_id=requirement_id,
            active_solution_version_id=solution.id,
        ),
        requirement=RequirementRevision(
            id=requirement_id,
            project_id=project_id,
            revision=1,
            status=RequirementStatus.APPROVED,
            goal="安全完成抛投",
            approved_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        modules=(module,),
        solution=solution,
        decisions=(decision,),
        candidates=(candidate,),
        evidence=(evidence,),
        findings=(),
        generated_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    assert "抛投无人机 工程决策报告" in report
    assert "FC-A" in report
    assert "https://example.com/spec" in report
    assert "规格原文片段" in report
    assert "确定性生成" in report
