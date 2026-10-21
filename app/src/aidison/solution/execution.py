"""Shared control-flow signals for bounded Solution run executions."""

from __future__ import annotations


class SolutionRunCancelled(RuntimeError):
    """Raised after an active worker acknowledges cancellation at a safe point."""


__all__ = ["SolutionRunCancelled"]
