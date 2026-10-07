"""Pure, explainable routing for structured Module Workstream memory."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from aidison.domain.models import (
    ModuleMemoryItem,
    ModuleMemoryItemStatus,
    ModuleWorkstream,
    ModuleWorkstreamStatus,
)


class MemoryRoute(StrEnum):
    REUSE = "reuse"
    REVALIDATE = "revalidate"
    FRESH = "fresh"


class MemoryRouteDecision(BaseModel):
    """A route is a planning input, not a claim that an old result is current."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: MemoryRoute
    item_id: UUID | None
    reason_code: str


def select_memory_route(
    *,
    workstream: ModuleWorkstream,
    items: tuple[ModuleMemoryItem, ...],
    stable_key: str,
    module_fingerprint: str,
    coverage_fingerprint: str,
    now: datetime,
) -> MemoryRouteDecision:
    """Choose a route without reading private prompts or inferring semantics.

    A matching module and coverage fingerprint proves current applicability
    even when an unrelated Project revision changed the whole Run basis.  A
    live but non-matching item remains useful only as a revalidation input;
    expired, inactive, or retired-workstream items are excluded completely.
    """

    if workstream.status is not ModuleWorkstreamStatus.ACTIVE:
        return MemoryRouteDecision(
            route=MemoryRoute.FRESH,
            item_id=None,
            reason_code="workstream_not_active",
        )

    candidates = tuple(
        item
        for item in items
        if item.workstream_id == workstream.id
        and item.stable_key == stable_key
        and item.status is ModuleMemoryItemStatus.ACTIVE
        and (item.freshness_deadline is None or item.freshness_deadline > now)
    )
    if not candidates:
        return MemoryRouteDecision(
            route=MemoryRoute.FRESH,
            item_id=None,
            reason_code="no_active_applicable_memory",
        )

    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (item.created_at, str(item.id)),
            reverse=True,
        )
    )
    for item in ordered:
        if (
            item.applicability.get("module_fingerprint") == module_fingerprint
            and item.applicability.get("coverage_fingerprint") == coverage_fingerprint
        ):
            return MemoryRouteDecision(
                route=MemoryRoute.REUSE,
                item_id=item.id,
                reason_code="current_and_applicable",
            )
    return MemoryRouteDecision(
        route=MemoryRoute.REVALIDATE,
        item_id=ordered[0].id,
        reason_code="applicability_changed",
    )
