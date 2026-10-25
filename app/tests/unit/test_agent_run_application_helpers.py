from __future__ import annotations

from types import SimpleNamespace

from aidison.application.agent_run_application import _research_requirement_context


def test_research_requirement_context_bounds_long_public_benchmark_prompt() -> None:
    requirement = SimpleNamespace(
        goal=("Research every cited engineering claim with primary sources. " * 20),
        usage_context="Benchmark smoke test.",
        budget_context="128k token ceiling.",
        skill_context="Web search and evidence extraction.",
        hard_constraints=("Do not use blocked URLs.", "Citations are mandatory."),
        preferences=("Prefer primary sources.",),
        available_resources=("Search and reader tools.",),
    )

    lines = _research_requirement_context(requirement)

    assert len(lines) == 7
    assert all(0 < len(line) <= 300 for line in lines)
    assert lines[0].startswith("Project goal: Research every cited")
    assert lines[0].endswith("…")
    assert "Hard constraints: Do not use blocked URLs.; Citations are mandatory." in lines


def test_research_requirement_context_normalizes_whitespace_without_truncating() -> None:
    requirement = SimpleNamespace(
        goal="  Compare\n two   candidates.  ",
        usage_context="",
        budget_context="",
        skill_context="",
        hard_constraints=(),
        preferences=(),
        available_resources=(),
    )

    assert _research_requirement_context(requirement) == (
        "Project goal: Compare two candidates.",
    )
