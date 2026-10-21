import pytest

from aidison.research.defaults import research_default_token_budget_per_task


def test_deep_default_budget_leaves_room_for_evidence_context_and_structured_output() -> None:
    assert research_default_token_budget_per_task(research_depth="deep") == 8_000
    assert research_default_token_budget_per_task(research_depth="standard") == 4_000
    assert research_default_token_budget_per_task(research_depth="focused") == 4_000


def test_unknown_research_depth_has_no_silent_budget_fallback() -> None:
    with pytest.raises(ValueError, match="unknown research depth"):
        research_default_token_budget_per_task(research_depth="unbounded")
