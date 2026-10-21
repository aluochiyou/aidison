from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.research.capability_subgraph import (
    AssemblyBackedContextAssembler,
    CapabilityAgent,
    CapabilityAgentFactory,
    CapabilityContextAssembler,
    CapabilityContextMaterialProvider,
    CapabilityInvocation,
    CapabilityPublicResult,
    PrivateCapabilityContext,
    build_capability_subgraph,
)
from aidison.research.context_assembly import (
    ContextAssemblyInput,
    ContextAssemblyPolicy,
    ContextCandidate,
    ContextLayer,
    ContextPriority,
)
from aidison.research.langgraph_contracts import TaskEnvelope


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _invocation(*, key: str) -> CapabilityInvocation:
    task = TaskEnvelope(
        run_id=uuid4(),
        task_key=key,
        basis_hash=_hash(f"basis-{key}"),
        plan_revision=1,
        capability="research",
        input_refs=(f"artifact://task-input/{key}",),
        dependency_task_ids=(),
        coverage_keys=(f"coverage.{key}",),
        allowed_tool_ids=("web.search",),
        budget_ref=f"budget://{key}",
        idempotency_key=f"task:{key}",
    )
    return CapabilityInvocation(
        task=task,
        task_instruction=f"Perform bounded research for {key}.",
        profile_ref="profile://research/1",
        canonical_fact_refs=(f"artifact://facts/{key}",),
        admitted_material_refs=(f"admitted://material/{key}",),
        advisory_refs=(f"memory://hint/{key}",),
    )


class _Assembler(CapabilityContextAssembler):
    def __init__(self) -> None:
        self.contexts: list[PrivateCapabilityContext] = []

    async def assemble(self, *, invocation: CapabilityInvocation) -> PrivateCapabilityContext:
        context = PrivateCapabilityContext.from_invocation(invocation)
        self.contexts.append(context)
        return context


class _Agent(CapabilityAgent):
    def __init__(self, *, created_index: int) -> None:
        self.created_index = created_index
        self.contexts: list[PrivateCapabilityContext] = []

    async def execute(self, *, context: PrivateCapabilityContext) -> CapabilityPublicResult:
        self.contexts.append(context)
        return CapabilityPublicResult(
            result_ref=f"artifact://candidate/{context.task_key}",
            result_manifest_hash=_hash(f"manifest-{context.task_key}"),
            raw_output_ref=f"artifact://raw/{context.task_key}",
            usage_ref=f"usage://{context.task_key}",
            unresolved_refs=(),
        )


class _Factory(CapabilityAgentFactory):
    def __init__(self) -> None:
        self.created: list[_Agent] = []

    def create(self, *, context: PrivateCapabilityContext) -> CapabilityAgent:
        agent = _Agent(created_index=len(self.created))
        self.created.append(agent)
        return agent


@pytest.mark.asyncio
async def test_capability_subgraph_keeps_context_and_messages_private() -> None:
    assembler = _Assembler()
    factory = _Factory()
    subgraph = build_capability_subgraph(assembler=assembler, agent_factory=factory)
    invocation = _invocation(key="first")

    result = await subgraph.execute(invocation=invocation)

    assert result.result_ref == "artifact://candidate/first"
    assert result.raw_output_ref == "artifact://raw/first"
    assert result.usage_ref == "usage://first"
    assert set(result.model_dump()) == {
        "result_ref",
        "result_manifest_hash",
        "raw_output_ref",
        "admission_ref",
        "usage_ref",
        "failure_ref",
        "unresolved_refs",
        "output_refs",
        "control_outcome",
    }
    assert len(assembler.contexts) == 1
    context = assembler.contexts[0]
    assert context.canonical_fact_refs == ("artifact://facts/first",)
    assert context.task_instruction == "Perform bounded research for first."
    assert context.admitted_material_refs == ("admitted://material/first",)
    assert context.advisory_refs == ("memory://hint/first",)
    assert len(factory.created) == 1
    assert factory.created[0].contexts == [context]


@pytest.mark.asyncio
async def test_capability_subgraph_creates_an_isolated_agent_per_invocation() -> None:
    assembler = _Assembler()
    factory = _Factory()
    subgraph = build_capability_subgraph(assembler=assembler, agent_factory=factory)

    first = _invocation(key="first")
    second = _invocation(key="second")
    first_result = await subgraph.execute(invocation=first)
    second_result = await subgraph.execute(invocation=second)

    assert first_result.result_ref.endswith("/first")
    assert second_result.result_ref.endswith("/second")
    assert len(factory.created) == 2
    assert factory.created[0] is not factory.created[1]
    assert factory.created[0].contexts[0].task_id == first.task.id
    assert factory.created[1].contexts[0].task_id == second.task.id
    assert factory.created[1].contexts[0].canonical_fact_refs == ("artifact://facts/second",)


@pytest.mark.asyncio
async def test_capability_subgraph_rejects_context_that_does_not_match_task_contract() -> None:
    class _WrongAssembler(CapabilityContextAssembler):
        async def assemble(self, *, invocation: CapabilityInvocation) -> PrivateCapabilityContext:
            other = _invocation(key="other")
            return PrivateCapabilityContext.from_invocation(other)

    subgraph = build_capability_subgraph(
        assembler=_WrongAssembler(),
        agent_factory=_Factory(),
    )

    with pytest.raises(ValueError, match="must match the CapabilityInvocation"):
        await subgraph.execute(invocation=_invocation(key="expected"))


@pytest.mark.asyncio
async def test_assembly_backed_context_stays_private_to_capability_invocation() -> None:
    class _MaterialProvider(CapabilityContextMaterialProvider):
        async def resolve(self, *, invocation: CapabilityInvocation) -> ContextAssemblyInput:
            policy_text = "Server policy: return a candidate, never write project facts."
            task_text = invocation.task_instruction
            return ContextAssemblyInput(
                policy=ContextAssemblyPolicy(
                    model_context_window_tokens=400,
                    max_output_tokens=100,
                    tool_loop_reserve_tokens=50,
                    safety_margin_tokens=50,
                    policy_version="context-policy-v1",
                    authorization_scope_hash=_hash("scope"),
                    tokenizer_binding_hash=_hash("tokenizer"),
                    renderer_version="context-renderer-v1",
                ),
                candidates=(
                    ContextCandidate(
                        source_ref="policy://research/v1",
                        layer=ContextLayer.POLICY,
                        priority=ContextPriority.MUST,
                        authority="server_owned",
                        content=policy_text,
                        content_hash=_hash(policy_text),
                        source_basis_hash=invocation.task.basis_hash,
                        stable_prefix=True,
                    ),
                    ContextCandidate(
                        source_ref=f"task://{invocation.task.task_key}",
                        layer=ContextLayer.TASK,
                        priority=ContextPriority.MUST,
                        authority="server_owned",
                        content=task_text,
                        content_hash=_hash(task_text),
                        source_basis_hash=invocation.task.basis_hash,
                    ),
                ),
                profile_definition_hash=_hash("research-profile"),
                model_binding_hash=_hash("model-binding"),
            )

    factory = _Factory()
    subgraph = build_capability_subgraph(
        assembler=AssemblyBackedContextAssembler(material_provider=_MaterialProvider()),
        agent_factory=factory,
    )

    result = await subgraph.execute(invocation=_invocation(key="assembled"))

    context = factory.created[0].contexts[0]
    assert context.context_manifest is not None
    assert context.rendered_context is not None
    assert context.rendered_context.index("policy://research/v1") < context.rendered_context.index(
        "task://assembled"
    )
    assert "rendered_context" not in result.model_dump()
