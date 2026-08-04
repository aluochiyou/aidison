from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import (
    MCPToolCallRequest,
    MCPToolCallResult,
)
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from trafilatura import extract

from aidison.artifacts.contracts import ArtifactMetadata
from aidison.runtime.contracts import JobClaim


class SearchUnavailableError(RuntimeError):
    pass


class UnsafeSourceError(RuntimeError):
    pass


class SearchContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: UUID
    claim: JobClaim
    budget_allocation_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    allowed_effects: tuple[str, ...] = ("discovery", "read")
    deadline: datetime


class RawSearchHit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=4_000)
    snippet: str = Field(default="", max_length=4_000)


class SearchHit(RawSearchHit):
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_ref: str
    span_text: str = Field(max_length=16_000)


class SearchBackend(Protocol):
    async def search(self, query: str, *, max_results: int) -> Sequence[RawSearchHit]: ...


class ArtifactSink(Protocol):
    async def put_bytes(
        self,
        *,
        project_id: UUID,
        attempt_id: UUID,
        basis_hash: str,
        kind: str,
        content: bytes,
        media_type: str,
        source_url: str | None = None,
    ) -> ArtifactMetadata: ...


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> tuple[str, bytes]: ...


class SearchBudgetBroker(Protocol):
    async def reserve(
        self,
        *,
        query: str,
        max_results: int,
        context: SearchContext,
    ) -> UUID: ...

    async def mark_dispatched(self, operation_id: UUID) -> None: ...

    async def settle(self, operation_id: UUID) -> None: ...


class TavilyMcpSearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    api_key: SecretStr | None = Field(
        default=None,
        validation_alias="TAVILY_API_KEY",
    )
    mcp_url: str = Field(
        default="https://mcp.tavily.com/mcp",
        validation_alias="TAVILY_MCP_URL",
    )
    search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] = Field(
        default="advanced",
        validation_alias="TAVILY_SEARCH_DEPTH",
    )
    timeout_seconds: float = Field(
        default=10,
        gt=0,
        le=30,
        validation_alias="TAVILY_SEARCH_TIMEOUT_SECONDS",
    )


async def _enforce_tavily_search_call(
    request: MCPToolCallRequest,
    handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
) -> MCPToolCallResult:
    if request.server_name != "tavily" or request.name != "tavily_search":
        raise SearchUnavailableError("Tavily MCP tool is not allowed")
    allowed_arguments = {
        "query",
        "search_depth",
        "max_results",
        "include_images",
        "include_raw_content",
    }
    if set(request.args) != allowed_arguments:
        raise SearchUnavailableError("Tavily MCP arguments are not allowed")
    if request.args.get("max_results") != 5:
        raise SearchUnavailableError("Tavily MCP result limit is not bounded")
    if request.args.get("include_images") is not False:
        raise SearchUnavailableError("Tavily MCP images are not allowed")
    if request.args.get("include_raw_content") is not False:
        raise SearchUnavailableError("Tavily MCP raw content is not allowed")
    return await handler(request)


def _mcp_result_text(result: object) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, list) or not result:
        raise SearchUnavailableError("Tavily MCP returned an invalid response")
    text_blocks: list[str] = []
    for block in result:
        if not isinstance(block, dict) or block.get("type") != "text":
            raise SearchUnavailableError("Tavily MCP returned unsupported content")
        text = block.get("text")
        if not isinstance(text, str):
            raise SearchUnavailableError("Tavily MCP returned invalid text content")
        text_blocks.append(text)
    return "\n".join(text_blocks)


