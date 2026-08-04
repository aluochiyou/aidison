from __future__ import annotations

import json
import os
import re
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.artifacts.contracts import ArtifactMetadata, ArtifactStatus
from aidison.infrastructure.orm import ArtifactRow, ProjectRow


class ArtifactNotFoundError(RuntimeError):
    pass


class ArtifactIntegrityError(RuntimeError):
    pass


def _metadata(row: ArtifactRow) -> ArtifactMetadata:
    return ArtifactMetadata(
        id=row.id,
        project_id=row.project_id,
        attempt_id=row.attempt_id,
        basis_hash=row.basis_hash,
        kind=row.kind,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        media_type=row.media_type,
        storage_key=row.storage_key,
        status=ArtifactStatus(row.status),
        source_url=row.source_url,
        created_at=row.created_at,
    )


class ContentAddressedArtifactStore:
    """Immutable byte storage with project-scoped PostgreSQL metadata."""

    def __init__(self, session: AsyncSession, root: Path) -> None:
        self._session = session
        self._root = root.resolve()

    @staticmethod
    def _storage_key(content_hash: str) -> str:
        return f"sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"

    def _path(self, storage_key: str) -> Path:
        candidate = (self._root / storage_key).resolve()
        if self._root not in candidate.parents:
            raise ArtifactIntegrityError("artifact storage key escaped the configured root")
        return candidate

    async def put_bytes(
        self,
        *,
        project_id: UUID,
        attempt_id: UUID,
        basis_hash: str,
        kind: str,
        content: bytes,
        media_type: str,
        source_url: str | None = None,
    ) -> ArtifactMetadata:
        if await self._session.get(ProjectRow, project_id) is None:
            raise ArtifactNotFoundError("project not found")

        content_hash = sha256(content).hexdigest()
        existing = await self._session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.project_id == project_id,
                ArtifactRow.attempt_id == attempt_id,
                ArtifactRow.kind == kind,
                ArtifactRow.content_hash == content_hash,
            )
        )
        if existing is not None:
            stored = self._path(existing.storage_key)
            if not stored.is_file():
                existing.status = ArtifactStatus.MISSING.value
                await self._session.commit()
                raise ArtifactIntegrityError("artifact metadata exists but bytes are missing")
            if sha256(stored.read_bytes()).hexdigest() != existing.content_hash:
                existing.status = ArtifactStatus.CORRUPT.value
                await self._session.commit()
                raise ArtifactIntegrityError("artifact bytes do not match metadata")
            await self._session.commit()
            return _metadata(existing)

        storage_key = self._storage_key(content_hash)
        target = self._path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        row = ArtifactRow(
            id=uuid4(),
            project_id=project_id,
            attempt_id=attempt_id,
            basis_hash=basis_hash,
            kind=kind,
            content_hash=content_hash,
            size_bytes=len(content),
            media_type=media_type,
            storage_key=storage_key,
            status=ArtifactStatus.PRESENT.value,
            source_url=source_url,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return _metadata(row)

    async def put_json(
        self,
        *,
        project_id: UUID,
        attempt_id: UUID,
        basis_hash: str,
        kind: str,
        value: object,
        source_url: str | None = None,
    ) -> ArtifactMetadata:
        content = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return await self.put_bytes(
            project_id=project_id,
            attempt_id=attempt_id,
            basis_hash=basis_hash,
            kind=kind,
            content=content,
            media_type="application/json",
            source_url=source_url,
        )

    @staticmethod
    def parse_ref(ref: str) -> tuple[str, UUID]:
        matched = re.fullmatch(
            r"artifact\+sha256://([a-f0-9]{64})/([0-9a-fA-F-]{36})",
            ref,
        )
        if matched is None:
            raise ArtifactIntegrityError("invalid artifact reference")
        return matched.group(1), UUID(matched.group(2))

    async def read_json_ref(
        self,
        *,
        project_id: UUID,
        basis_hash: str,
        ref: str,
    ) -> object:
        expected_hash, artifact_id = self.parse_ref(ref)
        metadata = await self.get_metadata(project_id=project_id, artifact_id=artifact_id)
        if metadata.content_hash != expected_hash or metadata.basis_hash != basis_hash:
            raise ArtifactIntegrityError("artifact reference does not match the frozen basis")
        if metadata.media_type != "application/json":
            raise ArtifactIntegrityError("artifact is not JSON")
        content = await self.read_bytes(project_id=project_id, artifact_id=artifact_id)
        return json.loads(content)

    async def get_metadata(self, *, project_id: UUID, artifact_id: UUID) -> ArtifactMetadata:
        row = await self._session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.id == artifact_id,
                ArtifactRow.project_id == project_id,
            )
        )
        if row is None:
            raise ArtifactNotFoundError("artifact not found")
        return _metadata(row)

    async def list_metadata(
        self,
        *,
        project_id: UUID,
        attempt_id: UUID,
        kind: str | None = None,
    ) -> tuple[ArtifactMetadata, ...]:
        statement = select(ArtifactRow).where(
            ArtifactRow.project_id == project_id,
            ArtifactRow.attempt_id == attempt_id,
        )
        if kind is not None:
            statement = statement.where(ArtifactRow.kind == kind)
        rows = await self._session.scalars(
            statement.order_by(ArtifactRow.created_at, ArtifactRow.id)
        )
        return tuple(_metadata(row) for row in rows)

    async def read_bytes(self, *, project_id: UUID, artifact_id: UUID) -> bytes:
        row = await self._session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.id == artifact_id,
                ArtifactRow.project_id == project_id,
            )
        )
        if row is None:
            raise ArtifactNotFoundError("artifact not found")
        if row.status != ArtifactStatus.PRESENT.value:
            raise ArtifactIntegrityError(f"artifact is {row.status}")

        target = self._path(row.storage_key)
        if not target.is_file():
            row.status = ArtifactStatus.MISSING.value
            await self._session.commit()
            raise ArtifactIntegrityError("artifact bytes are missing")
        content = target.read_bytes()
        if len(content) != row.size_bytes or sha256(content).hexdigest() != row.content_hash:
            row.status = ArtifactStatus.CORRUPT.value
            await self._session.commit()
            raise ArtifactIntegrityError("artifact bytes failed integrity verification")
        return content

    async def quarantine(self, *, project_id: UUID, artifact_id: UUID) -> ArtifactMetadata:
        row = await self._session.scalar(
            select(ArtifactRow)
            .where(
                ArtifactRow.id == artifact_id,
                ArtifactRow.project_id == project_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ArtifactNotFoundError("artifact not found")
        row.status = ArtifactStatus.QUARANTINED.value
        await self._session.commit()
        return _metadata(row)
