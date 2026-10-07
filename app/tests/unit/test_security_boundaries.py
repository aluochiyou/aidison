from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.artifacts.access import (
    ArtifactAccessDenied,
    ArtifactAccessScope,
    ScopedArtifactReader,
)
from aidison.artifacts.contracts import ArtifactMetadata
from aidison.security.secrets import SecretExposureError, assert_secret_free, redact_secrets
from aidison.security.urls import PublicHttpsUrlPolicy, UnsafeUrlError


class _Resolver:
    def __init__(self, addresses: tuple[str, ...]) -> None:
        self.addresses = addresses

    async def resolve(self, *, hostname: str, port: int) -> tuple[str, ...]:
        assert hostname == "public.example"
        assert port == 443
        return self.addresses


class _Artifacts:
    def __init__(self, metadata: ArtifactMetadata, content: bytes) -> None:
        self.metadata = metadata
        self.content = content

    async def get_metadata(self, *, project_id, artifact_id):  # type: ignore[no-untyped-def]
        assert project_id == self.metadata.project_id
        assert artifact_id == self.metadata.id
        return self.metadata

    async def read_bytes(self, *, project_id, artifact_id):  # type: ignore[no-untyped-def]
        assert project_id == self.metadata.project_id
        assert artifact_id == self.metadata.id
        return self.content


@pytest.mark.asyncio
async def test_url_policy_rejects_any_private_dns_answer_and_canonicalizes_public_host() -> None:
    accepted = await PublicHttpsUrlPolicy(
        resolver=_Resolver(("8.8.8.8", "2001:4860:4860::8888"))
    ).validate("https://PUBLIC.EXAMPLE:443/path?q=1")

    assert accepted.normalized_url == "https://public.example/path?q=1"

    with pytest.raises(UnsafeUrlError, match="non-public"):
        await PublicHttpsUrlPolicy(resolver=_Resolver(("8.8.8.8", "127.0.0.1"))).validate(
            "https://public.example/path"
        )


def test_secret_boundary_redacts_diagnostics_and_refuses_context_material() -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    diagnostic = f"Authorization: Bearer {secret}"

    assert secret not in redact_secrets(diagnostic)
    with pytest.raises(SecretExposureError, match="secret-like material"):
        assert_secret_free(diagnostic, boundary="context")


@pytest.mark.asyncio
async def test_scoped_artifact_reader_requires_project_basis_kind_and_hash() -> None:
    content = b'{"safe":true}'
    content_hash = sha256(content).hexdigest()
    project_id = uuid4()
    agent_run_id = uuid4()
    metadata = ArtifactMetadata(
        project_id=project_id,
        agent_run_id=agent_run_id,
        basis_hash="a" * 64,
        kind="context_manifest",
        content_hash=content_hash,
        size_bytes=len(content),
        media_type="application/json",
        storage_key=f"sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}",
        created_at=datetime.now(UTC),
    )
    reader = ScopedArtifactReader(_Artifacts(metadata, content))
    scope = ArtifactAccessScope(
        project_id=project_id,
        basis_hash="a" * 64,
        agent_run_id=agent_run_id,
        allowed_kinds=("context_manifest",),
        max_bytes=1024,
    )

    assert await reader.read_bytes_ref(scope=scope, ref=metadata.ref) == content

    with pytest.raises(ArtifactAccessDenied, match="basis"):
        await reader.read_bytes_ref(
            scope=scope.model_copy(update={"basis_hash": "b" * 64}),
            ref=metadata.ref,
        )
    with pytest.raises(ArtifactAccessDenied, match="kind"):
        await reader.read_bytes_ref(
            scope=scope.model_copy(update={"allowed_kinds": ("raw_prompt",)}),
            ref=metadata.ref,
        )
