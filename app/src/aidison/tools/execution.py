"""Storage-neutral contracts for one authorized physical tool attempt."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aidison.tools.dispatcher import AuthorizedToolInvocation


class ToolExecutionError(RuntimeError):
    """Base error for durable tool execution."""


class ToolExecutionReplayUnavailable(ToolExecutionError):
    """A prior attempt cannot be safely dispatched or replayed."""


class ToolAdapterFailure(ToolExecutionError):
    """A provider returned a definite, replayable failure response."""

    def __init__(self, reason_code: str, *, provider_request_id: str | None = None) -> None:
        self.reason_code = reason_code
        self.provider_request_id = provider_request_id
        super().__init__(reason_code)


class ToolExecutionFailure(ToolExecutionError):
    """A durable classified failure returned by a live or replayed attempt."""

    def __init__(self, reason_code: str, *, replayed: bool) -> None:
        self.reason_code = reason_code
        self.replayed = replayed
        super().__init__(reason_code)


class ToolAdapterResponse(BaseModel):
    """Secret-free JSON response produced by one adapter call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Any
    provider_request_id: str | None = Field(default=None, max_length=300)


class ToolExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Any
    replayed: bool
    logical_invocation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_artifact_ref: str = Field(min_length=1, max_length=500)


class ToolAdapter(Protocol):
    async def __call__(
        self,
        invocation: AuthorizedToolInvocation,
    ) -> ToolAdapterResponse: ...


ToolAdapterCallable = Callable[[AuthorizedToolInvocation], Awaitable[ToolAdapterResponse]]


__all__ = [
    "ToolAdapter",
    "ToolAdapterCallable",
    "ToolAdapterFailure",
    "ToolAdapterResponse",
    "ToolExecutionError",
    "ToolExecutionFailure",
    "ToolExecutionReplayUnavailable",
    "ToolExecutionResult",
]
