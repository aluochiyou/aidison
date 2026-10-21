"""Scoped Artifact reads so a ref alone never grants content access."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aidison.artifacts.contracts import ArtifactMetadata, ArtifactStatus


class ArtifactAccessDenied(RuntimeError):
    pass


class ArtifactAccessScope(BaseModel):
    """Trusted caller scope; it is derived from task/run/UI authorization, never a ref."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: UUID
    basis_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    agent_run_id: UUID | None = None
    allowed_kinds: tuple[str, ...] = Field(min_length=1, max_length=64)
    max_bytes: int = Field(gt=0, le=2_000_000_000)


class ArtifactReadPort(Protocol):
    async def get_metadata(self, *, project_id: UUID, artifact_id: UUID) -> ArtifactMetadata: ...

    async def read_bytes(self, *, project_id: UUID, artifact_id: UUID) -> bytes: ...


_ARTIFACT_REF = re.compile(r"artifact\+sha256://([a-f0-9]{64})/([0-9a-fA-F-]{36})")


class ScopedArtifactReader:
    """Recheck a trusted scope, ref identity, metadata and bytes before every read."""

    def __init__(self, store: ArtifactReadPort) -> None:
        self._store = store

    async def read_bytes_ref(self, *, scope: ArtifactAccessScope, ref: str) -> bytes:
        expected_hash, artifact_id = self._parse_ref(ref)
        metadata = await self._store.get_metadata(
            project_id=scope.project_id,
            artifact_id=artifact_id,
        )
        self._authorize(scope=scope, metadata=metadata, expected_hash=expected_hash)
        content = await self._store.read_bytes(
            project_id=scope.project_id,
            artifact_id=artifact_id,
        )
        if len(content) != metadata.size_bytes or sha256(content).hexdigest() != expected_hash:
            raise ArtifactAccessDenied("artifact bytes failed scoped integrity verification")
        return content

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str, UUID]:
        matched = _ARTIFACT_REF.fullmatch(ref)
        if matched is None:
            raise ArtifactAccessDenied("artifact access requires a content-addressed ref")
        return matched.group(1), UUID(matched.group(2))

    @staticmethod
    def _authorize(
        *,
        scope: ArtifactAccessScope,
        metadata: ArtifactMetadata,
        expected_hash: str,
    ) -> None:
        if metadata.project_id != scope.project_id:
            raise ArtifactAccessDenied("artifact project scope does not match")
        if scope.basis_hash is not None and metadata.basis_hash != scope.basis_hash:
            raise ArtifactAccessDenied("artifact basis scope does not match")
        if scope.agent_run_id is not None and metadata.agent_run_id != scope.agent_run_id:
            raise ArtifactAccessDenied("artifact AgentRun scope does not match")
        if metadata.kind not in scope.allowed_kinds:
            raise ArtifactAccessDenied("artifact kind is outside the access scope")
        if metadata.status is not ArtifactStatus.PRESENT:
            raise ArtifactAccessDenied("artifact is not available for scoped access")
        if metadata.size_bytes > scope.max_bytes:
            raise ArtifactAccessDenied("artifact exceeds scoped byte limit")
        if metadata.content_hash != expected_hash:
            raise ArtifactAccessDenied("artifact ref hash does not match metadata")


__all__ = [
    "ArtifactAccessDenied",
    "ArtifactAccessScope",
    "ArtifactReadPort",
    "ScopedArtifactReader",
]