def _parse_tavily_search_text(text: str, *, max_results: int) -> tuple[RawSearchHit, ...]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SearchUnavailableError("Tavily MCP returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SearchUnavailableError("Tavily MCP returned an invalid result envelope")
    hits: list[RawSearchHit] = []
    for item in payload["results"][:max_results]:
        if not isinstance(item, dict):
            raise SearchUnavailableError("Tavily MCP returned an invalid search result")
        title = item.get("title") or "Untitled source"
        url = item.get("url")
        snippet = item.get("content") or ""
        if not isinstance(title, str) or not isinstance(url, str) or not isinstance(snippet, str):
            raise SearchUnavailableError("Tavily MCP returned invalid result fields")
        try:
            hit = RawSearchHit(
                title=title[:500],
                url=url,
                snippet=snippet[:4_000],
            )
        except Exception as exc:
            raise SearchUnavailableError("Tavily MCP result failed validation") from exc
        hits.append(hit)
    return tuple(hits)


class TavilyMcpSearchBackend:
    """Thin adapter over the official Tavily Remote MCP server."""

    def __init__(self, settings: TavilyMcpSearchSettings | None = None) -> None:
        self._settings = settings or TavilyMcpSearchSettings()
        if self._settings.api_key is None:
            raise SearchUnavailableError("TAVILY_API_KEY is required for web search")
        endpoint = urlsplit(self._settings.mcp_url)
        if (
            endpoint.scheme != "https"
            or endpoint.hostname != "mcp.tavily.com"
            or endpoint.port not in {None, 443}
            or endpoint.username
            or endpoint.password
        ):
            raise SearchUnavailableError("TAVILY_MCP_URL must use the official HTTPS endpoint")

    async def search(self, query: str, *, max_results: int) -> Sequence[RawSearchHit]:
        assert self._settings.api_key is not None
        if not query.strip() or len(query) > 1_000 or not 1 <= max_results <= 5:
            raise SearchUnavailableError("Tavily MCP request is outside the bounded contract")
        connection: StreamableHttpConnection = {
            "transport": "streamable_http",
            "url": self._settings.mcp_url,
            "headers": {
                "Authorization": f"Bearer {self._settings.api_key.get_secret_value()}"
            },
            "timeout": self._settings.timeout_seconds,
            "sse_read_timeout": self._settings.timeout_seconds,
        }
        try:
            client = MultiServerMCPClient(
                {"tavily": connection},
                tool_interceptors=[_enforce_tavily_search_call],
                handle_tool_errors=False,
            )
            tools = await client.get_tools(server_name="tavily")
            search_tools = [tool for tool in tools if tool.name == "tavily_search"]
            if len(search_tools) != 1:
                raise SearchUnavailableError("Tavily MCP search tool is unavailable")
            result: Any = await search_tools[0].ainvoke(
                {
                    "query": query,
                    "search_depth": self._settings.search_depth,
                    "max_results": 5,
                    "include_images": False,
                    "include_raw_content": False,
                }
            )
            return _parse_tavily_search_text(
                _mcp_result_text(result),
                max_results=max_results,
            )
        except SearchUnavailableError:
            raise
        except Exception as exc:
            raise SearchUnavailableError("Tavily MCP search request failed") from exc


async def _resolve_public_addresses(hostname: str, port: int) -> None:
    try:
        parsed = ipaddress.ip_address(hostname)
        addresses = {parsed}
    except ValueError:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
        addresses = {ipaddress.ip_address(item[4][0]) for item in infos}
    if not addresses:
        raise UnsafeSourceError("source hostname did not resolve")
    if any(not address.is_global for address in addresses):
        raise UnsafeSourceError("source resolves to a non-public address")


async def validate_public_https_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise UnsafeSourceError("source must use HTTPS")
    if parsed.username or parsed.password:
        raise UnsafeSourceError("source URL cannot contain credentials")
    if parsed.port not in {None, 443}:
        raise UnsafeSourceError("source URL uses a disallowed port")
    await _resolve_public_addresses(parsed.hostname, parsed.port or 443)


class SafeHttpFetcher:
    def __init__(
        self,
        *,
        max_bytes: int = 1_000_000,
        max_redirects: int = 3,
        timeout_seconds: float = 10,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._timeout_seconds = timeout_seconds

    async def fetch(self, url: str) -> tuple[str, bytes]:
        current = url
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=self._timeout_seconds,
        ) as client:
            for redirect_count in range(self._max_redirects + 1):
                await validate_public_https_url(current)
                async with client.stream("GET", current, headers={"Accept": "text/*"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= self._max_redirects:
                            raise UnsafeSourceError("source exceeded the redirect limit")
                        location = response.headers.get("location")
                        if not location:
                            raise UnsafeSourceError("source redirect has no location")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    media_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if media_type not in {"text/html", "text/plain", "application/json"}:
                        raise UnsafeSourceError("source media type is not allowed")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_bytes:
                            raise UnsafeSourceError("source exceeds the byte limit")
                        chunks.append(chunk)
                    return media_type, b"".join(chunks)
        raise UnsafeSourceError("source could not be fetched")


def _extract_span(media_type: str, content: bytes) -> str:
    text = content.decode("utf-8", errors="replace")
    if media_type == "text/html":
        text = extract(text, include_comments=False, include_tables=True) or ""
    return text[:16_000]


class ControlledWebSearch:
    """Bounded discovery whose fetched sources become immutable artifacts."""

    def __init__(
        self,
        backend: SearchBackend,
        fetcher: PageFetcher,
        artifacts: ArtifactSink,
        budget: SearchBudgetBroker,
    ) -> None:
        self._backend = backend
        self._fetcher = fetcher
        self._artifacts = artifacts
        self._budget = budget

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        context: SearchContext,
    ) -> tuple[SearchHit, ...]:
        if "discovery" not in context.allowed_effects or "read" not in context.allowed_effects:
            raise SearchUnavailableError("delegation does not allow discovery and read effects")
        if datetime.now(UTC) >= context.deadline:
            raise SearchUnavailableError("delegation deadline has passed")
        if not query.strip() or len(query) > 1_000:
            raise SearchUnavailableError("search query is empty or too long")
        if max_results < 1 or max_results > 5:
            raise SearchUnavailableError("search result count exceeds the bounded result limit")

        operation_id = await self._budget.reserve(
            query=query,
            max_results=max_results,
            context=context,
        )
        await self._budget.mark_dispatched(operation_id)
        try:
            # Tavily is already called with a fixed five-result provider bound. Keep
            # those bounded candidates available so rejected URLs can be replaced
            # while the caller still receives no more than its requested count.
            raw_hits = await self._backend.search(query, max_results=5)
        except BaseException:
            await self._budget.settle(operation_id)
            raise
        await self._budget.settle(operation_id)
        results: list[SearchHit] = []
        for raw in raw_hits[:5]:
            if len(results) >= max_results:
                break
            try:
                media_type, content = await self._fetcher.fetch(raw.url)
            except (UnsafeSourceError, httpx.HTTPError, OSError):
                # Search-provider URLs are untrusted. Reject an unusable source without
                # discarding later bounded hits, but never relax the fetch policy.
                continue
            artifact = await self._artifacts.put_bytes(
                project_id=context.project_id,
                attempt_id=context.claim.attempt_id,
                basis_hash=context.basis_hash,
                kind="web_snapshot",
                content=content,
                media_type=media_type,
                source_url=raw.url,
            )
            results.append(
                SearchHit(
                    **raw.model_dump(),
                    snapshot_hash=artifact.content_hash,
                    snapshot_ref=artifact.ref,
                    span_text=_extract_span(media_type, content),
                )
            )
        if not results:
            raise SearchUnavailableError("search returned no fetchable public source")
        return tuple(results)
