"""Immutable user-provided project documents used as trusted research input.

These documents are intentionally not AgentRun artifacts and not project facts.
They are user-controlled source material: a Research run snapshots the exact
content it read before an LLM can use it, while formal Evidence Admission still
decides whether a quoted statement may support a project decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ProjectSourceDocumentStatus(StrEnum):
    ACTIVE = "active"
    MISSING = "missing"
    CORRUPT = "corrupt"
    QUARANTINED = "quarantined"


class ProjectSourceDocument(BaseModel):
    """Metadata for immutable textual material submitted for one project."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    name: str = Field(min_length=1, max_length=240)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1, le=384_000)
    media_type: str = Field(min_length=1, max_length=120)
    storage_key: str = Field(
        pattern=r"^project-sources/sha256/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{64}$"
    )
    status: ProjectSourceDocumentStatus = ProjectSourceDocumentStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
