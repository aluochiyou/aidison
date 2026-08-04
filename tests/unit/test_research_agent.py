from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel

from aidison.agents.contracts import (
    ImpactProposalPayload,
    ResearchProposalPayload,
    SolutionProposalPayload,
)
from aidison.agents.impact import _AIDISON_HARNESS_PROFILE as IMPACT_HARNESS_PROFILE
from aidison.agents.impact import build_impact_agent
from aidison.agents.research import _AIDISON_HARNESS_PROFILE as RESEARCH_HARNESS_PROFILE
from aidison.agents.research import build_github_tools, build_research_agent
from aidison.agents.solution import _AIDISON_HARNESS_PROFILE as SOLUTION_HARNESS_PROFILE
from aidison.agents.solution import build_solution_agent
from aidison.runtime.contracts import JobClaim
from aidison.tools.github import ControlledGitHubRead, GitHubSnapshot
from aidison.tools.web_search import SearchContext


def _search_context() -> SearchContext:
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    basis_hash = sha256(b"agent-basis").hexdigest()
    return SearchContext(
        project_id=uuid4(),
        claim=JobClaim(
            job_id=uuid4(),
            attempt_id=uuid4(),
            attempt_number=1,
            claim_generation=1,
            lease_token=uuid4(),
            lease_owner="unit-worker",
            lease_expires_at=deadline,
            basis_hash=basis_hash,
            basis_project_revision=1,
            profile_id="research-worker-ro",
            profile_revision=1,
        ),
        budget_allocation_id=uuid4(),
        basis_hash=basis_hash,
        deadline=deadline,
    )


def test_research_agent_disables_native_runtime_and_mutating_tools() -> None:
    model = MagicMock(spec=BaseChatModel)
    search = MagicMock()
    context = _search_context()
    compiled = MagicMock()

    # Test that only web_search tool is available when github=None (default)
    with patch("aidison.agents.research.create_deep_agent", return_value=compiled) as create:
        assert build_research_agent(model=model, search=search, context=context) is compiled

    kwargs = create.call_args.kwargs
    assert kwargs["model"] is model
    assert kwargs["response_format"] is ResearchProposalPayload
    assert "enable_native_subagents" not in kwargs
    assert kwargs["subagents"] == ()
    assert kwargs["checkpointer"] is None
    assert kwargs["store"] is None
    assert "excluded_tools" not in kwargs
    assert RESEARCH_HARNESS_PROFILE.excluded_tools == {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
        "task",
    }
    assert RESEARCH_HARNESS_PROFILE.general_purpose_subagent is not None
    assert RESEARCH_HARNESS_PROFILE.general_purpose_subagent.enabled is False
    assert [tool.name for tool in kwargs["tools"]] == ["web_search"]


def test_research_agent_includes_github_tools_when_github_provided() -> None:
    model = MagicMock(spec=BaseChatModel)
    search = MagicMock()
    github = MagicMock(spec=ControlledGitHubRead)
    context = _search_context()
    compiled = MagicMock()

    with patch("aidison.agents.research.create_deep_agent", return_value=compiled) as create:
        result = build_research_agent(
            model=model,
            search=search,
            context=context,
            github=github,
        )

    assert result is compiled
    kwargs = create.call_args.kwargs
    assert kwargs["subagents"] == ()
    assert [tool.name for tool in kwargs["tools"]] == [
        "web_search",
        "github_search_repositories",
        "github_search_code",
        "github_get_file_contents",
    ]


def test_research_agent_uses_frozen_profile_prompt() -> None:
    model = MagicMock(spec=BaseChatModel)
    search = MagicMock()
    context = _search_context()
    compiled = MagicMock()
    frozen_prompt = "frozen research-worker profile prompt"

    with patch("aidison.agents.research.create_deep_agent", return_value=compiled) as create:
        assert (
            build_research_agent(
                model=model,
                search=search,
                context=context,
                system_prompt=frozen_prompt,
            )
            is compiled
        )

    assert create.call_args.kwargs["system_prompt"] == frozen_prompt


@pytest.mark.asyncio
async def test_github_tools_return_only_snapshot_fields() -> None:
    github = MagicMock(spec=ControlledGitHubRead)
    snapshot = GitHubSnapshot(
        source_url="https://github.com/example/repo/blob/main/file.py",
        snapshot_hash=sha256(b"test-content").hexdigest(),
        snapshot_ref="ref123",
        span_text="test content",
    )
    github.search_repositories.return_value = snapshot
    github.search_code.return_value = snapshot
    github.get_file_contents.return_value = snapshot
    tools = build_github_tools(github, _search_context())

    results = [
        await tools[0].ainvoke({"query": "drone", "max_results": 1}),
        await tools[1].ainvoke({"query": "repo:o/r flight", "max_results": 1}),
        await tools[2].ainvoke({"owner": "o", "repo": "r", "path": "README.md"}),
    ]

    assert all(set(result) == set(GitHubSnapshot.model_fields) for result in results)


