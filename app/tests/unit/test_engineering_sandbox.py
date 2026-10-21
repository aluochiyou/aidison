from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.sandbox.engineering import (
    ENGINEERING_CALCULATION_TOOL_ID,
    EngineeringCalculationArguments,
    EngineeringSandboxAdapter,
    EngineeringSandboxProfile,
    build_engineering_tool_registry,
)
from aidison.sandbox.runner import SandboxExecutionResult
from aidison.sandbox.workspace import CollectedSandboxOutput
from aidison.tools.dispatcher import (
    DispatchAuthority,
    ToolCallIntent,
    ToolDispatchDenied,
    ToolDispatcher,
    ToolDispatchPolicy,
    ToolEffectClass,
    ToolProfileGrant,
    ToolRiskTier,
)


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        run_id=uuid4(),
        task_key="power.endurance",
        basis_hash="a" * 64,
        plan_revision=1,
        capability="engineering_calculation",
        input_refs=("project://basis",),
        dependency_task_ids=(),
        coverage_keys=("power.endurance",),
        allowed_tool_ids=(ENGINEERING_CALCULATION_TOOL_ID,),
        budget_ref="budget://run/1",
        idempotency_key="task-power-endurance",
    )


def _authority(task: TaskEnvelope, *, t2_available: bool = True) -> DispatchAuthority:
    return DispatchAuthority(
        task=task,
        grant=ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=1,
            lease_token=uuid4(),
            deadline_ref="deadline://task/power",
            idempotency_prefix="run-1/task-power",
        ),
        profile=ToolProfileGrant(
            profile_ref="profile://engineering/v1",
            allowed_tool_ids=(ENGINEERING_CALCULATION_TOOL_ID,),
            allowed_tool_classes=("engineering_calculation",),
            allowed_effect_classes=(ToolEffectClass.NONE,),
        ),
        policy=ToolDispatchPolicy(
            allowed_tool_classes=("engineering_calculation",),
            allowed_effect_classes=(ToolEffectClass.NONE,),
            available_risk_tiers=(
                (ToolRiskTier.T0, ToolRiskTier.T2)
                if t2_available
                else (ToolRiskTier.T0,)
            ),
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def _intent() -> ToolCallIntent:
    return ToolCallIntent(
        tool_call_id="calculate-endurance-1",
        tool_id=ENGINEERING_CALCULATION_TOOL_ID,
        revision=1,
        arguments={
            "request": {
                "calculation": "flight_endurance",
                "battery_voltage_v": "22.2",
                "battery_capacity_ah": "10",
                "usable_fraction": "0.8",
                "reserve_fraction": "0.15",
                "average_power_w": "450",
            }
        },
        purpose_code="verify_endurance_budget",
    )


def test_engineering_tool_is_t2_and_fails_closed_when_sandbox_is_unavailable() -> None:
    task = _task()
    dispatcher = ToolDispatcher(build_engineering_tool_registry())

    with pytest.raises(ToolDispatchDenied, match="risk tier"):
        dispatcher.authorize(_intent(), authority=_authority(task, t2_available=False))

    invocation = dispatcher.authorize(_intent(), authority=_authority(task))
    assert invocation.risk_tier is ToolRiskTier.T2
    assert invocation.effect_class is ToolEffectClass.NONE
    assert invocation.basis_hash == task.basis_hash
    assert invocation.canonical_arguments["request"]["battery_voltage_v"] == "22.2"


def test_profile_accepts_only_content_addressed_image_references() -> None:
    raw = EngineeringSandboxProfile(image_digest="sha256:" + "b" * 64)
    registry = EngineeringSandboxProfile(
        image_digest="registry.example/engineering@sha256:" + "c" * 64
    )

    assert raw.image_digest.startswith("sha256:")
    assert "@sha256:" in registry.image_digest
    with pytest.raises(ValueError, match="image_digest"):
        EngineeringSandboxProfile(image_digest="engineering:latest")


class _FakeRunner:
    def __init__(self) -> None:
        self.spec = None
        self.broker_paths: dict[str, Path] | None = None

    async def execute(self, *, spec, broker_paths):
        self.spec = spec
        self.broker_paths = broker_paths
        arguments = EngineeringCalculationArguments.model_validate(spec.arguments).model_dump(
            mode="json"
        )
        canonical = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload = {
            "schema_version": "engineering-calculation-output/v1",
            "input_hash": sha256(canonical.encode()).hexdigest(),
            "result": {
                "calculation": "flight_endurance",
                "nominal_energy_wh": "222.0000",
                "usable_energy_wh": "150.9600",
                "endurance_minutes": "20.1280",
            },
            "assumptions": ["constant average power"],
        }
        content = json.dumps(payload).encode()
        return SandboxExecutionResult(
            sandbox_task_id=str(spec.sandbox_task_id),
            duration_milliseconds=12,
            stdout=b"",
            stderr=b"",
            outputs=(
                CollectedSandboxOutput(
                    relative_path="result.json",
                    content=content,
                    content_hash=sha256(content).hexdigest(),
                ),
            ),
        )


@pytest.mark.asyncio
async def test_adapter_freezes_profile_and_rejects_model_control_of_runtime() -> None:
    task = _task()
    authority = _authority(task)
    invocation = ToolDispatcher(build_engineering_tool_registry()).authorize(
        _intent(), authority=authority
    )
    runner = _FakeRunner()
    output = await EngineeringSandboxAdapter(
        runner=runner,
        profile=EngineeringSandboxProfile(image_digest="sha256:" + "d" * 64),
    ).execute(invocation=invocation, deadline=authority.deadline)

    assert output.result.calculation == "flight_endurance"
    assert output.result.endurance_minutes == Decimal("20.1280")
    assert runner.spec is not None
    assert runner.spec.image_digest == "sha256:" + "d" * 64
    assert runner.spec.entrypoint_id == "engineering_calculate"
    assert runner.spec.network_mode.value == "none"
    assert runner.spec.input_artifacts == ()
    assert runner.broker_paths == {}
