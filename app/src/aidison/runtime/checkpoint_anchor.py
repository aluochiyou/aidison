"""Read-only verification for Control-owned LangGraph checkpoint anchors."""

from __future__ import annotations

from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver

from aidison.runtime.agent_runs import AdmittedCheckpointRef
from aidison.runtime.minimal_graph import admitted_checkpoint_config


class CheckpointAnchorVerificationError(RuntimeError):
    """A persisted checkpoint reference cannot be verified against its store."""


class CheckpointAnchorMissingError(CheckpointAnchorVerificationError):
    """The exact admitted checkpoint no longer exists in the configured store."""


async def verify_admitted_checkpoint_anchor(
    *,
    checkpointer: BaseCheckpointSaver[Any],
    checkpoint: AdmittedCheckpointRef,
) -> None:
    """Require an exact checkpoint-store hit without running a LangGraph node."""
    expected_config = admitted_checkpoint_config(checkpoint)
    checkpoint_tuple = await checkpointer.aget_tuple(cast(Any, expected_config))
    if checkpoint_tuple is None:
        raise CheckpointAnchorMissingError(
            "admitted checkpoint is absent from the configured checkpoint store"
        )
    configurable = checkpoint_tuple.config.get("configurable", {})
    expected = expected_config["configurable"]
    if any(configurable.get(key) != value for key, value in expected.items()):
        raise CheckpointAnchorVerificationError(
            "checkpoint store returned a different thread, namespace, or checkpoint identity"
        )


__all__ = [
    "CheckpointAnchorMissingError",
    "CheckpointAnchorVerificationError",
    "verify_admitted_checkpoint_anchor",
]
