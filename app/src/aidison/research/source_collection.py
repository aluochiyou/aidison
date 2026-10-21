"""Trusted source-document input boundary for Research tasks.

Collectors may retrieve from a provider, repository or user-controlled source,
but models never manufacture this input.  The application persists the exact
document before a model can cite it, then Evidence Admission revalidates every
quoted span proposed by the model.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aidison.infrastructure.project_documents import (
    ProjectSourceDocumentIntegrityError,
    ProjectSourceDocumentStore,
)
from aidison.research.evidence_diagnostics import ResearchSourceCollectionFailure
from aidison.research.langgraph_contracts import TaskEnvelope
from aidison.research.project_documents import ProjectSourceDocument
from aidison.research.source_observations import SourceIdentity, SourceKind, source_origin_key
from aidison.runtime.agent_runs import AgentRun
from aidison.tools.dispatcher import AuthorizedToolInvocation
from aidison.tools.execution import (
    ToolAdapterCallable,
    ToolAdapterFailure,
    ToolAdapterResponse,
    ToolExecutionFailure,
    ToolExecutionReplayUnavailable,
    ToolExecutionResult,
)
from aidison.tools.source_catalog import (
    FIRECRAWL_READ_TOOL_ID,
    FIRECRAWL_SEARCH_TOOL_ID,
    GITHUB_CONTENTS_TOOL_ID,
    PROJECT_DOCUMENT_READ_TOOL_ID,
    PROJECT_FILE_READ_TOOL_ID,
    TAVILY_SEARCH_TOOL_ID,
)


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


class ResearchSourceToolRuntime(Protocol):
    """Task-bound durable execution seam for one physical source read."""

    async def execute(
        self,
        *,
        tool_id: str,
        arguments: dict[str, Any],
        purpose_code: str,
        provider: str,
        target: str,
        invoke: ToolAdapterCallable,
    ) -> ToolExecutionResult: ...


class ResearchSourceCollector(Protocol):
    """Physical read-only source collection seam; authority remains outside models."""

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
        tool_runtime: ResearchSourceToolRuntime | None = None,
    ) -> tuple[CollectedResearchSource, ...]: ...


class NoopResearchSourceCollector:
    """Safe default until a deployment configures an authorized source adapter."""

    tool_ids: tuple[str, ...] = ()

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
        tool_runtime: ResearchSourceToolRuntime | None = None,
    ) -> tuple[CollectedResearchSource, ...]:
        del run, task, question, tool_runtime
        return ()


class ResearchSourceCollectionError(RuntimeError):
    """A typed failure at a trusted source-collection boundary."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"trusted research source collection failed: {reason_code}")


class TavilySourceCollectionError(ResearchSourceCollectionError):
    """A classified failure at the trusted Tavily source boundary."""


class GitHubSourceCollectionError(ResearchSourceCollectionError):
    """A classified failure at the trusted GitHub Contents API boundary."""


class LocalFileSourceCollectionError(ResearchSourceCollectionError):
    """A classified failure at the explicit local-source read boundary."""


class ProjectDocumentSourceCollectionError(ResearchSourceCollectionError):
    """A classified failure while reading user-uploaded project source material."""


class FirecrawlSourceCollectionError(ResearchSourceCollectionError):
    """A classified failure at the Firecrawl v2 scrape boundary."""


class ProjectDocumentResearchSourceCollector:
    """Expose active project documents as immutable, run-snapshotted input.

    A document is selected only by the durable Project scope; neither an LLM
    nor a task prompt can name a file path or read arbitrary host data. The
    normal Research source snapshot is still written before model execution,
    so later edits or quarantining do not rewrite an already-started Run.
    """

    tool_ids = (PROJECT_DOCUMENT_READ_TOOL_ID,)

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_root: Path,
        max_documents: int = 8,
        max_candidates: int = 32,
    ) -> None:
        if not 1 <= max_documents <= 32:
            raise ValueError("project source document limit must be between 1 and 32")
        if not max_documents <= max_candidates <= 64:
            raise ValueError("project source candidate limit must be between document limit and 64")
        self._session_factory = session_factory
        self._artifact_root = artifact_root
        self._max_documents = max_documents
        self._max_candidates = max_candidates

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
        tool_runtime: ResearchSourceToolRuntime | None = None,
    ) -> tuple[CollectedResearchSource, ...]:
        try:
            async with self._session_factory() as session:
                documents = ProjectSourceDocumentStore(session, self._artifact_root)
                try:
                    active = await documents.list_active(
                        project_id=run.project_id, limit=self._max_candidates
                    )
                    sources: list[tuple[ProjectSourceDocument, CollectedResearchSource]] = []
                    observed_at = datetime.now(UTC)
                    for document in active:
                        async def read_document(
                            _invocation: AuthorizedToolInvocation,
                            *,
                            selected: ProjectSourceDocument = document,
                        ) -> ToolAdapterResponse:
                            try:
                                _, content = await documents.read_text(
                                    project_id=run.project_id,
                                    document_id=selected.id,
                                )
                            except ProjectSourceDocumentIntegrityError as error:
                                await session.commit()
                                raise ToolAdapterFailure(
                                    "project_document_unavailable"
                                ) from error
                            return ToolAdapterResponse(value={"content": content})

                        if tool_runtime is None:
                            _, content = await documents.read_text(
                                project_id=run.project_id, document_id=document.id
                            )
                        else:
                            try:
                                executed = await tool_runtime.execute(
                                    tool_id=PROJECT_DOCUMENT_READ_TOOL_ID,
                                    arguments={
                                        "document_id": document.id,
                                        "content_hash": document.content_hash,
                                    },
                                    purpose_code="collect_project_evidence",
                                    provider="aidison-project-document",
                                    target=str(document.id),
                                    invoke=read_document,
                                )
                            except ToolExecutionFailure as error:
                                raise ProjectDocumentSourceCollectionError(
                                    error.reason_code
                                ) from error
                            except ToolExecutionReplayUnavailable as error:
                                raise ProjectDocumentSourceCollectionError(
                                    "project_document_replay_unavailable"
                                ) from error
                            value = executed.value
                            if not isinstance(value, dict) or not isinstance(
                                value.get("content"), str
                            ):
                                raise ProjectDocumentSourceCollectionError(
                                    "project_document_response_schema_invalid"
                                )
                            content = value["content"]
                        source = CollectedResearchSource(
                            key=f"project-source-{document.id.hex}",
                            source=SourceIdentity(
                                kind=SourceKind.USER_UPLOAD,
                                provider="aidison-project-document-v1",
                                canonical_locator=(
                                    "aidison://project-source/"
                                    f"{document.id}/{document.content_hash}"
                                ),
                            ),
                            normalized_document=content,
                            media_type=document.media_type,
                            representation="normalized-project-document-text-v1",
                            parser_revision="project-document-upload-v1",
                            observed_at=observed_at,
                            coverage_source_kinds=(SourceKind.USER_UPLOAD.value,),
                        )
                        sources.append((document, source))
                    task_policy = getattr(task, "collection_policy", None)
                    policy_limit = (
                        task_policy.max_documents_total if task_policy is not None else None
                    )
                    limit = min(
                        self._max_documents,
                        policy_limit if policy_limit is not None else self._max_documents,
                    )
                    return _rank_project_document_sources(
                        sources,
                        question=question,
                        limit=limit,
                    )
                except ProjectSourceDocumentIntegrityError:
                    # Persist the missing/corrupt lifecycle transition before
                    # this Run fails closed; otherwise session close rolls it
                    # back and a later run sees an apparently active document.
                    await session.commit()
                    raise
        except ProjectSourceDocumentIntegrityError as error:
            raise ProjectDocumentSourceCollectionError("project_document_unavailable") from error


