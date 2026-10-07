"""Small HTN-inspired method catalog; it compiles intent, never schedules work."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from aidison.domain.models import ResearchStrategyTask


class ResearchMethod(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    required_outputs: tuple[str, ...]
    minimum_depth: str = "focused"
    guidance: str = Field(min_length=1, max_length=500)


_METHODS = (
    ResearchMethod(
        key="evidence.source_triage",
        required_outputs=("evidence",),
        guidance="Collect source-backed constraints before recommending a candidate.",
    ),
    ResearchMethod(
        key="compatibility.interface_check",
        required_outputs=("compatibility",),
        minimum_depth="standard",
        guidance="Check interface limits and record incompatible combinations explicitly.",
    ),
    ResearchMethod(
        key="candidate.tradeoff_matrix",
        required_outputs=("candidate",),
        minimum_depth="standard",
        guidance="Compare candidates against the approved hard constraints and trade-offs.",
    ),
)


def select_methods(
    *, task: ResearchStrategyTask, research_depth: str
) -> tuple[ResearchMethod, ...]:
    """Select deterministic, bounded methods from declared task outputs."""

    depth_rank = {"focused": 0, "standard": 1, "deep": 2}
    if research_depth not in depth_rank:
        raise ValueError("unsupported research depth")
    outputs = set(task.expected_outputs)
    return tuple(
        method
        for method in _METHODS
        if set(method.required_outputs) <= outputs
        and depth_rank[method.minimum_depth] <= depth_rank[research_depth]
    )


__all__ = ["ResearchMethod", "select_methods"]
