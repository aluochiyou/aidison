from __future__ import annotations

from hashlib import sha256
from itertools import permutations
from uuid import uuid4

from aidison.research.adaptive_planning import (
    AdaptivePlanAction,
    AdaptivePlanningInput,
    AdaptivePlanningPolicy,
    VerifierRoutingInput,
    VerifierRoutingPolicy,
    build_bounded_gap_patch,
    route_verifier_tasks,
)
from aidison.research.consolidation import (
    ClaimKey,
    ConsolidationInput,
    CoverageObservationStatus,
    ResearchCoverageObservation,
    SufficiencyPolicy,
    consolidate_research,
)
from aidison.research.coverage import CoverageContract, CoverageKey, CoveragePriority


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _contract(*, independent: bool = False) -> CoverageContract:
    return CoverageContract(
        basis_hash=_hash("basis"),
        objective="Choose a compatible power system.",
        keys=(
            CoverageKey(
                key="power.voltage",
                question="What voltage is compatible?",
                priority=CoveragePriority.MUST,
                module_ids=("power",),
                required_source_kinds=("specification",),
                requires_independent_verification=independent,
            ),
            CoverageKey(
                key="power.cost",
                question="What is the expected cost?",
                priority=CoveragePriority.SHOULD,
                module_ids=("power",),
                required_source_kinds=("evidence",),
            ),
        ),
    )


def _answered(*, value: str) -> ResearchCoverageObservation:
    return ResearchCoverageObservation(
        result_id=uuid4(),
        admitted_ref=f"admitted://result/{value}",
        basis_hash=_hash("basis"),
        coverage_key="power.voltage",
        status=CoverageObservationStatus.ANSWERED,
        source_kinds=("specification",),
        evidence_refs=(f"evidence://{value}",),
        claim_key=ClaimKey(
            subject_identity="battery-pack-v1",
            predicate="nominal_voltage",
            applicability="hardware-revision-a",
            normalization_schema="voltage-v1",
        ),
        value_hash=_hash(value),
    )


def _input(*, snapshot, policy: AdaptivePlanningPolicy) -> AdaptivePlanningInput:
    return AdaptivePlanningInput(
        run_id=uuid4(),
        basis_hash=_hash("basis"),
        plan_revision=1,
        existing_task_count=2,
        patch_revision_count=0,
        consecutive_no_progress=0,
        coverage_contract=_contract(),
        snapshot=snapshot,
        allowed_tool_ids=("web.search",),
        budget_ref="budget://research/1",
        policy=policy,
    )


def test_gap_patch_targets_only_current_must_gap_and_is_deterministic() -> None:
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )
    value = _input(snapshot=snapshot, policy=AdaptivePlanningPolicy())

    patch = build_bounded_gap_patch(value)

    assert patch.action is AdaptivePlanAction.CREATE_GAP_PATCH
    assert patch.patch is not None
    assert patch.patch.target_coverage_keys == ("power.voltage",)
    assert patch.patch.next_plan_revision == 2
    assert patch.patch.tasks[0].coverage_keys == ("power.voltage",)
    assert patch.patch.tasks[0].allowed_tool_ids == ("web.search",)


def test_gap_patch_stops_when_bounds_or_no_progress_are_exhausted() -> None:
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )
    exhausted = _input(
        snapshot=snapshot,
        policy=AdaptivePlanningPolicy(max_patch_revisions=0, partial_delivery_allowed=True),
    )
    no_progress = _input(
        snapshot=snapshot,
        policy=AdaptivePlanningPolicy(max_consecutive_no_progress=0),
    ).model_copy(update={"consecutive_no_progress": 1})
    task_limited = _input(
        snapshot=snapshot,
        policy=AdaptivePlanningPolicy(max_total_tasks=2),
    )

    exhausted_decision = build_bounded_gap_patch(exhausted)
    no_progress_decision = build_bounded_gap_patch(no_progress)
    task_limit_decision = build_bounded_gap_patch(task_limited)

    assert exhausted_decision.action is AdaptivePlanAction.STOP_PARTIAL
    assert exhausted_decision.reason_codes == ("gap_patch_revision_limit",)
    assert no_progress_decision.action is AdaptivePlanAction.STOP_BLOCKED
    assert no_progress_decision.reason_codes == ("gap_patch_no_progress_limit",)
    assert task_limit_decision.action is AdaptivePlanAction.STOP_BLOCKED
    assert task_limit_decision.reason_codes == ("gap_patch_task_limit",)


def test_unbounded_policy_keeps_creating_gap_patches_after_legacy_bounds() -> None:
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )
    value = _input(
        snapshot=snapshot,
        policy=AdaptivePlanningPolicy(
            max_patch_revisions=None,
            max_total_tasks=None,
            max_tasks_per_patch=None,
            max_consecutive_no_progress=None,
        ),
    ).model_copy(
        update={
            "existing_task_count": 10_000,
            "patch_revision_count": 100,
            "consecutive_no_progress": 100,
        }
    )

    decision = build_bounded_gap_patch(value)

    assert decision.action is AdaptivePlanAction.CREATE_GAP_PATCH
    assert decision.patch is not None
    assert decision.patch.patch_index == 101


