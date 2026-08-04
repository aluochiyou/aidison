from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from langchain_mcp_adapters.interceptors import MCPToolCallRequest

from aidison.artifacts.contracts import ArtifactMetadata
from aidison.runtime.contracts import JobClaim
from aidison.tools.github import (
    ControlledGitHubRead,
    GitHubMcpBackend,
    GitHubMcpSession,
    GitHubMcpSettings,
    GitHubTool,
    GitHubUnavailableError,
    enforce_github_mcp_call,
)
from aidison.tools.web_search import SearchContext


class FakeTool:
    def __init__(self, name: str, result: object, *, read_only: bool = True) -> None:
        self.name = name
        self.metadata = {"readOnlyHint": read_only}
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, input: dict[str, Any]) -> object:
        self.calls.append(input)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeArtifacts:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def put_bytes(self, **kwargs: Any) -> ArtifactMetadata:
        self.writes.append(kwargs)
        content = cast(bytes, kwargs["content"])
        return ArtifactMetadata(
            project_id=kwargs["project_id"],
            attempt_id=kwargs["attempt_id"],
            basis_hash=kwargs["basis_hash"],
            kind=kwargs["kind"],
            content_hash=sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type=kwargs["media_type"],
            storage_key=f"sha256/{sha256(content).hexdigest()[:2]}/"
            f"{sha256(content).hexdigest()[2:4]}/{sha256(content).hexdigest()}",
            source_url=kwargs.get("source_url"),
        )


