"""Capture failed AgentRuns as immutable, human-review evaluation candidates."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
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
from aidison.evaluation.contracts import EvaluationMode, EvaluationReport, canonical_hash, utc_now
from aidison.evaluation.regression import (
    AgentRunRegressionOracle,
    GoldenRegressionTask,
    golden_regression_evaluation_case,
)
from aidison.evaluation.runner import run_evaluation
from aidison.evaluation.settings import EvaluationSettings, build_evaluation_reporter
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
FAILURE_REGRESSION_REVIEW_ARTIFACT_KIND = "failure_regression_review"
GOLDEN_REGRESSION_TASK_ARTIFACT_KIND = "golden_regression_task"
GOLDEN_REGRESSION_REPORT_ARTIFACT_KIND = "golden_regression_report"


class FailureRegressionReviewDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


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


class FailureRegressionReview(FrozenModel):
    schema_version: Literal["failure-regression-review.v1"] = "failure-regression-review.v1"
    project_id: UUID
    agent_run_id: UUID
    candidate_key: str = Field(pattern=r"^failure-[a-f0-9]{32}$")
    candidate_artifact_ref: str = Field(min_length=1, max_length=500)
    decision: FailureRegressionReviewDecision
    reviewed_by: str = Field(min_length=1, max_length=160)
    reviewed_at: datetime
    review_notes: str = Field(min_length=1, max_length=4_000)
    expected_outcome: str | None = Field(default=None, min_length=1, max_length=4_000)
    oracle: AgentRunRegressionOracle | None = None
    golden_task_ref: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def decision_matches_promotion(self) -> FailureRegressionReview:
        labels = self.expected_outcome is not None and self.oracle is not None
        has_any_label = self.expected_outcome is not None or self.oracle is not None
        if self.decision is FailureRegressionReviewDecision.APPROVED:
            if not labels or self.golden_task_ref is None:
                raise ValueError("approved review requires labels and a Golden Task")
        elif has_any_label or self.golden_task_ref is not None:
            raise ValueError("rejected review cannot carry Golden Task fields")
        return self


class FailureRegressionReviewResult(FrozenModel):
    review_artifact_ref: str = Field(min_length=1, max_length=500)
    review: FailureRegressionReview
    golden_task_artifact_ref: str | None = Field(default=None, max_length=500)
    golden_task: GoldenRegressionTask | None = None

    @model_validator(mode="after")
    def golden_fields_match_review(self) -> FailureRegressionReviewResult:
        if (self.golden_task_artifact_ref is None) != (self.golden_task is None):
            raise ValueError("Golden Task ref and payload must be present together")
        if self.review.golden_task_ref != self.golden_task_artifact_ref:
            raise ValueError("review and response Golden Task refs differ")
        return self


class GoldenRegressionEvaluation(FrozenModel):
    report_artifact_ref: str = Field(min_length=1, max_length=500)
    report: EvaluationReport


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

    async def review(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID,
        candidate_key: str,
        decision: FailureRegressionReviewDecision,
        reviewed_by: str,
        review_notes: str,
        expected_outcome: str | None,
        oracle: AgentRunRegressionOracle | None,
        idempotency_key: str,
    ) -> FailureRegressionReviewResult:
        run = await self._owned_run(project_id=project_id, agent_run_id=agent_run_id)
        candidate_capture = await self._candidate_by_key(
            run=run,
            candidate_key=candidate_key,
        )
        self._validate_review_request(
            candidate=candidate_capture.candidate,
            decision=decision,
            expected_outcome=expected_outcome,
            oracle=oracle,
        )
        payload_hash = canonical_hash(
            "review-failure-regression-candidate.v1",
            project_id,
            agent_run_id,
            candidate_key,
            decision,
            reviewed_by,
            review_notes,
            expected_outcome,
            oracle,
        )
        domain_store = PostgresDomainStore(self._session)
        receipt = await domain_store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            return await self._read_review_result(run=run, review_ref=receipt)

        existing = await self._reviews(run=run, candidate_key=candidate_key)
        if existing:
            raise AgentRunConflictError("failure regression candidate was already reviewed")

        reviewed_at = utc_now()
        artifacts = ContentAddressedArtifactStore(self._session, self._artifact_root)
        golden_task: GoldenRegressionTask | None = None
        golden_ref: str | None = None
        if decision is FailureRegressionReviewDecision.APPROVED:
            assert expected_outcome is not None and oracle is not None
            golden_task = GoldenRegressionTask(
                golden_task_key=(
                    "golden-"
                    + canonical_hash(
                        candidate_key,
                        candidate_capture.candidate.replay_bundle_manifest_hash,
                        expected_outcome,
                        oracle,
                    )[:32]
                ),
                project_id=project_id,
                source_agent_run_id=agent_run_id,
                run_kind=candidate_capture.candidate.run_kind,
                source_candidate_key=candidate_key,
                source_candidate_ref=candidate_capture.artifact_ref,
                replay_bundle_ref=candidate_capture.candidate.replay_bundle_ref,
                replay_bundle_manifest_hash=(
                    candidate_capture.candidate.replay_bundle_manifest_hash
                ),
                expected_outcome=expected_outcome,
                oracle=oracle,
                reviewed_by=reviewed_by,
                reviewed_at=reviewed_at,
            )
            golden_artifact = await artifacts.put_agent_run_json(
                project_id=project_id,
                agent_run_id=agent_run_id,
                basis_hash=run.basis_hash,
                kind=GOLDEN_REGRESSION_TASK_ARTIFACT_KIND,
                value=golden_task.model_dump(mode="json"),
                commit=False,
            )
            golden_ref = golden_artifact.ref

        review = FailureRegressionReview(
            project_id=project_id,
            agent_run_id=agent_run_id,
            candidate_key=candidate_key,
            candidate_artifact_ref=candidate_capture.artifact_ref,
            decision=decision,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            review_notes=review_notes,
            expected_outcome=expected_outcome,
            oracle=oracle,
            golden_task_ref=golden_ref,
        )
        review_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=agent_run_id,
            basis_hash=run.basis_hash,
            kind=FAILURE_REGRESSION_REVIEW_ARTIFACT_KIND,
            value=review.model_dump(mode="json"),
            commit=False,
        )
        await domain_store.append_event(
            project_id,
            "evaluation.failure_candidate_reviewed",
            {
                "agent_run_id": str(agent_run_id),
                "candidate_key": candidate_key,
                "decision": decision.value,
                "review_artifact_ref": review_artifact.ref,
                "golden_task_ref": golden_ref,
            },
        )
        await domain_store.save_command_receipt(
            idempotency_key,
            payload_hash,
            review_artifact.ref,
        )
        await self._session.commit()
        return FailureRegressionReviewResult(
            review_artifact_ref=review_artifact.ref,
            review=review,
            golden_task_artifact_ref=golden_ref,
            golden_task=golden_task,
        )

    async def list_golden_tasks(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID,
    ) -> tuple[GoldenRegressionTask, ...]:
        run = await self._owned_run(project_id=project_id, agent_run_id=agent_run_id)
        artifacts = ContentAddressedArtifactStore(
            self._session,
            self._artifact_root,
            record_integrity_status=False,
        )
        metadata = await artifacts.list_metadata(
            project_id=project_id,
            agent_run_id=agent_run_id,
            kind=GOLDEN_REGRESSION_TASK_ARTIFACT_KIND,
        )
        tasks = []
        for item in metadata:
            tasks.append(
                GoldenRegressionTask.model_validate(
                    await artifacts.read_json_ref(
                        project_id=project_id,
                        basis_hash=run.basis_hash,
                        ref=item.ref,
                        expected_kind=GOLDEN_REGRESSION_TASK_ARTIFACT_KIND,
                    )
                )
            )
        return tuple(tasks)

    async def evaluate_golden_task(
        self,
        *,
        project_id: UUID,
        source_agent_run_id: UUID,
        golden_task_key: str,
        observed_agent_run_id: UUID,
        idempotency_key: str,
    ) -> GoldenRegressionEvaluation:
        source_run = await self._owned_run(
            project_id=project_id,
            agent_run_id=source_agent_run_id,
        )
        observed_run = await self._owned_run(
            project_id=project_id,
            agent_run_id=observed_agent_run_id,
        )
        if observed_run.completed_at is None:
            raise AgentRunConflictError("only a terminal AgentRun can be regression evaluated")
        tasks = await self.list_golden_tasks(
            project_id=project_id,
            agent_run_id=source_agent_run_id,
        )
        golden_task = next(
            (task for task in tasks if task.golden_task_key == golden_task_key),
            None,
        )
        if golden_task is None:
            raise AgentRunNotFoundError("Golden regression task not found")
        payload_hash = canonical_hash(
            "evaluate-golden-regression-task.v1",
            golden_task.model_dump(mode="json"),
            observed_agent_run_id,
        )
        domain_store = PostgresDomainStore(self._session)
        receipt = await domain_store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            return await self._read_golden_evaluation(
                run=observed_run,
                report_ref=receipt,
            )
        trajectory = await AgentRunTrajectoryService(self._session).build(
            project_id=project_id,
            agent_run_id=observed_agent_run_id,
        )
        reporter = build_evaluation_reporter(EvaluationSettings())
        report = await run_evaluation(
            cases=(
                golden_regression_evaluation_case(
                    task=golden_task,
                    trajectory=trajectory,
                ),
            ),
            mode=EvaluationMode.LIVE if reporter is not None else EvaluationMode.OFFLINE,
            reporter=reporter,
        )
        artifact = await ContentAddressedArtifactStore(
            self._session,
            self._artifact_root,
        ).put_agent_run_json(
            project_id=project_id,
            agent_run_id=observed_agent_run_id,
            basis_hash=observed_run.basis_hash,
            kind=GOLDEN_REGRESSION_REPORT_ARTIFACT_KIND,
            value=report.model_dump(mode="json"),
            commit=False,
        )
        await domain_store.append_event(
            project_id,
            "evaluation.golden_regression_completed",
            {
                "source_agent_run_id": str(source_run.id),
                "observed_agent_run_id": str(observed_agent_run_id),
                "golden_task_key": golden_task_key,
                "report_artifact_ref": artifact.ref,
                "all_passed": report.summary.all_passed,
            },
        )
        await domain_store.save_command_receipt(
            idempotency_key,
            payload_hash,
            artifact.ref,
        )
        await self._session.commit()
        return GoldenRegressionEvaluation(report_artifact_ref=artifact.ref, report=report)

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

    async def _owned_run(self, *, project_id: UUID, agent_run_id: UUID) -> AgentRun:
        run = await AgentRunControl(self._session).get(agent_run_id)
        if run is None or run.project_id != project_id:
            raise AgentRunNotFoundError("AgentRun not found")
        return run

    async def _candidate_by_key(
        self,
        *,
        run: AgentRun,
        candidate_key: str,
    ) -> FailureRegressionCandidateCapture:
        candidates = await self.list(project_id=run.project_id, agent_run_id=run.id)
        candidate = next(
            (item for item in candidates if item.candidate.candidate_key == candidate_key),
            None,
        )
        if candidate is None:
            raise AgentRunNotFoundError("Failure regression candidate not found")
        return candidate

    async def _reviews(
        self,
        *,
        run: AgentRun,
        candidate_key: str,
    ) -> tuple[FailureRegressionReviewResult, ...]:
        artifacts = ContentAddressedArtifactStore(
            self._session,
            self._artifact_root,
            record_integrity_status=False,
        )
        metadata = await artifacts.list_metadata(
            project_id=run.project_id,
            agent_run_id=run.id,
            kind=FAILURE_REGRESSION_REVIEW_ARTIFACT_KIND,
        )
        reviews = []
        for item in metadata:
            result = await self._read_review_result(run=run, review_ref=item.ref)
            if result.review.candidate_key == candidate_key:
                reviews.append(result)
        return tuple(reviews)

    async def _read_review_result(
        self,
        *,
        run: AgentRun,
        review_ref: str,
    ) -> FailureRegressionReviewResult:
        artifacts = ContentAddressedArtifactStore(
            self._session,
            self._artifact_root,
            record_integrity_status=False,
        )
        review = FailureRegressionReview.model_validate(
            await artifacts.read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=review_ref,
                expected_kind=FAILURE_REGRESSION_REVIEW_ARTIFACT_KIND,
            )
        )
        golden_task = None
        if review.golden_task_ref is not None:
            golden_task = GoldenRegressionTask.model_validate(
                await artifacts.read_json_ref(
                    project_id=run.project_id,
                    basis_hash=run.basis_hash,
                    ref=review.golden_task_ref,
                    expected_kind=GOLDEN_REGRESSION_TASK_ARTIFACT_KIND,
                )
            )
        return FailureRegressionReviewResult(
            review_artifact_ref=review_ref,
            review=review,
            golden_task_artifact_ref=review.golden_task_ref,
            golden_task=golden_task,
        )

    async def _read_golden_evaluation(
        self,
        *,
        run: AgentRun,
        report_ref: str,
    ) -> GoldenRegressionEvaluation:
        report = EvaluationReport.model_validate(
            await ContentAddressedArtifactStore(
                self._session,
                self._artifact_root,
                record_integrity_status=False,
            ).read_json_ref(
                project_id=run.project_id,
                basis_hash=run.basis_hash,
                ref=report_ref,
                expected_kind=GOLDEN_REGRESSION_REPORT_ARTIFACT_KIND,
            )
        )
        return GoldenRegressionEvaluation(report_artifact_ref=report_ref, report=report)

    @staticmethod
    def _validate_review_request(
        *,
        candidate: FailureRegressionCandidate,
        decision: FailureRegressionReviewDecision,
        expected_outcome: str | None,
        oracle: AgentRunRegressionOracle | None,
    ) -> None:
        if decision is FailureRegressionReviewDecision.REJECTED:
            if expected_outcome is not None or oracle is not None:
                raise AgentRunConflictError(
                    "rejected candidate cannot create a Golden Task oracle"
                )
            return
        if expected_outcome is None or oracle is None:
            raise AgentRunConflictError(
                "approved candidate requires a human expected outcome and scoring oracle"
            )
        if candidate.replay_bundle_status is not ReplayBundleStatus.REPLAYABLE:
            raise AgentRunConflictError(
                "incomplete replay bundle cannot be promoted to a Golden Task"
            )


__all__ = [
    "FAILURE_REGRESSION_CANDIDATE_ARTIFACT_KIND",
    "FAILURE_REGRESSION_REVIEW_ARTIFACT_KIND",
    "GOLDEN_REGRESSION_REPORT_ARTIFACT_KIND",
    "GOLDEN_REGRESSION_TASK_ARTIFACT_KIND",
    "FailureRegressionCandidate",
    "FailureRegressionCandidateCapture",
    "FailureRegressionCandidateService",
    "FailureRegressionReview",
    "FailureRegressionReviewDecision",
    "FailureRegressionReviewResult",
    "GoldenRegressionEvaluation",
]
