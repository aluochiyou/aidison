"""R1 single-task research execution; Domain writes remain outside this module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.application.service import ProjectApplication
from aidison.domain.models import ModuleMemoryItem
from aidison.infrastructure.agent_results import AgentResultStore
from aidison.infrastructure.agent_run_controls import AgentRunControlRequestStore
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ContentAddressedArtifactStore,
)
from aidison.infrastructure.database import session_scope
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.consolidation import (
    ClaimKey,
    CoverageObservationStatus,
    ResearchCoverageObservation,
)
from aidison.research.evidence_admission import (
    EvidenceAdmissionInput,
    EvidenceAdmissionPolicy,
    EvidenceAdmissionStatus,
    EvidenceCandidate,
    EvidenceRelation,
    admit_evidence_candidate,
    materialize_admitted_binding,
)
from aidison.research.evidence_diagnostics import (
    ResearchEvidenceMaterializationIssue,
    ResearchEvidenceMaterializationReport,
    ResearchSourceCollectionReport,
    ResearchSourceUnavailableReport,
)
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ExecutionGrant,
    ProposalManifest,
    ResearchResultStatus,
    ResultEnvelope,
    TaskEnvelope,
)
from aidison.research.researcher import GatewayResearchOutput
from aidison.research.source_collection import (
    CollectedResearchSource,
    NoopResearchSourceCollector,
    ResearchSourceCollectionError,
    ResearchSourceCollector,
)
from aidison.research.source_observations import (
    ObservationOutcome,
    SourceKind,
    SourceObservation,
    SourceSnapshot,
    SourceSpan,
    SourceSpanKind,
    source_origin_key,
)
from aidison.runtime.agent_runs import AgentRun, AgentRunClaim
from aidison.runtime.control_requests import ControlRequestKind, ControlRequestStatus


class SingleTaskResearcher(Protocol):
    async def research(
        self,
        *,
        question: str,
        input_refs: tuple[str, ...],
        steering_instructions: tuple[str, ...] = (),
        evidence_context: tuple[CollectedResearchSource, ...] = (),
    ) -> object: ...


@runtime_checkable
class RunScopedSingleTaskResearcher(Protocol):
    """Optional production-only capability that owns a physical model call.

    Test researchers retain the small pure ``research`` seam.  The deployed
    researcher implements this richer protocol so it can bind the current
    Run claim and Task grant to ModelGateway's durable budget operation.
    """

    async def research_for_task(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        question: str,
        input_refs: tuple[str, ...],
        steering_instructions: tuple[str, ...] = (),
        evidence_context: tuple[CollectedResearchSource, ...] = (),
    ) -> object: ...


class AgentRunCancelled(RuntimeError):
    """Raised after the current worker records a requested cancellation safely."""


class AgentRunPaused(RuntimeError):
    """Raised after the worker records pause before a provider dispatch."""


class ResearchSourcesUnavailable(RuntimeError):
    """Raised when a bounded Research task has no trusted source material."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"Research sources unavailable before provider dispatch: {reason_code}")


class WorkstreamMemoryReuseUnavailable(RuntimeError):
    """A stored memory item cannot safely seed this current Run.

    The caller must fall back to a normal fresh research task.  It must never
    accept a partial or unverifiable historical item merely to avoid a model
    call.
    """


