import pytest
from langchain.agents.structured_output import StructuredOutputError
from openai import OpenAIError

from aidison.application.research import ResearchWorker
from aidison.infrastructure.budget import (
    BudgetClaimStaleError,
    BudgetConflictError,
    BudgetLimitExceededError,
)
from aidison.infrastructure.runtime import RuntimeConflictError
from aidison.providers.gateway import ProviderUnavailableError
from aidison.tools.web_search import SearchUnavailableError


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProviderUnavailableError("secret-key"), "provider_unavailable"),
        (OpenAIError("provider response with token=secret"), "provider_unavailable"),
        (
            SearchUnavailableError("Tavily MCP returned invalid JSON"),
            "search_tool_contract_error",
        ),
        (
            SearchUnavailableError("search returned no fetchable public source"),
            "no_fetchable_source",
        ),
        (SearchUnavailableError("url?token=secret"), "search_unavailable"),
        (BudgetConflictError("allocation token=secret"), "budget_conflict"),
        (BudgetLimitExceededError("tool token=secret"), "budget_limit_exceeded"),
        (BudgetClaimStaleError("claim token=secret"), "budget_claim_stale"),
        (
            ExceptionGroup(
                "transport token=secret",
                [BudgetLimitExceededError("wrapped token=secret")],
            ),
            "budget_limit_exceeded",
        ),
        (
            ExceptionGroup(
                "mixed token=secret",
                [
                    BudgetLimitExceededError("known token=secret"),
                    RuntimeError("unknown token=secret"),
                ],
            ),
            "worker_error",
        ),
        (RuntimeConflictError("runtime token=secret"), "runtime_conflict"),
        (StructuredOutputError("structured token=secret"), "invalid_agent_output"),
        (ValueError("invalid token=secret"), "invalid_agent_output"),
        (RuntimeError("unknown token=secret"), "worker_error"),
    ],
)
def test_error_code_is_fixed_and_never_persists_exception_details(
    error: Exception,
    expected: str,
) -> None:
    code = ResearchWorker._error_code(error)

    assert code == expected
    assert "secret" not in code
    assert "token" not in code
    assert "?" not in code
