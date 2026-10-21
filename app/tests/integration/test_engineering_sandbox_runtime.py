from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.sandbox.engineering import (
    ENGINEERING_CALCULATION_TOOL_ID,
    EngineeringSandboxAdapter,
    EngineeringSandboxProfile,
    build_engineering_output_contracts,
    build_engineering_tool_registry,
)
from aidison.sandbox.runner import LocalDockerSandboxRunner
from aidison.tools.dispatcher import (
    DispatchAuthority,
    ToolCallIntent,
    ToolDispatcher,
    ToolDispatchPolicy,
    ToolEffectClass,
    ToolProfileGrant,
    ToolRiskTier,
)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("calculation_request", "result_field", "expected"),
    (
        (
            {
                "calculation": "flight_endurance",
                "battery_voltage_v": "22.2",
                "battery_capacity_ah": "10",
                "usable_fraction": "0.8",
                "reserve_fraction": "0.15",
                "average_power_w": "450",
            },
            "endurance_minutes",
            Decimal("20.1280"),
        ),
        (
            {
                "calculation": "thrust_to_weight",
                "motor_count": 4,
                "thrust_per_motor_g": "1000",
                "all_up_weight_g": "2000",
            },
            "thrust_to_weight_ratio",
            Decimal("2.0000"),
        ),
        (
            {
                "calculation": "power_budget",
                "battery_voltage_v": "22.2",
                "components": [
                    {"name": "motors", "max_current_a": "10", "duty_cycle": "1"},
                    {"name": "avionics", "max_current_a": "2", "duty_cycle": "0.5"},
                ],
            },
            "average_power_w",
            Decimal("244.2000"),
        ),
    ),
)
async def test_content_addressed_engineering_profile_runs_in_hardened_docker(
    tmp_path: Path,
    calculation_request: dict[str, object],
    result_field: str,
    expected: object,
) -> None:
    image_digest = os.getenv("AIDISON_ENGINEERING_SANDBOX_IMAGE_DIGEST")
    if not image_digest:
        pytest.skip("AIDISON_ENGINEERING_SANDBOX_IMAGE_DIGEST is not configured")
    task = TaskEnvelope(
        run_id=uuid4(),
        task_key="power.endurance",
        basis_hash="a" * 64,
        plan_revision=1,
        capability="engineering_calculation",
        input_refs=("project://basis",),
        dependency_task_ids=(),
        coverage_keys=("power.endurance",),
        allowed_tool_ids=(ENGINEERING_CALCULATION_TOOL_ID,),
        budget_ref="budget://sandbox-runtime-test",
        idempotency_key="sandbox-runtime-test",
    )
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    authority = DispatchAuthority(
        task=task,
        grant=ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=1,
            lease_token=uuid4(),
            deadline_ref="deadline://sandbox-runtime-test",
            idempotency_prefix="sandbox-runtime-test",
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
            available_risk_tiers=(ToolRiskTier.T2,),
        ),
        deadline=deadline,
    )
    invocation = ToolDispatcher(build_engineering_tool_registry()).authorize(
        ToolCallIntent(
            tool_call_id="sandbox-runtime-test",
            tool_id=ENGINEERING_CALCULATION_TOOL_ID,
            revision=1,
            arguments={"request": calculation_request},
            purpose_code="verify_endurance_budget",
        ),
        authority=authority,
    )

    output = await EngineeringSandboxAdapter(
        runner=LocalDockerSandboxRunner(
            workspace_root=tmp_path / "workspaces",
            output_contracts=build_engineering_output_contracts(),
        ),
        profile=EngineeringSandboxProfile(image_digest=image_digest),
    ).execute(invocation=invocation, deadline=deadline)

    assert output.result.calculation == calculation_request["calculation"]
    assert getattr(output.result, result_field) == expected
    assert list((tmp_path / "workspaces").iterdir()) == []