def _rank_project_document_sources(
    sources: Sequence[tuple[ProjectSourceDocument, CollectedResearchSource]],
    *,
    question: str,
    limit: int,
) -> tuple[CollectedResearchSource, ...]:
    """Use a deterministic lexical pre-ranker before the frozen source budget.

    Project uploads are trusted *inputs*, not automatically relevant context.
    This deliberately small ranker avoids a second model call and makes the
    exact selection reproducible from the frozen task question and bytes. A
    later vector retrieval layer can replace this seam without changing the
    source snapshot or Evidence Admission contracts.
    """

    if limit < 1:
        return ()
    tokens = tuple(
        sorted(
            set(
                re.findall(
                    r"[a-z0-9][a-z0-9_.+-]{1,}|[\u4e00-\u9fff]",
                    question.casefold(),
                )
            )
        )
    )

    def score(source: CollectedResearchSource) -> int:
        document = source.normalized_document.casefold()
        return sum(min(document.count(token), 3) for token in tokens)

    ranked = sorted(
        sources,
        key=lambda item: (
            -score(item[1]),
            item[1].source.canonical_locator,
        ),
    )
    return tuple(source for _, source in ranked[:limit])


class LocalFileResearchSourceCollector:
    """Read text files from one configured directory and exact relative allowlist.

    This is a local-first input adapter, not a host filesystem tool. The model
    never supplies a path: every target is deployment configuration, resolved
    below ``source_root`` and checked again immediately before every read.
    """

    tool_ids = (PROJECT_FILE_READ_TOOL_ID,)

    def __init__(
        self,
        *,
        source_root: Path,
        source_targets: Sequence[str],
        max_document_characters: int = 32_000,
    ) -> None:
        if not source_targets:
            raise ValueError("local source targets must not be empty")
        if not 1 <= max_document_characters <= 96_000:
            raise ValueError("local source document limit must be between 1 and 96000")
        root = source_root.resolve()
        if not root.is_dir():
            raise ValueError("local source root must be an existing directory")
        self._root = root
        self._targets = tuple(self._parse_target(value) for value in source_targets)
        if len(set(self._targets)) != len(self._targets):
            raise ValueError("local source targets must be unique")
        self._max_document_characters = max_document_characters
        self._max_document_bytes = max_document_characters * 4

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
        tool_runtime: ResearchSourceToolRuntime | None = None,
    ) -> tuple[CollectedResearchSource, ...]:
        del run, task, question
        observed_at = datetime.now(UTC)
        sources: list[CollectedResearchSource] = []
        for target in self._targets:
            if tool_runtime is None:
                source = await self._read_target(target=target, observed_at=observed_at)
            else:
                async def read_target(
                    _invocation: AuthorizedToolInvocation,
                    *,
                    selected: str = target,
                ) -> ToolAdapterResponse:
                    try:
                        item = await self._read_target(
                            target=selected,
                            observed_at=observed_at,
                        )
                    except LocalFileSourceCollectionError as error:
                        raise ToolAdapterFailure(error.reason_code) from error
                    return ToolAdapterResponse(value=item.model_dump(mode="json"))

                try:
                    executed = await tool_runtime.execute(
                        tool_id=PROJECT_FILE_READ_TOOL_ID,
                        arguments={"target": target},
                        purpose_code="collect_project_evidence",
                        provider="aidison-project-file",
                        target=target,
                        invoke=read_target,
                    )
                except ToolExecutionFailure as error:
                    raise LocalFileSourceCollectionError(error.reason_code) from error
                except ToolExecutionReplayUnavailable as error:
                    raise LocalFileSourceCollectionError(
                        "local_source_replay_unavailable"
                    ) from error
                try:
                    source = CollectedResearchSource.model_validate(executed.value)
                except ValueError as error:
                    raise LocalFileSourceCollectionError(
                        "local_source_response_schema_invalid"
                    ) from error
            sources.append(source)
        return tuple(sources)

    @staticmethod
    def _parse_target(value: str) -> str:
        target = value.strip().replace("\\", "/")
        if not target or target.startswith("/") or "//" in target:
            raise ValueError("local source target must be a non-empty relative file path")
        parts = target.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("local source target path must stay inside its source root")
        return target

    async def _read_target(
        self,
        *,
        target: str,
        observed_at: datetime,
    ) -> CollectedResearchSource:
        path = (self._root / target).resolve()
        if self._root not in path.parents or not path.is_file():
            raise LocalFileSourceCollectionError("local_source_not_found")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise LocalFileSourceCollectionError("local_source_read_failed") from error
        if size > self._max_document_bytes:
            raise LocalFileSourceCollectionError("local_document_too_large")
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except OSError as error:
            raise LocalFileSourceCollectionError("local_source_read_failed") from error
        if len(content) > self._max_document_bytes:
            raise LocalFileSourceCollectionError("local_document_too_large")
        try:
            document = content.decode("utf-8").strip()[: self._max_document_characters]
        except UnicodeDecodeError as error:
            raise LocalFileSourceCollectionError("local_source_invalid_text") from error
        if not document:
            raise LocalFileSourceCollectionError("local_source_invalid_text")
        canonical_locator = f"aidison://project-file/{quote(target, safe='/._-')}"
        return CollectedResearchSource(
            key=f"local-{sha256(canonical_locator.encode()).hexdigest()[:20]}",
            source=SourceIdentity(
                kind=SourceKind.PROJECT_FILE,
                provider="local-file-v1",
                canonical_locator=canonical_locator,
            ),
            normalized_document=document,
            media_type=_document_media_type(target),
            representation="normalized-local-file-v1",
            parser_revision="local-file-v1",
            observed_at=observed_at,
            coverage_source_kinds=("evidence",),
        )


