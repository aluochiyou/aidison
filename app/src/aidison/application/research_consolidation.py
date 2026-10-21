"""Application seam that turns admitted Research result refs into a sufficiency decision."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ContentAddressedArtifactStore,
)
from aidison.infrastructure.database import session_scope
from aidison.research.consolidation import (
    ClaimKey,
    ConsolidationInput,
    ConsolidationSnapshot,
    CoverageObservationStatus,
    ResearchCoverageObservation,
    SufficiencyPolicy,
    consolidate_research,
)
from aidison.research.coverage import CoverageContract
from aidison.research.evidence_diagnostics import (
    ResearchEvidenceMaterializationReport,
    ResearchSourceCollectionReport,
    ResearchSourceUnavailableReport,
)
from aidison.runtime.agent_runs import AgentRun


@dataclass(frozen=True, slots=True)
class ResearchConsolidation:
    snapshot: ConsolidationSnapshot
    artifact_ref: str
    artifact_hash: str


class ResearchConsolidationApplication:
    """Read only accepted results; never promote raw model output into evidence."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root

    async def consolidate(
        self,
        *,
        run: AgentRun,
        coverage: CoverageContract,
        policy: SufficiencyPolicy | None = None,
    ) -> ResearchConsolidation:
        async with session_scope(self._session_factory) as session:
            snapshot = await read_research_consolidation_snapshot(
                session=session,
                artifact_root=self._artifact_root,
                run=run,
                coverage=coverage,
                policy=policy,
            )
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_consolidation_snapshot",
                value=snapshot.model_dump(mode="json"),
            )
        return ResearchConsolidation(
            snapshot=snapshot,
            artifact_ref=artifact.ref,
            artifact_hash=artifact.content_hash,
        )


async def read_research_consolidation_snapshot(
    *,
    session: AsyncSession,
    artifact_root: Path,
    run: AgentRun,
    coverage: CoverageContract,
    policy: SufficiencyPolicy | None = None,
) -> ConsolidationSnapshot:
    """Rebuild the research-quality projection without creating an Artifact.

    This is intentionally the read-side counterpart of ``consolidate``.  A UI
    refresh must never create a consolidation artifact, mutate a Run, or make
    missing evidence look admitted merely because it was inspected.
    """

    if coverage.basis_hash != run.basis_hash:
        raise ValueError("Coverage Contract basis does not match Research AgentRun")
    results = await AgentResultStore(session).admitted_results(run_id=run.id)
    artifacts = ContentAddressedArtifactStore(
        session,
        artifact_root,
        record_integrity_status=False,
    )
    observations: list[ResearchCoverageObservation] = []
    for result in results:
        for ref in result.coverage_observation_refs:
            value = await artifacts.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=ref,
                expected_kind="research_coverage_observation",
            )
            observation = ResearchCoverageObservation.model_validate(value)
            if observation.result_id != result.id:
                raise ValueError("coverage observation is not owned by its ResultEnvelope")
            observations.append(observation)
    observations.extend(_project_basis_observations(run=run, coverage=coverage))
    return consolidate_research(
        ConsolidationInput(
            coverage_contract=coverage,
            observations=tuple(observations),
            policy=policy or SufficiencyPolicy(),
        )
    )


async def read_research_evidence_diagnostics(
    *,
    session: AsyncSession,
    artifact_root: Path,
    run: AgentRun,
) -> dict[str, dict[str, object]]:
    """Aggregate non-admitted evidence explanations by affected Coverage Key.

    Reports are diagnostics only.  They intentionally read accepted producer
    Results, but never turn a rejected model claim into Coverage evidence.
    """

    results = await AgentResultStore(session).admitted_results(run_id=run.id)
    artifacts = ContentAddressedArtifactStore(
        session,
        artifact_root,
        record_integrity_status=False,
    )
    counts: dict[str, int] = {}
    reasons: dict[str, set[str]] = {}
    for result in results:
        for ref in result.unresolved_refs:
            try:
                value = await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=ref,
                    expected_kind="research_evidence_materialization_report",
                )
                report = ResearchEvidenceMaterializationReport.model_validate(value)
            except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
                # An unrelated unresolved ref or a damaged optional diagnostic
                # must not make valid Coverage evidence unavailable.
                continue
            if report.result_id != result.id or report.task_id != result.task_id:
                continue
            for issue in report.issues:
                affected_keys = (
                    (issue.coverage_key,)
                    if issue.coverage_key in issue.assigned_coverage_keys
                    else issue.assigned_coverage_keys
                )
                for coverage_key in affected_keys:
                    counts[coverage_key] = counts.get(coverage_key, 0) + 1
                    reasons.setdefault(coverage_key, set()).update(issue.reason_codes)
    return {
        key: {
            "rejected_claim_count": counts[key],
            "rejected_reason_codes": sorted(reasons[key]),
        }
        for key in sorted(counts)
    }


