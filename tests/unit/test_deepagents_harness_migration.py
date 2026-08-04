from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import ModuleType
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from langchain_openai import ChatOpenAI

import aidison.agents.impact as impact_module
import aidison.agents.research as research_module
import aidison.agents.solution as solution_module
from aidison.runtime.contracts import JobClaim
from aidison.tools.web_search import SearchContext

_AGENT_MODULES = (research_module, solution_module, impact_module)
_EXCLUDED_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "task"}
)


@pytest.fixture
def model() -> ChatOpenAI:
    return ChatOpenAI(model="migration-test", api_key="test-key", max_retries=0)


@pytest.fixture
def search_context() -> SearchContext:
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    basis_hash = sha256(b"harness-migration").hexdigest()
    return SearchContext(
        project_id=uuid4(),
        claim=JobClaim(
            job_id=uuid4(),
            attempt_id=uuid4(),
            attempt_number=1,
            claim_generation=1,
            lease_token=uuid4(),
            lease_owner="migration-test",
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


def _build_agent(
    module: ModuleType,
    *,
    model: ChatOpenAI,
    search_context: SearchContext,
) -> object:
    if module is research_module:
        return research_module.build_research_agent(
            model=model,
            search=MagicMock(),
            context=search_context,
        )
    if module is solution_module:
        return solution_module.build_solution_agent(model=model)
    return impact_module.build_impact_agent(model=model)


@pytest.mark.parametrize("module", _AGENT_MODULES)
def test_agent_builders_use_official_harness_profile_api(
    module: ModuleType,
    model: ChatOpenAI,
    search_context: SearchContext,
) -> None:
    compiled = MagicMock()

    with patch.object(module, "create_deep_agent", return_value=compiled) as create:
        assert _build_agent(module, model=model, search_context=search_context) is compiled

    kwargs = create.call_args.kwargs
    profile = module._AIDISON_HARNESS_PROFILE
    assert profile.excluded_tools == _EXCLUDED_TOOLS
    assert profile.general_purpose_subagent is not None
    assert profile.general_purpose_subagent.enabled is False
    assert kwargs["subagents"] == ()
    assert "excluded_tools" not in kwargs
    assert "enable_native_subagents" not in kwargs
    expected_tool_names = ["web_search"] if module is research_module else []
    assert [tool.name for tool in kwargs["tools"]] == expected_tool_names


@pytest.mark.parametrize("module", _AGENT_MODULES)
@pytest.mark.parametrize(
    "native_subagent",
    [
        {"name": "sync", "description": "forbidden", "system_prompt": "forbidden"},
        {"name": "async", "description": "forbidden", "graph_id": "forbidden"},
    ],
)
def test_agent_builders_reject_sync_and_async_native_subagents(
    module: ModuleType,
    native_subagent: dict[str, str],
    model: ChatOpenAI,
    search_context: SearchContext,
) -> None:
    with (
        patch.object(module, "_NO_NATIVE_SUBAGENTS", (native_subagent,)),
        patch.object(module, "create_deep_agent") as create,
        pytest.raises(ValueError, match="native synchronous and async subagents"),
    ):
        _build_agent(module, model=model, search_context=search_context)

    create.assert_not_called()


@pytest.mark.parametrize(
    ("module", "expected_tool_names"),
    [
        (research_module, ["web_search"]),
        (solution_module, []),
        (impact_module, []),
    ],
)
def test_registered_profile_enforces_tool_allowlist_without_native_middleware(
    module: ModuleType,
    expected_tool_names: list[str],
    model: ChatOpenAI,
    search_context: SearchContext,
) -> None:
    compiled = MagicMock()
    deepagents_agent = MagicMock()
    deepagents_agent.with_config.return_value = compiled

    with patch("deepagents.graph.create_agent", return_value=deepagents_agent) as create:
        assert _build_agent(module, model=model, search_context=search_context) is compiled

    kwargs = create.call_args.kwargs
    assert [tool.name for tool in kwargs["tools"]] == expected_tool_names
    middleware = kwargs["middleware"]
    assert not any(type(item).__name__ == "SubAgentMiddleware" for item in middleware)
    assert not any(type(item).__name__ == "AsyncSubAgentMiddleware" for item in middleware)
    exclusion = next(
        item for item in middleware if type(item).__name__ == "_ToolExclusionMiddleware"
    )
    assert exclusion._excluded == _EXCLUDED_TOOLS