class GitHubRepositorySourceCollector:
    """Read explicitly allowlisted text files through GitHub's Contents API.

    A source target has the form ``owner/repository@ref:path/to/file``.  The
    configured ref is only an input locator; the returned Git blob SHA becomes
    the canonical source locator, so a moving branch cannot silently rewrite a
    previously observed source.  This collector deliberately does not expose
    GitHub search: model-produced query text must not widen the configured
    repository read authority.
    """

    tool_ids = (GITHUB_CONTENTS_TOOL_ID,)

    _target_pattern = re.compile(
        r"^(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@"
        r"(?P<ref>[A-Za-z0-9_.:/-]+):(?P<path>[A-Za-z0-9_.+/@-]+)$"
    )
    _endpoint = "https://api.github.com/repos/{repository}/contents/{path}"

    def __init__(
        self,
        *,
        api_token: str,
        source_targets: Sequence[str],
        client: httpx.AsyncClient,
        max_document_characters: int = 32_000,
    ) -> None:
        if not api_token.strip():
            raise ValueError("GitHub API token must not be empty")
        if not source_targets:
            raise ValueError("GitHub source targets must not be empty")
        if not 1 <= max_document_characters <= 96_000:
            raise ValueError("GitHub source document limit must be between 1 and 96000")
        self._api_token = api_token
        self._targets = tuple(self._parse_target(value) for value in source_targets)
        if len(set(self._targets)) != len(self._targets):
            raise ValueError("GitHub source targets must be unique")
        self._client = client
        self._max_document_characters = max_document_characters
        # UTF-8 can consume four bytes per Unicode code point. Keep a bounded
        # allowance for normalization while rejecting oversized Base64 before
        # decoding it into process memory.
        self._max_document_bytes = max_document_characters * 4

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
        tool_runtime: ResearchSourceToolRuntime | None = None,
    ) -> tuple[CollectedResearchSource, ...]:
        del run, task, question
        observed_at = datetime.now(UTC)
        sources: list[CollectedResearchSource] = []
        for target in self._targets:
            if tool_runtime is None:
                source = await self._fetch_target(target=target, observed_at=observed_at)
            else:
                repository, ref, path = target

                async def fetch_target(
                    _invocation: AuthorizedToolInvocation,
                    *,
                    selected: tuple[str, str, str] = target,
                ) -> ToolAdapterResponse:
                    try:
                        item = await self._fetch_target(
                            target=selected,
                            observed_at=observed_at,
                        )
                    except GitHubSourceCollectionError as error:
                        if error.reason_code == "github_network_failure":
                            raise
                        raise ToolAdapterFailure(error.reason_code) from error
                    return ToolAdapterResponse(value=item.model_dump(mode="json"))

                try:
                    executed = await tool_runtime.execute(
                        tool_id=GITHUB_CONTENTS_TOOL_ID,
                        arguments={
                            "repository": repository,
                            "ref": ref,
                            "path": path,
                        },
                        purpose_code="collect_repository_evidence",
                        provider="github-contents",
                        target=f"{repository}@{ref}:{path}",
                        invoke=fetch_target,
                    )
                except ToolExecutionFailure as error:
                    raise GitHubSourceCollectionError(error.reason_code) from error
                except ToolExecutionReplayUnavailable as error:
                    raise GitHubSourceCollectionError(
                        "github_replay_unavailable"
                    ) from error
                try:
                    source = CollectedResearchSource.model_validate(executed.value)
                except ValueError as error:
                    raise GitHubSourceCollectionError(
                        "github_response_schema_invalid"
                    ) from error
            sources.append(source)
        return tuple(sources)

    @classmethod
    def _parse_target(cls, value: str) -> tuple[str, str, str]:
        match = cls._target_pattern.fullmatch(value.strip())
        if match is None:
            raise ValueError(
                "GitHub source target must be owner/repository@ref:path/to/file"
            )
        repository = match.group("repository")
        ref = match.group("ref")
        path = match.group("path")
        if path.startswith("/") or "//" in path or any(part == ".." for part in path.split("/")):
            raise ValueError("GitHub source target path must stay inside its repository")
        return repository, ref, path

    async def _fetch_target(
        self,
        *,
        target: tuple[str, str, str],
        observed_at: datetime,
    ) -> CollectedResearchSource:
        repository, ref, path = target
        try:
            response = await self._client.get(
                self._endpoint.format(repository=repository, path=path),
                params={"ref": ref},
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._api_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise GitHubSourceCollectionError(
                _github_http_failure_code(error.response)
            ) from error
        except httpx.RequestError as error:
            raise GitHubSourceCollectionError("github_network_failure") from error
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("GitHub response is not an object")
            if body.get("type") != "file":
                raise ValueError("GitHub source target is not a file")
            blob_sha = body.get("sha")
            encoded = body.get("content")
            encoding = body.get("encoding")
            size = body.get("size")
            if not isinstance(blob_sha, str) or not re.fullmatch(r"[a-f0-9]{40,64}", blob_sha):
                raise ValueError("GitHub file response has no valid blob SHA")
            if not isinstance(encoded, str) or encoding != "base64":
                raise ValueError("GitHub file response has no base64 text content")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError("GitHub file response has no valid byte size")
            if size > self._max_document_bytes:
                raise GitHubSourceCollectionError("github_document_too_large")
            # GitHub wraps large base64 payloads across lines. Whitespace is
            # transport formatting, not document content, so remove it before
            # retaining strict alphabet validation.
            compact_encoded = "".join(encoded.split())
            maximum_encoded_bytes = 4 * ((self._max_document_bytes + 2) // 3)
            if len(compact_encoded) > maximum_encoded_bytes:
                raise GitHubSourceCollectionError("github_document_too_large")
            decoded = base64.b64decode(compact_encoded, validate=True)
            if len(decoded) > self._max_document_bytes:
                raise GitHubSourceCollectionError("github_document_too_large")
            document = decoded.decode("utf-8").strip()[: self._max_document_characters]
            if not document:
                raise ValueError("GitHub source file has no usable UTF-8 text")
        except GitHubSourceCollectionError:
            raise
        except (ValueError, UnicodeDecodeError, binascii.Error) as error:
            raise GitHubSourceCollectionError("github_response_schema_invalid") from error

        canonical_locator = f"https://github.com/{repository}/blob/{blob_sha}/{path}"
        return CollectedResearchSource(
            key=f"github-{sha256(canonical_locator.encode()).hexdigest()[:20]}",
            source=SourceIdentity(
                kind=SourceKind.REPOSITORY,
                provider="github-contents-v1",
                canonical_locator=canonical_locator,
            ),
            normalized_document=document,
            media_type=_document_media_type(path),
            representation="normalized-github-contents-v1",
            parser_revision="github-contents-v1",
            observed_at=observed_at,
            coverage_source_kinds=("evidence",),
        )


class CompositeResearchSourceCollector:
    """Combine readers without hiding a recoverable configured-source outage."""

    def __init__(self, collectors: Sequence[ResearchSourceCollector]) -> None:
        if not collectors:
            raise ValueError("composite source collector needs at least one collector")
        self._collectors = tuple(collectors)

    @property
    def tool_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    tool_id
                    for collector in self._collectors
                    for tool_id in getattr(collector, "tool_ids", ())
                }
            )
        )

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
        tool_runtime: ResearchSourceToolRuntime | None = None,
    ) -> tuple[CollectedResearchSource, ...]:
        collected_by_collector: list[list[CollectedResearchSource]] = []
        seen_source_ids: set[str] = set()
        recoverable_failures: list[ResearchSourceCollectionError] = []
        for collector in self._collectors:
            try:
                if tool_runtime is not None and source_tool_ids(collector):
                    sources = await collector.collect(
                        run=run,
                        task=task,
                        question=question,
                        tool_runtime=tool_runtime,
                    )
                else:
                    sources = await collector.collect(
                        run=run,
                        task=task,
                        question=question,
                    )
            except ResearchSourceCollectionError as error:
                if not is_recoverable_source_collection_failure(error.reason_code):
                    raise
                recoverable_failures.append(error)
                continue
            collector_sources: list[CollectedResearchSource] = []
            for source in sources:
                source_id = source.source.id
                assert source_id is not None
                if source_id in seen_source_ids:
                    continue
                seen_source_ids.add(source_id)
                collector_sources.append(source)
            if collector_sources:
                collected_by_collector.append(collector_sources)
        collected = [source for group in collected_by_collector for source in group]
        if not collected and recoverable_failures:
            raise recoverable_failures[0]
        if collected and recoverable_failures:
            coverage_keys = tuple(sorted(set(getattr(task, "coverage_keys", ()))))
            visible_failures = tuple(
                ResearchSourceCollectionFailure(
                    coverage_key=coverage_key,
                    reason_code=error.reason_code,
                )
                for coverage_key in coverage_keys
                for error in recoverable_failures
            )
            collected_by_collector = [
                [
                    source.model_copy(
                        update={
                            "collection_failures": tuple(
                                sorted(
                                    {
                                        (failure.coverage_key, failure.reason_code): failure
                                        for failure in (
                                            *source.collection_failures,
                                            *visible_failures,
                                        )
                                    }.values(),
                                    key=lambda failure: (
                                        failure.coverage_key,
                                        failure.reason_code,
                                    ),
                                )
                            )
                        }
                    )
                    for source in group
                ]
                for group in collected_by_collector
            ]
        collection_policy = getattr(task, "collection_policy", None)
        max_documents = (
            collection_policy.max_documents_total
            if collection_policy is not None
            else None
        )
        if max_documents is None:
            return tuple(source for group in collected_by_collector for source in group)
        return _fairly_bounded_collector_sources(
            collected_by_collector,
            max_documents=max_documents,
        )


