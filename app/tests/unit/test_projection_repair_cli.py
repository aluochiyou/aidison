from __future__ import annotations

from argparse import Namespace
from uuid import uuid4

import pytest
from pytest import CaptureFixture, MonkeyPatch

from aidison.application.event_replay import (
    AgentResultProjectionRepairPreview,
    AgentRunProjectionRepairPreview,
    ExecutionPlanProjectionRepairPreview,
)
from aidison.operations import projection_repair


def test_apply_requires_the_hash_returned_by_a_reviewed_preview() -> None:
    with pytest.raises(SystemExit, match="2"):
        projection_repair.main(
            [
                "--project-id",
                str(uuid4()),
                "--execution-plan-id",
                str(uuid4()),
                "--apply",
            ]
        )


def test_preview_rejects_an_apply_only_precondition() -> None:
    with pytest.raises(SystemExit, match="2"):
        projection_repair.main(
            [
                "--project-id",
                str(uuid4()),
                "--execution-plan-id",
                str(uuid4()),
                "--expected-current-relation-hash",
                "reviewed-hash",
            ]
        )


def test_preview_cli_dispatches_a_read_only_operation(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    project_id = uuid4()
    execution_plan_id = uuid4()
    captured: dict[str, object] = {}
    preview = ExecutionPlanProjectionRepairPreview(
        project_id=project_id,
        execution_plan_id=execution_plan_id,
        event_cursor=17,
        event_count=3,
        current_relation_hash="current",
        replayed_relation_hash="replayed",
        repair_required=True,
    )

    async def fake_run(args: Namespace) -> projection_repair.ProjectionRepairCommandReport:
        captured["apply"] = args.apply
        captured["expected_current_relation_hash"] = args.expected_current_relation_hash
        return projection_repair.ProjectionRepairCommandReport(preview=preview, applied=False)

    monkeypatch.setattr(projection_repair, "_run", fake_run)

    assert (
        projection_repair.main(
            [
                "--project-id",
                str(project_id),
                "--execution-plan-id",
                str(execution_plan_id),
            ]
        )
        == 0
    )

    assert captured == {"apply": False, "expected_current_relation_hash": ""}
    output = capsys.readouterr().out
    assert '"applied": false' in output
    assert '"schema_version": "projection-repair.v1"' in output


def test_apply_cli_passes_the_reviewed_hash_to_the_command_boundary(
    monkeypatch: MonkeyPatch,
) -> None:
    project_id = uuid4()
    execution_plan_id = uuid4()
    captured: dict[str, object] = {}
    preview = ExecutionPlanProjectionRepairPreview(
        project_id=project_id,
        execution_plan_id=execution_plan_id,
        event_cursor=17,
        event_count=3,
        current_relation_hash="current",
        replayed_relation_hash="replayed",
        repair_required=True,
    )

    async def fake_run(args: Namespace) -> projection_repair.ProjectionRepairCommandReport:
        captured["apply"] = args.apply
        captured["expected_current_relation_hash"] = args.expected_current_relation_hash
        return projection_repair.ProjectionRepairCommandReport(preview=preview, applied=True)

    monkeypatch.setattr(projection_repair, "_run", fake_run)

    assert (
        projection_repair.main(
            [
                "--project-id",
                str(project_id),
                "--execution-plan-id",
                str(execution_plan_id),
                "--apply",
                "--expected-current-relation-hash",
                "current",
            ]
        )
        == 0
    )
    assert captured == {"apply": True, "expected_current_relation_hash": "current"}


def test_agent_run_preview_uses_the_same_explicit_maintenance_boundary(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    project_id = uuid4()
    agent_run_id = uuid4()
    preview = AgentRunProjectionRepairPreview(
        project_id=project_id,
        agent_run_id=agent_run_id,
        event_cursor=21,
        event_count=4,
        current_relation_hash="current",
        replayed_relation_hash="replayed",
        repair_required=True,
    )

    async def fake_run(args: Namespace) -> projection_repair.ProjectionRepairCommandReport:
        assert args.execution_plan_id is None
        assert args.agent_run_id == agent_run_id
        return projection_repair.ProjectionRepairCommandReport(preview=preview, applied=False)

    monkeypatch.setattr(projection_repair, "_run", fake_run)

    assert (
        projection_repair.main(
            ["--project-id", str(project_id), "--agent-run-id", str(agent_run_id)]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"aggregate_type": "agent_run"' in output
    assert f'"agent_run_id": "{agent_run_id}"' in output


def test_result_preview_keeps_the_admission_relation_explicit(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    project_id = uuid4()
    result_id = uuid4()
    preview = AgentResultProjectionRepairPreview(
        project_id=project_id,
        agent_run_result_id=result_id,
        event_cursor=22,
        event_count=2,
        current_relation_hash="current",
        replayed_relation_hash="replayed",
        repair_required=True,
    )

    async def fake_run(args: Namespace) -> projection_repair.ProjectionRepairCommandReport:
        assert args.agent_run_result_id == result_id
        return projection_repair.ProjectionRepairCommandReport(preview=preview, applied=False)

    monkeypatch.setattr(projection_repair, "_run", fake_run)

    assert (
        projection_repair.main(
            ["--project-id", str(project_id), "--agent-run-result-id", str(result_id)]
        )
        == 0
    )
    assert '"aggregate_type": "agent_run_result"' in capsys.readouterr().out
