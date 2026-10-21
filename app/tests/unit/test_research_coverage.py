from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

from aidison.domain.models import Module
from aidison.research.coverage import (
    CoverageCompileInput,
    CoverageContract,
    CoveragePriority,
    ModuleDepth,
    ModuleStrategy,
    OrchestrationSignals,
    VerificationMode,
    compile_coverage_contract,
    select_orchestration_shape,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _module(
    *,
    key: str,
    acceptance: tuple[str, ...] = (),
    open_questions: tuple[str, ...] = (),
) -> Module:
    return Module(
        project_id=uuid4(),
        requirement_revision_id=uuid4(),
        key=key,
        name=f"{key} module",
        responsibility=f"Own {key} engineering constraints.",
        acceptance=acceptance,
        open_questions=open_questions,
    )


def test_compile_coverage_contract_preserves_hard_acceptance_and_is_order_independent() -> None:
    power = _module(
        key="power",
        acceptance=("Voltage compatibility is proven.", "Peak current margin is proven."),
        open_questions=("What noise trade-off is acceptable?",),
    )
    structure = _module(
        key="structure",
        acceptance=("Mass budget is proven.",),
    )
    compile_input = CoverageCompileInput(
        basis_hash=_hash("basis"),
        objective="Choose a propulsion system.",
        modules=(structure, power),
    )

    first = compile_coverage_contract(compile_input)
    second = compile_coverage_contract(
        compile_input.model_copy(update={"modules": (power, structure)})
    )

    assert first.content_hash == second.content_hash
    assert first.keys == second.keys
    assert [item.priority for item in first.keys].count(CoveragePriority.MUST) == 4
    assert [item.priority for item in first.keys].count(CoveragePriority.SHOULD) == 1
    assert first.keys[0].key == "power.acceptance.01"
    assert first.keys[-1].key == "structure.acceptance.01"
    by_key = {item.key: item for item in first.keys}
    assert by_key["project.objective"].required_source_kinds == ("project_basis",)
    assert by_key["power.acceptance.01"].required_source_kinds == (
        "evidence",
        "project_basis",
    )
    assert by_key["power.question.01"].required_source_kinds == ("evidence",)


def test_compile_coverage_contract_scopes_independent_verification_to_external_must_keys() -> None:
    contract = compile_coverage_contract(
        CoverageCompileInput(
            basis_hash=_hash("basis"),
            objective="Choose a propulsion system.",
            modules=(
                _module(
                    key="power",
                    acceptance=("Voltage compatibility is proven.",),
                    open_questions=("What noise trade-off is acceptable?",),
                ),
            ),
            requires_independent_verification=True,
        )
    )

    by_key = {item.key: item for item in contract.keys}
    assert by_key["project.objective"].requires_independent_verification is False
    assert by_key["power.acceptance.01"].requires_independent_verification is True
    assert by_key["power.acceptance.01"].min_distinct_sources == 2
    assert by_key["power.acceptance.01"].min_distinct_origins == 2


def test_compile_coverage_contract_enforces_depth_source_minimum_on_must_keys() -> None:
    contract = compile_coverage_contract(
        CoverageCompileInput(
            basis_hash=_hash("basis"),
            objective="Choose a propulsion system.",
            modules=(
                _module(
                    key="power",
                    acceptance=("Voltage compatibility is proven.",),
                    open_questions=("What noise trade-off is acceptable?",),
                ),
            ),
            minimum_evidence_sources_for_must=2,
            minimum_evidence_origins_for_must=2,
        )
    )

    by_key = {item.key: item for item in contract.keys}
    assert by_key["power.acceptance.01"].min_distinct_sources == 2
    assert by_key["power.acceptance.01"].min_distinct_origins == 2
    assert by_key["power.question.01"].min_distinct_sources == 1
    assert by_key["power.question.01"].min_distinct_origins == 1


def test_frozen_legacy_contract_null_source_minimum_uses_one_source_default() -> None:
    contract = CoverageContract.model_validate(
        {
            "basis_hash": _hash("legacy-basis"),
            "objective": "Read an existing research contract.",
            "keys": [
                {
                    "key": "power.responsibility",
                    "question": "Own the power constraints.",
                    "priority": "must",
                    "module_ids": ["power-module"],
                    "required_source_kinds": ["evidence"],
                    "min_distinct_sources": None,
                }
            ],
            "content_hash": _hash("historical-null-contract"),
        }
    )

    assert contract.keys[0].min_distinct_sources == 1
    assert contract.content_hash != _hash("historical-null-contract")


def test_compiled_contract_carries_frozen_requirement_context_into_project_scope() -> None:
    contract = compile_coverage_contract(
        CoverageCompileInput(
            basis_hash=_hash("context-basis"),
            objective="Find a feasible camera drone build.",
            modules=(_module(key="power"),),
            context_lines=(
                "Budget context: 500 yuan",
                "Hard constraints: flight safety",
            ),
        )
    )

    assert contract.context_lines == (
        "Budget context: 500 yuan",
        "Hard constraints: flight safety",
    )
    assert {item.key for item in contract.keys} >= {
        "project.objective",
        "project.requirements",
        "power.responsibility",
    }


def test_shape_router_uses_minimal_single_path_without_hard_or_soft_triggers() -> None:
    shape = select_orchestration_shape(
        OrchestrationSignals(
            coverage_key_count=1,
            module_scope_count=1,
            source_strategy_count=1,
            max_concurrency=4,
        )
    )

    assert shape.module_strategy is ModuleStrategy.SINGLE
    assert shape.module_depth is ModuleDepth.DIRECT
    assert shape.verification is VerificationMode.ADMISSION_ONLY
    assert shape.reason_codes == ("default_minimal",)


def test_shape_router_expands_only_with_two_soft_signals_and_respects_concurrency_cap() -> None:
    parallel = select_orchestration_shape(
        OrchestrationSignals(
            coverage_key_count=3,
            module_scope_count=2,
            source_strategy_count=1,
            max_concurrency=3,
        )
    )
    batched = select_orchestration_shape(
        OrchestrationSignals(
            coverage_key_count=3,
            module_scope_count=2,
            source_strategy_count=1,
            max_concurrency=1,
        )
    )

    assert parallel.module_strategy is ModuleStrategy.PARALLEL
    assert parallel.module_depth is ModuleDepth.DECOMPOSED
    assert parallel.reason_codes == ("coverage_fanout", "module_separation")
    assert batched.module_strategy is ModuleStrategy.BATCHED
    assert batched.module_depth is ModuleDepth.DECOMPOSED
    assert batched.reason_codes == (
        "coverage_fanout",
        "module_separation",
        "concurrency_capped",
    )


def test_shape_router_hard_triggers_require_isolation_and_integration_verification() -> None:
    shape = select_orchestration_shape(
        OrchestrationSignals(
            coverage_key_count=1,
            module_scope_count=2,
            source_strategy_count=1,
            max_concurrency=4,
            estimated_context_ratio=0.72,
            requires_sandbox_isolation=True,
            requires_independent_verification=True,
            cross_module_compatibility=True,
        )
    )

    assert shape.module_depth is ModuleDepth.DECOMPOSED
    assert shape.verification is VerificationMode.INTEGRATION
    assert shape.reason_codes == (
        "context_isolation",
        "sandbox_isolation",
        "independent_verification",
        "cross_module_integration",
    )
