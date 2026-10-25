import pytest
from pydantic import ValidationError

from aidison.api.schemas import ResearchStrategyPlanRequest


def test_research_strategy_api_defaults_to_deep_but_keeps_explicit_cost_controls() -> None:
    assert ResearchStrategyPlanRequest().research_depth == "deep"
    assert ResearchStrategyPlanRequest(research_depth="focused").research_depth == "focused"
    assert ResearchStrategyPlanRequest(max_token_budget=128_000).max_token_budget == 128_000
    assert (
        ResearchStrategyPlanRequest(max_token_budget=1_000_000_000).max_token_budget
        == 1_000_000_000
    )
    with pytest.raises(ValidationError):
        ResearchStrategyPlanRequest(max_token_budget=1_000_000_001)
    assert ResearchStrategyPlanRequest(max_duration_seconds=36_000).max_duration_seconds == 36_000
