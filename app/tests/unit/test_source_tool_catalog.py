from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aidison.research.langgraph_contracts import ExecutionGrant, TaskEnvelope
from aidison.tools.dispatcher import (
    DispatchAuthority,
    ToolCallIntent,
    ToolDispatchDenied,
    ToolDispatcher,
    ToolDispatchPolicy,
    ToolEffectClass,
    ToolProfileGrant,
    ToolRiskTier,
)
from aidison.tools.source_catalog import (
    FIRECRAWL_READ_TOOL_ID,
    FIRECRAWL_SEARCH_TOOL_ID,
    GITHUB_CONTENTS_TOOL_ID,
    TAVILY_SEARCH_TOOL_ID,
    build_source_tool_registry,
)


def _authority(*, tool_id: str, tool_class: str) -> DispatchAuthority:
    task = TaskEnvelope(
        run_id=uuid4(),
        task_key="module.research",
        basis_hash="a" * 64,
        plan_revision=1,
        capability="research",
        input_refs=("artifact://coverage",),
        dependency_task_ids=(),
        coverage_keys=("battery.voltage",),
        allowed_tool_ids=(tool_id,),
        budget_ref="budget://run/1",
        idempotency_key="module-research",
    )
    return DispatchAuthority(
        task=task,
        grant=ExecutionGrant(
            task_id=task.id,
            attempt_id=uuid4(),
            generation=1,
            lease_token=uuid4(),
            deadline_ref="deadline://run/1",
            idempotency_prefix="module-research",
        ),
        profile=ToolProfileGrant(
            profile_ref="profile://research/1",
            allowed_tool_ids=(tool_id,),
            allowed_tool_classes=(tool_class,),
            allowed_effect_classes=(ToolEffectClass.READ,),
        ),
        policy=ToolDispatchPolicy(
            allowed_tool_classes=(tool_class,),
            allowed_effect_classes=(ToolEffectClass.READ,),
            available_risk_tiers=(ToolRiskTier.T0, ToolRiskTier.T1),
        ),
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.parametrize(
    ("tool_id", "tool_class", "arguments"),
    (
        (
            FIRECRAWL_SEARCH_TOOL_ID,
            "web_search",
            {"query": "6S battery discharge curve", "max_results": 3},
        ),
        (
            TAVILY_SEARCH_TOOL_ID,
            "web_search",
            {"query": "6S battery discharge curve", "max_results": 3, "search_depth": "basic"},
        ),
        (
            FIRECRAWL_READ_TOOL_ID,
            "web_read",
            {"url": "https://example.com/datasheet", "max_characters": 32_000},
        ),
        (
            GITHUB_CONTENTS_TOOL_ID,
            "repository_read",
            {"repository": "owner/repo", "ref": "main", "path": "docs/spec.md"},
        ),
    ),
)
def test_source_catalog_definitions_are_dispatchable(
    tool_id: str,
    tool_class: str,
    arguments: dict[str, object],
) -> None:
    authority = _authority(tool_id=tool_id, tool_class=tool_class)
    invocation = ToolDispatcher(build_source_tool_registry()).authorize(
        ToolCallIntent(
            tool_call_id="call-1",
            tool_id=tool_id,
            revision=1,
            arguments=arguments,
            purpose_code="collect_evidence",
        ),
        authority=authority,
    )

    assert invocation.tool_id == tool_id
    assert invocation.tool_class == tool_class
    assert invocation.effect_class is ToolEffectClass.READ


def test_firecrawl_contract_rejects_non_https_or_credentialed_urls() -> None:
    dispatcher = ToolDispatcher(build_source_tool_registry())
    authority = _authority(tool_id=FIRECRAWL_READ_TOOL_ID, tool_class="web_read")

    for url in ("http://example.com", "https://user:secret@example.com/page"):
        with pytest.raises(ToolDispatchDenied, match="input schema"):
            dispatcher.authorize(
                ToolCallIntent(
                    tool_call_id="call-1",
                    tool_id=FIRECRAWL_READ_TOOL_ID,
                    revision=1,
                    arguments={"url": url},
                    purpose_code="collect_evidence",
                ),
                authority=authority,
            )
