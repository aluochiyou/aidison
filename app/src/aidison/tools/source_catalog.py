"""Production source-tool definitions shared by planning and execution.

The catalog contains no credentials and performs no I/O.  It gives every
physical source adapter a stable, revisioned contract before the adapter is
allowed to reserve budget or cross an external boundary.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aidison.tools.dispatcher import (
    ToolEffectClass,
    ToolRegistry,
    ToolRiskTier,
    ToolSpecRevision,
)

TAVILY_SEARCH_TOOL_ID = "web.search.tavily"
FIRECRAWL_SEARCH_TOOL_ID = "web.search.firecrawl"
FIRECRAWL_READ_TOOL_ID = "web.read.firecrawl"
GITHUB_CONTENTS_TOOL_ID = "repository.read.github_contents"
PROJECT_DOCUMENT_READ_TOOL_ID = "project_document.read"
PROJECT_FILE_READ_TOOL_ID = "project_file.read"


class _StrictArguments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TavilySearchArguments(_StrictArguments):
    query: str = Field(min_length=1, max_length=8_000)
    max_results: int = Field(ge=1, le=20)
    search_depth: Literal["basic", "advanced"]


class FirecrawlSearchArguments(_StrictArguments):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(ge=1, le=20)


class FirecrawlReadArguments(_StrictArguments):
    url: str = Field(min_length=1, max_length=2_048)
    max_characters: int = Field(default=32_000, ge=1, le=96_000)

    @field_validator("url")
    @classmethod
    def url_is_https_without_credentials(cls, value: str) -> str:
        # Full DNS/IP and redirect validation belongs to the adapter immediately
        # before dispatch.  This schema still rejects the broadest unsafe forms.
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Firecrawl source URL must be credential-free HTTPS")
        return value


class GitHubContentsArguments(_StrictArguments):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
    ref: str = Field(pattern=r"^[A-Za-z0-9_.:/-]+$", max_length=200)
    path: str = Field(pattern=r"^[A-Za-z0-9_.+/@-]+$", max_length=500)

    @field_validator("path")
    @classmethod
    def path_stays_inside_repository(cls, value: str) -> str:
        if value.startswith("/") or "//" in value or ".." in value.split("/"):
            raise ValueError("GitHub path must stay inside its repository")
        return value


class ProjectDocumentReadArguments(_StrictArguments):
    document_id: UUID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProjectFileReadArguments(_StrictArguments):
    target: str = Field(min_length=1, max_length=500)

    @field_validator("target")
    @classmethod
    def target_is_safe_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if (
            normalized.startswith("/")
            or "//" in normalized
            or any(part in {"", ".", ".."} for part in normalized.split("/"))
        ):
            raise ValueError("project file target must be a safe relative path")
        return normalized


SOURCE_TOOL_DEFINITIONS: tuple[tuple[ToolSpecRevision, type[BaseModel]], ...] = (
    (
        ToolSpecRevision(
            tool_id=FIRECRAWL_SEARCH_TOOL_ID,
            revision=1,
            purpose=(
                "Discover bounded public web evidence and normalized markdown "
                "for one approved research question."
            ),
            tool_class="web_search",
            effect_class=ToolEffectClass.READ,
            risk_tier=ToolRiskTier.T1,
            input_schema_ref="schema://tool/web-search-firecrawl/v1",
            output_schema_ref="schema://tool/research-sources/v1",
        ),
        FirecrawlSearchArguments,
    ),
    (
        ToolSpecRevision(
            tool_id=TAVILY_SEARCH_TOOL_ID,
            revision=1,
            purpose="Discover bounded public web sources for one approved research question.",
            tool_class="web_search",
            effect_class=ToolEffectClass.READ,
            risk_tier=ToolRiskTier.T1,
            input_schema_ref="schema://tool/web-search-tavily/v1",
            output_schema_ref="schema://tool/research-sources/v1",
        ),
        TavilySearchArguments,
    ),
    (
        ToolSpecRevision(
            tool_id=FIRECRAWL_READ_TOOL_ID,
            revision=1,
            purpose="Read one approved complex web page as normalized evidence.",
            tool_class="web_read",
            effect_class=ToolEffectClass.READ,
            risk_tier=ToolRiskTier.T1,
            input_schema_ref="schema://tool/web-read-firecrawl/v1",
            output_schema_ref="schema://tool/research-sources/v1",
        ),
        FirecrawlReadArguments,
    ),
    (
        ToolSpecRevision(
            tool_id=GITHUB_CONTENTS_TOOL_ID,
            revision=1,
            purpose="Read one operator-allowlisted GitHub repository file.",
            tool_class="repository_read",
            effect_class=ToolEffectClass.READ,
            risk_tier=ToolRiskTier.T1,
            input_schema_ref="schema://tool/github-contents/v1",
            output_schema_ref="schema://tool/research-sources/v1",
        ),
        GitHubContentsArguments,
    ),
    (
        ToolSpecRevision(
            tool_id=PROJECT_DOCUMENT_READ_TOOL_ID,
            revision=1,
            purpose="Read one immutable user-uploaded project document.",
            tool_class="project_read",
            effect_class=ToolEffectClass.READ,
            risk_tier=ToolRiskTier.T0,
            input_schema_ref="schema://tool/project-document-read/v1",
            output_schema_ref="schema://tool/research-sources/v1",
        ),
        ProjectDocumentReadArguments,
    ),
    (
        ToolSpecRevision(
            tool_id=PROJECT_FILE_READ_TOOL_ID,
            revision=1,
            purpose="Read one deployment-allowlisted local project file.",
            tool_class="project_read",
            effect_class=ToolEffectClass.READ,
            risk_tier=ToolRiskTier.T0,
            input_schema_ref="schema://tool/project-file-read/v1",
            output_schema_ref="schema://tool/research-sources/v1",
        ),
        ProjectFileReadArguments,
    ),
)


def build_source_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for spec, input_model in SOURCE_TOOL_DEFINITIONS:
        registry.register(spec, input_model=input_model)
    return registry


__all__ = [
    "FIRECRAWL_SEARCH_TOOL_ID",
    "FIRECRAWL_READ_TOOL_ID",
    "FirecrawlSearchArguments",
    "FirecrawlReadArguments",
    "GITHUB_CONTENTS_TOOL_ID",
    "GitHubContentsArguments",
    "PROJECT_DOCUMENT_READ_TOOL_ID",
    "PROJECT_FILE_READ_TOOL_ID",
    "ProjectDocumentReadArguments",
    "ProjectFileReadArguments",
    "SOURCE_TOOL_DEFINITIONS",
    "TAVILY_SEARCH_TOOL_ID",
    "TavilySearchArguments",
    "build_source_tool_registry",
]