def test_repeated_observable_gap_stops_for_user_steering_not_a_breadth_limit() -> None:
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(),
            policy=SufficiencyPolicy(evidence_expansion_available=True),
        )
    )
    value = _input(
        snapshot=snapshot,
        policy=AdaptivePlanningPolicy(
            max_patch_revisions=None,
            max_total_tasks=None,
            max_tasks_per_patch=None,
            max_consecutive_no_progress=None,
        ),
    ).model_copy(
        update={
            "repeated_unresolved_signature": (
                "power.voltage|missing|0|0|specification|quote_not_unique_or_not_exact",
            )
        }
    )

    decision = build_bounded_gap_patch(value)

    assert decision.action is AdaptivePlanAction.STOP_BLOCKED
    assert decision.reason_codes == ("repeated_unresolved_gap_requires_user_steering",)


def test_verifier_is_routed_for_conflict_not_for_ordinary_answer() -> None:
    conflict_snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(_answered(value="12v"), _answered(value="16v")),
            policy=SufficiencyPolicy(verification_available=True),
        )
    )
    ordinary_snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(_answered(value="12v"),),
            policy=SufficiencyPolicy(),
        )
    )
    routing_input = VerifierRoutingInput(
        run_id=uuid4(),
        basis_hash=_hash("basis"),
        plan_revision=1,
        coverage_contract=_contract(),
        snapshot=conflict_snapshot,
        allowed_tool_ids=("web.search",),
        budget_ref="budget://research/1",
        policy=VerifierRoutingPolicy(max_verifier_tasks=2),
    )
    ordinary_input = routing_input.model_copy(update={"snapshot": ordinary_snapshot})

    routed = route_verifier_tasks(routing_input)
    ordinary = route_verifier_tasks(ordinary_input)

    assert len(routed.tasks) == 1
    assert routed.tasks[0].reason_codes == ("coverage_conflict",)
    assert routed.tasks[0].conflict_ids == conflict_snapshot.sufficiency.conflict_ids
    assert ordinary.tasks == ()
    assert ordinary.reason_codes == ("verifier_not_required",)


def test_contract_or_high_impact_can_route_verifier_without_a_conflict() -> None:
    contract = _contract(independent=True)
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=contract,
            observations=(_answered(value="12v"),),
            policy=SufficiencyPolicy(verification_available=True),
        )
    )
    routed = route_verifier_tasks(
        VerifierRoutingInput(
            run_id=uuid4(),
            basis_hash=_hash("basis"),
            plan_revision=1,
            coverage_contract=contract,
            snapshot=snapshot,
            allowed_tool_ids=("web.search",),
            budget_ref="budget://research/1",
            policy=VerifierRoutingPolicy(
                max_verifier_tasks=2,
                high_impact_coverage_keys=("power.voltage",),
            ),
        )
    )

    assert len(routed.tasks) == 1
    assert routed.tasks[0].reason_codes == (
        "coverage_requires_independent_verification",
        "high_impact_coverage",
    )


def test_verifier_limit_refuses_to_create_a_partial_verifier_batch() -> None:
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=_contract(),
            observations=(_answered(value="12v"), _answered(value="16v")),
            policy=SufficiencyPolicy(verification_available=True),
        )
    )
    decision = route_verifier_tasks(
        VerifierRoutingInput(
            run_id=uuid4(),
            basis_hash=_hash("basis"),
            plan_revision=1,
            coverage_contract=_contract(),
            snapshot=snapshot,
            allowed_tool_ids=("web.search",),
            budget_ref="budget://research/1",
            policy=VerifierRoutingPolicy(max_verifier_tasks=0),
        )
    )

    assert decision.tasks == ()
    assert decision.reason_codes == ("verifier_task_limit",)


def test_verifier_route_is_order_independent_for_multiple_conflicts() -> None:
    observations = (_answered(value="12v"), _answered(value="16v"))
    run_id = uuid4()
    expected = None
    for ordered in permutations(observations):
        snapshot = consolidate_research(
            ConsolidationInput(
                coverage_contract=_contract(),
                observations=ordered,
                policy=SufficiencyPolicy(verification_available=True),
            )
        )
        routed = route_verifier_tasks(
            VerifierRoutingInput(
                run_id=run_id,
                basis_hash=_hash("basis"),
                plan_revision=1,
                coverage_contract=_contract(),
                snapshot=snapshot,
                allowed_tool_ids=("web.search",),
                budget_ref="budget://research/1",
                policy=VerifierRoutingPolicy(max_verifier_tasks=2),
            )
        )
        if expected is None:
            expected = routed.tasks[0].model_dump(exclude={"run_id"})
        else:
            assert routed.tasks[0].model_dump(exclude={"run_id"}) == expected
