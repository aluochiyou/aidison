"""Private per-invocation LangGraph capability boundary.

The root ResearchGraph exchanges typed references only.  A Capability Agent is
created for one TaskEnvelope invocation, receives its private context, and can
keep messages inside this subgraph.  It returns a small public reference bundle
for later Result Admission; it cannot directly mutate Domain or Root state.
"""

from __future__ import annotations

from typing import Annotated, Any, Protocol, cast
from uuid import UUID

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from aidison.research.context_assembly import (
    ContextAssemblyEngine,
    ContextAssemblyInput,
    ContextManifest,
)
from aidison.research.langgraph_contracts import TaskEnvelope


class CapabilityInvocation(BaseModel):
    """The only root-to-capability input; all material is explicitly scoped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: TaskEnvelope
    task_instruction: str = Field(min_length=1, max_length=12_000)
    profile_ref: str = Field(min_length=1, max_length=500)
    canonical_fact_refs: tuple[str, ...] = Field(max_length=64)
    admitted_material_refs: tuple[str, ...] = Field(max_length=128)
    advisory_refs: tuple[str, ...] = Field(max_length=32)


class PrivateCapabilityContext(BaseModel):
    """Invocation-scoped context; never returned by the public graph facade."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    task_id: UUID
    task_key: str = Field(min_length=1, max_length=120)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    capability: str = Field(min_length=1, max_length=120)
    task_instruction: str = Field(min_length=1, max_length=12_000)
    profile_ref: str = Field(min_length=1, max_length=500)
    input_refs: tuple[str, ...]
    coverage_keys: tuple[str, ...]
    canonical_fact_refs: tuple[str, ...]
    admitted_material_refs: tuple[str, ...]
    advisory_refs: tuple[str, ...]
    context_manifest: ContextManifest | None = None
    rendered_context: str | None = None

    @classmethod
    def from_invocation(cls, invocation: CapabilityInvocation) -> PrivateCapabilityContext:
        task = invocation.task
        return cls(
            run_id=task.run_id,
            task_id=task.id,
            task_key=task.task_key,
            basis_hash=task.basis_hash,
            capability=task.capability,
            task_instruction=invocation.task_instruction,
            profile_ref=invocation.profile_ref,
            input_refs=task.input_refs,
            coverage_keys=task.coverage_keys,
            canonical_fact_refs=invocation.canonical_fact_refs,
            admitted_material_refs=invocation.admitted_material_refs,
            advisory_refs=invocation.advisory_refs,
        )

    def matches(self, invocation: CapabilityInvocation) -> bool:
        task = invocation.task
        return (
            self.run_id == task.run_id
            and self.task_id == task.id
            and self.task_key == task.task_key
            and self.basis_hash == task.basis_hash
            and self.capability == task.capability
            and self.task_instruction == invocation.task_instruction
            and self.profile_ref == invocation.profile_ref
            and self.input_refs == task.input_refs
            and self.coverage_keys == task.coverage_keys
            and self.canonical_fact_refs == invocation.canonical_fact_refs
            and self.admitted_material_refs == invocation.admitted_material_refs
            and self.advisory_refs == invocation.advisory_refs
        )


