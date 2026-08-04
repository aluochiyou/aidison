from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest, MCPToolCallResult
from langchain_mcp_adapters.sessions import StdioConnection
from langchain_mcp_adapters.tools import load_mcp_tools
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from aidison.artifacts.contracts import ArtifactStatus
from aidison.tools.web_search import ArtifactSink, SearchContext

_SERVER_NAME = "github"
_ALLOWED_TOOLS = frozenset(
    {"search_repositories", "search_code", "get_file_contents"}
)
_MAX_RESULT_BYTES = 256 * 1024


class GitHubUnavailableError(RuntimeError):
    pass


class GitHubSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_url: str = Field(min_length=1, max_length=4_000)
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_ref: str
    span_text: str = Field(max_length=16_000)


class GitHubTool(Protocol):
    name: str
    metadata: dict[str, Any] | None

    async def ainvoke(self, input: dict[str, Any]) -> Any: ...


class GitHubBudgetBroker(Protocol):
    async def reserve(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: SearchContext,
    ) -> UUID: ...

    async def mark_dispatched(self, operation_id: UUID) -> None: ...

    async def settle(self, operation_id: UUID) -> None: ...

    async def release_undispatched(self, operation_id: UUID) -> None: ...

    async def mark_ambiguous(self, operation_id: UUID) -> None: ...


class GitHubMcpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    api_key: SecretStr | None = Field(
        default=None,
        validation_alias="GITHUB_API_KEY",
    )
    binary_path: str = Field(
        default="/server/github-mcp-server",
        validation_alias="GITHUB_MCP_BINARY_PATH",
    )

    def connection(self) -> StdioConnection:
        if self.api_key is None:
            raise GitHubUnavailableError("GITHUB_API_KEY is required for GitHub MCP")
        if not self.binary_path or not os.path.isabs(self.binary_path):
            raise GitHubUnavailableError("GitHub MCP binary path must be absolute")
        return {
            "transport": "stdio",
            "command": self.binary_path,
            "args": ["stdio"],
            "env": {
                "GITHUB_PERSONAL_ACCESS_TOKEN": self.api_key.get_secret_value(),
                "GITHUB_READ_ONLY": "1",
                "GITHUB_TOOLS": ",".join(sorted(_ALLOWED_TOOLS)),
            },
        }


