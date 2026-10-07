from __future__ import annotations

import pytest

from aidison.runtime.graphs import GraphRegistry, RegisteredGraph
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily, UnsupportedRuntimeBinding


def _binding(**changes: object) -> RuntimeBinding:
    values: dict[str, object] = {
        "runtime_family": RuntimeFamily.LANGGRAPH_V1,
        "runtime_revision": "runtime-v1",
        "graph_key": "research",
        "graph_revision": "research-v1",
        "state_schema_version": "research-state-v1",
        "profile_binding_ref": "profile://research/default@1",
        "policy_binding_ref": "policy://research/default@1",
    }
    values.update(changes)
    return RuntimeBinding.model_validate(values)


def test_registry_resolves_only_an_exact_pinned_binding() -> None:
    compiled = object()
    registry = GraphRegistry((RegisteredGraph(binding=_binding(), compiled_graph=compiled),))

    assert registry.resolve(_binding()) is compiled

    with pytest.raises(UnsupportedRuntimeBinding, match="no compiled LangGraph"):
        registry.resolve(_binding(graph_revision="research-v2"))


def test_registry_rejects_duplicate_binding_registration() -> None:
    binding = _binding()

    with pytest.raises(ValueError, match="duplicate"):
        GraphRegistry(
            (
                RegisteredGraph(binding=binding, compiled_graph=object()),
                RegisteredGraph(binding=binding, compiled_graph=object()),
            )
        )
