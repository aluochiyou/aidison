from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from aidison.application.workspace import build_workspace_projection

NOW = datetime(2026, 8, 7, 1, 2, tzinfo=UTC)


def _item(**values: object) -> SimpleNamespace:
    return SimpleNamespace(**values)


def _snapshot(
    *,
    active_requirement_revision_id: str | None = "req-1",
    active_blueprint_id: str | None = None,
    agent_runs: list[dict[str, object]] | None = None,
    decisions: list[SimpleNamespace] | None = None,
    proposals: list[SimpleNamespace] | None = None,
    requirements_change_proposals: list[SimpleNamespace] | None = None,
    reshape_proposals: list[SimpleNamespace] | None = None,
    change_impact_previews: list[SimpleNamespace] | None = None,
    spend_budget_proposals: list[SimpleNamespace] | None = None,
    execution_plans: list[SimpleNamespace] | None = None,
) -> dict[str, object]:
    return {
        "project": _item(
            id="project-1",
            name="四旋翼",
            goal="做一台可验证的四旋翼",
            stage="research",
            revision=7,
            updated_at="2026-08-06T22:00:00Z",
            active_requirement_revision_id=active_requirement_revision_id,
            active_blueprint_id=active_blueprint_id,
            active_solution_version_id=None,
        ),
        "requirements": [],
        "modules": [
            _item(
                id="module-1",
                name="动力系统",
                responsibility="完成推力与功耗闭合",
                stage="researching",
                open_questions=("电机余量是多少？",),
                dependency_ids=(),
            )
        ],
        "decisions": decisions or [],
        "solution_proposals": proposals or [],
        "impacts": [],
        "requirements_change_proposals": requirements_change_proposals or [],
        "reshape_proposals": reshape_proposals or [],
        "change_impact_previews": change_impact_previews or [],
        "spend_budget_proposals": spend_budget_proposals or [],
        "execution_plans": execution_plans or [],
        "spend_budget_impact_previews": [],
        "agent_runs": agent_runs or [],
    }


def test_workspace_projection_requests_requirements_without_guessing_module_state() -> None:
    projection = build_workspace_projection(
        _snapshot(active_requirement_revision_id=None),
        event_cursor=12,
        generated_at=NOW,
    )

    assert projection.schema_version == "project-workspace.v1"
    assert projection.event_cursor == 12
    assert projection.attention.state == "needs_input"
    assert projection.next_actions[0].kind == "clarify_requirements"
    assert projection.next_actions[0].source_refs[0].type == "project"
    assert projection.modules[0].domain_stage == "researching"
    assert projection.modules[0].work_state is None
    assert projection.attention.since.isoformat() == "2026-08-06T22:00:00+00:00"


def test_workspace_projection_defaults_missing_revision_without_mutating_facts() -> None:
    snapshot = _snapshot(active_requirement_revision_id=None)
    delattr(snapshot["project"], "revision")

    projection = build_workspace_projection(snapshot, event_cursor=0, generated_at=NOW)

    assert projection.project_revision == 1


def test_workspace_projection_selects_pending_decision_deterministically() -> None:
    later_call = build_workspace_projection(
        _snapshot(
            decisions=[
                _item(id="decision-z", status="pending", affected_module_ids=()),
                _item(id="decision-a", status="pending", affected_module_ids=()),
            ]
        ),
        event_cursor=12,
        generated_at=NOW,
    )

    assert later_call.next_actions[0].id == "review_decision:decision-a"
    assert later_call.attention.since.isoformat() == "2026-08-06T22:00:00+00:00"


def test_workspace_projection_prioritizes_human_review_over_background_work() -> None:
    projection = build_workspace_projection(
        _snapshot(
            agent_runs=[
                {
                    "id": "run-1",
                    "kind": "research",
                    "status": "running",
                    "created_at": "2026-08-07T00:00:00Z",
                    "updated_at": "2026-08-07T00:01:00Z",
                    "completed_at": None,
                }
            ],
            decisions=[_item(id="decision-1", status="pending", affected_module_ids=("module-1",))],
        ),
        event_cursor=3,
        generated_at=NOW,
    )

    assert projection.attention.state == "ready_to_review"
    assert projection.next_actions[0].kind == "review_decision"
    assert projection.attention.affected_module_ids == ("module-1",)
    assert projection.work[0].state == "running"


