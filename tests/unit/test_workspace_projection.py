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
    jobs: list[dict[str, object]] | None = None,
    attempts: list[dict[str, object]] | None = None,
    decisions: list[SimpleNamespace] | None = None,
    proposals: list[SimpleNamespace] | None = None,
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
        "runtime": {
            "jobs": jobs or [],
            "attempts": attempts or [],
            "delegations": [],
            "join_groups": [],
            "budget_accounts": [],
            "budget_allocations": [],
            "budget_operations": [],
        },
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
            jobs=[
                {
                    "id": "job-1",
                    "parent_job_id": None,
                    "kind": "research_wave",
                    "status": "running",
                    "created_at": "2026-08-07T00:00:00Z",
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


def test_workspace_projection_explains_latest_failed_root_and_child_progress() -> None:
    projection = build_workspace_projection(
        _snapshot(
            jobs=[
                {
                    "id": "job-old",
                    "parent_job_id": None,
                    "kind": "research_wave",
                    "status": "succeeded",
                    "created_at": "2026-08-06T23:00:00Z",
                    "completed_at": "2026-08-06T23:03:00Z",
                },
                {
                    "id": "job-root",
                    "parent_job_id": None,
                    "kind": "research_wave",
                    "status": "failed",
                    "created_at": "2026-08-07T00:00:00Z",
                    "completed_at": "2026-08-07T00:02:00Z",
                },
                {
                    "id": "job-child-1",
                    "parent_job_id": "job-root",
                    "kind": "delegated_research",
                    "status": "succeeded",
                    "created_at": "2026-08-07T00:00:01Z",
                    "completed_at": "2026-08-07T00:01:00Z",
                },
                {
                    "id": "job-child-2",
                    "parent_job_id": "job-root",
                    "kind": "delegated_research",
                    "status": "failed",
                    "created_at": "2026-08-07T00:00:01Z",
                    "completed_at": "2026-08-07T00:01:30Z",
                },
            ],
            attempts=[
                {
                    "id": "attempt-1",
                    "job_id": "job-root",
                    "status": "failed",
                    "normalized_error": "provider_timeout",
                }
            ],
        ),
        event_cursor=9,
        generated_at=NOW,
    )

    assert projection.attention.state == "recoverable_failure"
    assert projection.next_actions[0].kind == "inspect_failure"
    assert projection.work[-1].run_id == "job-root"
    assert projection.work[-1].completed_units == 2
    assert projection.work[-1].failed_units == 1
    assert projection.work[-1].latest_error == "provider_timeout"
    assert projection.modules[0].domain_stage == "researching"
    assert projection.warnings == ("runtime_module_relation_unavailable",)
