from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from aidison.artifacts.contracts import ArtifactMetadata
from aidison.runtime.contracts import JobClaim
from aidison.tools.web_search import (
    ControlledWebSearch,
    RawSearchHit,
    SearchContext,
    SearchUnavailableError,
    TavilyMcpSearchBackend,
    TavilyMcpSearchSettings,
    UnsafeSourceError,
    validate_public_https_url,
)


class FakeBackend:
    async def search(self, query: str, *, max_results: int) -> tuple[RawSearchHit, ...]:
        assert query == "safe drone frame"
        return (
            RawSearchHit(
                title="Engineering source",
                url="https://example.com/frame",
                snippet="A bounded source",
            ),
        )[:max_results]


class FakeFetcher:
    async def fetch(self, url: str) -> tuple[str, bytes]:
        assert url == "https://example.com/frame"
        return (
            "text/html",
            (
                b"<html><body><nav>Site menu</nav><main><article>"
                b"<h1>Frame evidence</h1><p>The documented frame interface supports "
                b"the selected engineering route and provides measured dimensions.</p>"
                b"</article></main><script>ignore me</script></body></html>"
            ),
        )


class MixedBackend:
    async def search(self, query: str, *, max_results: int) -> tuple[RawSearchHit, ...]:
        assert query == "safe drone frame"
        return (
            RawSearchHit(
                title="Rejected source",
                url="https://example.com/rejected",
                snippet="The first provider result is unusable",
            ),
            RawSearchHit(
                title="Engineering source",
                url="https://example.com/frame",
                snippet="A bounded source",
            ),
        )[:max_results]


class MixedFetcher(FakeFetcher):
    async def fetch(self, url: str) -> tuple[str, bytes]:
        if url.endswith("/rejected"):
            raise UnsafeSourceError("source exceeds the byte limit")
        return await super().fetch(url)


class RejectingFetcher:
    async def fetch(self, url: str) -> tuple[str, bytes]:
        del url
        raise UnsafeSourceError("source exceeds the byte limit")


class FakeArtifacts:
    def __init__(self) -> None:
        self.saved: list[bytes] = []

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
    ) -> ArtifactMetadata:
        self.saved.append(content)
        content_hash = sha256(content).hexdigest()
        return ArtifactMetadata(
            project_id=project_id,
            attempt_id=attempt_id,
            basis_hash=basis_hash,
            kind=kind,
            content_hash=content_hash,
            size_bytes=len(content),
            media_type=media_type,
            storage_key=f"sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}",
            source_url=source_url,
        )


class FakeBudget:
    def __init__(self) -> None:
        self.operation_id = uuid4()
        self.events: list[str] = []

    async def reserve(
        self,
        *,
        query: str,
        max_results: int,
        context: SearchContext,
    ) -> UUID:
        assert query == "safe drone frame"
        assert max_results >= 1
        assert context.claim.attempt_id
        self.events.append("reserved")
        return self.operation_id

    async def mark_dispatched(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("dispatched")

    async def settle(self, operation_id: UUID) -> None:
        assert operation_id == self.operation_id
        self.events.append("settled")


def _context() -> SearchContext:
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    return SearchContext(
        project_id=uuid4(),
        claim=JobClaim(
            job_id=uuid4(),
            attempt_id=uuid4(),
            attempt_number=1,
            claim_generation=1,
            lease_token=uuid4(),
            lease_owner="unit-worker",
            lease_expires_at=deadline,
            basis_hash=sha256(b"basis").hexdigest(),
            basis_project_revision=1,
            profile_id="research-worker-ro",
            profile_revision=1,
        ),
        budget_allocation_id=uuid4(),
        basis_hash=sha256(b"basis").hexdigest(),
        deadline=deadline,
    )


@pytest.mark.asyncio
async def test_controlled_search_snapshots_untrusted_source() -> None:
    artifacts = FakeArtifacts()
    budget = FakeBudget()
    search = ControlledWebSearch(FakeBackend(), FakeFetcher(), artifacts, budget)
    hits = await search.search("safe drone frame", max_results=1, context=_context())

    assert len(hits) == 1
    assert hits[0].snapshot_hash == sha256(artifacts.saved[0]).hexdigest()
    assert "Frame" in hits[0].span_text
    assert "ignore me" not in hits[0].span_text
    assert hits[0].snapshot_ref.startswith("artifact+sha256://")
    assert budget.events == ["reserved", "dispatched", "settled"]


@pytest.mark.asyncio
async def test_controlled_search_skips_unusable_hit_without_relaxing_policy() -> None:
    artifacts = FakeArtifacts()
    budget = FakeBudget()
    search = ControlledWebSearch(MixedBackend(), MixedFetcher(), artifacts, budget)

    hits = await search.search("safe drone frame", max_results=2, context=_context())

    assert [item.url for item in hits] == ["https://example.com/frame"]
    assert len(artifacts.saved) == 1
    assert budget.events == ["reserved", "dispatched", "settled"]


@pytest.mark.asyncio
async def test_controlled_search_fails_when_all_hits_are_unusable() -> None:
    search = ControlledWebSearch(
        MixedBackend(), RejectingFetcher(), FakeArtifacts(), FakeBudget()
    )

    with pytest.raises(SearchUnavailableError, match="no fetchable public source"):
        await search.search("safe drone frame", max_results=1, context=_context())


@pytest.mark.asyncio
async def test_search_budget_and_effects_fail_closed() -> None:
    search = ControlledWebSearch(FakeBackend(), FakeFetcher(), FakeArtifacts(), FakeBudget())
    context = _context().model_copy(update={"allowed_effects": ("read",)})
    with pytest.raises(SearchUnavailableError, match="does not allow"):
        await search.search("safe drone frame", max_results=1, context=context)
    with pytest.raises(SearchUnavailableError, match="bounded result"):
        await search.search("safe drone frame", max_results=6, context=_context())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/source",
        "https://127.0.0.1/source",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.1/source",
        "https://user:password@example.com/source",
    ],
)
async def test_source_url_validation_rejects_private_or_credentialed_urls(url: str) -> None:
    with pytest.raises(UnsafeSourceError):
        await validate_public_https_url(url)


