"""Fixed, deterministic engineering-calculation Sandbox profile."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.sandbox.contracts import (
    SandboxNetworkMode,
    SandboxResourcePolicy,
    SandboxTaskSpec,
)
from aidison.sandbox.runner import (
    SandboxExecutionResult,
    SandboxOutputContract,
    SandboxOutputContractRegistry,
)
from aidison.tools.dispatcher import (
    AuthorizedToolInvocation,
    ToolEffectClass,
    ToolRegistry,
    ToolRiskTier,
    ToolSpecRevision,
)

ENGINEERING_CALCULATION_TOOL_ID = "engineering.calculate"
ENGINEERING_PROFILE_REF = "sandbox-profile://engineering-calculator/v1"
ENGINEERING_OUTPUT_SCHEMA_REF = "schema://sandbox/engineering-calculation-output/v1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FlightEnduranceRequest(_StrictModel):
    calculation: Literal["flight_endurance"]
    battery_voltage_v: Decimal = Field(gt=0, le=100)
    battery_capacity_ah: Decimal = Field(gt=0, le=200)
    usable_fraction: Decimal = Field(gt=0, le=1)
    reserve_fraction: Decimal = Field(ge=0, le=Decimal("0.5"))
    average_power_w: Decimal = Field(gt=0, le=100_000)


class ThrustToWeightRequest(_StrictModel):
    calculation: Literal["thrust_to_weight"]
    motor_count: int = Field(ge=1, le=32)
    thrust_per_motor_g: Decimal = Field(gt=0, le=100_000)
    all_up_weight_g: Decimal = Field(gt=0, le=1_000_000)


class PowerComponent(_StrictModel):
    name: str = Field(min_length=1, max_length=120)
    max_current_a: Decimal = Field(ge=0, le=10_000)
    duty_cycle: Decimal = Field(ge=0, le=1)


class PowerBudgetRequest(_StrictModel):
    calculation: Literal["power_budget"]
    battery_voltage_v: Decimal = Field(gt=0, le=100)
    components: tuple[PowerComponent, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def component_names_are_unique(self) -> PowerBudgetRequest:
        names = tuple(component.name.strip().casefold() for component in self.components)
        if len(names) != len(set(names)):
            raise ValueError("power budget component names must be unique")
        return self


EngineeringRequest = Annotated[
    FlightEnduranceRequest | ThrustToWeightRequest | PowerBudgetRequest,
    Field(discriminator="calculation"),
]


class EngineeringCalculationArguments(_StrictModel):
    request: EngineeringRequest


class FlightEnduranceResult(_StrictModel):
    calculation: Literal["flight_endurance"]
    nominal_energy_wh: Decimal
    usable_energy_wh: Decimal
    endurance_minutes: Decimal


class ThrustToWeightResult(_StrictModel):
    calculation: Literal["thrust_to_weight"]
    total_static_thrust_g: Decimal
    thrust_to_weight_ratio: Decimal
    estimated_hover_throttle_percent: Decimal
    meets_2_to_1_recommendation: bool


class PowerBudgetResult(_StrictModel):
    calculation: Literal["power_budget"]
    component_count: int = Field(ge=1, le=64)
    peak_current_a: Decimal
    average_current_a: Decimal
    peak_power_w: Decimal
    average_power_w: Decimal


EngineeringResult = Annotated[
    FlightEnduranceResult | ThrustToWeightResult | PowerBudgetResult,
    Field(discriminator="calculation"),
]


class EngineeringCalculationOutput(_StrictModel):
    schema_version: Literal["engineering-calculation-output/v1"]
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result: EngineeringResult
    assumptions: tuple[str, ...] = Field(max_length=16)


ENGINEERING_TOOL_SPEC = ToolSpecRevision(
    tool_id=ENGINEERING_CALCULATION_TOOL_ID,
    revision=1,
    purpose=(
        "Run one deterministic UAV engineering calculation in a network-disabled, "
        "fixed-entrypoint sandbox."
    ),
    tool_class="engineering_calculation",
    effect_class=ToolEffectClass.NONE,
    risk_tier=ToolRiskTier.T2,
    input_schema_ref="schema://tool/engineering-calculation/v1",
    output_schema_ref=ENGINEERING_OUTPUT_SCHEMA_REF,
)


class EngineeringSandboxRunner(Protocol):
    async def execute(
        self,
        *,
        spec: SandboxTaskSpec,
        broker_paths: dict[str, Path],
    ) -> SandboxExecutionResult: ...


class EngineeringSandboxProfile(_StrictModel):
    image_digest: str = Field(pattern=r"^(?:[^\s@]+@)?sha256:[a-f0-9]{64}$")
    cpu_millis: int = Field(default=250, ge=100, le=1_000)
    memory_mebibytes: int = Field(default=64, ge=64, le=256)
    wall_clock_seconds: int = Field(default=10, ge=1, le=30)

    def task_spec(
        self,
        *,
        invocation: AuthorizedToolInvocation,
        deadline: datetime,
    ) -> SandboxTaskSpec:
        return SandboxTaskSpec(
            invocation_id=_invocation_uuid(invocation),
            run_id=invocation.run_id,
            task_id=invocation.task_id,
            producer_generation=invocation.generation,
            basis_hash=invocation.basis_hash,
            sandbox_profile_ref=ENGINEERING_PROFILE_REF,
            image_digest=self.image_digest,
            entrypoint_id="engineering_calculate",
            arguments=invocation.canonical_arguments,
            input_artifacts=(),
            output_schema_ref=ENGINEERING_OUTPUT_SCHEMA_REF,
            network_mode=SandboxNetworkMode.NONE,
            resource_policy=SandboxResourcePolicy(
                cpu_millis=self.cpu_millis,
                memory_mebibytes=self.memory_mebibytes,
                pids_limit=16,
                fd_limit=64,
                wall_clock_seconds=self.wall_clock_seconds,
                output_max_bytes=32_768,
                output_max_files=1,
                stdio_max_bytes=8_192,
            ),
            secret_policy_ref="secret-policy://none/v1",
            deadline=deadline,
            idempotency_key=invocation.logical_idempotency_key,
        )


class EngineeringSandboxAdapter:
    """Trusted control-plane adapter; it never accepts an image or entrypoint from a model."""

    def __init__(
        self,
        *,
        runner: EngineeringSandboxRunner,
        profile: EngineeringSandboxProfile,
    ) -> None:
        self._runner = runner
        self._profile = profile

    async def execute(
        self,
        *,
        invocation: AuthorizedToolInvocation,
        deadline: datetime,
    ) -> EngineeringCalculationOutput:
        if invocation.tool_id != ENGINEERING_CALCULATION_TOOL_ID:
            raise ValueError("engineering sandbox adapter received another tool")
        arguments = EngineeringCalculationArguments.model_validate(
            invocation.canonical_arguments
        )
        spec = self._profile.task_spec(invocation=invocation, deadline=deadline)
        execution = await self._runner.execute(spec=spec, broker_paths={})
        output = next(
            (item for item in execution.outputs if item.relative_path == "result.json"),
            None,
        )
        if output is None:
            raise ValueError("engineering sandbox returned no result.json")
        parsed = EngineeringCalculationOutput.model_validate_json(output.content)
        expected_hash = _canonical_input_hash(arguments.model_dump(mode="json"))
        if parsed.input_hash != expected_hash:
            raise ValueError("engineering sandbox output does not match authorized input")
        return parsed


def build_engineering_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ENGINEERING_TOOL_SPEC, input_model=EngineeringCalculationArguments)
    return registry


def build_engineering_output_contracts() -> SandboxOutputContractRegistry:
    registry = SandboxOutputContractRegistry()
    registry.register(
        SandboxOutputContract(
            schema_ref=ENGINEERING_OUTPUT_SCHEMA_REF,
            allowed_relative_paths=("result.json",),
            required_relative_paths=("result.json",),
            json_models=(("result.json", EngineeringCalculationOutput),),
        )
    )
    return registry


def _canonical_input_hash(arguments: Mapping[str, object]) -> str:
    from hashlib import sha256

    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode()).hexdigest()


def _invocation_uuid(invocation: AuthorizedToolInvocation) -> UUID:
    return UUID(invocation.logical_invocation_id[:32])


__all__ = [
    "ENGINEERING_CALCULATION_TOOL_ID",
    "ENGINEERING_OUTPUT_SCHEMA_REF",
    "ENGINEERING_PROFILE_REF",
    "ENGINEERING_TOOL_SPEC",
    "EngineeringCalculationArguments",
    "EngineeringCalculationOutput",
    "EngineeringSandboxAdapter",
    "EngineeringSandboxProfile",
    "EngineeringSandboxRunner",
    "build_engineering_output_contracts",
    "build_engineering_tool_registry",
]
