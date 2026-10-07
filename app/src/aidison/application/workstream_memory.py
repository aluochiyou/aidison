"""Write and route structured cross-Run memory without restoring private state."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

from aidison.application.ports import DomainStore
from aidison.application.service import DomainConflictError
from aidison.domain.models import (
    Module,
    ModuleMemoryItem,
    ModuleMemoryItemKind,
    ModuleWorkstream,
)
from aidison.research.consolidation import CoverageDisposition, CoverageMatrixEntry
from aidison.research.coverage import CoverageContract, CoverageKey
from aidison.research.langgraph_contracts import ResultEnvelope
from aidison.workstreams.memory_routing import MemoryRouteDecision, select_memory_route


def _hash(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def module_fingerprint(module: Module) -> str:
    """Hash only the module facts that can make its evidence inapplicable."""

    return _hash(
        {
            "key": module.key,
            "name": module.name,
            "responsibility": module.responsibility,
            "acceptance": module.acceptance,
            "open_questions": module.open_questions,
        }
    )


def coverage_fingerprint(coverage_key: CoverageKey) -> str:
    """Hash the concrete research question and its evidence requirements."""

    return _hash(coverage_key.model_dump(mode="json"))


class ModuleWorkstreamMemoryApplication:
    """Application service for memory assets derived from accepted research only."""

    def __init__(self, store: DomainStore) -> None:
        self._store = store

    async def route_for_coverage(
        self,
        *,
        project_id: UUID,
        module: Module,
        coverage_key: CoverageKey,
        now: datetime | None = None,
    ) -> MemoryRouteDecision:
        if module.lineage_id is None:
            raise DomainConflictError("active module has no workstream lineage")
        workstream = next(
            (
                item
                for item in await self._store.list_module_workstreams(project_id)
                if item.module_lineage_id == module.lineage_id
            ),
            None,
        )
        if workstream is None:
            raise DomainConflictError("active module has no durable workstream")
        return select_memory_route(
            workstream=workstream,
            items=tuple(
                await self._store.list_module_memory_items(
                    workstream.id,
                    stable_key=coverage_key.key,
                )
            ),
            stable_key=coverage_key.key,
            module_fingerprint=module_fingerprint(module),
            coverage_fingerprint=coverage_fingerprint(coverage_key),
            now=now or datetime.now(UTC),
        )

    async def record_answered_coverage(
        self,
        *,
        project_id: UUID,
        project_revision: int,
        coverage: CoverageContract,
        modules: Sequence[Module],
        matrix: Sequence[CoverageMatrixEntry],
        admitted_results: Sequence[ResultEnvelope],
    ) -> tuple[ModuleMemoryItem, ...]:
        """Persist only coverage that deterministic consolidation marked answered.

        Memory stays a projection of an accepted ResultEnvelope.  It never
        upgrades a raw response, creates Project facts, or copies private
        messages.  Replaying the same result yields the same item identity.
        """

        workstreams = {
            item.module_lineage_id: item
            for item in await self._store.list_module_workstreams(project_id)
        }
        modules_by_id = {str(item.id): item for item in modules}
        coverage_by_key = {item.key: item for item in coverage.keys}
        results_by_id = {item.id: item for item in admitted_results}
        items: list[ModuleMemoryItem] = []
        touched_workstreams: dict[UUID, ModuleWorkstream] = {}

        for entry in matrix:
            if entry.status is not CoverageDisposition.ANSWERED:
                continue
            coverage_key = coverage_by_key[entry.coverage_key]
            if len(coverage_key.module_ids) != 1:
                continue
            module = modules_by_id.get(coverage_key.module_ids[0])
            if module is None or module.lineage_id is None:
                raise DomainConflictError("answered coverage has no active module lineage")
            workstream = workstreams.get(module.lineage_id)
            if workstream is None:
                raise DomainConflictError("answered coverage has no durable workstream")
            for result_id in entry.result_ids:
                result = results_by_id.get(result_id)
                if result is None or not result.evidence_refs:
                    continue
                if result.producer_profile_ref == "system://workstream-memory-reuse/v1":
                    # A reuse Result is a current-Run admission projection of
                    # an existing memory asset, not a new research discovery.
                    # Persisting it again would create an infinite lineage of
                    # equivalent items on every repeat Run.
                    continue
                content_hash = _hash(
                    {
                        "coverage_key": coverage_key.key,
                        "result_manifest_hash": result.manifest_hash,
                        "result_id": str(result.id),
                    }
                )
                items.append(
                    ModuleMemoryItem(
                        id=uuid5(
                            NAMESPACE_URL,
                            (
                                "aidison://module-memory/"
                                f"{workstream.id}/{coverage_key.key}/{content_hash}"
                            ),
                        ),
                        workstream_id=workstream.id,
                        kind=ModuleMemoryItemKind.FINDING,
                        stable_key=coverage_key.key,
                        basis_hash=result.basis_hash,
                        content_hash=content_hash,
                        artifact_ref=result.artifact_ref,
                        evidence_refs=result.evidence_refs,
                        source_result_id=result.id,
                        applicability={
                            "module_fingerprint": module_fingerprint(module),
                            "coverage_fingerprint": coverage_fingerprint(coverage_key),
                        },
                    )
                )
                touched_workstreams[workstream.id] = workstream

        if items:
            await self._store.add_module_memory_items(items)
        for workstream in touched_workstreams.values():
            if (
                workstream.last_project_revision == project_revision
                and workstream.last_basis_hash == coverage.basis_hash
            ):
                continue
            await self._store.update_module_workstream(
                workstream.model_copy(
                    update={
                        "last_project_revision": project_revision,
                        "last_basis_hash": coverage.basis_hash,
                        "optimistic_revision": workstream.optimistic_revision + 1,
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
        return tuple(items)
