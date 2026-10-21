"""Privacy-bounded model invocation summaries for the runtime tracer."""

from __future__ import annotations

from aidison.observability.contracts import TelemetryCorrelation
from aidison.observability.runtime_tracing import RuntimeTracer
from aidison.providers.model_gateway import ModelInvocationObservation


class RuntimeModelInvocationObserver:
    """Translate a settled gateway result into one payload-free Trace span.

    PostgreSQL budget and invocation ledgers remain authoritative. This adapter
    receives only scalar diagnostics after settlement and has no access to
    ``provider_payload``, prompt artifacts or response artifacts.
    """

    def __init__(self, tracer: RuntimeTracer) -> None:
        self._tracer = tracer

    def observe(self, observation: ModelInvocationObservation) -> None:
        attributes: dict[str, str | int | float | bool | None] = {
            "aidison.event": "model_invocation_completed",
            "aidison.outcome": observation.status,
            "aidison.attempt_count": observation.attempt_count,
            "aidison.fallback_used": observation.fallback_used,
            "gen_ai.provider.name": observation.provider,
            "gen_ai.request.model": observation.model,
            "gen_ai.usage.total_tokens": observation.usage_tokens,
            "error.class": observation.failure.value if observation.failure is not None else None,
        }
        with self._tracer.span(
            name="aidison.model.invoke",
            correlation=TelemetryCorrelation(
                project_id=observation.project_id,
                run_id=observation.run_id,
                task_id=str(observation.task_id),
                invocation_id=observation.logical_invocation_id,
            ),
            kind="agent",
            attributes=attributes,
        ):
            pass


__all__ = ["RuntimeModelInvocationObserver"]
