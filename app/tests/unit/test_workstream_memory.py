from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aidison.application.workstream_memory import ModuleWorkstreamMemoryApplication
from aidison.domain.models import (
    Module,
    ModuleMemoryItem,
    ModuleMemoryItemKind,
    ModuleMemoryItemStatus,
    ModuleWorkstream,
)
from aidison.research.consolidation import CoverageDisposition, CoverageMatrixEntry
from aidison.research.coverage import CoverageContract, CoverageKey, CoveragePriority
from aidison.research.langgraph_contracts import ResearchResultStatus, ResultEnvelope
from aidison.workstreams.memory_routing import MemoryRoute, select_memory_route
from tests.fakes import InMemoryDomainStore


def _workstream() -> ModuleWorkstream:
    return ModuleWorkstream(project_id=uuid4(), module_lineage_id=uuid4())


def _item(
    workstream: ModuleWorkstream,
    *,
    basis_hash: str = "a" * 64,
    module_fingerprint: str = "b" * 64,
    coverage_fingerprint: str = "c" * 64,
    freshness_deadline: datetime | None = None,
    status: ModuleMemoryItemStatus = ModuleMemoryItemStatus.ACTIVE,
) -> ModuleMemoryItem:
    return ModuleMemoryItem(
        workstream_id=workstream.id,
        kind=ModuleMemoryItemKind.FINDING,
        stable_key="flight-control.compatibility",
        basis_hash=basis_hash,
        content_hash="d" * 64,
        artifact_ref="artifact://sha256/test",
        evidence_refs=("artifact://sha256/evidence",),
        applicability={
            "module_fingerprint": module_fingerprint,
            "coverage_fingerprint": coverage_fingerprint,
        },
        freshness_deadline=freshness_deadline,
        status=status,
    )


def test_same_module_and_coverage_fingerprint_reuses_memory_across_unrelated_basis_change() -> None:
    workstream = _workstream()
    item = _item(workstream, basis_hash="a" * 64)

    decision = select_memory_route(
        workstream=workstream,
        items=(item,),
        stable_key=item.stable_key,
        module_fingerprint="b" * 64,
        coverage_fingerprint="c" * 64,
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )

    assert decision.route is MemoryRoute.REUSE
    assert decision.item_id == item.id
    assert decision.reason_code == "current_and_applicable"


def test_changed_module_or_coverage_revalidates_instead_of_silently_reusing() -> None:
    workstream = _workstream()
    item = _item(workstream)

    decision = select_memory_route(
        workstream=workstream,
        items=(item,),
        stable_key=item.stable_key,
        module_fingerprint="e" * 64,
        coverage_fingerprint="c" * 64,
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )

    assert decision.route is MemoryRoute.REVALIDATE
    assert decision.item_id == item.id
    assert decision.reason_code == "applicability_changed"


def test_expired_or_non_active_memory_never_enters_the_current_run() -> None:
    workstream = _workstream()
    expired = _item(
        workstream,
        freshness_deadline=datetime(2026, 9, 14, tzinfo=UTC),
    )
    stale = _item(workstream, status=ModuleMemoryItemStatus.STALE)

    decision = select_memory_route(
        workstream=workstream,
        items=(expired, stale),
        stable_key=expired.stable_key,
        module_fingerprint="b" * 64,
        coverage_fingerprint="c" * 64,
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )

    assert decision.route is MemoryRoute.FRESH
    assert decision.item_id is None
    assert decision.reason_code == "no_active_applicable_memory"


def test_most_recent_equivalent_memory_wins_deterministically() -> None:
    workstream = _workstream()
    older = _item(workstream)
    newer = older.model_copy(update={"id": uuid4(), "created_at": older.created_at + timedelta(1)})

    decision = select_memory_route(
        workstream=workstream,
        items=(older, newer),
        stable_key=older.stable_key,
        module_fingerprint="b" * 64,
        coverage_fingerprint="c" * 64,
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )

    assert decision.route is MemoryRoute.REUSE
    assert decision.item_id == newer.id


@pytest.mark.asyncio
async def test_answered_coverage_becomes_idempotent_structured_workstream_memory() -> None:
    project_id = uuid4()
    module = Module(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        key="flight-control",
        name="Flight control",
        responsibility="Stabilize the aircraft",
        lineage_id=uuid4(),
    )
    workstream = ModuleWorkstream(
        project_id=project_id,
        module_lineage_id=module.lineage_id,
    )
    coverage_key = CoverageKey(
        key="flight-control.compatibility",
        question="Which controller compatibility constraints are evidenced?",
        priority=CoveragePriority.MUST,
        module_ids=(str(module.id),),
        required_source_kinds=("evidence",),
    )
    coverage = CoverageContract(
        basis_hash="a" * 64,
        objective="Build an aircraft",
        keys=(coverage_key,),
    )
    result = ResultEnvelope(
        run_id=uuid4(),
        task_id=uuid4(),
        basis_hash=coverage.basis_hash,
        producer_attempt_id=uuid4(),
        producer_generation=1,
        producer_profile_ref="profile://research",
        status=ResearchResultStatus.SUCCEEDED,
        artifact_ref="artifact://sha256/result",
        manifest_hash="b" * 64,
        evidence_refs=("artifact://sha256/evidence",),
        coverage_observation_refs=(),
        unresolved_refs=(),
    )
    matrix = CoverageMatrixEntry(
        coverage_key=coverage_key.key,
        priority=coverage_key.priority,
        status=CoverageDisposition.ANSWERED,
        result_ids=(result.id,),
        admitted_refs=(f"admitted://agent-run-results/{result.id}",),
        evidence_refs=result.evidence_refs,
        observed_source_kinds=("evidence",),
        observed_source_count=1,
        min_distinct_sources=1,
        observed_origin_count=1,
        min_distinct_origins=1,
        missing_source_kinds=(),
        conflict_ids=(),
        requires_independent_verification=False,
        independently_verified=False,
    )
    store = InMemoryDomainStore()
    await store.add_module_workstreams((workstream,))
    memory = ModuleWorkstreamMemoryApplication(store)

    created = await memory.record_answered_coverage(
        project_id=project_id,
        project_revision=3,
        coverage=coverage,
        modules=(module,),
        matrix=(matrix,),
        admitted_results=(result,),
    )
    replayed = await memory.record_answered_coverage(
        project_id=project_id,
        project_revision=3,
        coverage=coverage,
        modules=(module,),
        matrix=(matrix,),
        admitted_results=(result,),
    )

    assert len(created) == 1
    assert created[0].source_result_id == result.id
    assert created[0].applicability["module_fingerprint"]
    assert created[0].applicability["coverage_fingerprint"]
    assert replayed[0].id == created[0].id
    assert len(await store.list_module_memory_items(workstream.id)) == 1
    refreshed_workstream = (await store.list_module_workstreams(project_id))[0]
    assert refreshed_workstream.last_project_revision == 3
    assert refreshed_workstream.optimistic_revision == 2