async def read_research_source_collection_diagnostics(
    *,
    session: AsyncSession,
    artifact_root: Path,
    run: AgentRun,
) -> dict[str, dict[str, object]]:
    """Aggregate trusted collection inputs without treating them as evidence."""

    results = await AgentResultStore(session).admitted_results(run_id=run.id)
    artifacts = ContentAddressedArtifactStore(
        session,
        artifact_root,
        record_integrity_status=False,
    )
    source_counts: dict[str, int] = {}
    profiles: dict[str, set[str]] = {}
    source_kinds: dict[str, set[str]] = {}
    unavailable_reasons: dict[str, set[str]] = {}
    for result in results:
        ref = result.source_collection_report_ref
        if ref is not None:
            try:
                value = await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=ref,
                    expected_kind="research_source_collection_report",
                )
                report = ResearchSourceCollectionReport.model_validate(value)
            except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
                report = None
            if (
                report is not None
                and report.result_id == result.id
                and report.task_id == result.task_id
            ):
                for coverage_key in report.coverage_keys:
                    source_counts[coverage_key] = (
                        source_counts.get(coverage_key, 0) + report.collected_source_count
                    )
                    profiles.setdefault(coverage_key, set()).add(report.collection_profile)
                    source_kinds.setdefault(coverage_key, set()).update(
                        report.collected_source_kinds
                    )
                for failure in report.unavailable_coverage_reasons:
                    if failure.coverage_key in report.coverage_keys:
                        unavailable_reasons.setdefault(failure.coverage_key, set()).add(
                            failure.reason_code
                        )
        for unresolved_ref in result.unresolved_refs:
            try:
                value = await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=unresolved_ref,
                    expected_kind="research_source_unavailable_report",
                )
                unavailable = ResearchSourceUnavailableReport.model_validate(value)
            except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
                continue
            if unavailable.result_id != result.id or unavailable.task_id != result.task_id:
                continue
            for coverage_key in unavailable.coverage_keys:
                unavailable_reasons.setdefault(coverage_key, set()).add(
                    unavailable.reason_code
                )
    keys = set(source_counts) | set(unavailable_reasons)
    return {
        key: {
            "collected_source_count": source_counts.get(key, 0),
            "collection_profiles": sorted(profiles.get(key, set())),
            "collected_source_kinds": sorted(source_kinds.get(key, set())),
            "unavailable_reason_codes": sorted(unavailable_reasons.get(key, set())),
        }
        for key in sorted(keys)
    }


__all__ = [
    "ResearchConsolidation",
    "ResearchConsolidationApplication",
    "read_research_consolidation_snapshot",
    "read_research_evidence_diagnostics",
    "read_research_source_collection_diagnostics",
]


def _project_basis_observations(
    *,
    run: AgentRun,
    coverage: CoverageContract,
) -> tuple[ResearchCoverageObservation, ...]:
    """Represent frozen Project requirements without asking a model to cite them.

    The Project basis supplies the specification side of a coverage obligation.
    It cannot supply the independent external ``evidence`` side, so a model
    still needs an admitted collected source before a MUST key is complete.
    """

    if run.coverage_contract_ref is None:
        return ()
    observations: list[ResearchCoverageObservation] = []
    for key in coverage.keys:
        if "project_basis" not in key.required_source_kinds:
            continue
        result_id = uuid5(NAMESPACE_URL, f"aidison://project-basis/{run.id}/{key.key}")
        observations.append(
            ResearchCoverageObservation(
                result_id=result_id,
                admitted_ref=f"admitted://project-basis/{run.id}/{key.key}",
                basis_hash=run.basis_hash,
                coverage_key=key.key,
                status=CoverageObservationStatus.ANSWERED,
                source_kinds=("project_basis",),
                source_ids=(f"project-basis:{run.project_id}",),
                evidence_refs=(run.coverage_contract_ref,),
                claim_key=ClaimKey(
                    subject_identity=f"project:{run.project_id}",
                    predicate=(
                        "objective" if key.key == "project.objective" else "approved_requirement"
                    ),
                    applicability=(
                        f"project-revision:{run.basis_project_revision}/coverage:{key.key}"
                    ),
                    normalization_schema="project-coverage-v1",
                ),
                value_hash=sha256(key.question.encode()).hexdigest(),
            )
        )
    return tuple(observations)
