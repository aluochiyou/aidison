"""Shared, fail-closed capability checks for runtime-controlled tools."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol


class ToolCapabilityContext(Protocol):
    allowed_tool_classes: tuple[str, ...]
    allowed_effects: tuple[str, ...]
    deadline: datetime


class ToolCapabilityDenied(RuntimeError):
    pass


def require_tool_capability(
    context: ToolCapabilityContext,
    *,
    tool_class: str,
    required_effects: Iterable[str],
) -> None:
    """Require the profile-frozen tool class, effects and remaining lease time."""

    if tool_class not in context.allowed_tool_classes:
        raise ToolCapabilityDenied(f"delegation does not allow {tool_class} tool capability")
    missing_effects = set(required_effects) - set(context.allowed_effects)
    if missing_effects:
        names = ", ".join(sorted(missing_effects))
        raise ToolCapabilityDenied(f"delegation does not allow required effects: {names}")
    if datetime.now(UTC) >= context.deadline:
        raise ToolCapabilityDenied("delegation deadline has passed")