class ResearchEvidenceClaim(BaseModel):
    """Untrusted model assertion tied to one collected source and Coverage Key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    source_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    quote_text: str = Field(min_length=1, max_length=16_000)
    claim: str = Field(min_length=1, max_length=8_000)
    subject_identity: str = Field(min_length=1, max_length=500)
    predicate: str = Field(min_length=1, max_length=200)
    applicability: str = Field(min_length=1, max_length=500)
    normalization_schema: str = Field(min_length=1, max_length=120)
    normalized_value: str = Field(min_length=1, max_length=8_000)


class SingleTaskResearchPayload(BaseModel):
    """Small validated proposal suitable for the first research vertical slice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    summary: str = Field(min_length=1, max_length=12_000)
    recommended_option: str = Field(min_length=1, max_length=2_000)
    alternatives: tuple[str, ...] = Field(min_length=1, max_length=8)
    evidence_claims: tuple[ResearchEvidenceClaim, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def presents_distinct_decision_options(self) -> SingleTaskResearchPayload:
        """Reject a model response that only looks like it gives the user a choice.

        The proposal reducer turns these labels into durable Candidate and
        DecisionOption records. Letting a recommendation reappear as an
        alternative would make the later approval screen structurally valid
        but semantically meaningless. This boundary is deliberately
        deterministic rather than prompt-only, and tolerates only cosmetic
        whitespace/case differences when detecting a duplicate.
        """

        option_labels = (self.recommended_option, *self.alternatives)
        normalized = tuple(" ".join(label.split()).casefold() for label in option_labels)
        if not all(normalized) or len(normalized) != len(set(normalized)):
            raise ValueError(
                "research recommendation and alternatives must be distinct non-blank options"
            )
        return self


class SingleTaskResearchExecution(BaseModel):
    """Only references leave the private research execution boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # No raw model output exists when trusted source collection was unavailable
    # and the task was converted into a source-gap partial before model dispatch.
    raw_artifact_ref: str | None = None
    result: ResultEnvelope
    admission: AdmissionRecord
    proposal: ProposalManifest


@dataclass(frozen=True, slots=True)
class _PersistedResearchSource:
    collected: CollectedResearchSource
    snapshot: SourceSnapshot
    normalized_document: str


def _research_result_status(
    *,
    task: TaskEnvelope,
    admitted_coverage_keys: tuple[str, ...],
    unresolved_refs: tuple[str, ...],
) -> ResearchResultStatus:
    """Make a leaf task's visible status match its authorized evidence output.

    Result admission can accept a structurally valid proposal even when it is
    incomplete; only a later Coverage/Sufficiency gate decides whether the Run
    may produce a user proposal.  The task itself must nevertheless be honest:
    it is partial until every server-assigned Coverage Key has at least one
    admitted observation, or whenever materialization left an unresolved
    diagnostic.
    """

    if unresolved_refs or set(task.coverage_keys) - set(admitted_coverage_keys):
        return ResearchResultStatus.PARTIAL
    return ResearchResultStatus.SUCCEEDED


def _is_recoverable_source_gap(reason_code: str) -> bool:
    """Classify source failures that may safely defer to bounded gap planning."""

    return reason_code in {
        "no_trusted_research_sources",
        "tavily_network_failure",
        "tavily_provider_unavailable",
    }


class SingleTaskResearchExecutor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
        researcher: SingleTaskResearcher,
        source_collector: ResearchSourceCollector | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_root = artifact_root
        self._researcher = researcher
        self._source_collector = source_collector or NoopResearchSourceCollector()

    async def execute(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        question: str,
        steering_instructions: tuple[str, ...] = (),
        consume_pre_dispatch_controls: bool = True,
        independently_verified: bool = False,
    ) -> SingleTaskResearchExecution:
        if task.run_id != run.id or grant.task_id != task.id:
            raise ValueError(
                "research task and execution grant must belong to the claimed AgentRun"
            )
        if task.basis_hash != run.basis_hash or grant.generation != claim.generation:
            raise ValueError("research task/grant does not match frozen run basis or generation")
        if independently_verified and task.capability != "research_verifier":
            raise ValueError("only a research_verifier task may mark an observation independent")

        if consume_pre_dispatch_controls:
            if steering_instructions:
                raise ValueError("only the control consumer may provide steering instructions")
            steering_instructions = await self.consume_pre_dispatch_controls(
                run=run,
                claim=claim,
            )
        try:
            collected_sources = await self._source_collector.collect(
                run=run,
                task=task,
                question=question,
            )
        except ResearchSourceCollectionError as error:
            if _is_recoverable_source_gap(error.reason_code):
                return await self._record_source_unavailable(
                    run=run,
                    claim=claim,
                    task=task,
                    grant=grant,
                    question=question,
                    reason_code=error.reason_code,
                )
            raise ResearchSourcesUnavailable(error.reason_code) from error
        _validate_collected_sources(collected_sources)
        if not collected_sources:
            return await self._record_source_unavailable(
                run=run,
                claim=claim,
                task=task,
                grant=grant,
                question=question,
                reason_code="no_trusted_research_sources",
            )
        persisted_sources = await self._persist_collected_sources(
            run=run,
            collected_sources=collected_sources,
        )
        result_id = uuid4()
        source_collection_report_ref = await self._materialize_source_collection_report(
            run=run,
            task=task,
            result_id=result_id,
            persisted_sources=persisted_sources,
        )
        # Coverage keys are a server-owned execution contract.  The capability
        # receives a closed list rather than being asked to invent subtopics.
        authorized_question = (
            f"{question}\n\nAuthorized Coverage Keys (closed list; use these exact keys only):\n"
            + "\n".join(f"- {key}" for key in task.coverage_keys)
            + _evidence_sufficiency_instruction(task=task)
        )
        if isinstance(self._researcher, RunScopedSingleTaskResearcher):
            raw_value = await self._researcher.research_for_task(
                run=run,
                claim=claim,
                task=task,
                grant=grant,
                question=authorized_question,
                input_refs=task.input_refs,
                steering_instructions=steering_instructions,
                evidence_context=collected_sources,
            )
        else:
            raw_value = await self._researcher.research(
                question=authorized_question,
                input_refs=task.input_refs,
                steering_instructions=steering_instructions,
                evidence_context=collected_sources,
            )
        context_manifest_ref: str | None = None
        if isinstance(raw_value, GatewayResearchOutput):
            context_manifest_ref = raw_value.context_manifest_ref
            raw_value = raw_value.payload
        async with session_scope(self._session_factory) as session:
            artifact_store = ContentAddressedArtifactStore(session, self._artifact_root)
            raw = await artifact_store.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_raw_output",
                value=raw_value,
            )

        payload = SingleTaskResearchPayload.model_validate(raw_value)
        (
            evidence_refs,
            coverage_observation_refs,
            unresolved_refs,
            admitted_coverage_keys,
        ) = await self._materialize_evidence(
            run=run,
            task=task,
            result_id=result_id,
            persisted_sources=persisted_sources,
            payload=payload,
            independently_verified=independently_verified,
        )
        manifest_value = {
            "task_id": str(task.id),
            "payload": payload.model_dump(mode="json"),
            "raw_artifact_ref": raw.ref,
            "evidence_refs": evidence_refs,
            "coverage_observation_refs": coverage_observation_refs,
            "unresolved_refs": unresolved_refs,
            "source_collection_report_ref": source_collection_report_ref,
            "context_manifest_ref": context_manifest_ref,
        }
        async with session_scope(self._session_factory) as session:
            proposal_artifact = await ContentAddressedArtifactStore(
                session, self._artifact_root
            ).put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_proposal_manifest",
                value=manifest_value,
            )

        result = ResultEnvelope(
            id=result_id,
            run_id=run.id,
            task_id=task.id,
            basis_hash=run.basis_hash,
            producer_attempt_id=grant.attempt_id,
            producer_generation=grant.generation,
            producer_profile_ref=run.runtime_binding.profile_binding_ref,
            status=_research_result_status(
                task=task,
                admitted_coverage_keys=admitted_coverage_keys,
                unresolved_refs=unresolved_refs,
            ),
            artifact_ref=proposal_artifact.ref,
            manifest_hash=proposal_artifact.content_hash,
            evidence_refs=evidence_refs,
            coverage_observation_refs=coverage_observation_refs,
            unresolved_refs=unresolved_refs,
            source_collection_report_ref=source_collection_report_ref,
            context_manifest_ref=context_manifest_ref,
        )
        proposal = ProposalManifest(
            run_id=run.id,
            basis_hash=run.basis_hash,
            artifact_ref=proposal_artifact.ref,
            manifest_hash=proposal_artifact.content_hash,
        )
        admission = AdmissionRecord(
            run_id=run.id,
            result_id=result.id,
            result_manifest_hash=result.manifest_hash,
            disposition=AdmissionDisposition.ACCEPTED,
            reason_codes=("runtime_fenced", "schema_valid", "artifact_present"),
            admitted_ref=f"admitted://agent-run-results/{result.id}",
        )
        cancelled_at_safe_point = False
        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            active = await control.ensure_active_claim(claim=claim)
            project = await PostgresDomainStore(session).get_project(run.project_id)
            if project is None or project.revision != run.basis_project_revision:
                await control.request_cancel(run_id=run.id)
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                cancelled_at_safe_point = True
            elif active.cancel_requested:
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                cancelled_at_safe_point = True
            else:
                result_store = AgentResultStore(session)
                await result_store.record_result(result)
                await result_store.admit(admission)

        if cancelled_at_safe_point:
            raise AgentRunCancelled(
                "AgentRun cancellation acknowledged at result-admission safe point"
            )

        return SingleTaskResearchExecution(
            raw_artifact_ref=raw.ref,
            result=result,
            admission=admission,
            proposal=proposal,
        )

    async def reuse_workstream_memory(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        memory_items: tuple[ModuleMemoryItem, ...],
    ) -> SingleTaskResearchExecution:
        """Re-admit exact, current module memory without restoring old state.

        Only immutable proposal/evidence artifacts are read.  The method
        projects their answered observations into a *new* current-Run result
        and creates a new AdmissionRecord under the current claim.  It never
        copies a private graph checkpoint, model messages, grant or admission.
        """

        if task.run_id != run.id or grant.task_id != task.id:
            raise ValueError("memory reuse task and grant must belong to the claimed AgentRun")
        if task.basis_hash != run.basis_hash or grant.generation != claim.generation:
            raise ValueError("memory reuse task/grant does not match the active claim")
        if (
            not memory_items
            or {item.stable_key for item in memory_items} != set(task.coverage_keys)
        ):
            raise WorkstreamMemoryReuseUnavailable("memory does not cover this exact task")
        if any(item.source_result_id is None for item in memory_items):
            raise WorkstreamMemoryReuseUnavailable("memory has no immutable source result")

        source_result_ids = {item.source_result_id for item in memory_items}
        if len(source_result_ids) != 1:
            raise WorkstreamMemoryReuseUnavailable("task memory has multiple source results")
        source_result_id = next(iter(source_result_ids))
        assert source_result_id is not None
        result_id = uuid4()
        admitted_ref = f"admitted://agent-run-results/{result_id}"

        try:
            async with session_scope(self._session_factory) as session:
                result_store = AgentResultStore(session)
                source_result = await result_store.get_result(result_id=source_result_id)
                if source_result is None:
                    raise WorkstreamMemoryReuseUnavailable("memory source result is missing")
                artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
                source_manifest = await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=source_result.basis_hash,
                    ref=source_result.artifact_ref,
                    expected_kind="research_proposal_manifest",
                )
                if not isinstance(source_manifest, dict):
                    raise WorkstreamMemoryReuseUnavailable("memory source manifest is invalid")
                payload = SingleTaskResearchPayload.model_validate(source_manifest.get("payload"))

                observations: list[ResearchCoverageObservation] = []
                requested_keys = set(task.coverage_keys)
                for ref in source_result.coverage_observation_refs:
                    value = await artifacts.read_json_ref(
                        project_id=run.project_id,
                        basis_hash=source_result.basis_hash,
                        ref=ref,
                        expected_kind="research_coverage_observation",
                    )
                    observation = ResearchCoverageObservation.model_validate(value)
                    if observation.result_id != source_result.id:
                        raise WorkstreamMemoryReuseUnavailable(
                            "memory observation is not owned by its source result"
                        )
                    if (
                        observation.coverage_key in requested_keys
                        and observation.status is CoverageObservationStatus.ANSWERED
                    ):
                        observations.append(
                            observation.model_copy(
                                update={
                                    "result_id": result_id,
                                    "admitted_ref": admitted_ref,
                                    "basis_hash": run.basis_hash,
                                }
                            )
                        )
                if {item.coverage_key for item in observations} != requested_keys:
                    raise WorkstreamMemoryReuseUnavailable(
                        "memory lacks answered observations for this exact task"
                    )
                observation_refs: list[str] = []
                for item in observations:
                    artifact = await artifacts.put_agent_run_json(
                        project_id=run.project_id,
                        agent_run_id=run.id,
                        basis_hash=run.basis_hash,
                        kind="research_coverage_observation",
                        value=item.model_dump(mode="json"),
                    )
                    observation_refs.append(artifact.ref)
                evidence_refs = tuple(
                    sorted({ref for item in observations for ref in item.evidence_refs})
                )
                manifest_artifact = await artifacts.put_agent_run_json(
                    project_id=run.project_id,
                    agent_run_id=run.id,
                    basis_hash=run.basis_hash,
                    kind="research_proposal_manifest",
                    value={
                        "task_id": str(task.id),
                        "payload": payload.model_dump(mode="json"),
                        "raw_artifact_ref": None,
                        "evidence_refs": evidence_refs,
                        "coverage_observation_refs": tuple(observation_refs),
                        "unresolved_refs": (),
                        "source_collection_report_ref": None,
                        "context_manifest_ref": None,
                        "workstream_memory_reuse": {
                            "schema_version": "workstream-memory-reuse-v1",
                            "memory_item_ids": tuple(str(item.id) for item in memory_items),
                            "source_result_id": str(source_result.id),
                            "source_artifact_ref": source_result.artifact_ref,
                        },
                    },
                )
                result = ResultEnvelope(
                    id=result_id,
                    run_id=run.id,
                    task_id=task.id,
                    basis_hash=run.basis_hash,
                    producer_attempt_id=grant.attempt_id,
                    producer_generation=grant.generation,
                    producer_profile_ref="system://workstream-memory-reuse/v1",
                    status=ResearchResultStatus.SUCCEEDED,
                    artifact_ref=manifest_artifact.ref,
                    manifest_hash=manifest_artifact.content_hash,
                    evidence_refs=evidence_refs,
                    coverage_observation_refs=tuple(observation_refs),
                    unresolved_refs=(),
                )
                admission = AdmissionRecord(
                    run_id=run.id,
                    result_id=result.id,
                    result_manifest_hash=result.manifest_hash,
                    disposition=AdmissionDisposition.ACCEPTED,
                    reason_codes=(
                        "runtime_fenced",
                        "schema_valid",
                        "artifact_present",
                        "workstream_memory_current",
                    ),
                    admitted_ref=admitted_ref,
                )
                control = AgentRunControl(session)
                active = await control.ensure_active_claim(claim=claim)
                project = await PostgresDomainStore(session).get_project(run.project_id)
                if project is None or project.revision != run.basis_project_revision:
                    await control.request_cancel(run_id=run.id)
                    await control.acknowledge_cancel_at_safe_point(claim=claim)
                    raise AgentRunCancelled(
                        "AgentRun cancellation acknowledged before workstream memory admission"
                    )
                if active.cancel_requested:
                    await control.acknowledge_cancel_at_safe_point(claim=claim)
                    raise AgentRunCancelled(
                        "AgentRun cancellation acknowledged before workstream memory admission"
                    )
                await result_store.record_result(result)
                await result_store.admit(admission)
        except (ArtifactIntegrityError, ArtifactNotFoundError, ValueError) as error:
            raise WorkstreamMemoryReuseUnavailable(str(error)) from error

        return SingleTaskResearchExecution(
            raw_artifact_ref=None,
            result=result,
            admission=admission,
            proposal=ProposalManifest(
                run_id=run.id,
                basis_hash=run.basis_hash,
                artifact_ref=result.artifact_ref,
                manifest_hash=result.manifest_hash,
            ),
        )

    async def _record_source_unavailable(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
        task: TaskEnvelope,
        grant: ExecutionGrant,
        question: str,
        reason_code: str,
    ) -> SingleTaskResearchExecution:
        """Admit a source-gap partial without letting the model manufacture content."""

        result_id = uuid4()
        report = ResearchSourceUnavailableReport(
            result_id=result_id,
            task_id=task.id,
            coverage_keys=task.coverage_keys,
            reason_code=reason_code,
        )
        payload = SingleTaskResearchPayload(
            question=question[:2_000],
            summary=(
                "未收集到可保存、可核验的研究来源；未调用模型，"
                "等待受控补题或用户重新规划。"
            ),
            recommended_option="当前不提出候选。",
            alternatives=("补充可信来源后重新研究。",),
        )
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            diagnostic = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_source_unavailable_report",
                value=report.model_dump(mode="json"),
                commit=False,
            )
            proposal_artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_proposal_manifest",
                value={
                    "task_id": str(task.id),
                    "payload": payload.model_dump(mode="json"),
                    "raw_artifact_ref": None,
                    "evidence_refs": (),
                    "coverage_observation_refs": (),
                    "unresolved_refs": (diagnostic.ref,),
                    "source_collection_report_ref": None,
                    "context_manifest_ref": None,
                },
                commit=False,
            )
            result = ResultEnvelope(
                id=result_id,
                run_id=run.id,
                task_id=task.id,
                basis_hash=run.basis_hash,
                producer_attempt_id=grant.attempt_id,
                producer_generation=grant.generation,
                producer_profile_ref=run.runtime_binding.profile_binding_ref,
                status=ResearchResultStatus.PARTIAL,
                artifact_ref=proposal_artifact.ref,
                manifest_hash=proposal_artifact.content_hash,
                evidence_refs=(),
                coverage_observation_refs=(),
                unresolved_refs=(diagnostic.ref,),
            )
            admission = AdmissionRecord(
                run_id=run.id,
                result_id=result.id,
                result_manifest_hash=result.manifest_hash,
                disposition=AdmissionDisposition.ACCEPTED,
                reason_codes=(
                    "runtime_fenced",
                    "schema_valid",
                    "artifact_present",
                    "source_gap_partial",
                ),
                admitted_ref=f"admitted://agent-run-results/{result.id}",
            )
            control = AgentRunControl(session)
            active = await control.ensure_active_claim(claim=claim)
            project = await PostgresDomainStore(session).get_project(run.project_id)
            cancelled_at_safe_point = False
            if project is None or project.revision != run.basis_project_revision:
                await control.request_cancel(run_id=run.id)
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                cancelled_at_safe_point = True
            elif active.cancel_requested:
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                cancelled_at_safe_point = True
            else:
                result_store = AgentResultStore(session)
                await result_store.record_result(result)
                await result_store.admit(admission)
            await session.commit()

        if cancelled_at_safe_point:
            raise AgentRunCancelled(
                "AgentRun cancellation acknowledged at source-gap admission safe point"
            )
        return SingleTaskResearchExecution(
            result=result,
            admission=admission,
            proposal=ProposalManifest(
                run_id=run.id,
                basis_hash=run.basis_hash,
                artifact_ref=result.artifact_ref,
                manifest_hash=result.manifest_hash,
            ),
        )

    async def _materialize_evidence(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        result_id: UUID,
        persisted_sources: dict[str, _PersistedResearchSource],
        payload: SingleTaskResearchPayload,
        independently_verified: bool,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Persist source bytes first, then admit only exact model-proposed quotes.

        An invalid source key, coverage key, or quote is deliberately omitted
        from the shared result rather than repaired from model text.  The
        resulting coverage gap remains visible to the later Sufficiency Gate.
        """

        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            project = await PostgresDomainStore(session).get_project(run.project_id)
            if project is None:
                raise ValueError("research project does not exist while admitting evidence")
            active_modules = tuple(
                await ProjectApplication(PostgresDomainStore(session))._active_modules(project)
            )
            module_id = _task_module_id(task=task, active_modules=active_modules)
            evidence_refs: set[str] = set()
            observation_refs: set[str] = set()
            admitted_coverage_keys: set[str] = set()
            issues: list[ResearchEvidenceMaterializationIssue] = []
            for claim_index, claim in enumerate(payload.evidence_claims):
                if module_id is None:
                    issues.append(
                        _evidence_issue(
                            claim_index,
                            claim,
                            task.coverage_keys,
                            "task_module_scope_unavailable",
                        )
                    )
                    continue
                persisted_source = persisted_sources.get(claim.source_key)
                if claim.coverage_key not in task.coverage_keys:
                    issues.append(
                        _evidence_issue(
                            claim_index,
                            claim,
                            task.coverage_keys,
                            "coverage_key_not_assigned_to_task",
                        )
                    )
                    continue
                if persisted_source is None:
                    issues.append(
                        _evidence_issue(
                            claim_index,
                            claim,
                            task.coverage_keys,
                            "source_key_not_collected",
                        )
                    )
                    continue
                if persisted_source.normalized_document.count(claim.quote_text) != 1:
                    issues.append(
                        _evidence_issue(
                            claim_index,
                            claim,
                            task.coverage_keys,
                            "quote_not_unique_or_not_exact",
                        )
                    )
                    continue
                start = persisted_source.normalized_document.index(claim.quote_text)
                identity = persisted_source.collected.source.id
                assert identity is not None
                candidate = EvidenceCandidate(
                    id=uuid5(
                        NAMESPACE_URL,
                        "aidison://evidence-candidate/"
                        f"{run.id}/{task.id}/{identity}/{claim.coverage_key}/"
                        f"{sha256(claim.quote_text.encode()).hexdigest()}/"
                        f"{sha256(claim.claim.encode()).hexdigest()}",
                    ),
                    project_id=run.project_id,
                    module_id=module_id,
                    basis_hash=run.basis_hash,
                    source=persisted_source.collected.source,
                    observation=SourceObservation(
                        agent_run_id=run.id,
                        task_id=task.id,
                        basis_hash=run.basis_hash,
                        requested_source_id=identity,
                        resolved_source_id=identity,
                        outcome=ObservationOutcome.FETCHED,
                        observed_at=persisted_source.collected.observed_at,
                        http_status=200,
                        snapshot_id=persisted_source.snapshot.id,
                    ),
                    snapshot=persisted_source.snapshot,
                    span=SourceSpan(
                        snapshot_id=persisted_source.snapshot.id,
                        artifact_ref=persisted_source.snapshot.artifact_ref,
                        content_hash=persisted_source.snapshot.content_hash,
                        kind=SourceSpanKind.MARKDOWN_TEXT,
                        structural_locator=f"text:{claim.source_key}",
                        start_char=start,
                        end_char=start + len(claim.quote_text),
                        quote_text=claim.quote_text,
                    ),
                    relation=EvidenceRelation.SUPPORTS,
                    claim=claim.claim,
                )
                decision = admit_evidence_candidate(
                    EvidenceAdmissionInput(
                        candidate=candidate,
                        artifact=await artifacts.get_metadata(
                            project_id=run.project_id,
                            artifact_id=persisted_source.snapshot.id,
                        ),
                        normalized_document=persisted_source.normalized_document,
                        active_module_ids=tuple(item.id for item in active_modules),
                        observed_before=datetime.now(UTC),
                        policy=EvidenceAdmissionPolicy(
                            allowed_source_kinds=tuple(SourceKind),
                            max_observation_age=timedelta(days=30),
                        ),
                    )
                )
                if decision.status is not EvidenceAdmissionStatus.ACCEPTED:
                    issues.append(
                        _evidence_issue(
                            claim_index,
                            claim,
                            task.coverage_keys,
                            *(f"evidence_admission:{code}" for code in decision.reason_codes),
                        )
                    )
                    continue
                binding = materialize_admitted_binding(decision, binding_id=candidate.id)
                evidence = await artifacts.put_agent_run_json(
                    project_id=run.project_id,
                    agent_run_id=run.id,
                    basis_hash=run.basis_hash,
                    kind="research_admitted_evidence",
                    value=binding.model_dump(mode="json"),
                    commit=False,
                )
                observation = ResearchCoverageObservation(
                    result_id=result_id,
                    admitted_ref=f"admitted://agent-run-results/{result_id}",
                    basis_hash=run.basis_hash,
                    coverage_key=claim.coverage_key,
                    status=CoverageObservationStatus.ANSWERED,
                    source_kinds=persisted_source.collected.coverage_source_kinds,
                    source_ids=(str(identity),),
                    source_origins=(source_origin_key(persisted_source.collected.source),),
                    evidence_refs=(evidence.ref,),
                    claim_key=ClaimKey(
                        subject_identity=claim.subject_identity,
                        predicate=claim.predicate,
                        applicability=claim.applicability,
                        normalization_schema=claim.normalization_schema,
                    ),
                    value_hash=sha256(claim.normalized_value.encode()).hexdigest(),
                    independently_verified=independently_verified,
                )
                observation_artifact = await artifacts.put_agent_run_json(
                    project_id=run.project_id,
                    agent_run_id=run.id,
                    basis_hash=run.basis_hash,
                    kind="research_coverage_observation",
                    value=observation.model_dump(mode="json"),
                    commit=False,
                )
                evidence_refs.add(evidence.ref)
                observation_refs.add(observation_artifact.ref)
                admitted_coverage_keys.add(claim.coverage_key)
            unresolved_refs: tuple[str, ...] = ()
            if issues:
                report = ResearchEvidenceMaterializationReport(
                    result_id=result_id,
                    task_id=task.id,
                    issues=tuple(issues),
                )
                diagnostic = await artifacts.put_agent_run_json(
                    project_id=run.project_id,
                    agent_run_id=run.id,
                    basis_hash=run.basis_hash,
                    kind="research_evidence_materialization_report",
                    value=report.model_dump(mode="json"),
                    commit=False,
                )
                unresolved_refs = (diagnostic.ref,)
            await session.commit()
        return (
            tuple(sorted(evidence_refs)),
            tuple(sorted(observation_refs)),
            unresolved_refs,
            tuple(sorted(admitted_coverage_keys)),
        )

    async def _materialize_source_collection_report(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        result_id: UUID,
        persisted_sources: dict[str, _PersistedResearchSource],
    ) -> str:
        """Persist the bounded, model-visible source set before model dispatch."""

        policy = task.collection_policy
        if policy is None:
            # Legacy callers without a frozen policy remain auditable, but all
            # ResearchRun-generated tasks carry one.
            profile = "standard"
            max_queries: int | None = 1
            max_documents: int | None = len(persisted_sources)
        else:
            profile = policy.profile
            max_queries = policy.max_queries
            max_documents = policy.max_documents_total
        source_kinds = tuple(
            sorted(
                {
                    kind
                    for source in persisted_sources.values()
                    for kind in source.collected.coverage_source_kinds
                }
            )
        )
        unavailable_coverage_reasons = tuple(
            sorted(
                {
                    (failure.coverage_key, failure.reason_code): failure
                    for source in persisted_sources.values()
                    for failure in source.collected.collection_failures
                }.values(),
                key=lambda failure: (failure.coverage_key, failure.reason_code),
            )
        )
        report = ResearchSourceCollectionReport(
            result_id=result_id,
            task_id=task.id,
            coverage_keys=task.coverage_keys,
            collection_profile=profile,
            configured_max_queries=max_queries,
            configured_max_documents=max_documents,
            collected_source_count=len(persisted_sources),
            collected_source_kinds=source_kinds,
            source_snapshot_refs=tuple(
                sorted(source.snapshot.artifact_ref for source in persisted_sources.values())
            ),
            unavailable_coverage_reasons=unavailable_coverage_reasons,
        )
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            artifact = await artifacts.put_agent_run_json(
                project_id=run.project_id,
                agent_run_id=run.id,
                basis_hash=run.basis_hash,
                kind="research_source_collection_report",
                value=report.model_dump(mode="json"),
            )
        return artifact.ref

    async def _persist_collected_sources(
        self,
        *,
        run: AgentRun,
        collected_sources: tuple[CollectedResearchSource, ...],
    ) -> dict[str, _PersistedResearchSource]:
        """Make every model-visible source replayable before provider dispatch."""

        if not collected_sources:
            return {}
        async with session_scope(self._session_factory) as session:
            artifacts = ContentAddressedArtifactStore(session, self._artifact_root)
            persisted: dict[str, _PersistedResearchSource] = {}
            for source in collected_sources:
                artifact = await artifacts.put_agent_run_bytes(
                    project_id=run.project_id,
                    agent_run_id=run.id,
                    basis_hash=run.basis_hash,
                    kind="research_source_snapshot",
                    content=source.normalized_document.encode("utf-8"),
                    media_type=source.media_type,
                    source_url=source.source.canonical_locator,
                    commit=False,
                )
                source_id = source.source.id
                assert source_id is not None
                persisted[source.key] = _PersistedResearchSource(
                    collected=source,
                    snapshot=SourceSnapshot(
                        id=artifact.id,
                        source_id=source_id,
                        content_hash=artifact.content_hash,
                        artifact_ref=artifact.ref,
                        media_type=source.media_type,
                        representation=source.representation,
                        parser_revision=source.parser_revision,
                    ),
                    normalized_document=source.normalized_document,
                )
            await session.commit()
        return persisted

    async def consume_pre_dispatch_controls(
        self,
        *,
        run: AgentRun,
        claim: AgentRunClaim,
    ) -> tuple[str, ...]:
        """Consume only controls safe before a physical model invocation.

        Basis steering changes project facts and is intentionally left pending:
        it must become a separately approved Project revision rather than an
        advisory prompt fragment.  Cancellation wins over all other controls.
        """

        cancelled_before_dispatch = False
        paused_before_dispatch = False
        instructions: list[str] = []
        async with session_scope(self._session_factory) as session:
            control = AgentRunControl(session)
            active = await control.ensure_active_claim(claim=claim)
            project = await PostgresDomainStore(session).get_project(run.project_id)
            if project is None or project.revision != run.basis_project_revision:
                await control.request_cancel(run_id=run.id)
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                cancelled_before_dispatch = True
            elif active.cancel_requested:
                await control.acknowledge_cancel_at_safe_point(claim=claim)
                cancelled_before_dispatch = True
            else:
                requests = await AgentRunControlRequestStore(session).list_for_run(
                    agent_run_id=run.id
                )
                pending = tuple(
                    item
                    for item in requests
                    if item.status is ControlRequestStatus.REQUESTED
                    and item.basis_hash == run.basis_hash
                )
                pause = next(
                    (item for item in pending if item.kind is ControlRequestKind.PAUSE),
                    None,
                )
                if pause is not None:
                    await AgentRunControlRequestStore(session).acknowledge(pause.id)
                    await control.wait_for_decision(claim=claim)
                    paused_before_dispatch = True
                else:
                    request_store = AgentRunControlRequestStore(session)
                    # Steering is durable Run-local intent.  Include already
                    # acknowledged instructions as well as newly acknowledged
                    # rows so a restarted graph can rebuild the same advisory
                    # context without checkpointing private prompt state.
                    instructions.extend(
                        str(request.payload["instruction"]).strip()
                        for request in requests
                        if request.kind is ControlRequestKind.RUNTIME_STEERING
                        and request.status is ControlRequestStatus.ACKNOWLEDGED
                        and isinstance(request.payload.get("instruction"), str)
                        and str(request.payload["instruction"]).strip()
                    )
                    for request in pending:
                        if request.kind is not ControlRequestKind.RUNTIME_STEERING:
                            continue
                        instruction = request.payload.get("instruction")
                        if not isinstance(instruction, str) or not instruction.strip():
                            raise ValueError("runtime steering instruction is invalid")
                        instructions.append(instruction.strip())
                        await request_store.acknowledge(request.id)
        if cancelled_before_dispatch:
            raise AgentRunCancelled("AgentRun cancellation acknowledged before provider dispatch")
        if paused_before_dispatch:
            raise AgentRunPaused("AgentRun pause acknowledged before provider dispatch")
        return tuple(dict.fromkeys(instructions))


def _validate_collected_sources(sources: tuple[CollectedResearchSource, ...]) -> None:
    keys = tuple(item.key for item in sources)
    if len(set(keys)) != len(keys):
        raise ValueError("collected research source keys must be unique")


def _evidence_sufficiency_instruction(*, task: TaskEnvelope) -> str:
    """Expose a frozen evidence bar without granting the model completion authority."""

    policy = task.collection_policy
    if policy is None or policy.profile != "deep":
        return (
            "\n\nEvidence sufficiency: cite every supported claim exactly; "
            "report a gap when support is absent."
        )
    return (
        "\n\nDeep evidence sufficiency: for every Authorized Coverage Key you claim, "
        "provide exact evidence_claims from at least two distinct source_key values. "
        "If two distinct source documents do not support the key, omit the unsupported claim "
        "and report the evidence gap explicitly; never duplicate or invent a citation."
    )


def _evidence_issue(
    claim_index: int,
    claim: ResearchEvidenceClaim,
    assigned_coverage_keys: tuple[str, ...],
    *reason_codes: str,
) -> ResearchEvidenceMaterializationIssue:
    return ResearchEvidenceMaterializationIssue(
        claim_index=claim_index,
        coverage_key=claim.coverage_key,
        source_key=claim.source_key,
        assigned_coverage_keys=assigned_coverage_keys,
        reason_codes=tuple(reason_codes),
    )


def _task_module_id(*, task: TaskEnvelope, active_modules: tuple[object, ...]) -> UUID | None:
    module_refs = tuple(item for item in task.input_refs if item.startswith("module://"))
    if len(module_refs) == 1:
        try:
            return UUID(module_refs[0].removeprefix("module://"))
        except ValueError as error:
            raise ValueError("research task module reference is invalid") from error
    if module_refs:
        raise ValueError("research task may not carry multiple module references")
    if len(active_modules) == 1:
        module_id = getattr(active_modules[0], "id", None)
        if isinstance(module_id, UUID):
            return module_id
    return None


__all__ = [
    "AgentRunCancelled",
    "AgentRunPaused",
    "ResearchSourcesUnavailable",
    "SingleTaskResearchExecution",
    "SingleTaskResearchExecutor",
    "ResearchEvidenceClaim",
    "SingleTaskResearchPayload",
    "SingleTaskResearcher",
]
