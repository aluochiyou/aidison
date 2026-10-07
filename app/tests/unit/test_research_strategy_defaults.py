from aidison.api.schemas import ResearchStrategyPlanRequest


def test_research_strategy_api_defaults_to_deep_but_keeps_explicit_cost_controls() -> None:
    assert ResearchStrategyPlanRequest().research_depth == "deep"
    assert ResearchStrategyPlanRequest(research_depth="focused").research_depth == "focused"
    assert ResearchStrategyPlanRequest(max_token_budget=200_000_000).max_token_budget == 200_000_000
    assert ResearchStrategyPlanRequest(max_duration_seconds=36_000).max_duration_seconds == 36_000