def test_workspace_research_progress_is_read_only_projection_of_plan_and_admissions() -> None:
    projection = build_workspace_projection(
        _snapshot(
            agent_runs=[
                {
                    "id": "run-1",
                    "kind": "research",
                    "status": "running",
                    "created_at": "2026-08-07T00:00:00Z",
                    "updated_at": "2026-08-07T00:01:00Z",
                    "completed_at": None,
                    "research_progress": {
                        "initial_task_count": 3,
                        "admitted_task_count": 4,
                        "succeeded_task_count": 2,
                        "partial_task_count": 2,
                        "additional_task_count": 1,
                    },
                }
            ]
        ),
        event_cursor=3,
        generated_at=NOW,
    )

    [work] = projection.work
    assert work.total_units == 4
    assert work.completed_units == 2
    assert work.partial_units == 2


def test_workspace_projection_presents_pending_requirements_change() -> None:
    projection = build_workspace_projection(
        _snapshot(
            requirements_change_proposals=[
                _item(id="rc-1", status="proposed", created_at="2026-08-06T23:00:00Z"),
            ]
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    assert projection.attention.state == "ready_to_review"
    assert projection.attention.title == "有一份需求与结构变更待你审核"
    assert projection.next_actions[0].kind == "review_requirements_change"
    assert projection.next_actions[0].id == "review_requirements_change:rc-1"


def test_workspace_projection_presents_pending_reshape_as_reviewable() -> None:
    projection = build_workspace_projection(
        _snapshot(
            reshape_proposals=[
                _item(
                    id="reshape-1",
                    status="proposed",
                    affected_module_ids=("module-1",),
                    created_at="2026-08-06T23:00:00Z",
                ),
            ]
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    assert projection.attention.state == "ready_to_review"
    assert projection.attention.title == "有一份结构草案等你确认"
    assert projection.next_actions[0].kind == "review_reshape"
    assert projection.next_actions[0].id == "review_reshape:reshape-1"
    assert projection.attention.affected_module_ids == ("module-1",)


def test_workspace_projection_prefers_pending_decision_over_reshape() -> None:
    projection = build_workspace_projection(
        _snapshot(
            decisions=[_item(id="decision-1", status="pending", affected_module_ids=())],
            reshape_proposals=[
                _item(id="reshape-1", status="proposed", affected_module_ids=()),
            ],
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    assert projection.next_actions[0].kind == "review_decision"


def test_workspace_projection_explains_latest_failed_agent_run() -> None:
    projection = build_workspace_projection(
        _snapshot(
            active_blueprint_id="blueprint-1",
            agent_runs=[
                {
                    "id": "run-old",
                    "kind": "research",
                    "status": "succeeded",
                    "created_at": "2026-08-06T23:00:00Z",
                    "updated_at": "2026-08-06T23:03:00Z",
                    "completed_at": "2026-08-06T23:03:00Z",
                },
                {
                    "id": "run-failed",
                    "kind": "research",
                    "status": "failed",
                    "latest_error": "资料核验未达到最低证据要求",
                    "research_quality": {
                        "outcome": "needs_more_evidence",
                        "gap_coverage_keys": [
                            "power.thrust_margin",
                            "control.flight_safety",
                        ],
                        "coverage": [
                            {
                                "coverage_key": "power.thrust_margin",
                                "module_ids": ["module-1"],
                                "status": "missing",
                            },
                            {
                                "coverage_key": "control.flight_safety",
                                "module_ids": ["module-2", "module-1"],
                                "status": "missing",
                            },
                        ],
                    },
                    "created_at": "2026-08-07T00:00:00Z",
                    "updated_at": "2026-08-07T00:02:00Z",
                    "completed_at": "2026-08-07T00:02:00Z",
                },
            ],
        ),
        event_cursor=9,
        generated_at=NOW,
    )

    assert projection.attention.state == "recoverable_failure"
    assert projection.next_actions[0].kind == "inspect_failure"
    assert projection.work[-1].run_id == "run-failed"
    assert projection.work[-1].completed_units == 0
    assert projection.work[-1].failed_units == 1
    assert projection.work[-1].latest_error == "资料核验未达到最低证据要求"
    assert projection.work[-1].failure_coverage_keys == (
        "control.flight_safety",
        "power.thrust_margin",
    )
    assert projection.work[-1].affected_module_ids == ("module-1", "module-2")
    assert "control.flight_safety" in projection.attention.reason
    assert projection.attention.affected_module_ids == ("module-1", "module-2")
    assert projection.modules[0].domain_stage == "researching"
    assert projection.warnings == ()


def test_workspace_projection_presents_change_impact_preview() -> None:
    """A flushed adjustment batch's ChangeImpactPreview surfaces as reviewable."""
    projection = build_workspace_projection(
        _snapshot(
            change_impact_previews=[
                _item(
                    id="cip-1",
                    status="proposed",
                    affected_module_ids=("module-1", "module-2"),
                    invalidated_refs=(
                        _item(kind="decision", entity_id="dec-1"),
                    ),
                    created_at="2026-08-07T00:30:00Z",
                ),
            ]
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    assert projection.attention.state == "ready_to_review"
    assert "全局影响分析" in projection.attention.title
    assert projection.attention.affected_module_ids == ("module-1", "module-2")
    assert projection.next_actions[0].kind == "review_change_impact"
    assert projection.next_actions[0].id == "review_change_impact:cip-1"
    assert projection.next_actions[0].source_refs[1].type == "change_impact_preview"


def test_workspace_projection_prefers_decision_over_change_impact() -> None:
    """A pending decision takes priority over a change impact preview."""
    projection = build_workspace_projection(
        _snapshot(
            decisions=[_item(id="dec-1", status="pending", affected_module_ids=())],
            change_impact_previews=[
                _item(
                    id="cip-1",
                    status="proposed",
                    affected_module_ids=("module-1",),
                ),
            ],
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    # Decision wins — change impact is lower priority
    assert projection.next_actions[0].kind == "review_decision"


def test_workspace_projection_prefers_spend_budget_over_change_impact() -> None:
    """A pending spend budget takes priority over change impact preview."""
    projection = build_workspace_projection(
        _snapshot(
            spend_budget_proposals=[
                _item(
                    id="sb-1", status="proposed",
                    amount="800.00", currency="CNY",
                    created_at="2026-08-07T00:00:00Z",
                ),
            ],
            change_impact_previews=[
                _item(
                    id="cip-1", status="proposed",
                    affected_module_ids=("module-1",),
                ),
            ],
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    assert projection.next_actions[0].kind == "review_spend_budget"


def test_workspace_projection_presents_pending_execution_plan_as_reviewable() -> None:
    """An approval-gated ExecutionPlanProposal surfaces as the next action."""
    projection = build_workspace_projection(
        _snapshot(
            execution_plans=[
                _item(id="plan-1", status="proposed", created_at="2026-08-06T23:00:00Z"),
            ]
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    assert projection.attention.state == "ready_to_review"
    assert projection.attention.title == "有一份执行计划等你批准"
    assert projection.next_actions[0].kind == "review_execution_plan"
    assert projection.next_actions[0].id == "review_execution_plan:plan-1"
    assert projection.next_actions[0].source_refs[1].type == "execution_plan"


def test_workspace_projection_only_surfaces_proposed_execution_plans() -> None:
    """Resolved plans are no longer actionable and must not gate the view."""
    projection = build_workspace_projection(
        _snapshot(
            execution_plans=[
                _item(id="plan-old", status="approved", created_at="2026-08-06T22:00:00Z"),
                _item(id="plan-new", status="proposed", created_at="2026-08-06T23:00:00Z"),
            ]
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    assert projection.next_actions[0].kind == "review_execution_plan"
    assert projection.next_actions[0].id == "review_execution_plan:plan-new"


def test_workspace_projection_prefers_pending_execution_plan_over_idle_start() -> None:
    """A proposed execution plan preempts the generic start-research invitation."""
    projection = build_workspace_projection(
        _snapshot(
            execution_plans=[
                _item(id="plan-1", status="proposed", created_at="2026-08-06T23:00:00Z"),
            ]
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    assert projection.next_actions[0].kind == "review_execution_plan"
    assert projection.attention.state == "ready_to_review"


def test_workspace_projection_prefers_pending_decision_over_execution_plan() -> None:
    """Human research choices keep priority over an execution plan approval."""
    projection = build_workspace_projection(
        _snapshot(
            decisions=[_item(id="decision-1", status="pending", affected_module_ids=())],
            execution_plans=[
                _item(id="plan-1", status="proposed", created_at="2026-08-06T23:00:00Z"),
            ],
        ),
        event_cursor=5,
        generated_at=NOW,
    )

    assert projection.next_actions[0].kind == "review_decision"
