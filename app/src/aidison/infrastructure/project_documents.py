"""PostgreSQL metadata and content-addressed bytes for project source documents."""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import ProjectRow, ProjectSourceDocumentRow
from aidison.research.project_documents import (
    ProjectSourceDocument,
    ProjectSourceDocumentStatus,
)


class ProjectSourceDocumentNotFoundError(RuntimeError):
    pass


class ProjectSourceDocumentIntegrityError(RuntimeError):
    pass


def _document(row: ProjectSourceDocumentRow) -> ProjectSourceDocument:
    return ProjectSourceDocument(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        media_type=row.media_type,
        storage_key=row.storage_key,
        status=ProjectSourceDocumentStatus(row.status),
        created_at=row.created_at,
    )


class ProjectSourceDocumentStore:
    """Store user-controlled textual inputs separately from AgentRun Artifacts."""

    def __init__(self, session: AsyncSession, root: Path) -> None:
        self._session = session
        self._root = root.resolve()

    @staticmethod
    def _storage_key(content_hash: str) -> str:
        return f"project-sources/sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"

    def _path(self, storage_key: str) -> Path:
        candidate = (self._root / storage_key).resolve()
        if self._root not in candidate.parents:
            raise ProjectSourceDocumentIntegrityError(
                "project source document storage key escaped the configured root"
            )
        return candidate

    async def put_text(
        self,
        *,
        project_id: UUID,
        name: str,
        content: str,
        media_type: str,
        commit: bool = True,
    ) -> ProjectSourceDocument:
        encoded = content.encode("utf-8")
        if not encoded:
            raise ValueError("project source document content must not be empty")
        if len(encoded) > 384_000:
            raise ValueError("project source document content exceeds the byte limit")
        if await self._session.get(ProjectRow, project_id) is None:
            raise ProjectSourceDocumentNotFoundError("project not found")

        content_hash = sha256(encoded).hexdigest()
        existing = await self._session.scalar(
            select(ProjectSourceDocumentRow).where(
                ProjectSourceDocumentRow.project_id == project_id,
                ProjectSourceDocumentRow.content_hash == content_hash,
            )
        )
        if existing is not None:
            await self._verify_row_bytes(existing, expected=encoded)
            await self._flush_or_commit(commit=commit)
            return _document(existing)

        storage_key = self._storage_key(content_hash)
        target = self._path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        document_id = uuid4()
        inserted_id = await self._session.scalar(
            insert(ProjectSourceDocumentRow)
            .values(
                id=document_id,
                project_id=project_id,
                name=name,
                content_hash=content_hash,
                size_bytes=len(encoded),
                media_type=media_type,
                storage_key=storage_key,
                status=ProjectSourceDocumentStatus.ACTIVE.value,
            )
            .on_conflict_do_nothing(index_elements=("project_id", "content_hash"))
            .returning(ProjectSourceDocumentRow.id)
        )
        await self._flush_or_commit(commit=commit)
        if inserted_id is None:
            row = await self._session.scalar(
                select(ProjectSourceDocumentRow).where(
                    ProjectSourceDocumentRow.project_id == project_id,
                    ProjectSourceDocumentRow.content_hash == content_hash,
                )
            )
            if row is None:  # pragma: no cover - PostgreSQL conflict is visible after flush
                raise ProjectSourceDocumentIntegrityError(
                    "document conflict did not resolve to canonical metadata"
                )
        else:
            row = await self._session.get(ProjectSourceDocumentRow, inserted_id)
            if row is None:  # pragma: no cover - inserted row must be readable in-session
                raise ProjectSourceDocumentIntegrityError(
                    "inserted project source document is not readable"
                )
        return _document(row)

    async def get(
        self, *, project_id: UUID, document_id: UUID
    ) -> ProjectSourceDocument:
        row = await self._row(project_id=project_id, document_id=document_id)
        return _document(row)

    async def list_active(
        self, *, project_id: UUID, limit: int = 32
    ) -> tuple[ProjectSourceDocument, ...]:
        if not 1 <= limit <= 64:
            raise ValueError("project source document limit must be between 1 and 64")
        rows = await self._session.scalars(
            select(ProjectSourceDocumentRow)
            .where(
                ProjectSourceDocumentRow.project_id == project_id,
                ProjectSourceDocumentRow.status == ProjectSourceDocumentStatus.ACTIVE.value,
            )
            .order_by(ProjectSourceDocumentRow.created_at, ProjectSourceDocumentRow.id)
            .limit(limit)
        )
        return tuple(_document(row) for row in rows)

    async def list_all(
        self, *, project_id: UUID, limit: int = 64
    ) -> tuple[ProjectSourceDocument, ...]:
        if not 1 <= limit <= 128:
            raise ValueError("project source document limit must be between 1 and 128")
        rows = await self._session.scalars(
            select(ProjectSourceDocumentRow)
            .where(ProjectSourceDocumentRow.project_id == project_id)
            .order_by(ProjectSourceDocumentRow.created_at, ProjectSourceDocumentRow.id)
            .limit(limit)
        )
        return tuple(_document(row) for row in rows)

    async def quarantine(
        self,
        *,
        project_id: UUID,
        document_id: UUID,
        commit: bool = True,
    ) -> tuple[ProjectSourceDocument, bool]:
        """Stop future research reads without deleting auditable source bytes."""

        row = await self._session.scalar(
            select(ProjectSourceDocumentRow)
            .where(
                ProjectSourceDocumentRow.id == document_id,
                ProjectSourceDocumentRow.project_id == project_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ProjectSourceDocumentNotFoundError("project source document not found")
        changed = row.status != ProjectSourceDocumentStatus.QUARANTINED.value
        if changed:
            row.status = ProjectSourceDocumentStatus.QUARANTINED.value
        await self._flush_or_commit(commit=commit)
        return _document(row), changed

    async def restore(
        self,
        *,
        project_id: UUID,
        document_id: UUID,
        commit: bool = True,
    ) -> tuple[ProjectSourceDocument, bool]:
        """Re-enable a quarantined document only after rechecking its bytes."""

        row = await self._session.scalar(
            select(ProjectSourceDocumentRow)
            .where(
                ProjectSourceDocumentRow.id == document_id,
                ProjectSourceDocumentRow.project_id == project_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ProjectSourceDocumentNotFoundError("project source document not found")
        if row.status == ProjectSourceDocumentStatus.ACTIVE.value:
            await self._flush_or_commit(commit=commit)
            return _document(row), False
        if row.status != ProjectSourceDocumentStatus.QUARANTINED.value:
            raise ProjectSourceDocumentIntegrityError(
                f"project source document cannot be restored from {row.status}"
            )
        await self._read_verified_bytes(row)
        row.status = ProjectSourceDocumentStatus.ACTIVE.value
        await self._flush_or_commit(commit=commit)
        return _document(row), True

    async def read_text(
        self,
        *,
        project_id: UUID,
        document_id: UUID,
        require_active: bool = True,
    ) -> tuple[ProjectSourceDocument, str]:
        row = await self._row(project_id=project_id, document_id=document_id)
        if require_active and row.status != ProjectSourceDocumentStatus.ACTIVE.value:
            raise ProjectSourceDocumentIntegrityError(
                f"project source document is {row.status}"
            )
        if row.status in {
            ProjectSourceDocumentStatus.MISSING.value,
            ProjectSourceDocumentStatus.CORRUPT.value,
        }:
            raise ProjectSourceDocumentIntegrityError(
                f"project source document is {row.status}"
            )
        content = await self._read_verified_bytes(row)
        try:
            return _document(row), content.decode("utf-8")
        except UnicodeDecodeError as error:  # pragma: no cover - upload boundary guarantees UTF-8
            row.status = ProjectSourceDocumentStatus.CORRUPT.value
            await self._session.flush()
            raise ProjectSourceDocumentIntegrityError(
                "project source document is not UTF-8 text"
            ) from error

    async def _row(self, *, project_id: UUID, document_id: UUID) -> ProjectSourceDocumentRow:
        row = await self._session.scalar(
            select(ProjectSourceDocumentRow).where(
                ProjectSourceDocumentRow.id == document_id,
                ProjectSourceDocumentRow.project_id == project_id,
            )
        )
        if row is None:
            raise ProjectSourceDocumentNotFoundError("project source document not found")
        return row

    async def _verify_row_bytes(self, row: ProjectSourceDocumentRow, *, expected: bytes) -> None:
        content = await self._read_verified_bytes(row)
        if content != expected:
            raise ProjectSourceDocumentIntegrityError(
                "existing document bytes differ from the submitted content"
            )

    async def _read_verified_bytes(self, row: ProjectSourceDocumentRow) -> bytes:
        target = self._path(row.storage_key)
        if not target.is_file():
            row.status = ProjectSourceDocumentStatus.MISSING.value
            await self._session.flush()
            raise ProjectSourceDocumentIntegrityError("project source document bytes are missing")
        content = target.read_bytes()
        if len(content) != row.size_bytes or sha256(content).hexdigest() != row.content_hash:
            row.status = ProjectSourceDocumentStatus.CORRUPT.value
            await self._session.flush()
            raise ProjectSourceDocumentIntegrityError(
                "project source document bytes failed integrity verification"
            )
        return content

    async def _flush_or_commit(self, *, commit: bool) -> None:
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