def _bounded_text(value: object, *, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise GitHubUnavailableError(f"GitHub {field} is outside the bounded contract")
    if any(ord(character) < 32 for character in value):
        raise GitHubUnavailableError(f"GitHub {field} contains control characters")
    return value


def _per_page(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
        raise GitHubUnavailableError("GitHub result count is outside the bounded contract")
    return value


def _repository_arguments(query: object, per_page: object) -> dict[str, Any]:
    return {
        "query": _bounded_text(query, field="repository query", max_length=256),
        "page": 1,
        "perPage": _per_page(per_page),
        "minimal_output": True,
    }


def _code_arguments(query: object, per_page: object) -> dict[str, Any]:
    bounded_query = _bounded_text(query, field="code query", max_length=256)
    if not any(token in bounded_query.lower() for token in ("repo:", "org:", "user:")):
        raise GitHubUnavailableError("GitHub code query requires a repository or owner scope")
    return {
        "query": bounded_query,
        "page": 1,
        "perPage": _per_page(per_page),
    }


def _content_arguments(
    owner: object,
    repo: object,
    path: object,
    *,
    ref: object = None,
    sha: object = None,
) -> dict[str, Any]:
    bounded_owner = _bounded_text(owner, field="owner", max_length=100)
    bounded_repo = _bounded_text(repo, field="repository", max_length=100)
    bounded_path = _bounded_text(path, field="path", max_length=1_024)
    lowered_path = bounded_path.lower()
    if (
        bounded_path.startswith("/")
        or "\\" in bounded_path
        or ".." in bounded_path.split("/")
        or any(marker in lowered_path for marker in ("://", "token=", "secret=", "key="))
    ):
        raise GitHubUnavailableError("GitHub path is outside the read-only contract")
    if ref is not None and sha is not None:
        raise GitHubUnavailableError("GitHub ref and sha are mutually exclusive")
    arguments: dict[str, Any] = {
        "owner": bounded_owner,
        "repo": bounded_repo,
        "path": bounded_path,
    }
    if ref is not None:
        arguments["ref"] = _bounded_text(ref, field="ref", max_length=256)
    if sha is not None:
        arguments["sha"] = _bounded_text(sha, field="sha", max_length=256)
    return arguments


async def enforce_github_mcp_call(
    request: MCPToolCallRequest,
    handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
) -> MCPToolCallResult:
    if request.server_name != _SERVER_NAME or request.name not in _ALLOWED_TOOLS:
        raise GitHubUnavailableError("GitHub MCP tool is not allowed")
    if request.name == "search_repositories":
        expected = _repository_arguments(
            request.args.get("query"),
            request.args.get("perPage"),
        )
    elif request.name == "search_code":
        expected = _code_arguments(
            request.args.get("query"),
            request.args.get("perPage"),
        )
    else:
        expected = _content_arguments(
            request.args.get("owner"),
            request.args.get("repo"),
            request.args.get("path"),
            ref=request.args.get("ref"),
            sha=request.args.get("sha"),
        )
    if request.args != expected:
        raise GitHubUnavailableError("GitHub MCP arguments are not allowed")
    return await handler(request)


def _validated_tools(tools: Sequence[GitHubTool]) -> dict[str, GitHubTool]:
    names = [tool.name for tool in tools]
    if len(names) != len(_ALLOWED_TOOLS) or set(names) != _ALLOWED_TOOLS:
        raise GitHubUnavailableError("GitHub MCP tool set is outside the allowlist")
    if any((tool.metadata or {}).get("readOnlyHint") is not True for tool in tools):
        raise GitHubUnavailableError("GitHub MCP tool annotations are not read-only")
    return {tool.name: tool for tool in tools}


class GitHubMcpSession:
    def __init__(self, tools: Mapping[str, GitHubTool]) -> None:
        self._tools = dict(tools)

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> object:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise GitHubUnavailableError("GitHub MCP tool is unavailable")
        return await tool.ainvoke(arguments)


class GitHubMcpBackend:
    def __init__(self, settings: GitHubMcpSettings | None = None) -> None:
        self._settings = settings or GitHubMcpSettings()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[GitHubMcpSession]:
        connection = self._settings.connection()
        client = MultiServerMCPClient({_SERVER_NAME: connection})
        ready = False
        try:
            async with client.session(_SERVER_NAME) as mcp_session:
                tools = await load_mcp_tools(
                    mcp_session,
                    tool_interceptors=[enforce_github_mcp_call],
                    server_name=_SERVER_NAME,
                    handle_tool_errors=False,
                )
                validated = _validated_tools(tools)
                ready = True
                yield GitHubMcpSession(validated)
        except GitHubUnavailableError:
            raise
        except Exception:
            if ready:
                raise
            raise GitHubUnavailableError("GitHub MCP session could not start") from None


def _canonical_content(value: object) -> tuple[bytes, str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            content = value.encode("utf-8")
            media_type = "text/plain"
        else:
            if not isinstance(parsed, (dict, list)):
                raise GitHubUnavailableError("GitHub MCP returned an unsupported JSON value")
            content = json.dumps(
                parsed,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            media_type = "application/json"
    elif isinstance(value, list) and value and all(
        isinstance(block, dict) and "type" in block for block in value
    ):
        if any(block.get("type") != "text" for block in value):
            raise GitHubUnavailableError("GitHub MCP returned binary or unsupported content")
        texts = [block.get("text") for block in value]
        if not all(isinstance(text, str) for text in texts):
            raise GitHubUnavailableError("GitHub MCP returned invalid text content")
        return _canonical_content("\n".join(texts))
    elif isinstance(value, (dict, list)):
        content = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        media_type = "application/json"
    else:
        raise GitHubUnavailableError("GitHub MCP returned an unsupported result")
    if len(content) > _MAX_RESULT_BYTES:
        raise GitHubUnavailableError("GitHub MCP result exceeds 256 KiB")
    return content, media_type


class ControlledGitHubRead:
    def __init__(
        self,
        session: GitHubMcpSession,
        artifacts: ArtifactSink,
        budget: GitHubBudgetBroker,
    ) -> None:
        self._session = session
        self._artifacts = artifacts
        self._budget = budget

    async def search_repositories(
        self,
        query: str,
        *,
        max_results: int,
        context: SearchContext,
    ) -> GitHubSnapshot:
        self._check_context(context, discovery=True)
        return await self._execute(
            tool_name="search_repositories",
            arguments=_repository_arguments(query, max_results),
            source_url="https://github.com/search?type=repositories",
            context=context,
        )

    async def search_code(
        self,
        query: str,
        *,
        max_results: int,
        context: SearchContext,
    ) -> GitHubSnapshot:
        self._check_context(context, discovery=True)
        return await self._execute(
            tool_name="search_code",
            arguments=_code_arguments(query, max_results),
            source_url="https://github.com/search?type=code",
            context=context,
        )

    async def get_file_contents(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        context: SearchContext,
        ref: str | None = None,
        sha: str | None = None,
    ) -> GitHubSnapshot:
        self._check_context(context, discovery=False)
        arguments = _content_arguments(owner, repo, path, ref=ref, sha=sha)
        revision = sha or ref or "HEAD"
        source_url = (
            f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}/blob/"
            f"{quote(revision, safe='/')}/{quote(path, safe='/')}"
        )
        return await self._execute(
            tool_name="get_file_contents",
            arguments=arguments,
            source_url=source_url,
            context=context,
        )

    @staticmethod
    def _check_context(context: SearchContext, *, discovery: bool) -> None:
        required = {"read"}
        if discovery:
            required.add("discovery")
        if not required <= set(context.allowed_effects):
            raise GitHubUnavailableError("delegation does not allow this GitHub read")
        if datetime.now(UTC) >= context.deadline:
            raise GitHubUnavailableError("delegation deadline has passed")

    async def _execute(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        source_url: str,
        context: SearchContext,
    ) -> GitHubSnapshot:
        operation_id = await self._budget.reserve(
            tool_name=tool_name,
            arguments=arguments,
            context=context,
        )
        try:
            await self._budget.mark_dispatched(operation_id)
        except BaseException:
            await self._budget.release_undispatched(operation_id)
            raise
        try:
            result = await self._session.invoke(tool_name, arguments)
        except BaseException as exc:
            await self._budget.mark_ambiguous(operation_id)
            if isinstance(exc, GitHubUnavailableError):
                raise
            raise GitHubUnavailableError("GitHub MCP tool call failed") from None
        await self._budget.settle(operation_id)
        content, media_type = _canonical_content(result)
        try:
            artifact = await self._artifacts.put_bytes(
                project_id=context.project_id,
                attempt_id=context.claim.attempt_id,
                basis_hash=context.basis_hash,
                kind="github_snapshot",
                content=content,
                media_type=media_type,
                source_url=source_url,
            )
        except Exception:
            raise GitHubUnavailableError("GitHub result could not be persisted") from None
        if artifact.status is not ArtifactStatus.PRESENT:
            raise GitHubUnavailableError("GitHub snapshot is not present")
        return GitHubSnapshot(
            source_url=source_url,
            snapshot_hash=artifact.content_hash,
            snapshot_ref=artifact.ref,
            span_text=content.decode("utf-8")[:16_000],
        )
