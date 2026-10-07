"""Trusted source-document input boundary for Research tasks.

Collectors may retrieve from a provider, repository or user-controlled source,
but models never manufacture this input.  The application persists the exact
document before a model can cite it, then Evidence Admission revalidates every
quoted span proposed by the model.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aidison.research.evidence_diagnostics import ResearchSourceCollectionFailure
from aidison.research.langgraph_contracts import TaskEnvelope
from aidison.research.source_observations import SourceIdentity, SourceKind, source_origin_key
from aidison.runtime.agent_runs import AgentRun


class CollectedResearchSource(BaseModel):
    """One trusted normalized document available to a Research task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    source: SourceIdentity
    normalized_document: str = Field(min_length=1, max_length=96_000)
    media_type: str = Field(min_length=1, max_length=200)
    representation: str = Field(min_length=1, max_length=160)
    parser_revision: str = Field(min_length=1, max_length=160)
    observed_at: datetime
    coverage_source_kinds: tuple[str, ...] = Field(min_length=1, max_length=16)
    collection_failures: tuple[ResearchSourceCollectionFailure, ...] = Field(
        default=(), max_length=16
    )

    @field_validator("coverage_source_kinds")
    @classmethod
    def coverage_source_kinds_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("coverage_source_kinds must be a sorted unique tuple")
        return value

    @field_validator("collection_failures")
    @classmethod
    def collection_failures_are_canonical(
        cls, value: tuple[ResearchSourceCollectionFailure, ...]
    ) -> tuple[ResearchSourceCollectionFailure, ...]:
        if len({(item.coverage_key, item.reason_code) for item in value}) != len(value):
            raise ValueError("collected source collection failures must be unique")
        if tuple(sorted(value, key=lambda item: (item.coverage_key, item.reason_code))) != value:
            raise ValueError("collected source collection failures must be sorted")
        return value

    @model_validator(mode="after")
    def is_a_normalized_timestamped_document(self) -> CollectedResearchSource:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if not self.representation.startswith("normalized-"):
            raise ValueError("collected source representation must be normalized")
        return self


class ResearchSourceCollector(Protocol):
    """Physical read-only source collection seam; authority remains outside models."""

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]: ...


class NoopResearchSourceCollector:
    """Safe default until a deployment configures an authorized source adapter."""

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]:
        return ()


