"""Explicit defaults used when the user asks for a bounded research plan.

These are plan-construction limits, not executable Agent profiles. A real
AgentRun records its immutable ``RuntimeBinding.profile_binding_ref`` instead.
"""

RESEARCH_DEFAULT_ALLOWED_TOOL_CLASSES = ("web_search", "repository_read")
RESEARCH_DEFAULT_MAX_CONCURRENCY = 8
RESEARCH_DEFAULT_MAX_TOKEN_BUDGET = 200_000_000
RESEARCH_DEFAULT_MAX_DURATION_SECONDS = 36_000

# This remains a per-physical-call context reservation, rather than a Run
# ceiling. Provider context windows are independent constraints and are not
# relaxed by the intentionally permissive user-approved Run budget.
RESEARCH_DEFAULT_TOKEN_BUDGET_PER_TASK = 4_000


def research_default_token_budget_per_task(*, research_depth: str) -> int:
    """Return a visible per-task ceiling that can hold the selected depth.

    This is retained only for older callers that deliberately calculate a
    per-task estimate. New research plans use
    ``RESEARCH_DEFAULT_MAX_TOKEN_BUDGET`` as one permissive Run ceiling.
    """

    try:
        return {
            "focused": RESEARCH_DEFAULT_TOKEN_BUDGET_PER_TASK,
            "standard": RESEARCH_DEFAULT_TOKEN_BUDGET_PER_TASK,
            "deep": 8_000,
        }[research_depth]
    except KeyError as exc:
        raise ValueError("unknown research depth") from exc