class CapabilityPublicResult(BaseModel):
    """References that may cross the private capability boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_ref: str = Field(min_length=1, max_length=500)
    result_manifest_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    raw_output_ref: str | None = Field(default=None, max_length=500)
    admission_ref: str | None = Field(default=None, max_length=500)
    usage_ref: str | None = Field(default=None, max_length=500)
    failure_ref: str | None = Field(default=None, max_length=500)
    unresolved_refs: tuple[str, ...] = Field(max_length=64)
    output_refs: tuple[str, ...] = Field(default=(), max_length=32)
    control_outcome: str | None = Field(default=None, max_length=120)


class CapabilityContextAssembler(Protocol):
    """Build a private context from references; it must not widen task scope."""

    async def assemble(self, *, invocation: CapabilityInvocation) -> PrivateCapabilityContext: ...


class TaskScopedContextAssembler:
    """Minimal deterministic assembler until Context Artifact rendering lands."""

    async def assemble(self, *, invocation: CapabilityInvocation) -> PrivateCapabilityContext:
        return PrivateCapabilityContext.from_invocation(invocation)


class CapabilityContextMaterialProvider(Protocol):
    """Resolve already-authorized source fragments for one private invocation."""

    async def resolve(self, *, invocation: CapabilityInvocation) -> ContextAssemblyInput: ...


class AssemblyBackedContextAssembler:
    """Attach a bounded rendered context without exposing it to the root graph."""

    def __init__(
        self,
        *,
        material_provider: CapabilityContextMaterialProvider,
        engine: ContextAssemblyEngine | None = None,
    ) -> None:
        self._material_provider = material_provider
        self._engine = engine or ContextAssemblyEngine()

    async def assemble(self, *, invocation: CapabilityInvocation) -> PrivateCapabilityContext:
        material = await self._material_provider.resolve(invocation=invocation)
        assembly = self._engine.assemble(
            task=invocation.task,
            policy=material.policy,
            candidates=material.candidates,
            profile_definition_hash=material.profile_definition_hash,
            model_binding_hash=material.model_binding_hash,
        )
        return PrivateCapabilityContext.from_invocation(invocation).model_copy(
            update={
                "context_manifest": assembly.manifest,
                "rendered_context": assembly.rendered_context,
            }
        )


class CapabilityAgent(Protocol):
    """One short-lived capability instance with no cross-invocation messages."""

    async def execute(self, *, context: PrivateCapabilityContext) -> CapabilityPublicResult: ...


class CapabilityAgentFactory(Protocol):
    """Create a new CapabilityAgent for each graph invocation."""

    def create(self, *, context: PrivateCapabilityContext) -> CapabilityAgent: ...


class _PrivateCapabilityState(TypedDict, total=False):
    invocation: CapabilityInvocation
    context: PrivateCapabilityContext
    messages: Annotated[list[AnyMessage], add_messages]
    public_result: CapabilityPublicResult


class CapabilitySubgraph:
    """Facade that prevents callers from receiving private state or messages."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def execute(self, *, invocation: CapabilityInvocation) -> CapabilityPublicResult:
        state = await self._graph.ainvoke({"invocation": invocation})
        return CapabilityPublicResult.model_validate(state["public_result"])


def build_capability_subgraph(
    *,
    assembler: CapabilityContextAssembler,
    agent_factory: CapabilityAgentFactory,
) -> CapabilitySubgraph:
    """Build a private per-invocation subgraph without exposing its state.

    The parent ``@task`` owns durable replay.  This subgraph deliberately has
    no independent saver or thread identity, so it cannot become a competing
    runtime or a second long-lived conversation memory.
    """

    async def assemble_context(
        state: _PrivateCapabilityState,
    ) -> _PrivateCapabilityState:
        invocation = state["invocation"]
        context = await assembler.assemble(invocation=invocation)
        if not context.matches(invocation):
            raise ValueError("PrivateCapabilityContext must match the CapabilityInvocation")
        return {
            "context": context,
            "messages": [
                HumanMessage(
                    content=(
                        "Execute the bounded capability task "
                        f"{context.task_key} for the supplied private context."
                    )
                )
            ],
        }

    async def invoke_capability(
        state: _PrivateCapabilityState,
    ) -> _PrivateCapabilityState:
        context = state["context"]
        agent = agent_factory.create(context=context)
        result = await agent.execute(context=context)
        return {
            "public_result": result,
            "messages": [AIMessage(content="Capability invocation produced a typed result.")],
        }

    builder = StateGraph(_PrivateCapabilityState)
    builder.add_node("assemble_context", cast(Any, assemble_context))
    builder.add_node("invoke_capability", cast(Any, invoke_capability))
    builder.add_edge(START, "assemble_context")
    builder.add_edge("assemble_context", "invoke_capability")
    builder.add_edge("invoke_capability", END)
    return CapabilitySubgraph(
        builder.compile(checkpointer=False, name="aidison_private_capability_v1")
    )


__all__ = [
    "CapabilityAgent",
    "CapabilityAgentFactory",
    "CapabilityContextAssembler",
    "CapabilityContextMaterialProvider",
    "CapabilityInvocation",
    "CapabilityPublicResult",
    "CapabilitySubgraph",
    "AssemblyBackedContextAssembler",
    "PrivateCapabilityContext",
    "TaskScopedContextAssembler",
    "build_capability_subgraph",
]
