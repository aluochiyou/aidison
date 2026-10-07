"""Durable storage for immutable ResultEnvelope and separate AdmissionRecord."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import AgentRunResultAdmissionRow, AgentRunResultRow, AgentRunRow
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ResultEnvelope,
    validate_result_admission,
)
from aidison.runtime.agent_result_events import (
    AgentResultEventType,
    agent_result_event_metadata,
)


class AgentResultConflictError(RuntimeError):
    pass


class AgentResultStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_result(self, result: ResultEnvelope) -> ResultEnvelope:
        existing = await self._session.scalar(
            select(AgentRunResultRow)
            .where(AgentRunResultRow.id == result.id)
            .with_for_update()
        )
        if existing is not None:
            if existing.payload != result.model_dump(mode="json"):
                raise AgentResultConflictError("ResultEnvelope id has another payload")
            return ResultEnvelope.model_validate(existing.payload)
        run = await self._session.scalar(
            select(AgentRunRow).where(AgentRunRow.id == result.run_id)
        )
        if run is None:
            raise AgentResultConflictError("ResultEnvelope references a missing AgentRun")
        row = AgentRunResultRow(
            id=result.id,
            agent_run_id=result.run_id,
            task_id=result.task_id,
            basis_hash=result.basis_hash,
            manifest_hash=result.manifest_hash,
            event_version=1,
            payload=result.model_dump(mode="json"),
        )
        self._session.add(row)
        await self._session.flush()
        payload: dict[str, object] = {"result": result.model_dump(mode="json")}
        await self._append_event(
            project_id=run.project_id,
            result=result,
            aggregate_version=1,
            event_type=AgentResultEventType.RECORDED,
            payload=payload,
            occurred_at=row.created_at,
            artifact_refs=_result_artifact_refs(result),
        )
        return result

    async def get_result(self, *, result_id: UUID) -> ResultEnvelope | None:
        """Load one immutable producer result without treating it as admitted.

        Callers that use this for cross-Run reuse must still create a new
        current-Run ResultEnvelope and AdmissionRecord. This read method never
        transfers admission authority across Run boundaries.
        """

        row = await self._session.get(AgentRunResultRow, result_id)
        return ResultEnvelope.model_validate(row.payload) if row is not None else None

    async def get_admission(self, *, result_id: UUID) -> AdmissionRecord | None:
        """Load the one durable verdict without inferring admission from the result row."""
        row = await self._session.scalar(
            select(AgentRunResultAdmissionRow).where(
                AgentRunResultAdmissionRow.result_id == result_id
            )
        )
        return AdmissionRecord.model_validate(row.payload) if row is not None else None

    async def admit(self, admission: AdmissionRecord) -> AdmissionRecord:
        result = await self._session.scalar(
            select(AgentRunResultRow)
            .where(AgentRunResultRow.id == admission.result_id)
            .with_for_update()
        )
        if result is None or result.agent_run_id != admission.run_id:
            raise AgentResultConflictError(
                "AdmissionRecord references another or missing AgentRun result"
            )
        if result.manifest_hash != admission.result_manifest_hash:
            raise AgentResultConflictError("AdmissionRecord manifest hash does not match result")
        try:
            validate_result_admission(
                result=ResultEnvelope.model_validate(result.payload),
                admission=admission,
            )
        except ValueError as exc:
            raise AgentResultConflictError(str(exc)) from exc
        existing = await self._session.scalar(
            select(AgentRunResultAdmissionRow).where(
                AgentRunResultAdmissionRow.result_id == admission.result_id
            )
        )
        if existing is not None:
            if existing.payload != admission.model_dump(mode="json"):
                raise AgentResultConflictError("result already has another admission verdict")
            return AdmissionRecord.model_validate(existing.payload)

        # A task may produce several immutable Results while a provider call is
        # retried or an earlier one is rejected.  It may, however, contribute
        # only one accepted Result to a Run's dependency projection.  Lock the
        # parent Run before querying so two concurrent admission transactions
        # cannot both observe an empty accepted set and promote different
        # Results for the same stable task identity.
        run = await self._session.scalar(
            select(AgentRunRow)
            .where(AgentRunRow.id == admission.run_id)
            .with_for_update()
        )
        if run is None:
            raise AgentResultConflictError("AdmissionRecord references a missing AgentRun")
        if admission.disposition is AdmissionDisposition.ACCEPTED:
            accepted_for_task = await self._session.scalar(
                select(AgentRunResultAdmissionRow.id)
                .join(
                    AgentRunResultRow,
                    AgentRunResultRow.id == AgentRunResultAdmissionRow.result_id,
                )
                .where(
                    AgentRunResultRow.agent_run_id == admission.run_id,
                    AgentRunResultRow.task_id == result.task_id,
                    AgentRunResultAdmissionRow.agent_run_id == admission.run_id,
                    AgentRunResultAdmissionRow.disposition == AdmissionDisposition.ACCEPTED.value,
                )
            )
            if accepted_for_task is not None:
                raise AgentResultConflictError("task already has an accepted result")
        self._session.add(
            AgentRunResultAdmissionRow(
                id=admission.id,
                agent_run_id=admission.run_id,
                result_id=admission.result_id,
                disposition=admission.disposition.value,
                payload=admission.model_dump(mode="json"),
            )
        )
        if result.event_version != 1:
            # Historical rows may exist before this aggregate's stream was
            # introduced. Preserve their relational authority rather than
            # fabricating a version-2 event with no version-1 predecessor.
            await self._session.flush()
            return admission
        result.event_version = 2
        await self._session.flush()
        admission_row = await self._session.scalar(
            select(AgentRunResultAdmissionRow).where(
                AgentRunResultAdmissionRow.id == admission.id
            )
        )
        if admission_row is None:
            raise AgentResultConflictError("admission row disappeared before event append")
        result_envelope = ResultEnvelope.model_validate(result.payload)
        payload: dict[str, object] = {
            "result": result_envelope.model_dump(mode="json"),
            "admission": admission.model_dump(mode="json"),
        }
        await self._append_event(
            project_id=run.project_id,
            result=result_envelope,
            aggregate_version=2,
            event_type={
                AdmissionDisposition.ACCEPTED: AgentResultEventType.ACCEPTED,
                AdmissionDisposition.QUARANTINED: AgentResultEventType.QUARANTINED,
                AdmissionDisposition.REJECTED: AgentResultEventType.REJECTED,
            }[admission.disposition],
            payload=payload,
            occurred_at=admission_row.created_at,
            artifact_refs=_result_artifact_refs(result_envelope, admission=admission),
        )
        return admission

    async def _append_event(
        self,
        *,
        project_id: UUID,
        result: ResultEnvelope,
        aggregate_version: int,
        event_type: AgentResultEventType,
        payload: dict[str, object],
        occurred_at: datetime,
        artifact_refs: tuple[str, ...],
    ) -> None:
        """Persist an aggregate event in the caller's existing transaction."""

        from aidison.infrastructure.store import PostgresDomainStore

        metadata = agent_result_event_metadata(
            result=result,
            aggregate_version=aggregate_version,
            payload=payload,
            occurred_at=occurred_at,
            artifact_refs=artifact_refs,
        )
        await PostgresDomainStore(self._session).append_event(
            project_id,
            event_type.value,
            payload,
            metadata,
        )
    async def admitted_task_ids(self, *, run_id: UUID) -> tuple[UUID, ...]:
        """Return the durable Control facts that may satisfy task dependencies."""

        rows = await self._session.scalars(
            select(AgentRunResultRow.task_id)
            .join(
                AgentRunResultAdmissionRow,
                AgentRunResultAdmissionRow.result_id == AgentRunResultRow.id,
            )
            .where(
                AgentRunResultRow.agent_run_id == run_id,
                AgentRunResultAdmissionRow.agent_run_id == run_id,
                AgentRunResultAdmissionRow.disposition == AdmissionDisposition.ACCEPTED.value,
            )
            .distinct()
            .order_by(AgentRunResultRow.task_id)
        )
        return tuple(rows)

    async def admitted_results(self, *, run_id: UUID) -> tuple[ResultEnvelope, ...]:
        """Return accepted immutable results in stable task/result order.

        This is a read projection for deterministic consolidation.  It does
        not create a second canonical result store and it never returns
        merely-produced or quarantined outputs.
        """

        rows = (
            await self._session.scalars(
                select(AgentRunResultRow)
                .join(
                    AgentRunResultAdmissionRow,
                    AgentRunResultAdmissionRow.result_id == AgentRunResultRow.id,
                )
                .where(
                    AgentRunResultRow.agent_run_id == run_id,
                    AgentRunResultAdmissionRow.agent_run_id == run_id,
                    AgentRunResultAdmissionRow.disposition == AdmissionDisposition.ACCEPTED.value,
                )
                .order_by(AgentRunResultRow.task_id, AgentRunResultRow.id)
            )
        ).all()
        return tuple(ResultEnvelope.model_validate(row.payload) for row in rows)


def _result_artifact_refs(
    result: ResultEnvelope,
    *,
    admission: AdmissionRecord | None = None,
) -> tuple[str, ...]:
    """Expose references, never artifact bodies, in the event envelope."""

    refs = [
        result.artifact_ref,
        *result.evidence_refs,
        *result.coverage_observation_refs,
        *result.unresolved_refs,
    ]
    for optional_ref in (
        result.source_collection_report_ref,
        result.context_manifest_ref,
        result.usage_ref,
        result.failure_ref,
        admission.admitted_ref if admission is not None else None,
    ):
        if optional_ref is not None:
            refs.append(optional_ref)
    # The event envelope carries a bounded index only; the complete reference
    # lists remain in the hashed payload and immutable ResultEnvelope.
    return tuple(dict.fromkeys(refs))[:64]
