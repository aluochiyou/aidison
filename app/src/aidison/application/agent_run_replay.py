"""Read-only assembly of an AgentRun evaluation/replay bundle."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.artifacts.contracts import ArtifactMetadata, ArtifactStatus
from aidison.evaluation.contracts import EvaluationMode, EvaluationReport
from aidison.evaluation.replay_bundle import replay_bundle_evaluation_case
from aidison.evaluation.reporter import EvaluationReporter
from aidison.evaluation.runner import run_evaluation
from aidison.infrastructure.agent_runs import AgentRunControl
from aidison.infrastructure.artifacts import (
    ArtifactIntegrityError,
    ContentAddressedArtifactStore,
)
from aidison.infrastructure.replay import InvocationRecordingRepository
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.agent_run_events import replay_agent_run
from aidison.runtime.checkpoint_anchor import (
    CheckpointAnchorVerificationError,
    verify_admitted_checkpoint_anchor,
)
from aidison.runtime.contracts import InvocationRecording
from aidison.runtime.replay_bundle import (
    EvaluationReplayBundle,
    InvocationReplayReference,
    LifecycleEventReference,
    ReplayBundleIntegrityError,
    build_evaluation_replay_bundle,
    verify_evaluation_replay_bundle,
)


class AgentRunReplayBundleNotFoundError(RuntimeError):
    """The requested AgentRun does not have a control-plane record."""


EVALUATION_REPLAY_BUNDLE_ARTIFACT_KIND = "evaluation_replay_bundle"


class ReplayCheckpointVerifierUnavailableError(CheckpointAnchorVerificationError):
    """Evaluation was requested without a read-only checkpoint-store verifier."""


class AgentRunReplayBundleService:
    """Assemble and validate replay inputs without starting LangGraph or a tool."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        artifact_root: Path,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        self._session = session
        self._artifact_root = artifact_root
        self._checkpointer = checkpointer

    async def build(self, *, agent_run_id: UUID) -> EvaluationReplayBundle:
        control = AgentRunControl(self._session)
        run = await control.get(agent_run_id)
        if run is None:
            raise AgentRunReplayBundleNotFoundError("AgentRun not found")
        events = tuple(
            await PostgresDomainStore(self._session).list_aggregate_events(
                run.project_id,
                aggregate_type="agent_run",
                aggregate_id=run.id,
            )
        )
        invocations = await InvocationRecordingRepository(self._session).list_for_run(
            project_id=run.project_id,
            agent_run_id=run.id,
        )
        available_refs, corrupt_refs = await self._artifact_availability(
            run.project_id,
            run.basis_hash,
            invocations,
        )
        return build_evaluation_replay_bundle(
            run=run,
            lifecycle_events=events,
            invocations=invocations,
            available_artifact_refs=available_refs,
            corrupt_artifact_refs=corrupt_refs,
        )

    async def verify(self, *, agent_run_id: UUID) -> EvaluationReplayBundle:
        bundle = await self.build(agent_run_id=agent_run_id)
        control = AgentRunControl(self._session)
        run = await control.get(agent_run_id)
        if run is None:  # pragma: no cover - build read it in the same session
            raise AgentRunReplayBundleNotFoundError("AgentRun disappeared during replay check")
        events = tuple(
            await PostgresDomainStore(self._session).list_aggregate_events(
                run.project_id,
                aggregate_type="agent_run",
                aggregate_id=run.id,
            )
        )
        invocations = await InvocationRecordingRepository(self._session).list_for_run(
            project_id=run.project_id,
            agent_run_id=run.id,
        )
        available_refs, corrupt_refs = await self._artifact_availability(
            run.project_id,
            run.basis_hash,
            invocations,
        )
        verify_evaluation_replay_bundle(
            bundle,
            run=run,
            lifecycle_events=events,
            invocations=invocations,
            available_artifact_refs=available_refs,
            corrupt_artifact_refs=corrupt_refs,
        )
        return bundle

    async def persist(self, *, agent_run_id: UUID) -> ArtifactMetadata:
        """Freeze the current bundle as an immutable Artifact.

        Incomplete bundles are intentionally persisted too: evaluation needs a
        durable explanation of which historical input was unavailable, rather
        than silently discarding that failed replay attempt.
        """
        bundle = await self.build(agent_run_id=agent_run_id)
        control = AgentRunControl(self._session)
        run = await control.get(agent_run_id)
        if run is None:  # pragma: no cover - build read it in the same session
            raise AgentRunReplayBundleNotFoundError("AgentRun disappeared before bundle persist")
        return await ContentAddressedArtifactStore(
            self._session,
            self._artifact_root,
        ).put_agent_run_json(
            project_id=run.project_id,
            agent_run_id=run.id,
            basis_hash=run.basis_hash,
            kind=EVALUATION_REPLAY_BUNDLE_ARTIFACT_KIND,
            value=bundle.model_dump(mode="json"),
        )

    async def verify_persisted(
        self,
        *,
        agent_run_id: UUID,
        bundle_ref: str,
    ) -> EvaluationReplayBundle:
        """Verify an immutable bundle artifact against current recorded inputs only."""
        control = AgentRunControl(self._session)
        run = await control.get(agent_run_id)
        if run is None:
            raise AgentRunReplayBundleNotFoundError("AgentRun not found")
        payload = await ContentAddressedArtifactStore(
            self._session,
            self._artifact_root,
            record_integrity_status=False,
        ).read_json_ref(
            project_id=run.project_id,
            basis_hash=run.basis_hash,
            ref=bundle_ref,
            expected_kind=EVALUATION_REPLAY_BUNDLE_ARTIFACT_KIND,
        )
        bundle = EvaluationReplayBundle.model_validate(payload)
        all_events = tuple(
            await PostgresDomainStore(self._session).list_aggregate_events(
                run.project_id,
                aggregate_type="agent_run",
                aggregate_id=run.id,
            )
        )
        events = tuple(
            event for event in all_events if event.project_seq <= bundle.event_cursor
        )
        event_refs = tuple(LifecycleEventReference.from_event(event) for event in events)
        if event_refs != bundle.lifecycle_events:
            raise ReplayBundleIntegrityError(
                "persisted replay bundle lifecycle events no longer match the recorded prefix"
            )
        historical_run = replay_agent_run(list(events)).agent_run
        if (
            historical_run.id != bundle.agent_run_id
            or historical_run.project_id != bundle.project_id
            or historical_run.basis_hash != bundle.basis_hash
            or historical_run.runtime_binding != bundle.runtime_binding
            or historical_run.admitted_checkpoint != bundle.admitted_checkpoint
        ):
            raise ReplayBundleIntegrityError(
                "persisted replay bundle does not match its reconstructed AgentRun prefix"
            )
        all_invocations = await InvocationRecordingRepository(self._session).list_for_run(
            project_id=run.project_id,
            agent_run_id=run.id,
        )
        invocations_by_key = {
            invocation.idempotency_key: invocation for invocation in all_invocations
        }
        try:
            invocations = tuple(
                invocations_by_key[reference.idempotency_key]
                for reference in bundle.invocations
            )
        except KeyError as exc:
            raise ReplayBundleIntegrityError(
                "persisted replay bundle invocation is missing from the replay ledger"
            ) from exc
        if tuple(
            InvocationReplayReference.from_recording(invocation) for invocation in invocations
        ) != bundle.invocations:
            raise ReplayBundleIntegrityError(
                "persisted replay bundle invocation no longer matches the replay ledger"
            )
        available_refs, corrupt_refs = await self._artifact_availability(
            historical_run.project_id,
            historical_run.basis_hash,
            invocations,
        )
        verify_evaluation_replay_bundle(
            bundle,
            run=historical_run,
            lifecycle_events=events,
            invocations=invocations,
            available_artifact_refs=available_refs,
            corrupt_artifact_refs=corrupt_refs,
        )
        return bundle

    async def evaluate_persisted(
        self,
        *,
        agent_run_id: UUID,
        bundle_ref: str,
        mode: EvaluationMode = EvaluationMode.OFFLINE,
        reporter: EvaluationReporter | None = None,
    ) -> EvaluationReport:
        """Evaluate a verified historical bundle through the offline harness.

        Database and Artifact verification happens before fixture construction.
        The runner and optional Langfuse reporter receive only the frozen,
        secret-free bundle payload and never a live execution handle.
        """
        bundle = await self.verify_persisted(
            agent_run_id=agent_run_id,
            bundle_ref=bundle_ref,
        )
        checkpoint = bundle.admitted_checkpoint
        if checkpoint is None:  # pragma: no cover - verify_persisted requires replayable
            raise CheckpointAnchorVerificationError("replayable bundle is missing checkpoint")
        if self._checkpointer is None:
            raise ReplayCheckpointVerifierUnavailableError(
                "replay evaluation requires an injected LangGraph checkpoint verifier"
            )
        await verify_admitted_checkpoint_anchor(
            checkpointer=self._checkpointer,
            checkpoint=checkpoint,
        )
        return await run_evaluation(
            cases=(replay_bundle_evaluation_case(bundle),),
            mode=mode,
            reporter=reporter,
        )

    async def _artifact_availability(
        self,
        project_id: UUID,
        basis_hash: str,
        invocations: tuple[InvocationRecording, ...],
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Return readable and corrupt recorded outputs without provider fallback."""
        artifacts = ContentAddressedArtifactStore(
            self._session,
            self._artifact_root,
            record_integrity_status=False,
        )
        refs: set[str] = set()
        corrupt_refs: set[str] = set()
        for invocation in invocations:
            reference = invocation.response_artifact_ref
            if reference is None:
                continue
            try:
                _, artifact_id = artifacts.parse_ref(reference)
                metadata = await artifacts.get_metadata(
                    project_id=project_id,
                    artifact_id=artifact_id,
                )
                if (
                    metadata.basis_hash != basis_hash
                    or metadata.status is not ArtifactStatus.PRESENT
                ):
                    continue
                await artifacts.read_bytes(project_id=project_id, artifact_id=artifact_id)
            except ArtifactIntegrityError:
                corrupt_refs.add(reference)
                continue
            except RuntimeError:
                continue
            refs.add(reference)
        return frozenset(refs), frozenset(corrupt_refs)
