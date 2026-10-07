"""Exact LangGraph registration keyed by a frozen RuntimeBinding."""

from __future__ import annotations

from dataclasses import dataclass

from aidison.runtime.identity import RuntimeBinding, UnsupportedRuntimeBinding, WorkerRuntimeSupport


@dataclass(frozen=True, slots=True)
class RegisteredGraph:
    """One already-compiled graph and the exact contract under which it runs."""

    binding: RuntimeBinding
    compiled_graph: object


class GraphRegistry:
    """Startup-owned registry; it never falls back to a similarly named graph."""

    def __init__(self, registrations: tuple[RegisteredGraph, ...]) -> None:
        if not registrations:
            raise ValueError("GraphRegistry requires at least one graph registration")
        entries: dict[RuntimeBinding, object] = {}
        for registration in registrations:
            if registration.binding in entries:
                raise ValueError(
                    "GraphRegistry received duplicate runtime binding: "
                    f"{registration.binding.model_dump_json()}"
                )
            entries[registration.binding] = registration.compiled_graph
        self._entries = entries

    @property
    def worker_support(self) -> WorkerRuntimeSupport:
        """The exact immutable bindings this deployment can safely execute."""

        return WorkerRuntimeSupport(
            worker_revision="langgraph-worker-v1",
            bindings=tuple(self._entries),
        )

    def resolve(self, binding: RuntimeBinding) -> object:
        try:
            return self._entries[binding]
        except KeyError as error:
            raise UnsupportedRuntimeBinding(
                "no compiled LangGraph is registered for pinned binding: "
                f"{binding.model_dump_json()}"
            ) from error
