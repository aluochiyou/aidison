from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from aidison.application.service import ProjectApplication
from aidison.artifacts.contracts import ArtifactStatus
from aidison.infrastructure.artifacts import (
    ArtifactIntegrityError,
    ContentAddressedArtifactStore,
)
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.runtime import PostgresRuntime
from aidison.infrastructure.store import PostgresDomainStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_content_addressed_artifact_dedupes_and_detects_corruption(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE artifacts, jobs CASCADE"))
            await session.commit()
            project = await ProjectApplication(PostgresDomainStore(session)).create_project(
                name="Artifact fixture",
                goal="Verify immutable proposal storage",
                idempotency_key=f"artifact-project-{uuid4()}",
            )
            basis_hash = sha256(b"artifact-basis").hexdigest()
            runtime = PostgresRuntime(session)
            job_id = await runtime.create_job(
                project_id=project.id,
                kind="artifact_fixture",
                basis_hash=basis_hash,
                basis_project_revision=project.revision,
                profile_id="research-orchestrator",
                profile_revision=1,
            )
            claim = await runtime.claim_next_job(worker_id="artifact-test")
            assert claim is not None and claim.job_id == job_id

            store = ContentAddressedArtifactStore(session, tmp_path)
            payload = {"answer": "typed proposal", "version": 1}
            first = await store.put_json(
                project_id=project.id,
                attempt_id=claim.attempt_id,
                basis_hash=basis_hash,
                kind="research_proposal",
                value=payload,
            )
            duplicate = await store.put_json(
                project_id=project.id,
                attempt_id=claim.attempt_id,
                basis_hash=basis_hash,
                kind="research_proposal",
                value=payload,
            )
            assert duplicate == first
            assert first.status is ArtifactStatus.PRESENT
            assert await store.read_json_ref(
                project_id=project.id,
                basis_hash=basis_hash,
                ref=first.ref,
            ) == payload

            artifact_path = tmp_path / first.storage_key
            artifact_path.write_bytes(b"corrupt")
            with pytest.raises(ArtifactIntegrityError, match="integrity"):
                await store.read_bytes(project_id=project.id, artifact_id=first.id)
            changed = await store.get_metadata(project_id=project.id, artifact_id=first.id)
            assert changed.status is ArtifactStatus.CORRUPT
    finally:
        await engine.dispose()
