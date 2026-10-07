"""Offline evaluation-case construction for verified AgentRun replay bundles."""

from __future__ import annotations

from aidison.evaluation.contracts import EvaluationCase, EvaluationLayer, FixtureKind
from aidison.runtime.replay_bundle import EvaluationReplayBundle


def replay_bundle_evaluation_case(bundle: EvaluationReplayBundle) -> EvaluationCase:
    """Freeze one verified bundle as a trace-review case without external I/O."""
    return EvaluationCase(
        key=f"replay-{bundle.manifest_hash[:32]}",
        title="AgentRun replay bundle remains deterministically reviewable",
        description=(
            "The bundle must prove its checkpoint anchor, recorded invocation outputs and "
            "control-plane event prefix are sufficient for offline evaluation."
        ),
        metric_id="replay_bundle_readiness",
        layer=EvaluationLayer.TRAJECTORY,
        fixture_kind=FixtureKind.TRACE_REVIEW,
        fixture_revision="replay-bundle-v1",
        inputs={"bundle": bundle.model_dump(mode="json")},
    )


__all__ = ["replay_bundle_evaluation_case"]
