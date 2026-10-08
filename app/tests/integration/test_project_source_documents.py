from __future__ import annotations

import asyncio
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
async def test_concurrent_project_document_uploads_converge_to_one_content_record(
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
    body = {
        "name": "shared-battery-spec.md",
        "content": "Battery A supports 6S and a continuous 40A discharge current.",
        "media_type": "text/markdown",
    }
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/projects",
                json={"name": "Concurrent sources", "goal": "Choose a battery"},
                headers={"Idempotency-Key": f"{prefix}:project"},
            )
            assert created.status_code == 201
            project_id = created.json()["id"]

            async def upload(index: int) -> httpx.Response:
                return await client.post(
                    f"/api/projects/{project_id}/source-documents",
                    json=body,
                    headers={"Idempotency-Key": f"{prefix}:document:{index}"},
                )

            uploads = await asyncio.gather(*(upload(index) for index in range(6)))
            assert all(response.status_code == 201 for response in uploads)
            assert len({response.json()["id"] for response in uploads}) == 1
            listed = await client.get(f"/api/projects/{project_id}/source-documents")
            assert listed.status_code == 200
            assert len(listed.json()) == 1
            assert listed.json()[0]["content_hash"] == uploads[0].json()["content_hash"]
    finally:
        await engine.dispose()


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

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            second = await client.post(
                f"/api/projects/{project_id}/source-documents",
                json={
                    "name": "safety-notes.txt",
                    "content": "Keep a current margin before choosing a battery.",
                    "media_type": "text/plain",
                },
                headers={"Idempotency-Key": f"{prefix}:second-document"},
            )
            assert second.status_code == 201
            safe_document = second.json()
            quarantined = await client.post(
                f"/api/projects/{project_id}/source-documents/{safe_document['id']}/quarantine",
                headers={"Idempotency-Key": f"{prefix}:quarantine"},
            )
            assert quarantined.status_code == 200
            assert quarantined.json()["status"] == "quarantined"

            replayed_quarantine = await client.post(
                f"/api/projects/{project_id}/source-documents/{safe_document['id']}/quarantine",
                headers={"Idempotency-Key": f"{prefix}:quarantine"},
            )
            assert replayed_quarantine.status_code == 200
            assert replayed_quarantine.json()["id"] == safe_document["id"]

            inspected = await client.get(
                f"/api/projects/{project_id}/source-documents/{safe_document['id']}/content"
            )
            assert inspected.status_code == 200
            assert inspected.text == "Keep a current margin before choosing a battery."

            listed_after = await client.get(f"/api/projects/{project_id}/source-documents")
            assert listed_after.status_code == 200
            assert {item["status"] for item in listed_after.json()} == {
                "corrupt",
                "quarantined",
            }

        assert await collector.collect(
            run=SimpleNamespace(project_id=project_id),  # type: ignore[arg-type]
            task=SimpleNamespace(),  # type: ignore[arg-type]
            question="No active user document should remain",
        ) == ()

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            restored = await client.post(
                f"/api/projects/{project_id}/source-documents/{safe_document['id']}/restore",
                headers={"Idempotency-Key": f"{prefix}:restore"},
            )
            assert restored.status_code == 200
            assert restored.json()["status"] == "active"
            replayed_restore = await client.post(
                f"/api/projects/{project_id}/source-documents/{safe_document['id']}/restore",
                headers={"Idempotency-Key": f"{prefix}:restore"},
            )
            assert replayed_restore.status_code == 200
            assert replayed_restore.json()["id"] == safe_document["id"]
            unrelated = await client.post(
                f"/api/projects/{project_id}/source-documents",
                json={
                    "name": "motor-notes.txt",
                    "content": "Motor A requires a 35A peak current at 12V.",
                    "media_type": "text/plain",
                },
                headers={"Idempotency-Key": f"{prefix}:third-document"},
            )
            assert unrelated.status_code == 201

        restored_sources = await collector.collect(
            run=SimpleNamespace(project_id=project_id),  # type: ignore[arg-type]
            task=SimpleNamespace(),  # type: ignore[arg-type]
            question="Which source explains the current margin?",
        )
        assert len(restored_sources) == 2
        assert restored_sources[0].normalized_document == (
            "Keep a current margin before choosing a battery."
        )
        bounded_sources = await collector.collect(
            run=SimpleNamespace(project_id=project_id),  # type: ignore[arg-type]
            task=SimpleNamespace(
                collection_policy=SimpleNamespace(max_documents_total=1)
            ),  # type: ignore[arg-type]
            question="Which source explains the current margin?",
        )
        assert [source.normalized_document for source in bounded_sources] == [
            "Keep a current margin before choosing a battery."
        ]
    finally:
        await engine.dispose()