def _fairly_bounded_collector_sources(
    collected_by_collector: Sequence[Sequence[CollectedResearchSource]],
    *,
    max_documents: int,
) -> tuple[CollectedResearchSource, ...]:
    """Allocate a task's shared source budget across enabled source adapters.

    Each collector keeps its own relevance/ranking policy. The composite only
    prevents static local or repository inputs from consuming the entire task
    budget before an independent enabled source collector receives one slot.
    """

    remaining = [list(group) for group in collected_by_collector]
    selected: list[CollectedResearchSource] = []
    while len(selected) < max_documents:
        made_progress = False
        for group in remaining:
            if not group or len(selected) >= max_documents:
                continue
            selected.append(group.pop(0))
            made_progress = True
        if not made_progress:
            break
    return tuple(selected)


class FirecrawlWebReader:
    """Read one public HTTPS page through Firecrawl v2 as a bounded fallback.

    Discovery remains Tavily's responsibility.  This adapter is invoked only
    when an already-discovered result has no usable raw body, so enabling it
    does not duplicate every search result or silently broaden task scope.
    """

    tool_ids = (FIRECRAWL_READ_TOOL_ID,)
    _endpoint = "https://api.firecrawl.dev/v2/scrape"

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient,
        max_document_characters: int = 32_000,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Firecrawl API key must not be empty")
        if not 1 <= max_document_characters <= 96_000:
            raise ValueError("Firecrawl document limit must be between 1 and 96000")
        self._api_key = api_key
        self._client = client
        self._max_document_characters = max_document_characters

    async def read(
        self,
        *,
        url: str,
        observed_at: datetime,
        tool_runtime: ResearchSourceToolRuntime | None,
    ) -> CollectedResearchSource:
        normalized_url = _public_https_locator(url)
        if normalized_url is None:
            raise FirecrawlSourceCollectionError("firecrawl_url_not_public_https")

        if tool_runtime is None:
            value, _ = await self._scrape_direct(normalized_url)
        else:
            async def invoke(
                _invocation: AuthorizedToolInvocation,
            ) -> ToolAdapterResponse:
                try:
                    value, provider_request_id = await self._scrape_direct(normalized_url)
                except FirecrawlSourceCollectionError as error:
                    if error.reason_code == "firecrawl_network_failure":
                        raise
                    raise ToolAdapterFailure(error.reason_code) from error
                return ToolAdapterResponse(
                    value=value,
                    provider_request_id=provider_request_id,
                )

            try:
                executed = await tool_runtime.execute(
                    tool_id=FIRECRAWL_READ_TOOL_ID,
                    arguments={
                        "url": normalized_url,
                        "max_characters": self._max_document_characters,
                    },
                    purpose_code="read_complex_web_evidence",
                    provider="firecrawl",
                    target="scrape-v2",
                    invoke=invoke,
                )
            except ToolExecutionFailure as error:
                raise FirecrawlSourceCollectionError(error.reason_code) from error
            except ToolExecutionReplayUnavailable as error:
                raise FirecrawlSourceCollectionError(
                    "firecrawl_replay_unavailable"
                ) from error
            value = executed.value

        markdown = value.get("markdown") if isinstance(value, dict) else None
        if not isinstance(markdown, str):
            raise FirecrawlSourceCollectionError("firecrawl_response_schema_invalid")
        document = markdown.strip()[: self._max_document_characters]
        if not document:
            raise FirecrawlSourceCollectionError("firecrawl_document_empty")
        return CollectedResearchSource(
            key=f"firecrawl-{sha256(normalized_url.encode()).hexdigest()[:20]}",
            source=SourceIdentity(
                kind=SourceKind.WEB,
                provider="firecrawl-scrape-v2",
                canonical_locator=normalized_url,
            ),
            normalized_document=document,
            media_type="text/markdown",
            representation="normalized-firecrawl-markdown-v2",
            parser_revision="firecrawl-scrape-v2",
            observed_at=observed_at,
            coverage_source_kinds=("evidence",),
        )

    async def _scrape_direct(self, url: str) -> tuple[dict[str, object], str | None]:
        try:
            response = await self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "url": url,
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "removeBase64Images": True,
                    "parsers": ["pdf"],
                    "zeroDataRetention": True,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise FirecrawlSourceCollectionError(
                _firecrawl_http_failure_code(error.response.status_code)
            ) from error
        except httpx.RequestError as error:
            raise FirecrawlSourceCollectionError("firecrawl_network_failure") from error
        try:
            body = response.json()
        except ValueError as error:
            raise FirecrawlSourceCollectionError("firecrawl_response_invalid") from error
        if not isinstance(body, dict) or body.get("success") is not True:
            raise FirecrawlSourceCollectionError("firecrawl_response_schema_invalid")
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("markdown"), str):
            raise FirecrawlSourceCollectionError("firecrawl_response_schema_invalid")
        provider_request_id = response.headers.get("x-request-id")
        metadata = data.get("metadata")
        if provider_request_id is None and isinstance(metadata, dict):
            scrape_id = metadata.get("scrapeId")
            provider_request_id = scrape_id if isinstance(scrape_id, str) else None
        return {"markdown": data["markdown"]}, provider_request_id


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
        fallback_reader: FirecrawlWebReader | None = None,
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
        self._fallback_reader = fallback_reader

    @property
    def tool_ids(self) -> tuple[str, ...]:
        configured = {TAVILY_SEARCH_TOOL_ID}
        if self._fallback_reader is not None:
            configured.update(self._fallback_reader.tool_ids)
        return tuple(sorted(configured))

    async def collect(
        self,
        *,
        run: AgentRun,
        task: TaskEnvelope,
        question: str,
        tool_runtime: ResearchSourceToolRuntime | None = None,
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
                    tool_runtime=tool_runtime,
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
                if not isinstance(locator, str):
                    continue
                normalized_locator = _normalized_https_locator(locator)
                if normalized_locator is None or normalized_locator in seen_query_locators:
                    continue
                document = (
                    raw_content.strip()[: self._max_document_characters]
                    if isinstance(raw_content, str)
                    else ""
                )
                fallback_source: CollectedResearchSource | None = None
                if not document and self._fallback_reader is not None:
                    try:
                        fallback_source = await self._fallback_reader.read(
                            url=normalized_locator,
                            observed_at=observed_at,
                            tool_runtime=tool_runtime,
                        )
                    except FirecrawlSourceCollectionError as error:
                        if not is_recoverable_source_collection_failure(error.reason_code):
                            raise
                        recoverable_failures.append(TavilySourceCollectionError(error.reason_code))
                        if coverage_key is not None:
                            collection_failures.append(
                                ResearchSourceCollectionFailure(
                                    coverage_key=coverage_key,
                                    reason_code=error.reason_code,
                                )
                            )
                        continue
                if not document and fallback_source is None:
                    continue
                seen_query_locators.add(normalized_locator)
                query_candidates.append(
                    fallback_source
                    or CollectedResearchSource(
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
        tool_runtime: ResearchSourceToolRuntime | None = None,
    ) -> dict[str, list[object]]:
        if tool_runtime is None:
            return await self._search_direct(
                query,
                max_results=max_results,
                search_depth=search_depth,
            )

        async def invoke(_invocation: AuthorizedToolInvocation) -> ToolAdapterResponse:
            try:
                value = await self._search_direct(
                    query,
                    max_results=max_results,
                    search_depth=search_depth,
                )
            except TavilySourceCollectionError as error:
                if error.reason_code == "tavily_network_failure":
                    raise
                raise ToolAdapterFailure(error.reason_code) from error
            return ToolAdapterResponse(value=value)

        try:
            executed = await tool_runtime.execute(
                tool_id=TAVILY_SEARCH_TOOL_ID,
                arguments={
                    "query": query,
                    "max_results": max_results or 5,
                    "search_depth": search_depth,
                },
                purpose_code="discover_web_evidence",
                provider="tavily",
                target="search-v1",
                invoke=invoke,
            )
        except ToolExecutionFailure as error:
            raise TavilySourceCollectionError(error.reason_code) from error
        except ToolExecutionReplayUnavailable as error:
            raise TavilySourceCollectionError("tavily_replay_unavailable") from error
        value = executed.value
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            raise TavilySourceCollectionError("tavily_response_schema_invalid")
        return {"results": value["results"]}

    async def _search_direct(
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


class FirecrawlSearchResearchSourceCollector:
    """Collect bounded public evidence through Firecrawl Search v2.

    This explicitly replaces Tavily's discovery role when configured. Firecrawl
    returns rendered markdown with each result, so a task has one metered,
    authorized search operation per coverage question instead of a model-led
    search-then-scrape fan-out.
    """

    tool_ids = (FIRECRAWL_SEARCH_TOOL_ID,)
    _endpoint = "https://api.firecrawl.dev/v2/search"

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
            raise ValueError("Firecrawl API key must not be empty")
        if max_results is not None and max_results < 1:
            raise ValueError("Firecrawl max_results must be positive when configured")
        if max_queries is not None and max_queries < 1:
            raise ValueError("Firecrawl max_queries must be positive when configured")
        if not 1 <= max_document_characters <= 96_000:
            raise ValueError("Firecrawl source document limit must be between 1 and 96000")
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
        tool_runtime: ResearchSourceToolRuntime | None = None,
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
        observed_at = datetime.now(UTC)
        candidates_by_query: list[tuple[CollectedResearchSource, ...]] = []
        recoverable_failures: list[FirecrawlSourceCollectionError] = []
        collection_failures: list[ResearchSourceCollectionFailure] = []
        for coverage_key, query in _coverage_search_queries(question, limit=max_queries):
            try:
                body = await self._search(
                    query,
                    max_results=max_results_per_query,
                    tool_runtime=tool_runtime,
                )
            except FirecrawlSourceCollectionError as error:
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

            candidates: list[CollectedResearchSource] = []
            seen_locators: set[str] = set()
            for result in body["results"]:
                if not isinstance(result, dict):
                    continue
                locator = result.get("url")
                markdown = result.get("markdown")
                if not isinstance(locator, str) or not isinstance(markdown, str):
                    continue
                # Firecrawl Search is an external discovery service, not a
                # trust boundary. Reapply the same public-address rule used
                # by the direct reader before any returned document becomes
                # model-visible evidence.
                normalized_locator = _public_https_locator(locator)
                document = markdown.strip()[: self._max_document_characters]
                if (
                    normalized_locator is None
                    or normalized_locator in seen_locators
                    or not document
                ):
                    continue
                seen_locators.add(normalized_locator)
                candidates.append(
                    CollectedResearchSource(
                        key=f"firecrawl-search-{sha256(normalized_locator.encode()).hexdigest()[:20]}",
                        source=SourceIdentity(
                            kind=SourceKind.WEB,
                            provider="firecrawl-search-v2",
                            canonical_locator=normalized_locator,
                        ),
                        normalized_document=document,
                        media_type="text/markdown",
                        representation="normalized-firecrawl-search-markdown-v2",
                        parser_revision="firecrawl-search-v2",
                        observed_at=observed_at,
                        coverage_source_kinds=("evidence",),
                    )
                )
            candidates_by_query.append(tuple(candidates))

        sources = _fairly_bounded_sources(candidates_by_query, max_results=max_results)
        if not sources and recoverable_failures:
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
        tool_runtime: ResearchSourceToolRuntime | None,
    ) -> dict[str, list[object]]:
        if tool_runtime is None:
            value, _ = await self._search_direct(query, max_results=max_results)
            return value

        async def invoke(_invocation: AuthorizedToolInvocation) -> ToolAdapterResponse:
            try:
                value, provider_request_id = await self._search_direct(
                    query, max_results=max_results
                )
            except FirecrawlSourceCollectionError as error:
                if error.reason_code == "firecrawl_network_failure":
                    raise
                raise ToolAdapterFailure(error.reason_code) from error
            return ToolAdapterResponse(value=value, provider_request_id=provider_request_id)

        try:
            executed = await tool_runtime.execute(
                tool_id=FIRECRAWL_SEARCH_TOOL_ID,
                arguments={"query": query, "max_results": max_results or 5},
                purpose_code="discover_rendered_web_evidence",
                provider="firecrawl",
                target="search-v2",
                invoke=invoke,
            )
        except ToolExecutionFailure as error:
            raise FirecrawlSourceCollectionError(error.reason_code) from error
        except ToolExecutionReplayUnavailable as error:
            raise FirecrawlSourceCollectionError("firecrawl_replay_unavailable") from error
        value = executed.value
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            raise FirecrawlSourceCollectionError("firecrawl_response_schema_invalid")
        return {"results": value["results"]}

    async def _search_direct(
        self, query: str, *, max_results: int | None
    ) -> tuple[dict[str, list[object]], str | None]:
        try:
            response = await self._client.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "query": query,
                    "limit": max_results or 5,
                    "sources": ["web"],
                    "safe": True,
                    "ignoreInvalidURLs": True,
                    "scrapeOptions": {
                        "formats": ["markdown"],
                        "onlyMainContent": True,
                        "removeBase64Images": True,
                    },
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise FirecrawlSourceCollectionError(
                _firecrawl_http_failure_code(error.response.status_code)
            ) from error
        except httpx.RequestError as error:
            raise FirecrawlSourceCollectionError("firecrawl_network_failure") from error
        try:
            body = response.json()
        except ValueError as error:
            raise FirecrawlSourceCollectionError("firecrawl_response_invalid") from error
        if not isinstance(body, dict) or body.get("success") is not True:
            raise FirecrawlSourceCollectionError("firecrawl_response_schema_invalid")
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("web"), list):
            raise FirecrawlSourceCollectionError("firecrawl_response_schema_invalid")
        return {"results": data["web"]}, response.headers.get("x-request-id")


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
# Search providers rank short, domain-specific queries more reliably than a
# pasted execution contract.  The model still receives the full frozen
# contract later; this is only the trusted discovery query.
_MAX_EXTERNAL_SEARCH_QUERY_CHARACTERS = 900
_PEER_CONTEXT_MARKERS = (
    "Admitted upstream research leads (non-evidence",
    "Peer research signals (non-evidence):",
)
_SEARCH_CONTEXT_PREFIXES = (
    "Project objective:",
    "Project goal:",
    "Module:",
    "Task objective:",
    "Hard constraints:",
    "Budget context:",
)


def _coverage_search_queries(
    question: str, *, limit: int | None
) -> tuple[tuple[str | None, str], ...]:
    """Derive deterministic search queries from Coverage Key lines.

    A task question carries a stable project/module prefix followed by
    ``coverage_key: question`` lines.  Searching each coverage question avoids
    one broad query being dominated by the first mentioned concern.  Generic
    questions retain the legacy one-request behavior.
    """

    lines = _searchable_question_lines(question)
    coverage_questions = [
        (match.group(0).split(":", maxsplit=1)[0], match.group(1).strip())
        for line in lines
        if (match := _COVERAGE_LINE.match(line))
    ]
    if not coverage_questions:
        return ((None, _bounded_generic_search_query(lines)),)
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
    source_strategy_guidance = (
        _SOURCE_STRATEGY_GUIDANCE[source_strategy]
        if source_strategy is not None
        else None
    )
    research_lenses = _research_lenses(lines)
    if research_lenses:
        # Interleave lens rounds rather than expanding the first Coverage Key
        # completely, preserving Coverage-Key fairness under a bounded budget.
        queries = tuple(
            (
                coverage_key,
                _bounded_coverage_search_query(
                    context=context,
                    coverage_question=coverage_question,
                    research_lens=lens,
                    source_strategy_guidance=source_strategy_guidance,
                ),
            )
            for lens in research_lenses
            for coverage_key, coverage_question in coverage_questions
        )
    else:
        queries = tuple(
            (
                coverage_key,
                _bounded_coverage_search_query(
                    context=context,
                    coverage_question=coverage_question,
                    source_strategy_guidance=source_strategy_guidance,
                ),
            )
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


def _searchable_question_lines(question: str) -> tuple[str, ...]:
    """Remove collaboration-only material before forming an external query.

    Peer discoveries remain useful model context, but they can contain prior
    summaries and long URLs.  Sending them verbatim to a search provider both
    leaks irrelevant prompt material and can exceed provider query limits.
    """

    lines: list[str] = []
    for raw_line in question.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(_PEER_CONTEXT_MARKERS):
            break
        lines.append(line)
    return tuple(lines)


def _bounded_generic_search_query(lines: tuple[str, ...]) -> str:
    """Keep legacy free-form collection useful without exposing peer context."""

    return _truncate_search_text("\n".join(lines), _MAX_EXTERNAL_SEARCH_QUERY_CHARACTERS)


def _bounded_coverage_search_query(
    *,
    context: tuple[str, ...],
    coverage_question: str,
    source_strategy_guidance: str | None = None,
    research_lens: str | None = None,
) -> str:
    """Compile one small provider query from the frozen task contract.

    The model receives rich task and collaboration context separately.  This
    function is deliberately deterministic: search gets the question first,
    followed by only the few project constraints that improve retrieval.
    """

    # The former implementation sent every task-contract line whenever the
    # whole request happened to fit inside a character cap.  For a drone
    # power task that mixed a generic project objective with several unrelated
    # constraints, and real search returned a transformer encyclopedia page.
    # Compile the same way for every task: the coverage gap first, followed by
    # the small amount of module context that helps retrieval.
    selected_context = _search_context_for_coverage_query(context)
    candidates = (
        (coverage_question, 420),
        (f"Research angle: {research_lens}", 140) if research_lens else None,
        (source_strategy_guidance, 100) if source_strategy_guidance else None,
        *((line, limit) for line, limit in selected_context),
    )
    parts: list[str] = []
    remaining = _MAX_EXTERNAL_SEARCH_QUERY_CHARACTERS
    for candidate in candidates:
        if candidate is None or remaining < 32:
            continue
        text, preferred_limit = candidate
        compact = _truncate_search_text(text, min(preferred_limit, remaining))
        if not compact:
            continue
        parts.append(compact)
        remaining -= len(compact) + 1
    return "\n".join(parts)


def _search_context_for_coverage_query(
    context: tuple[str, ...],
) -> tuple[tuple[str, int], ...]:
    """Select retrieval-relevant contract fragments in deterministic order.

    ``Project objective`` often describes the generic research workflow rather
    than the engineering subject.  It is deliberately excluded in favour of
    the task objective, module and measurable project constraints.
    """

    preferred_prefixes = (
        ("Module:", 120),
        ("Task objective:", 260),
        ("Project goal:", 180),
        ("Hard constraints:", 160),
        ("Budget context:", 100),
    )
    selected: list[tuple[str, int]] = []
    for prefix, limit in preferred_prefixes:
        selected.extend((line, limit) for line in context if line.startswith(prefix))
    return tuple(selected)


def _truncate_search_text(value: str, limit: int) -> str:
    """Bound a provider query on characters without breaking its text framing."""

    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    if limit < 2:
        return normalized[:limit]
    return normalized[: limit - 1].rstrip() + "…"


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


def _public_https_locator(value: str) -> str | None:
    """Reject obvious local/private targets before delegating a remote scrape."""

    normalized = _normalized_https_locator(value)
    if normalized is None:
        return None
    hostname = urlsplit(normalized).hostname
    assert hostname is not None
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return normalized
    if not address.is_global:
        return None
    return normalized


def _tavily_http_failure_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "tavily_authentication_failed"
    if status_code == 429:
        return "tavily_quota_exhausted"
    if status_code >= 500:
        return "tavily_provider_unavailable"
    return "tavily_request_rejected"


def _firecrawl_http_failure_code(status_code: int) -> str:
    if status_code in {401, 403}:
        return "firecrawl_authentication_failed"
    if status_code in {402, 429}:
        return "firecrawl_quota_exhausted"
    if status_code >= 500:
        return "firecrawl_provider_unavailable"
    return "firecrawl_request_rejected"


def _github_http_failure_code(response: httpx.Response) -> str:
    """Classify GitHub's overloaded HTTP 403 without inspecting response prose."""

    status_code = response.status_code
    if status_code in {401, 403}:
        # GitHub documents both forbidden credentials and exhausted primary or
        # secondary limits as 403.  The remaining header is machine-readable;
        # never infer semantics from provider message text.
        if response.headers.get("x-ratelimit-remaining") == "0":
            return "github_quota_exhausted"
        return "github_authentication_failed"
    if status_code == 404:
        return "github_source_not_found"
    if status_code == 429:
        return "github_quota_exhausted"
    if status_code >= 500:
        return "github_provider_unavailable"
    return "github_request_rejected"


def _document_media_type(path: str) -> str:
    suffix = path.rsplit(".", maxsplit=1)[-1].lower() if "." in path else ""
    return {
        "md": "text/markdown",
        "json": "application/json",
        "yaml": "application/yaml",
        "yml": "application/yaml",
    }.get(suffix, "text/plain")


def _is_recoverable_query_failure(reason_code: str) -> bool:
    """Backward-compatible alias for provider-query recovery classification."""

    return is_recoverable_source_collection_failure(reason_code)


def is_recoverable_source_collection_failure(reason_code: str) -> bool:
    """Whether a physical source outage may become a bounded coverage gap."""

    return reason_code in {
        "firecrawl_network_failure",
        "firecrawl_provider_unavailable",
        "github_network_failure",
        "github_provider_unavailable",
        "tavily_network_failure",
        "tavily_provider_unavailable",
    }


def source_tool_ids(collector: object) -> tuple[str, ...]:
    """Return the exact configured source capabilities without inspecting prompts."""

    return tuple(sorted(set(getattr(collector, "tool_ids", ()))))


__all__ = [
    "CollectedResearchSource",
    "CompositeResearchSourceCollector",
    "FirecrawlSearchResearchSourceCollector",
    "FirecrawlSourceCollectionError",
    "FirecrawlWebReader",
    "GitHubRepositorySourceCollector",
    "GitHubSourceCollectionError",
    "LocalFileResearchSourceCollector",
    "LocalFileSourceCollectionError",
    "is_recoverable_source_collection_failure",
    "NoopResearchSourceCollector",
    "ProjectDocumentResearchSourceCollector",
    "ProjectDocumentSourceCollectionError",
    "ResearchSourceCollectionError",
    "ResearchSourceCollector",
    "ResearchSourceToolRuntime",
    "source_tool_ids",
    "TavilySearchResearchSourceCollector",
    "TavilySourceCollectionError",
]
