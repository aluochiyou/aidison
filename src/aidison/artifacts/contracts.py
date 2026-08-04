from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ArtifactStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    CORRUPT = "corrupt"
    QUARANTINED = "quarantined"
    EXPIRED = "expired"


class ArtifactMetadata(BaseModel):
    """Project-scoped metadata for immutable content-addressed bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    attempt_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=200)
    storage_key: str = Field(pattern=r"^sha256/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{64}$")
    status: ArtifactStatus = ArtifactStatus.PRESENT
    source_url: str | None = Field(default=None, max_length=4_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def ref(self) -> str:
        return f"artifact+sha256://{self.content_hash}/{self.id}"
