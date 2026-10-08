from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.research.source_collection import (
    ProjectDocumentResearchSourceCollector,
    ProjectDocumentSourceCollectionError,
)
from aidison.research.source_observations import SourceKind

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_project_document_upload_is_idempotent_and_becomes_research_input(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    api = create_app(factory, artifact_root=tmp_path)
    transport = httpx.ASGITransport(app=api)
    prefix = uuid4().hex
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Source fixture", "goal": "Choose a safe power module"},
                headers={"Idempotency-Key": f"{prefix}:project"},
            )
            assert created.status_code == 201
            project_id = created.json()["id"]

            body = {
                "name": "battery-notes.md",
                "content": "Battery A supports 6S. Verify current margin before selection.",
                "media_type": "text/markdown",
            }
            uploaded = await client.post(
                f"/api/projects/{project_id}/source-documents",
                json=body,
                headers={"Idempotency-Key": f"{prefix}:document"},
            )
            assert uploaded.status_code == 201
            document = uploaded.json()
            assert document["status"] == "active"
            assert document["name"] == body["name"]
            assert uploaded.headers["etag"] == created.headers["etag"]

            replayed = await client.post(
                f"/api/projects/{project_id}/source-documents",
                json=body,
                headers={"Idempotency-Key": f"{prefix}:document"},
            )
            assert replayed.status_code == 201
            assert replayed.json()["id"] == document["id"]

            listed = await client.get(f"/api/projects/{project_id}/source-documents")
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()] == [document["id"]]

            content = await client.get(
                f"/api/projects/{project_id}/source-documents/{document['id']}/content"
            )
            assert content.status_code == 200
            assert content.text == body["content"]
            assert content.headers["x-content-sha256"] == document["content_hash"]

        collector = ProjectDocumentResearchSourceCollector(
            session_factory=factory,
            artifact_root=tmp_path,
        )
        sources = await collector.collect(
            run=SimpleNamespace(project_id=project_id),  # type: ignore[arg-type]
            task=SimpleNamespace(),  # type: ignore[arg-type]
            question="Which battery has compatible voltage?",
        )
        assert len(sources) == 1
        assert sources[0].source.kind is SourceKind.USER_UPLOAD
        assert sources[0].normalized_document == body["content"]
        assert document["content_hash"] in sources[0].source.canonical_locator

        (tmp_path / document["storage_key"]).write_text("corrupted", encoding="utf-8")
        with pytest.raises(
            ProjectDocumentSourceCollectionError,
            match="project_document_unavailable",
        ):
            await collector.collect(
                run=SimpleNamespace(project_id=project_id),  # type: ignore[arg-type]
                task=SimpleNamespace(),  # type: ignore[arg-type]
                question="Retry after a storage failure",
            )
    finally:
        await engine.dispose()
