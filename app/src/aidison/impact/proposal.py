"""Immutable user-review manifest for a bounded Impact Analyst candidate."""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ImpactProposalReadiness(StrEnum):
    """A proposal is reviewable only after deterministic scope admission."""

    READY = "ready"


class ImpactProposalManifest(BaseModel):
    """Frozen review input; it cannot itself mutate ImpactAnalysis or SolutionVersion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "impact-proposal-manifest-v1"
    run_id: UUID
    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    impact_contract_ref: str = Field(min_length=1, max_length=500)
    impact_report_manifest_ref: str = Field(min_length=1, max_length=500)
    impact_report_manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_output_ref: str = Field(min_length=1, max_length=500)
    raw_output_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    patch_candidate_ref: str = Field(min_length=1, max_length=500)
    patch_candidate_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    readiness: ImpactProposalReadiness = ImpactProposalReadiness.READY
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def content_hash_matches(self) -> ImpactProposalManifest:
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = sha256(canonical.encode()).hexdigest()
        if self.content_hash is not None and self.content_hash != expected:
            raise ValueError("content_hash does not match ImpactProposalManifest content")
        object.__setattr__(self, "content_hash", expected)
        return self

    @property
    def can_create_patch(self) -> bool:
        return self.readiness is ImpactProposalReadiness.READY


__all__ = ["ImpactProposalManifest", "ImpactProposalReadiness"]
