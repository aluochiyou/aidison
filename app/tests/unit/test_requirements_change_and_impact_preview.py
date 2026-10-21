"""Domain invariants for requirements-change proposals and change impact previews."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aidison.domain.models import (
    ChangeImpactPreview,
    ChangeImpactPreviewStatus,
    ImpactedPendingRef,
    ProposedModule,
    RequirementsChangeProposal,
    RequirementsChangeProposalStatus,
)


def _module(key: str) -> ProposedModule:
    return ProposedModule(key=key, name=key.title(), responsibility=f"Own {key}")


class TestRequirementsChangeProposal:
    def test_default_status_is_proposed(self) -> None:
        p = RequirementsChangeProposal(
            project_id=uuid4(),
            target_goal="New goal",
            summary="Rewrite",
            modules=(_module("core"),),
        )
        assert p.status is RequirementsChangeProposalStatus.PROPOSED
        assert p.resolved_at is None

    def test_applied_requires_resolved_at(self) -> None:
        with pytest.raises(ValueError, match="requires resolved_at"):
            RequirementsChangeProposal(
                project_id=uuid4(),
                target_goal="New goal",
                summary="Rewrite",
                modules=(_module("core"),),
                status=RequirementsChangeProposalStatus.APPLIED,
            )

    def test_proposed_cannot_have_resolved_at(self) -> None:
        with pytest.raises(ValueError, match="cannot have resolved_at"):
            RequirementsChangeProposal(
                project_id=uuid4(),
                target_goal="New goal",
                summary="Rewrite",
                modules=(_module("core"),),
                status=RequirementsChangeProposalStatus.PROPOSED,
                resolved_at=datetime.now(UTC),
            )

    def test_applied_with_timestamp_is_valid(self) -> None:
        p = RequirementsChangeProposal(
            project_id=uuid4(),
            target_goal="New goal",
            summary="Rewrite",
            modules=(_module("core"),),
            status=RequirementsChangeProposalStatus.APPLIED,
            resolved_at=datetime.now(UTC),
        )
        assert p.status is RequirementsChangeProposalStatus.APPLIED

    def test_module_keys_must_be_unique(self) -> None:
        with pytest.raises(ValueError, match="module keys must be unique"):
            RequirementsChangeProposal(
                project_id=uuid4(),
                target_goal="New goal",
                summary="Rewrite",
                modules=(_module("core"), _module("core")),
            )

    def test_proposal_is_frozen(self) -> None:
        p = RequirementsChangeProposal(
            project_id=uuid4(),
            target_goal="New goal",
            summary="Rewrite",
            modules=(_module("core"),),
        )
        with pytest.raises(ValueError):
            p.target_goal = "mutated"  # type: ignore[misc]


class TestChangeImpactPreview:
    def test_default_status_is_proposed(self) -> None:
        module_id = uuid4()
        preview = ChangeImpactPreview(
            project_id=uuid4(),
            batch_id=uuid4(),
            adjustment_ids=(uuid4(),),
            basis_project_revision=3,
            basis_hash="a" * 64,
            direct_affected_module_ids=(module_id,),
            transitive_affected_module_ids=(),
            affected_module_ids=(module_id,),
            unaffected_module_ids=(),
            summary="Impact preview",
        )
        assert preview.status is ChangeImpactPreviewStatus.PROPOSED

    def test_direct_and_transitive_must_be_disjoint(self) -> None:
        module_id = uuid4()
        with pytest.raises(ValueError, match="must be disjoint"):
            ChangeImpactPreview(
                project_id=uuid4(),
                batch_id=uuid4(),
                adjustment_ids=(uuid4(),),
                basis_project_revision=3,
                basis_hash="a" * 64,
                direct_affected_module_ids=(module_id,),
                transitive_affected_module_ids=(module_id,),
                affected_module_ids=(module_id,),
                unaffected_module_ids=(),
                summary="Impact preview",
            )

    def test_direct_union_transitive_must_equal_affected(self) -> None:
        direct = uuid4()
        transitive = uuid4()
        with pytest.raises(ValueError, match="must equal affected modules"):
            ChangeImpactPreview(
                project_id=uuid4(),
                batch_id=uuid4(),
                adjustment_ids=(uuid4(),),
                basis_project_revision=3,
                basis_hash="a" * 64,
                direct_affected_module_ids=(direct,),
                transitive_affected_module_ids=(transitive,),
                affected_module_ids=(direct,),
                unaffected_module_ids=(),
                summary="Impact preview",
            )

    def test_affected_and_unaffected_must_be_disjoint(self) -> None:
        affected = uuid4()
        with pytest.raises(ValueError, match="must be disjoint"):
            ChangeImpactPreview(
                project_id=uuid4(),
                batch_id=uuid4(),
                adjustment_ids=(uuid4(),),
                basis_project_revision=3,
                basis_hash="a" * 64,
                direct_affected_module_ids=(affected,),
                transitive_affected_module_ids=(),
                affected_module_ids=(affected,),
                unaffected_module_ids=(affected,),
                summary="Impact preview",
            )

    def test_adjustment_ids_must_be_unique(self) -> None:
        adjustment_id = uuid4()
        affected_id = uuid4()
        with pytest.raises(ValueError, match="must be unique"):
            ChangeImpactPreview(
                project_id=uuid4(),
                batch_id=uuid4(),
                adjustment_ids=(adjustment_id, adjustment_id),
                basis_project_revision=3,
                basis_hash="a" * 64,
                direct_affected_module_ids=(affected_id,),
                transitive_affected_module_ids=(),
                affected_module_ids=(affected_id,),
                unaffected_module_ids=(),
                summary="Impact preview",
            )

    def test_invalidated_ref_requires_structured_fields(self) -> None:
        ref = ImpactedPendingRef(
            kind="decision",
            entity_id=uuid4(),
            summary="pending decision",
            reason="basis stale",
        )
        assert ref.kind == "decision"
