from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StdioConnection
from langchain_mcp_adapters.tools import load_mcp_tools

from aidison.artifacts.contracts import ArtifactMetadata
from aidison.runtime.contracts import JobClaim
from aidison.tools.github import (
    ControlledGitHubRead,
    GitHubMcpSession,
    GitHubTool,
    enforce_github_mcp_call,
)
from aidison.tools.web_search import SearchContext


class RecordingBudget:
    def __init__(self) -> None:
        self.operation_id = uuid4()
        self.events: list[str] = []

    async def reserve(self, **kwargs: Any) -> UUID:
        assert kwargs["tool_name"] == "search_repositories"
        self.events.append("reserved")
        return self.operation_id

    async def mark_dispatched(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("dispatched")

    async def settle(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("settled")

    async def release_undispatched(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("released")

    async def mark_ambiguous(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("ambiguous")


class RecordingArtifacts:
    def __init__(self) -> None:
        self.content: bytes | None = None

    async def put_bytes(self, **kwargs: Any) -> ArtifactMetadata:
        content = cast(bytes, kwargs["content"])
        content_hash = sha256(content).hexdigest()
        self.content = content
        return ArtifactMetadata(
            project_id=kwargs["project_id"],
            attempt_id=kwargs["attempt_id"],
            basis_hash=kwargs["basis_hash"],
            kind=kwargs["kind"],
            content_hash=content_hash,
            size_bytes=len(content),
            media_type=kwargs["media_type"],
            storage_key=f"sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}",
            source_url=kwargs["source_url"],
        )


def make_context() -> SearchContext:
    basis_hash = sha256(b"fake-stdio").hexdigest()
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    claim = JobClaim(
        job_id=uuid4(),
        attempt_id=uuid4(),
        attempt_number=1,
        claim_generation=1,
        lease_token=uuid4(),
        lease_owner="fake-stdio",
        lease_expires_at=deadline,
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
        deadline=deadline,
    )


@pytest.mark.asyncio
async def test_real_adapter_stdio_interceptor_budget_and_artifact(tmp_path: Path) -> None:
    server = tmp_path / "fake_github_mcp.py"
    server.write_text(
        """from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("fake-github")
read_only = ToolAnnotations(readOnlyHint=True)

@mcp.tool(annotations=read_only)
def search_repositories(query: str, page: int, perPage: int, minimal_output: bool):
    \"\"\"Return a deterministic repository result.\"\"\"
    return {"results": [{"name": "flight-stack", "query": query}], "page": page,
            "perPage": perPage, "minimal_output": minimal_output}

@mcp.tool(annotations=read_only)
def search_code(query: str, page: int, perPage: int):
    \"\"\"Return a deterministic code result.\"\"\"
    return {"results": [], "query": query, "page": page, "perPage": perPage}

@mcp.tool(annotations=read_only)
def get_file_contents(owner: str, repo: str, path: str, ref: str | None = None,
                      sha: str | None = None):
    \"\"\"Return deterministic UTF-8 file content.\"\"\"
    return f"{owner}/{repo}/{path}@{sha or ref or 'HEAD'}"

if __name__ == "__main__":
    mcp.run("stdio")
""",
        encoding="utf-8",
    )
    connection: StdioConnection = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(server)],
    }
    client = MultiServerMCPClient({"github": connection})
    budget = RecordingBudget()
    artifacts = RecordingArtifacts()

    async with client.session("github") as raw_session:
        tools = await load_mcp_tools(
            raw_session,
            tool_interceptors=[enforce_github_mcp_call],
            server_name="github",
            handle_tool_errors=False,
        )
        assert {tool.name for tool in tools} == {
            "search_repositories",
            "search_code",
            "get_file_contents",
        }
        assert all((tool.metadata or {}).get("readOnlyHint") is True for tool in tools)
        session = GitHubMcpSession(
            {tool.name: cast(GitHubTool, tool) for tool in tools}
        )
        snapshot = await ControlledGitHubRead(session, artifacts, budget).search_repositories(
            "quadcopter",
            max_results=2,
            context=make_context(),
        )

    assert budget.events == ["reserved", "dispatched", "settled"]
    assert artifacts.content is not None
    assert snapshot.snapshot_hash == sha256(artifacts.content).hexdigest()
    assert snapshot.snapshot_ref.startswith("artifact+sha256://")
    assert "flight-stack" in snapshot.span_text
