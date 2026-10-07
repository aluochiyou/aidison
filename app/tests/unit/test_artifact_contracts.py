from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from aidison.artifacts.contracts import ArtifactMetadata


def test_artifact_metadata_requires_producing_agent_run_lineage() -> None:
    project_id = uuid4()
    agent_run_id = uuid4()

    artifact = ArtifactMetadata(
        project_id=project_id,
        agent_run_id=agent_run_id,
        basis_hash=sha256(b"artifact-basis").hexdigest(),
        kind="research_proposal",
        content_hash=sha256(b"artifact-content").hexdigest(),
        size_bytes=16,
        media_type="application/json",
        storage_key=f"sha256/ab/cd/{sha256(b'artifact-content').hexdigest()}",
        created_at=datetime.now(UTC),
    )

    assert artifact.project_id == project_id
    assert artifact.agent_run_id == agent_run_id