class ResearchSourceCollectionError(RuntimeError):
    """A typed failure at a trusted source-collection boundary."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"trusted research source collection failed: {reason_code}")


class TavilySourceCollectionError(ResearchSourceCollectionError):
    """A classified failure at the trusted Tavily source boundary."""


class TavilySearchResearchSourceCollector:
    """Collect bounded raw webpage material through Tavily's read-only Search API.

    Search snippets and Tavily-generated answers are intentionally ignored.
    Only a returned ``raw_content`` document becomes model-visible source
    material, and it remains an external ``evidence`` source rather than a
    project specification.
    """

    _endpoint = "https://api.tavily.com/search"

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient,
        max_results: int | None = 3,
        max_queries: int | None = 3,
        max_document_characters: int = 32_000,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Tavily API key must not be empty")
        if max_results is not None and max_results < 1:
            raise ValueError("Tavily max_results must be positive when configured")
        if max_queries is not None and max_queries < 1:
            raise ValueError("Tavily max_queries must be positive when configured")
        if not 1 <= max_document_characters <= 96_000:
            raise ValueError("Tavily source document limit must be between 1 and 96000")
        self._api_key = api_key
        self._client = client
        self._max_results = max_results
        self._max_queries = max_queries
        self._max_document_characters = max_document_characters

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
    ) -> tuple[CollectedResearchSource, ...]:
        del run
        collection_policy = getattr(task, "collection_policy", None)
        max_queries = _narrowest_configured_limit(
            self._max_queries,
            collection_policy.max_queries if collection_policy is not None else None,
        )
        max_results = _narrowest_configured_limit(
            self._max_results,
            collection_policy.max_documents_total if collection_policy is not None else None,
        )
        max_results_per_query = _narrowest_configured_limit(
            self._max_results,
            (
                collection_policy.max_documents_per_query
                if collection_policy is not None
                else None
            ),
        )
        search_depth = (
            collection_policy.search_depth if collection_policy is not None else "basic"
        )
        observed_at = datetime.now(UTC)
        candidates_by_query: list[tuple[CollectedResearchSource, ...]] = []
        recoverable_failures: list[TavilySourceCollectionError] = []
        collection_failures: list[ResearchSourceCollectionFailure] = []
        for coverage_key, query in _coverage_search_queries(question, limit=max_queries):
            try:
                body = await self._search(
                    query,
                    max_results=max_results_per_query,
                    search_depth=search_depth,
                )
            except TavilySourceCollectionError as error:
                # A deep task can have several independent Coverage Keys.  A
                # transient failure for one query must not discard a trusted
                # document already collected for another key; Coverage and the
                # bounded gap loop will keep the failed key visible.  Do not
                # hide non-retryable auth/quota/schema failures behind a
                # partial result, because a later task could not repair those
                # deployment conditions without user action.
                if not _is_recoverable_query_failure(error.reason_code):
                    raise
                recoverable_failures.append(error)
                if coverage_key is not None:
                    collection_failures.append(
                        ResearchSourceCollectionFailure(
                            coverage_key=coverage_key,
                            reason_code=error.reason_code,
                        )
                    )
                candidates_by_query.append(())
                continue
            query_candidates: list[CollectedResearchSource] = []
            seen_query_locators: set[str] = set()
            for result in body["results"]:
                if not isinstance(result, dict):
                    continue
                locator = result.get("url")
                raw_content = result.get("raw_content")
                if not isinstance(locator, str) or not isinstance(raw_content, str):
                    continue
                normalized_locator = _normalized_https_locator(locator)
                document = raw_content.strip()[: self._max_document_characters]
                if (
                    normalized_locator is None
                    or not document
                    or normalized_locator in seen_query_locators
                ):
                    continue
                seen_query_locators.add(normalized_locator)
                query_candidates.append(
                    CollectedResearchSource(
                        key=f"tavily-{sha256(normalized_locator.encode()).hexdigest()[:20]}",
                        source=SourceIdentity(
                            kind=SourceKind.WEB,
                            provider="tavily-search-v1",
                            canonical_locator=normalized_locator,
                        ),
                        normalized_document=document,
                        media_type="text/markdown",
                        representation="normalized-tavily-raw-content-v1",
                        parser_revision="tavily-search-v1",
                        observed_at=observed_at,
                        coverage_source_kinds=("evidence",),
                    )
                )
            candidates_by_query.append(tuple(query_candidates))

        sources = _fairly_bounded_sources(
            candidates_by_query,
            max_results=max_results,
        )
        if not sources and recoverable_failures:
            # Preserve the existing typed failure path when every collection
            # attempt was unavailable; the executor will record a safe reason
            # instead of asking the model to research without sources.
            raise recoverable_failures[0]
        failures = tuple(
            sorted(
                {
                    (item.coverage_key, item.reason_code): item
                    for item in collection_failures
                }.values(),
                key=lambda item: (item.coverage_key, item.reason_code),
            )
        )
        return tuple(
            source.model_copy(update={"collection_failures": failures}) for source in sources
        )

    async def _search(
        self,
        query: str,
        *,
        max_results: int | None,
        search_depth: str,
    ) -> dict[str, list[object]]:
        try:
            payload: dict[str, object] = {
                "query": query,
                "search_depth": search_depth,
                "include_answer": False,
                "include_raw_content": "markdown",
                "auto_parameters": False,
            }
            if max_results is not None:
                payload["max_results"] = max_results
            response = await self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise TavilySourceCollectionError(
                _tavily_http_failure_code(error.response.status_code)
            ) from error
        except httpx.RequestError as error:
            raise TavilySourceCollectionError("tavily_network_failure") from error
        try:
            body = response.json()
        except ValueError as error:
            raise TavilySourceCollectionError("tavily_response_invalid") from error
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise TavilySourceCollectionError("tavily_response_schema_invalid")
        return {"results": body["results"]}


_COVERAGE_LINE = re.compile(r"^[a-z][a-z0-9_.-]{0,159}:\s*(.+)$")
_SOURCE_STRATEGY_LINE = re.compile(
    r"^Source strategy:\s*(primary|independent|official|mixed)\s*$",
    re.IGNORECASE,
)
_RESEARCH_LENSES_LINE = re.compile(r"^Research lenses:\s*(.+)$", re.IGNORECASE)

_SOURCE_STRATEGY_GUIDANCE = {
    "primary": "Prefer primary technical documentation when it is available.",
    "official": "Prefer official manufacturer, standards-body, or public-authority documentation.",
    "independent": (
        "Prefer an independent test, review, or academic source; do not rely only on vendor claims."
    ),
    "mixed": (
        "Seek both official primary documentation and independent validation "
        "where the source budget permits."
    ),
}


def _coverage_search_queries(
    question: str, *, limit: int | None
) -> tuple[tuple[str | None, str], ...]:
    """Derive deterministic search queries from Coverage Key lines.

    A task question carries a stable project/module prefix followed by
    ``coverage_key: question`` lines.  Searching each coverage question avoids
    one broad query being dominated by the first mentioned concern.  Generic
    questions retain the legacy one-request behavior.
    """

    lines = tuple(line.strip() for line in question.splitlines() if line.strip())
    coverage_questions = [
        (match.group(0).split(":", maxsplit=1)[0], match.group(1).strip())
        for line in lines
        if (match := _COVERAGE_LINE.match(line))
    ]
    if not coverage_questions:
        return ((None, question),)
    context = tuple(
        line
        for line in lines
        if not _COVERAGE_LINE.match(line) and not _RESEARCH_LENSES_LINE.match(line)
    )
    source_strategy = next(
        (
            matched.group(1).lower()
            for line in context
            if (matched := _SOURCE_STRATEGY_LINE.match(line)) is not None
        ),
        None,
    )
    if source_strategy is not None:
        context = (*context, _SOURCE_STRATEGY_GUIDANCE[source_strategy])
    research_lenses = _research_lenses(lines)
    if research_lenses:
        # Interleave lens rounds rather than expanding the first Coverage Key
        # completely, preserving Coverage-Key fairness under a bounded budget.
        queries = tuple(
            (
                coverage_key,
                "\n".join(
                    (*context, f"Research angle: {lens}", coverage_question)
                ),
            )
            for lens in research_lenses
            for coverage_key, coverage_question in coverage_questions
        )
    else:
        queries = tuple(
            (coverage_key, "\n".join((*context, coverage_question)))
            for coverage_key, coverage_question in coverage_questions
        )
    unique: list[tuple[str, str]] = []
    seen_queries: set[str] = set()
    for coverage_key, query in queries:
        if query in seen_queries:
            continue
        seen_queries.add(query)
        unique.append((coverage_key, query))
    return tuple(unique) if limit is None else tuple(unique)[:limit]


def _research_lenses(lines: tuple[str, ...]) -> tuple[str, ...]:
    """Read all approved strategy lenses from a frozen task question."""

    raw = next(
        (
            matched.group(1)
            for line in lines
            if (matched := _RESEARCH_LENSES_LINE.match(line)) is not None
        ),
        "",
    )
    lenses: list[str] = []
    seen: set[str] = set()
    for value in raw.split("|"):
        lens = value.strip()
        key = lens.casefold()
        if not lens or key in seen:
            continue
        seen.add(key)
        lenses.append(lens)
    return tuple(lenses)


def _fairly_bounded_sources(
    candidates_by_query: list[tuple[CollectedResearchSource, ...]],
    *,
    max_results: int | None,
) -> tuple[CollectedResearchSource, ...]:
    """Give each coverage query one unique source before spending spare capacity.

    Tavily returns ranked results per query, while ``max_results`` is a task-wide
    source budget.  A greedy append would let the first coverage question spend
    that entire budget.  This two-pass selection first allocates at most one
    document to every query, then round-robins the spare capacity. Within each
    allocation it prefers the earliest candidate from a new URL origin; this
    prevents a second page from one site crowding out an available second site.
    If no new origin remains, the earliest unique locator is retained rather
    than fabricating diversity or discarding useful evidence.
    """

    selected: list[CollectedResearchSource] = []
    seen_locators: set[str] = set()
    seen_origins: set[str] = set()
    remaining_by_query = [list(candidates) for candidates in candidates_by_query]

    def take_next_unique(query_index: int) -> bool:
        candidates = remaining_by_query[query_index]

        # Select the highest-ranked not-yet-selected locator on a new origin.
        for position, candidate in enumerate(candidates):
            locator = candidate.source.canonical_locator
            if locator in seen_locators or source_origin_key(candidate.source) in seen_origins:
                continue
            candidates.pop(position)
            seen_locators.add(locator)
            seen_origins.add(source_origin_key(candidate.source))
            selected.append(candidate)
            return True

        # Diversity is a preference, not a replacement for evidence. When no
        # different origin is available, keep the highest-ranked new page.
        for position, candidate in enumerate(candidates):
            locator = candidate.source.canonical_locator
            if locator in seen_locators:
                continue
            candidates.pop(position)
            seen_locators.add(locator)
            seen_origins.add(source_origin_key(candidate.source))
            selected.append(candidate)
            return True
        return False

    # Fair pass: no query can consume a second slot until every query had one.
    for query_index in range(len(candidates_by_query)):
        if max_results is not None and len(selected) >= max_results:
            return tuple(selected)
        take_next_unique(query_index)

    # Supplement pass: retain query-local ranking, but share the spare budget.
    while max_results is None or len(selected) < max_results:
        selected_in_round = False
        for query_index in range(len(candidates_by_query)):
            if max_results is not None and len(selected) >= max_results:
                return tuple(selected)
            selected_in_round = take_next_unique(query_index) or selected_in_round
        if not selected_in_round:
            break
    return tuple(selected)


def _narrowest_configured_limit(*values: int | None) -> int | None:
    configured = tuple(value for value in values if value is not None)
    return min(configured) if configured else None


def _normalized_https_locator(value: str) -> str | None:
    """Keep provenance URLs bounded without initiating a second network read."""

    try:
        parsed = urlsplit(value)
        port = parsed.port or 443
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or port != 443
        or parsed.username
        or parsed.password
    ):
        return None
    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    return urlunsplit(("https", hostname, parsed.path or "/", parsed.query, ""))


def _tavily_http_failure_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "tavily_authentication_failed"
    if status_code == 429:
        return "tavily_quota_exhausted"
    if status_code >= 500:
        return "tavily_provider_unavailable"
    return "tavily_request_rejected"


def _is_recoverable_query_failure(reason_code: str) -> bool:
    """Whether one failed Coverage Key may defer to the gap loop safely."""

    return reason_code in {"tavily_network_failure", "tavily_provider_unavailable"}


__all__ = [
    "CollectedResearchSource",
    "NoopResearchSourceCollector",
    "ResearchSourceCollectionError",
    "ResearchSourceCollector",
    "TavilySearchResearchSourceCollector",
    "TavilySourceCollectionError",
]
