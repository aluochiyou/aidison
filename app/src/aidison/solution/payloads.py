"""Untrusted structured output accepted from the Solution capability only.

The payload deliberately contains draft identifiers and JSON-compatible
configuration values, never Artifact references chosen by the model and never
canonical ``SolutionVersion`` fields.  A deterministic normalizer validates
every identifier against the frozen SolutionContract before anything can be
admitted.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.solution.elements import InterfaceQuantityConstraint, InterfaceType


class SolutionElementDraft(BaseModel):
    """Untrusted proposed element for one known module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    element_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    module_id: UUID
    responsibility: str = Field(min_length=1, max_length=4_000)
    selected_candidate_id: UUID
    configuration: dict[str, Any] = Field(default_factory=dict, max_length=128)
    input_interface_keys: tuple[str, ...] = Field(default=(), max_length=64)
    output_interface_keys: tuple[str, ...] = Field(default=(), max_length=64)
    evidence_binding_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    unresolved: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def references_are_unique_and_directional(self) -> SolutionElementDraft:
        collections = (
            ("input_interface_keys", self.input_interface_keys),
            ("output_interface_keys", self.output_interface_keys),
            ("evidence_binding_ids", self.evidence_binding_ids),
            ("unresolved", self.unresolved),
        )
        for field_name, values in collections:
            if len(set(values)) != len(values):
                raise ValueError(f"SolutionElementDraft {field_name} must be unique")
        if set(self.input_interface_keys) & set(self.output_interface_keys):
            raise ValueError("SolutionElementDraft cannot consume and produce the same interface")
        return self


class InterfaceContractDraft(BaseModel):
    """Untrusted interface content; verification status is assigned by Control later."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interface_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    producer_module_id: UUID
    consumer_module_id: UUID
    interface_type: InterfaceType
    schema_or_unit: str = Field(min_length=1, max_length=500)
    direction: str = Field(min_length=1, max_length=80)
    range_or_capacity: str = Field(min_length=1, max_length=1_000)
    quantity_constraints: tuple[InterfaceQuantityConstraint, ...] = Field(default=(), max_length=32)
    protocol_or_format: str | None = Field(default=None, max_length=500)
    timing_or_frequency: str | None = Field(default=None, max_length=500)
    environmental_conditions: tuple[str, ...] = Field(default=(), max_length=32)
    evidence_binding_ids: tuple[UUID, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def boundary_is_not_self_referential(self) -> InterfaceContractDraft:
        if self.producer_module_id == self.consumer_module_id:
            raise ValueError("InterfaceContractDraft cannot connect a module to itself")
        if len(set(self.evidence_binding_ids)) != len(self.evidence_binding_ids):
            raise ValueError("InterfaceContractDraft evidence_binding_ids must be unique")
        metrics = [item.metric_key for item in self.quantity_constraints]
        if len(set(metrics)) != len(metrics):
            raise ValueError("InterfaceContractDraft quantity constraint metrics must be unique")
        return self


class SolutionCompositionPayload(BaseModel):
    """Entire untrusted output from one Solution Composer model invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    elements: tuple[SolutionElementDraft, ...] = Field(min_length=1, max_length=64)
    interfaces: tuple[InterfaceContractDraft, ...] = Field(max_length=128)
    risks: tuple[str, ...] = Field(default=(), max_length=64)
    unknowns: tuple[str, ...] = Field(default=(), max_length=64)
    consequences: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def stable_keys_are_unique_and_interface_links_are_closed(self) -> SolutionCompositionPayload:
        element_keys = [item.element_key for item in self.elements]
        if len(set(element_keys)) != len(element_keys):
            raise ValueError("SolutionCompositionPayload element keys must be unique")
        module_ids = [item.module_id for item in self.elements]
        if len(set(module_ids)) != len(module_ids):
            raise ValueError("SolutionCompositionPayload modules must be unique")
        by_interface = {item.interface_key: item for item in self.interfaces}
        if len(by_interface) != len(self.interfaces):
            raise ValueError("SolutionCompositionPayload interface keys must be unique")
        for element in self.elements:
            for key in element.input_interface_keys:
                interface = by_interface.get(key)
                if interface is None or interface.consumer_module_id != element.module_id:
                    raise ValueError("input interface must target its declaring element module")
            for key in element.output_interface_keys:
                interface = by_interface.get(key)
                if interface is None or interface.producer_module_id != element.module_id:
                    raise ValueError(
                        "output interface must originate from its declaring element module"
                    )
        return self


__all__ = [
    "InterfaceContractDraft",
    "SolutionCompositionPayload",
    "SolutionElementDraft",
]
