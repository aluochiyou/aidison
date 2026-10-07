from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.impact.analyst import (
    ImpactPatchProposalPayload,
    validate_impact_patch_scope,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _payload(*, module_id: str, candidate_id: str, snapshot_hash: str) -> dict[str, object]:
    return {
        "summary": "Replace the affected source candidate and re-verify its consumer.",
        "module_patches": [
            {
                "module_id": module_id,
                "base_snapshot_hash": snapshot_hash,
                "candidate_id": candidate_id,
                "candidate_name": "Replacement source",
                "rationale": "The observation invalidates the old source output.",
                "evidence_binding_ids": [],
                "risks": ["Consumer compatibility must be re-verified."],
            }
        ],
        "replacement_bom_items": [],
        "replacement_implementation_steps": [
            {
                "step_id": "implement-source",
                "title": "Replace source",
                "instruction": "Install the replacement source.",
                "module_ids": [module_id],
                "acceptance": ["Source output is available."],
            }
        ],
        "replacement_verification_steps": [
            {
                "step_id": "verify-source",
                "title": "Verify source",
                "instruction": "Verify source and dependent consumer compatibility.",
                "module_ids": [module_id],
                "acceptance": ["Interface is verified."],
            }
        ],
        "stale_evidence_binding_ids": [],
        "risks": ["Consumer compatibility must be re-verified."],
        "unknowns": [],
    }


def test_impact_patch_proposal_accepts_only_the_frozen_affected_frontier() -> None:
    module_id, candidate_id = uuid4(), uuid4()
    snapshot_hash = _hash("source-snapshot")
    payload = ImpactPatchProposalPayload.model_validate(
        _payload(
            module_id=str(module_id),
            candidate_id=str(candidate_id),
            snapshot_hash=snapshot_hash,
        )
    )

    accepted = validate_impact_patch_scope(
        payload=payload,
        affected_module_ids=(module_id,),
        snapshots_by_module_id={module_id: snapshot_hash},
    )

    assert accepted == payload


def test_impact_patch_proposal_rejects_an_unaffected_module_or_stale_snapshot() -> None:
    module_id, candidate_id, unrelated_module_id = uuid4(), uuid4(), uuid4()
    snapshot_hash = _hash("source-snapshot")
    payload = ImpactPatchProposalPayload.model_validate(
        _payload(
            module_id=str(module_id),
            candidate_id=str(candidate_id),
            snapshot_hash=snapshot_hash,
        )
    )

    with pytest.raises(ValueError, match="unaffected module"):
        validate_impact_patch_scope(
            payload=payload,
            affected_module_ids=(unrelated_module_id,),
            snapshots_by_module_id={module_id: snapshot_hash},
        )
    with pytest.raises(ValueError, match="stale base snapshot"):
        validate_impact_patch_scope(
            payload=payload,
            affected_module_ids=(module_id,),
            snapshots_by_module_id={module_id: _hash("newer-source-snapshot")},
        )


def test_impact_patch_proposal_rejects_duplicate_stable_step_identity() -> None:
    module_id, candidate_id = uuid4(), uuid4()
    raw = _payload(
        module_id=str(module_id),
        candidate_id=str(candidate_id),
        snapshot_hash=_hash("source-snapshot"),
    )
    raw["replacement_verification_steps"] = [
        raw["replacement_verification_steps"][0],
        raw["replacement_verification_steps"][0],
    ]

    with pytest.raises(ValidationError, match="replacement_verification_steps must be unique"):
        ImpactPatchProposalPayload.model_validate(raw)