def test_research_payload_rejects_out_of_range_evidence_reference() -> None:
    with pytest.raises(ValueError, match="evidence index"):
        ResearchProposalPayload.model_validate(
            {
                "evidence": [
                    {
                        "module_key": "frame",
                        "claim": "A supported claim",
                        "source_url": "https://example.com/source",
                        "snapshot_hash": sha256(b"source").hexdigest(),
                        "span_text": "Supporting text",
                        "status": "supported",
                    }
                ],
                "candidates": [
                    {
                        "module_key": "frame",
                        "name": "Candidate",
                        "description": "Description",
                        "evidence_indexes": [1],
                    }
                ],
                "findings": [],
                "decision_question": "Which candidate?",
                "decision_options": [
                    {
                        "option_id": "candidate",
                        "label": "Candidate",
                        "summary": "Use the researched candidate.",
                        "candidate_indexes": [0],
                        "evidence_indexes": [0],
                    },
                    {
                        "option_id": "alternate",
                        "label": "Research more",
                        "summary": "Keep the current candidate as the comparison basis.",
                        "candidate_indexes": [0],
                        "evidence_indexes": [0],
                    },
                ],
            }
        )


def test_research_payload_rejects_out_of_range_option_candidate_reference() -> None:
    with pytest.raises(ValueError, match="candidate index"):
        ResearchProposalPayload.model_validate(
            {
                "evidence": [
                    {
                        "module_key": "frame",
                        "claim": "A supported claim",
                        "source_url": "https://example.com/source",
                        "snapshot_hash": sha256(b"source").hexdigest(),
                        "span_text": "Supporting text",
                        "status": "supported",
                    }
                ],
                "candidates": [
                    {
                        "module_key": "frame",
                        "name": "Candidate",
                        "description": "Description",
                        "evidence_indexes": [0],
                    }
                ],
                "findings": [],
                "decision_question": "Which candidate?",
                "decision_options": [
                    {
                        "option_id": "candidate",
                        "label": "Candidate",
                        "summary": "Use the researched candidate.",
                        "candidate_indexes": [1],
                        "evidence_indexes": [0],
                    },
                    {
                        "option_id": "comparison",
                        "label": "Comparison",
                        "summary": "Keep the valid candidate as a comparison basis.",
                        "candidate_indexes": [0],
                        "evidence_indexes": [0],
                    },
                ],
            }
        )


def test_solution_agent_is_proposal_only_and_has_no_tools() -> None:
    model = MagicMock(spec=BaseChatModel)
    compiled = MagicMock()

    with patch("aidison.agents.solution.create_deep_agent", return_value=compiled) as create:
        assert build_solution_agent(model=model) is compiled

    kwargs = create.call_args.kwargs
    assert kwargs["model"] is model
    assert kwargs["response_format"] is SolutionProposalPayload
    assert kwargs["tools"] == []
    assert "enable_native_subagents" not in kwargs
    assert kwargs["subagents"] == ()
    assert kwargs["checkpointer"] is None
    assert kwargs["store"] is None
    assert "execute" in SOLUTION_HARNESS_PROFILE.excluded_tools
    assert "task" in SOLUTION_HARNESS_PROFILE.excluded_tools
    assert SOLUTION_HARNESS_PROFILE.general_purpose_subagent is not None
    assert SOLUTION_HARNESS_PROFILE.general_purpose_subagent.enabled is False


def test_impact_agent_is_proposal_only_and_has_no_tools() -> None:
    model = MagicMock(spec=BaseChatModel)
    compiled = MagicMock()

    with patch("aidison.agents.impact.create_deep_agent", return_value=compiled) as create:
        assert build_impact_agent(model=model) is compiled

    kwargs = create.call_args.kwargs
    assert kwargs["model"] is model
    assert kwargs["response_format"] is ImpactProposalPayload
    assert kwargs["tools"] == []
    assert "enable_native_subagents" not in kwargs
    assert kwargs["subagents"] == ()
    assert kwargs["checkpointer"] is None
    assert kwargs["store"] is None
    assert "execute" in IMPACT_HARNESS_PROFILE.excluded_tools
    assert "task" in IMPACT_HARNESS_PROFILE.excluded_tools
    assert IMPACT_HARNESS_PROFILE.general_purpose_subagent is not None
    assert IMPACT_HARNESS_PROFILE.general_purpose_subagent.enabled is False