class FakeBudget:
    def __init__(self, *, dispatch_error: Exception | None = None) -> None:
        self.operation_id = uuid4()
        self.events: list[str] = []
        self.dispatch_error = dispatch_error

    async def reserve(self, **kwargs: Any) -> UUID:
        assert kwargs["tool_name"] in {
            "search_repositories",
            "search_code",
            "get_file_contents",
        }
        self.events.append("reserved")
        return self.operation_id

    async def mark_dispatched(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("dispatched")
        if self.dispatch_error is not None:
            raise self.dispatch_error

    async def settle(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("settled")

    async def release_undispatched(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("released")

    async def mark_ambiguous(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("ambiguous")


def make_context(*, effects: tuple[str, ...] = ("discovery", "read")) -> SearchContext:
    basis_hash = sha256(b"github-test").hexdigest()
    claim = JobClaim(
        job_id=uuid4(),
        attempt_id=uuid4(),
        attempt_number=1,
        claim_generation=1,
        lease_token=uuid4(),
        lease_owner="unit-test",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        basis_hash=basis_hash,
        basis_project_revision=1,
        profile_id="research-worker-ro",
        profile_revision=4,
    )
    return SearchContext(
        project_id=uuid4(),
        claim=claim,
        budget_allocation_id=uuid4(),
        basis_hash=basis_hash,
        allowed_effects=effects,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


def test_connection_uses_only_the_fixed_stdio_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_API_KEY", "top-secret")
    settings = GitHubMcpSettings(_env_file=None)

    connection = settings.connection()

    assert connection["command"] == "/server/github-mcp-server"
    assert connection["args"] == ["stdio"]
    assert connection["env"] == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "top-secret",
        "GITHUB_READ_ONLY": "1",
        "GITHUB_TOOLS": "get_file_contents,search_code,search_repositories",
    }
    assert "GITHUB_TOOLSETS" not in connection["env"]
    assert "top-secret" not in repr(settings)


def test_connection_fails_closed_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_API_KEY", raising=False)
    settings = GitHubMcpSettings(_env_file=None)

    with pytest.raises(GitHubUnavailableError, match="GITHUB_API_KEY"):
        settings.connection()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_request",
    [
        MCPToolCallRequest(
            server_name="other",
            name="search_repositories",
            args={"query": "drone", "page": 1, "perPage": 1, "minimal_output": True},
        ),
        MCPToolCallRequest(
            server_name="github",
            name="search_repositories",
            args={"query": "drone", "page": 2, "perPage": 1, "minimal_output": True},
        ),
        MCPToolCallRequest(
            server_name="github",
            name="search_code",
            args={"query": "flight controller", "page": 1, "perPage": 1},
        ),
        MCPToolCallRequest(
            server_name="github",
            name="get_file_contents",
            args={"owner": "o", "repo": "r", "path": "../secret", "ref": "main"},
        ),
        MCPToolCallRequest(
            server_name="github",
            name="get_file_contents",
            args={"owner": "o", "repo": "r", "path": "README.md", "extra": True},
        ),
    ],
)
async def test_interceptor_rejects_out_of_contract_calls(
    call_request: MCPToolCallRequest,
) -> None:
    handler = AsyncMock()

    with pytest.raises(GitHubUnavailableError):
        await enforce_github_mcp_call(call_request, handler)
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_interceptor_allows_exact_bounded_call() -> None:
    request = MCPToolCallRequest(
        server_name="github",
        name="search_repositories",
        args={"query": "drone", "page": 1, "perPage": 2, "minimal_output": True},
    )
    handler = AsyncMock(return_value=cast(Any, "ok"))

    result = await enforce_github_mcp_call(request, handler)

    assert result == "ok"
    handler.assert_awaited_once_with(request)


@pytest.mark.asyncio
async def test_backend_uses_one_session_and_validates_annotations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = [
        FakeTool("search_repositories", []),
        FakeTool("search_code", []),
        FakeTool("get_file_contents", "README"),
    ]
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, connections: dict[str, Any]) -> None:
            captured["connections"] = connections

        @asynccontextmanager
        async def session(self, server_name: str):
            captured["server_name"] = server_name
            yield object()

    async def fake_load_tools(session: object, **kwargs: Any) -> list[FakeTool]:
        captured["load_session"] = session
        captured["load_kwargs"] = kwargs
        return tools

    monkeypatch.setenv("GITHUB_API_KEY", "secret")
    monkeypatch.setattr("aidison.tools.github.MultiServerMCPClient", FakeClient)
    monkeypatch.setattr("aidison.tools.github.load_mcp_tools", fake_load_tools)

    async with GitHubMcpBackend(GitHubMcpSettings(_env_file=None)).session() as session:
        assert await session.invoke("get_file_contents", {"path": "README.md"}) == "README"

    assert captured["server_name"] == "github"
    assert captured["load_kwargs"]["server_name"] == "github"
    assert captured["load_kwargs"]["handle_tool_errors"] is False
    assert captured["load_kwargs"]["tool_interceptors"] == [enforce_github_mcp_call]


@pytest.mark.asyncio
async def test_backend_rejects_tool_set_or_annotation_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, connections: dict[str, Any]) -> None:
            del connections

        @asynccontextmanager
        async def session(self, server_name: str):
            del server_name
            yield object()

    async def fake_load_tools(session: object, **kwargs: Any) -> list[FakeTool]:
        del session, kwargs
        return [
            FakeTool("search_repositories", []),
            FakeTool("search_code", [], read_only=False),
            FakeTool("get_file_contents", "README"),
        ]

    monkeypatch.setenv("GITHUB_API_KEY", "secret")
    monkeypatch.setattr("aidison.tools.github.MultiServerMCPClient", FakeClient)
    monkeypatch.setattr("aidison.tools.github.load_mcp_tools", fake_load_tools)

    with pytest.raises(GitHubUnavailableError, match="annotations"):
        async with GitHubMcpBackend(GitHubMcpSettings(_env_file=None)).session():
            pytest.fail("invalid tools must fail before yielding")


@pytest.mark.asyncio
async def test_controlled_read_persists_canonical_artifact_before_return() -> None:
    tool = FakeTool("search_repositories", '{"z":2,"a":1}')
    artifacts = FakeArtifacts()
    budget = FakeBudget()
    reader = ControlledGitHubRead(
        GitHubMcpSession({tool.name: cast(GitHubTool, tool)}),
        artifacts,
        budget,
    )

    snapshot = await reader.search_repositories(
        "drone",
        max_results=2,
        context=make_context(),
    )

    assert budget.events == ["reserved", "dispatched", "settled"]
    assert artifacts.writes[0]["kind"] == "github_snapshot"
    assert artifacts.writes[0]["content"] == b'{"a":1,"z":2}'
    assert snapshot.snapshot_hash == sha256(b'{"a":1,"z":2}').hexdigest()
    assert snapshot.snapshot_ref.startswith("artifact+sha256://")


@pytest.mark.asyncio
async def test_controlled_read_releases_before_dispatch() -> None:
    budget = FakeBudget(dispatch_error=RuntimeError("secret-token"))
    reader = ControlledGitHubRead(GitHubMcpSession({}), FakeArtifacts(), budget)

    with pytest.raises(RuntimeError, match="secret-token"):
        await reader.search_repositories("drone", max_results=1, context=make_context())

    assert budget.events == ["reserved", "dispatched", "released"]


@pytest.mark.asyncio
async def test_controlled_read_marks_uncertain_call_ambiguous_without_leaking() -> None:
    tool = FakeTool("search_repositories", RuntimeError("token=secret"))
    budget = FakeBudget()
    reader = ControlledGitHubRead(
        GitHubMcpSession({tool.name: cast(GitHubTool, tool)}),
        FakeArtifacts(),
        budget,
    )

    with pytest.raises(GitHubUnavailableError) as caught:
        await reader.search_repositories("drone", max_results=1, context=make_context())

    assert budget.events == ["reserved", "dispatched", "ambiguous"]
    assert "secret" not in str(caught.value)
    assert "token" not in str(caught.value)


@pytest.mark.asyncio
async def test_controlled_read_rejects_oversize_before_artifact() -> None:
    tool = FakeTool("search_repositories", "x" * (256 * 1024 + 1))
    artifacts = FakeArtifacts()
    budget = FakeBudget()
    reader = ControlledGitHubRead(
        GitHubMcpSession({tool.name: cast(GitHubTool, tool)}),
        artifacts,
        budget,
    )

    with pytest.raises(GitHubUnavailableError, match="256 KiB"):
        await reader.search_repositories("drone", max_results=1, context=make_context())

    assert budget.events == ["reserved", "dispatched", "settled"]
    assert artifacts.writes == []


@pytest.mark.asyncio
async def test_file_read_rejects_discovery_only_context_before_budget() -> None:
    budget = FakeBudget()
    reader = ControlledGitHubRead(GitHubMcpSession({}), FakeArtifacts(), budget)

    with pytest.raises(GitHubUnavailableError, match="does not allow"):
        await reader.get_file_contents(
            "owner",
            "repo",
            "README.md",
            context=make_context(effects=("discovery",)),
        )

    assert budget.events == []
