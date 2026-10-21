"""Ref-only contracts for the deterministic first ImpactGraph vertical."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

from aidison.research.langgraph_contracts import AdmittedResultRef, merge_admitted_result_refs


class ImpactContract(BaseModel):
    """Frozen input boundary for one version-pinned Impact AgentRun."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "impact-contract-v1"
    impact_run_id: UUID
    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis_project_revision: int = Field(ge=1)
    base_solution_version_id: UUID
    base_solution_basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    change_set_ref: str = Field(min_length=1, max_length=500)
    coverage_contract_ref: str = Field(min_length=1, max_length=500)
    impact_policy_ref: str = Field(min_length=1, max_length=500)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def content_hash_matches(self) -> ImpactContract:
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = sha256(canonical.encode()).hexdigest()
        if self.content_hash is not None and self.content_hash != expected:
            raise ValueError("content_hash does not match ImpactContract content")
        object.__setattr__(self, "content_hash", expected)
        return self


class ImpactReportManifest(BaseModel):
    """Reviewable ImpactGraph output; it is not a Domain mutation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "impact-report-manifest-v1"
    impact_run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    impact_contract_ref: str = Field(min_length=1, max_length=500)
    change_set_ref: str = Field(min_length=1, max_length=500)
    structural_partition_ref: str = Field(min_length=1, max_length=500)
    change_plan_ref: str = Field(min_length=1, max_length=500)
    steering_preview_ref: str = Field(min_length=1, max_length=500)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def content_hash_matches(self) -> ImpactReportManifest:
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = sha256(canonical.encode()).hexdigest()
        if self.content_hash is not None and self.content_hash != expected:
            raise ValueError("content_hash does not match ImpactReportManifest content")
        object.__setattr__(self, "content_hash", expected)
        return self


class ImpactGraphState(TypedDict, total=False):
    """Shared checkpoint state contains only immutable output references."""

    run_id: str
    impact_contract_ref: str
    change_set_ref: str
    structural_partition_ref: str
    change_plan_ref: str
    steering_preview_ref: str
    report_manifest_ref: str
    admitted_result_refs: Annotated[tuple[AdmittedResultRef, ...], merge_admitted_result_refs]


__all__ = ["ImpactContract", "ImpactGraphState", "ImpactReportManifest"]