@pytest.mark.asyncio
async def test_tavily_backend_missing_key_fails_closed() -> None:
    with pytest.raises(SearchUnavailableError, match="TAVILY_API_KEY"):
        TavilyMcpSearchBackend(TavilyMcpSearchSettings(api_key=None))


@pytest.mark.asyncio
async def test_tavily_mcp_backend_maps_bounded_search_results() -> None:
    search_tool = MagicMock()
    search_tool.name = "tavily_search"
    search_tool.ainvoke = AsyncMock(
        return_value=[
            {
                "type": "text",
                "text": (
                    '{"query":"frame","results":['
                    '{"title":"Official engineering source",'
                    '"url":"https://example.com/specification",'
                    '"content":"Documented dimensions and interfaces."},'
                    '{"title":"Secondary source",'
                    '"url":"https://example.org/secondary",'
                    '"content":"Additional details."}]}'
                ),
            }
        ]
    )
    deferred_tool = MagicMock()
    deferred_tool.name = "tavily_crawl"
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[search_tool, deferred_tool])
    with patch(
        "aidison.tools.web_search.MultiServerMCPClient",
        return_value=client,
    ) as client_factory:
        hits = await TavilyMcpSearchBackend(
            TavilyMcpSearchSettings(api_key="test-only-placeholder")
        ).search("documented frame interface", max_results=1)

    assert hits == (
        RawSearchHit(
            title="Official engineering source",
            url="https://example.com/specification",
            snippet="Documented dimensions and interfaces.",
        ),
    )
    client.get_tools.assert_awaited_once_with(server_name="tavily")
    search_tool.ainvoke.assert_awaited_once_with(
        {
            "query": "documented frame interface",
            "search_depth": "advanced",
            "max_results": 5,
            "include_images": False,
            "include_raw_content": False,
        }
    )
    connection = client_factory.call_args.args[0]["tavily"]
    assert connection["transport"] == "streamable_http"
    assert connection["url"] == "https://mcp.tavily.com/mcp"
    assert connection["headers"] == {"Authorization": "Bearer test-only-placeholder"}
    assert client_factory.call_args.kwargs["handle_tool_errors"] is False
    assert len(client_factory.call_args.kwargs["tool_interceptors"]) == 1


@pytest.mark.asyncio
async def test_tavily_mcp_backend_rejects_unofficial_endpoint() -> None:
    with pytest.raises(SearchUnavailableError, match="official HTTPS endpoint"):
        TavilyMcpSearchBackend(
            TavilyMcpSearchSettings(
                api_key="test-only-placeholder",
                mcp_url="https://example.com/mcp",
            )
        )


@pytest.mark.asyncio
async def test_tavily_mcp_backend_fails_closed_on_missing_tool() -> None:
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[])
    with (
        patch("aidison.tools.web_search.MultiServerMCPClient", return_value=client),
        pytest.raises(SearchUnavailableError, match="search tool is unavailable"),
    ):
        await TavilyMcpSearchBackend(
            TavilyMcpSearchSettings(api_key="test-only-placeholder")
        ).search("documented frame interface", max_results=1)


@pytest.mark.asyncio
async def test_tavily_mcp_backend_fails_closed_on_invalid_result() -> None:
    search_tool = MagicMock()
    search_tool.name = "tavily_search"
    search_tool.ainvoke = AsyncMock(
        return_value=[{"type": "text", "text": "provider format changed"}]
    )
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=[search_tool])
    with (
        patch("aidison.tools.web_search.MultiServerMCPClient", return_value=client),
        pytest.raises(SearchUnavailableError, match="invalid JSON"),
    ):
        await TavilyMcpSearchBackend(
            TavilyMcpSearchSettings(api_key="test-only-placeholder")
        ).search("documented frame interface", max_results=1)
