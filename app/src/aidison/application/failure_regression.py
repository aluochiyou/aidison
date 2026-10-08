"""Capture failed AgentRuns as immutable, human-review evaluation candidates."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.application.agent_run_replay import (
    EVALUATION_REPLAY_BUNDLE_ARTIFACT_KIND,
    AgentRunReplayBundleService,
)
from aidison.application.agent_run_trajectory import (
    AgentRunTrajectoryFailureAnalysis,
    AgentRunTrajectoryService,
    AgentRunTrajectorySummary,
)
from aidison.domain.models import FrozenModel
from aidison.evaluation.contracts import canonical_hash
from aidison.infrastructure.agent_runs import (
    AgentRunConflictError,
    AgentRunControl,
    AgentRunNotFoundError,
)
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.runtime.replay_bundle import ReplayBundleStatus

FAILURE_REGRESSION_CANDIDATE_ARTIFACT_KIND = "failure_regression_candidate"


class FailureRegressionCandidate(FrozenModel):
    """Frozen failure evidence that is not yet a Golden Task."""

    schema_version: Literal["failure-regression-candidate.v1"] = (
        "failure-regression-candidate.v1"
    )
    candidate_key: str = Field(pattern=r"^failure-[a-f0-9]{32}$")
    project_id: UUID
    agent_run_id: UUID
    run_kind: AgentRunKind
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_completed_at: datetime
    replay_bundle_ref: str = Field(min_length=1, max_length=500)
    replay_bundle_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    replay_bundle_status: ReplayBundleStatus
    replay_bundle_reason_codes: tuple[str, ...]
    trajectory_summary: AgentRunTrajectorySummary
    failure_analysis: AgentRunTrajectoryFailureAnalysis
    review_state: Literal["pending_human_label"] = "pending_human_label"
    requires_human_review: Literal[True] = True
    eligible_for_golden_promotion: Literal[False] = False
    promotion_blockers: tuple[str, ...] = Field(min_length=2)


class FailureRegressionCandidateCapture(FrozenModel):
    artifact_ref: str = Field(min_length=1, max_length=500)
    candidate: FailureRegressionCandidate


class FailureRegressionCandidateService:
    """Create/list replay-backed candidates without running a graph or provider."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        artifact_root: Path,
    ) -> None:
        self._session = session
        self._artifact_root = artifact_root

    async def capture(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID,
        idempotency_key: str,
    ) -> FailureRegressionCandidateCapture:
        control = AgentRunControl(self._session)
        run = await control.get(agent_run_id)
        if run is None or run.project_id != project_id:
            raise AgentRunNotFoundError("AgentRun not found")
        if run.status is not AgentRunStatus.FAILED or run.completed_at is None:
            raise AgentRunConflictError(
                "only a terminal failed AgentRun can become a regression candidate"
            )

        payload_hash = canonical_hash(
            "capture-failure-regression-candidate.v1",
            project_id,
            agent_run_id,
            run.basis_hash,
        )
        domain_store = PostgresDomainStore(self._session)
        receipt = await domain_store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            return await self._read_capture(run=run, artifact_ref=receipt)

        existing = await self.list(project_id=project_id, agent_run_id=agent_run_id)
        if existing:
            capture = existing[-1]
            await domain_store.save_command_receipt(
                idempotency_key,
                payload_hash,
                capture.artifact_ref,
            )
            await self._session.commit()
            return capture

        replay = await AgentRunReplayBundleService(
            session=self._session,
            artifact_root=self._artifact_root,
        ).build(agent_run_id=agent_run_id)
        artifacts = ContentAddressedArtifactStore(self._session, self._artifact_root)
        replay_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=agent_run_id,
            basis_hash=run.basis_hash,
            kind=EVALUATION_REPLAY_BUNDLE_ARTIFACT_KIND,
            value=replay.model_dump(mode="json"),
            commit=False,
        )
        trajectory = await AgentRunTrajectoryService(self._session).build(
            project_id=project_id,
            agent_run_id=agent_run_id,
        )
        blockers = [
            "human_expected_outcome_required",
            "human_scoring_rubric_required",
        ]
        blockers.extend(
            f"replay_{reason}" for reason in replay.reason_codes if reason.strip()
        )
        candidate = FailureRegressionCandidate(
            candidate_key=f"failure-{replay.manifest_hash[:32]}",
            project_id=project_id,
            agent_run_id=agent_run_id,
            run_kind=run.kind,
            basis_hash=run.basis_hash,
            source_completed_at=run.completed_at,
            replay_bundle_ref=replay_artifact.ref,
            replay_bundle_manifest_hash=replay.manifest_hash,
            replay_bundle_status=replay.status,
            replay_bundle_reason_codes=replay.reason_codes,
            trajectory_summary=trajectory.summary,
            failure_analysis=trajectory.failure_analysis,
            promotion_blockers=tuple(blockers),
        )
        candidate_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=agent_run_id,
            basis_hash=run.basis_hash,
            kind=FAILURE_REGRESSION_CANDIDATE_ARTIFACT_KIND,
            value=candidate.model_dump(mode="json"),
            commit=False,
        )
        await domain_store.append_event(
            project_id,
            "evaluation.failure_candidate_created",
            {
                "agent_run_id": str(agent_run_id),
                "candidate_key": candidate.candidate_key,
                "candidate_artifact_ref": candidate_artifact.ref,
                "replay_bundle_ref": replay_artifact.ref,
                "review_state": candidate.review_state,
            },
        )
        await domain_store.save_command_receipt(
            idempotency_key,
            payload_hash,
            candidate_artifact.ref,
        )
        await self._session.commit()
        return FailureRegressionCandidateCapture(
            artifact_ref=candidate_artifact.ref,
            candidate=candidate,
        )

    async def list(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID,
    ) -> tuple[FailureRegressionCandidateCapture, ...]:
        run = await AgentRunControl(self._session).get(agent_run_id)
        if run is None or run.project_id != project_id:
            raise AgentRunNotFoundError("AgentRun not found")
        artifacts = ContentAddressedArtifactStore(
            self._session,
            self._artifact_root,
            record_integrity_status=False,
        )
        metadata = await artifacts.list_metadata(
            project_id=project_id,
            agent_run_id=agent_run_id,
            kind=FAILURE_REGRESSION_CANDIDATE_ARTIFACT_KIND,
        )
        captures = []
        for item in metadata:
            captures.append(await self._read_capture(run=run, artifact_ref=item.ref))
        return tuple(captures)

    async def _read_capture(
        self,
        *,
        run: AgentRun,
        artifact_ref: str,
    ) -> FailureRegressionCandidateCapture:
        value = await ContentAddressedArtifactStore(
            self._session,
            self._artifact_root,
            record_integrity_status=False,
        ).read_json_ref(
            project_id=run.project_id,
            basis_hash=run.basis_hash,
            ref=artifact_ref,
            expected_kind=FAILURE_REGRESSION_CANDIDATE_ARTIFACT_KIND,
        )
        return FailureRegressionCandidateCapture(
            artifact_ref=artifact_ref,
            candidate=FailureRegressionCandidate.model_validate(value),
        )


__all__ = [
    "FAILURE_REGRESSION_CANDIDATE_ARTIFACT_KIND",
    "FailureRegressionCandidate",
    "FailureRegressionCandidateCapture",
    "FailureRegressionCandidateService",
]
