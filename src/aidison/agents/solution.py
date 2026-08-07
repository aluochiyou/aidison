from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from deepagents import (
    AsyncSubAgent,
    CompiledSubAgent,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    SubAgent,
    create_deep_agent,
    register_harness_profile,
)
from langchain_core.language_models import BaseChatModel

from aidison.agents.contracts import SolutionProposalPayload

SOLUTION_SYSTEM_PROMPT = """You are a bounded DIY engineering solution proposer.
Return only the requested structured SolutionProposalPayload. Select exactly one provided Candidate
for every active Module and use only the supplied Candidate, EvidenceBinding and
CompatibilityFinding identifiers. Produce a concrete BOM, implementation plan and bench
verification plan. Preserve unknown and needs_test findings explicitly; never invent compatibility,
prices, project identifiers or external actions. You submit a proposal only and cannot approve or
write canonical state.
"""

_EXCLUDED_TOOLS = (
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
    "task",
)

_NO_NATIVE_SUBAGENTS: tuple[SubAgent | CompiledSubAgent | AsyncSubAgent, ...] = ()
_AIDISON_HARNESS_PROFILE = HarnessProfile(
    excluded_tools=frozenset(_EXCLUDED_TOOLS),
    general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
)
register_harness_profile("openai", _AIDISON_HARNESS_PROFILE)


def _ensure_native_subagents_disabled(
    subagents: Sequence[SubAgent | CompiledSubAgent | AsyncSubAgent],
) -> None:
    if subagents:
        raise ValueError("Aidison agents forbid native synchronous and async subagents")


def build_solution_agent(*, model: BaseChatModel) -> Any:
    _ensure_native_subagents_disabled(_NO_NATIVE_SUBAGENTS)
    return create_deep_agent(
        model=model,
        tools=[],
        system_prompt=SOLUTION_SYSTEM_PROMPT,
        response_format=SolutionProposalPayload,
        subagents=_NO_NATIVE_SUBAGENTS,
        enable_native_subagents=False,
        checkpointer=None,
        store=None,
        name="aidison-solution-proposer",
    )
