from __future__ import annotations

import pytest

from aidison.runtime.identity import (
    RuntimeBinding,
    RuntimeBindingConflict,
    RuntimeFamily,
    UnsupportedRuntimeBinding,
    WorkerRuntimeSupport,
    ensure_runtime_binding_unchanged,
    ensure_worker_supports,
)


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


def test_run_runtime_binding_is_idempotent_but_cannot_change() -> None:
    pinned = _binding()

    assert ensure_runtime_binding_unchanged(pinned=pinned, requested=_binding()) is pinned

    with pytest.raises(RuntimeBindingConflict, match="graph_revision"):
        ensure_runtime_binding_unchanged(
            pinned=pinned,
            requested=_binding(graph_revision="research-v2"),
        )


def test_worker_rejects_a_run_outside_its_exact_supported_bindings() -> None:
    supported = WorkerRuntimeSupport(
        worker_revision="orchestrator-v1",
        bindings=(_binding(),),
    )

    assert ensure_worker_supports(binding=_binding(), support=supported) == _binding()

    with pytest.raises(UnsupportedRuntimeBinding, match="research-state-v2"):
        ensure_worker_supports(
            binding=_binding(state_schema_version="research-state-v2"),
            support=supported,
        )
