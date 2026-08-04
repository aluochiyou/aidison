from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.runtime.contracts import DelegationSpec, JoinMode, JoinPolicy


def basis() -> str:
    return sha256(b"basis").hexdigest()


def test_delegation_is_proposal_only() -> None:
    with pytest.raises(ValidationError, match="canonical"):
        DelegationSpec(
            parent_job_id=uuid4(),
            parent_attempt_id=uuid4(),
            parent_claim_generation=1,
            graph_step_id="research.wave.1",
            profile_id="research_worker_ro",
            profile_revision=1,
            basis_hash=basis(),
            shard_key="power",
            idempotency_key="parent:research.wave.1:power",
            token_budget=2_000,
            tool_call_budget=4,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
            canonical_write=True,
        )


def test_v0_join_all_required_requires_every_child() -> None:
    with pytest.raises(ValidationError, match="all_required"):
        JoinPolicy(
            mode=JoinMode.ALL_REQUIRED,
            expected_delegation_ids=(uuid4(), uuid4()),
            min_successes=1,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_v0_join_rejects_more_than_two_children() -> None:
    with pytest.raises(ValidationError):
        JoinPolicy(
            mode=JoinMode.BOUNDED_PARTIAL,
            expected_delegation_ids=(uuid4(), uuid4(), uuid4()),
            min_successes=1,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )
