from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from aidison.agents.impact import IMPACT_SYSTEM_PROMPT
from aidison.agents.research import (
    RESEARCH_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT_V1,
    RESEARCH_SYSTEM_PROMPT_V3,
)
from aidison.agents.solution import SOLUTION_SYSTEM_PROMPT
from aidison.runtime.contracts import AgentProfileRevision


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def build_profile_revision(
    *,
    profile_id: str,
    revision: int,
    purpose: str,
    prompt_template: str,
    input_schema_ref: str,
    output_schema_ref: str,
    allowed_tool_classes: tuple[str, ...] = (),
    allowed_effects: tuple[str, ...] = (),
    memory_read_scopes: tuple[str, ...] = (),
    memory_write_scopes: tuple[str, ...] = (),
    model_capabilities: tuple[str, ...] = (),
    token_cap: int,
    tool_call_cap: int,
    concurrency_cap: int,
    timeout_seconds: int,
    retry_policy: dict[str, Any] | None = None,
    evaluator_policy: dict[str, Any] | None = None,
) -> AgentProfileRevision:
    """Build one content-addressed, immutable Profile revision."""
    prompt_hash = sha256(prompt_template.encode("utf-8")).hexdigest()
    payload = {
        "profile_id": profile_id,
        "revision": revision,
        "purpose": purpose,
        "prompt_template": prompt_template,
        "prompt_hash": prompt_hash,
        "input_schema_ref": input_schema_ref,
        "output_schema_ref": output_schema_ref,
        "allowed_tool_classes": allowed_tool_classes,
        "allowed_effects": allowed_effects,
        "memory_read_scopes": memory_read_scopes,
        "memory_write_scopes": memory_write_scopes,
        "model_capabilities": model_capabilities,
        "token_cap": token_cap,
        "tool_call_cap": tool_call_cap,
        "concurrency_cap": concurrency_cap,
        "timeout_seconds": timeout_seconds,
        "retry_policy": retry_policy or {},
        "evaluator_policy": evaluator_policy or {},
    }
    return AgentProfileRevision(
        profile_id=profile_id,
        revision=revision,
        purpose=purpose,
        prompt_template=prompt_template,
        prompt_hash=prompt_hash,
        definition_hash=_canonical_hash(payload),
        input_schema_ref=input_schema_ref,
        output_schema_ref=output_schema_ref,
        allowed_tool_classes=allowed_tool_classes,
        allowed_effects=allowed_effects,
        memory_read_scopes=memory_read_scopes,
        memory_write_scopes=memory_write_scopes,
        model_capabilities=model_capabilities,
        token_cap=token_cap,
        tool_call_cap=tool_call_cap,
        concurrency_cap=concurrency_cap,
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy or {},
        evaluator_policy=evaluator_policy or {},
    )


RESEARCH_WORKER_PROFILE_V1 = build_profile_revision(
    profile_id="research-worker-ro",
    revision=1,
    purpose="Research assigned DIY modules and return evidence-backed typed proposals.",
    prompt_template=RESEARCH_SYSTEM_PROMPT_V1,
    input_schema_ref="aidison://schemas/research-worker-input/v1",
    output_schema_ref="aidison://schemas/research-proposal/v1",
    allowed_tool_classes=("web_search",),
    allowed_effects=("discovery", "read"),
    memory_read_scopes=("project.requirements", "project.modules", "artifact.web_snapshot"),
    model_capabilities=("structured_output", "tool_calling"),
    token_cap=4_000,
    tool_call_cap=3,
    concurrency_cap=2,
    timeout_seconds=300,
    retry_policy={"max_physical_attempts": 1, "hidden_provider_retries": 0},
    evaluator_policy={"require_structured_response": True, "require_snapshot_evidence": True},
)


RESEARCH_WORKER_PROFILE_V3 = build_profile_revision(
    profile_id="research-worker-ro",
    revision=3,
    purpose="Research assigned DIY modules and return evidence-bound typed decision options.",
    prompt_template=RESEARCH_SYSTEM_PROMPT_V3,
    input_schema_ref="aidison://schemas/research-worker-input/v1",
    output_schema_ref="aidison://schemas/research-proposal/v2",
    allowed_tool_classes=("web_search",),
    allowed_effects=("discovery", "read"),
    memory_read_scopes=("project.requirements", "project.modules", "artifact.web_snapshot"),
    model_capabilities=("structured_output", "tool_calling"),
    token_cap=4_000,
    tool_call_cap=3,
    concurrency_cap=2,
    timeout_seconds=300,
    retry_policy={"max_physical_attempts": 1, "hidden_provider_retries": 0},
    evaluator_policy={"require_structured_response": True, "require_snapshot_evidence": True},
)


RESEARCH_WORKER_PROFILE_V4 = build_profile_revision(
    profile_id="research-worker-ro",
    revision=4,
    purpose=(
        "Research assigned DIY modules through bounded Web and GitHub evidence and return "
        "evidence-bound typed decision options."
    ),
    prompt_template=RESEARCH_SYSTEM_PROMPT,
    input_schema_ref="aidison://schemas/research-worker-input/v1",
    output_schema_ref="aidison://schemas/research-proposal/v2",
    allowed_tool_classes=("web_search", "github_read"),
    allowed_effects=("discovery", "read"),
    memory_read_scopes=(
        "project.requirements",
        "project.modules",
        "artifact.web_snapshot",
        "artifact.github_snapshot",
    ),
    model_capabilities=("structured_output", "tool_calling"),
    token_cap=4_000,
    tool_call_cap=3,
    concurrency_cap=2,
    timeout_seconds=300,
    retry_policy={"max_physical_attempts": 1, "hidden_provider_retries": 0},
    evaluator_policy={"require_structured_response": True, "require_snapshot_evidence": True},
)


RESEARCH_WORKER_PROFILE = build_profile_revision(
    profile_id="research-worker-ro",
    revision=5,
    purpose=(
        "Research one bounded N-way DIY module shard through Web and GitHub evidence and return "
        "evidence-bound typed decision options."
    ),
    prompt_template=RESEARCH_SYSTEM_PROMPT,
    input_schema_ref="aidison://schemas/research-worker-input/v1",
    output_schema_ref="aidison://schemas/research-proposal/v2",
    allowed_tool_classes=("web_search", "github_read"),
    allowed_effects=("discovery", "read"),
    memory_read_scopes=(
        "project.requirements",
        "project.modules",
        "artifact.web_snapshot",
        "artifact.github_snapshot",
    ),
    model_capabilities=("structured_output", "tool_calling"),
    token_cap=4_000,
    tool_call_cap=3,
    concurrency_cap=8,
    timeout_seconds=300,
    retry_policy={"max_physical_attempts": 1, "hidden_provider_retries": 0},
    evaluator_policy={"require_structured_response": True, "require_snapshot_evidence": True},
)


RESEARCH_ORCHESTRATOR_PROFILE = build_profile_revision(
    profile_id="research-orchestrator",
    revision=1,
    purpose="Freeze and coordinate one deterministic durable research wave.",
    prompt_template="Deterministic runtime role. It never invokes a model or external tool.",
    input_schema_ref="aidison://schemas/research-orchestrator-input/v1",
    output_schema_ref="aidison://schemas/research-orchestrator-output/v1",
    token_cap=1,
    tool_call_cap=0,
    concurrency_cap=1,
    timeout_seconds=600,
    retry_policy={"max_physical_attempts": 0, "hidden_provider_retries": 0},
    evaluator_policy={"deterministic": True},
)


SOLUTION_PROPOSER_PROFILE = build_profile_revision(
    profile_id="solution-proposer-ro",
    revision=1,
    purpose="Turn an approved research Decision into a typed reviewable DIY solution proposal.",
    prompt_template=SOLUTION_SYSTEM_PROMPT,
    input_schema_ref="aidison://schemas/solution-proposer-input/v1",
    output_schema_ref="aidison://schemas/solution-proposal/v1",
    allowed_effects=("read",),
    memory_read_scopes=(
        "project.requirements",
        "project.modules",
        "project.evidence",
        "project.candidates",
        "project.compatibility_findings",
        "project.decisions",
    ),
    model_capabilities=("structured_output",),
    token_cap=8_000,
    tool_call_cap=0,
    concurrency_cap=1,
    timeout_seconds=300,
    retry_policy={"max_physical_attempts": 1, "hidden_provider_retries": 0},
    evaluator_policy={"require_structured_response": True, "require_canonical_refs": True},
)


SOLUTION_ORCHESTRATOR_PROFILE = build_profile_revision(
    profile_id="solution-orchestrator",
    revision=1,
    purpose="Freeze and coordinate one deterministic durable solution-proposal wave.",
    prompt_template="Deterministic runtime role. It never invokes a model or external tool.",
    input_schema_ref="aidison://schemas/solution-orchestrator-input/v1",
    output_schema_ref="aidison://schemas/solution-orchestrator-output/v1",
    token_cap=1,
    tool_call_cap=0,
    concurrency_cap=1,
    timeout_seconds=600,
    retry_policy={"max_physical_attempts": 0, "hidden_provider_retries": 0},
    evaluator_policy={"deterministic": True},
)


IMPACT_PROPOSER_PROFILE = build_profile_revision(
    profile_id="impact-proposer-ro",
    revision=1,
    purpose="Turn an immutable Observation into a typed, reviewable local revision proposal.",
    prompt_template=IMPACT_SYSTEM_PROMPT,
    input_schema_ref="aidison://schemas/impact-proposer-input/v1",
    output_schema_ref="aidison://schemas/impact-proposal/v1",
    allowed_effects=("read",),
    memory_read_scopes=(
        "project.requirements",
        "project.modules",
        "project.evidence",
        "project.candidates",
        "project.solutions",
        "project.observations",
    ),
    model_capabilities=("structured_output",),
    token_cap=8_000,
    tool_call_cap=0,
    concurrency_cap=1,
    timeout_seconds=300,
    retry_policy={"max_physical_attempts": 1, "hidden_provider_retries": 0},
    evaluator_policy={"require_structured_response": True, "require_canonical_refs": True},
)


IMPACT_ORCHESTRATOR_PROFILE = build_profile_revision(
    profile_id="impact-orchestrator",
    revision=1,
    purpose="Freeze and coordinate one deterministic durable impact-proposal wave.",
    prompt_template="Deterministic runtime role. It never invokes a model or external tool.",
    input_schema_ref="aidison://schemas/impact-orchestrator-input/v1",
    output_schema_ref="aidison://schemas/impact-orchestrator-output/v1",
    token_cap=1,
    tool_call_cap=0,
    concurrency_cap=1,
    timeout_seconds=600,
    retry_policy={"max_physical_attempts": 0, "hidden_provider_retries": 0},
    evaluator_policy={"deterministic": True},
)


BUILTIN_AGENT_PROFILES: tuple[AgentProfileRevision, ...] = (
    RESEARCH_ORCHESTRATOR_PROFILE,
    RESEARCH_WORKER_PROFILE_V1,
    RESEARCH_WORKER_PROFILE_V3,
    RESEARCH_WORKER_PROFILE_V4,
    RESEARCH_WORKER_PROFILE,
    SOLUTION_ORCHESTRATOR_PROFILE,
    SOLUTION_PROPOSER_PROFILE,
    IMPACT_ORCHESTRATOR_PROFILE,
    IMPACT_PROPOSER_PROFILE,
)
