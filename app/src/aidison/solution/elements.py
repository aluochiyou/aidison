"""Proposal-stage Solution Elements and Interface Contracts.

These are typed, immutable candidate structures.  They are deliberately not
Domain ``SolutionVersion`` entities: only a later approved proposal command
may turn them into canonical project facts.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InterfaceType(StrEnum):
    MECHANICAL = "mechanical"
    ELECTRICAL = "electrical"
    DATA = "data"
    SOFTWARE = "software"
    THERMAL = "thermal"
    TIMING = "timing"


class InterfaceVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    CONFLICTED = "conflicted"
    BLOCKED = "blocked"


class InterfaceQuantityConstraint(BaseModel):
    """One machine-checkable producer supply and consumer demand interval.

    Units are intentionally explicit on both sides.  The current deterministic
    checker requires exact equality rather than guessing a conversion from
    free-text specifications.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,119}$")
    producer_unit: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9*^/._-]{0,39}$")
    consumer_unit: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9*^/._-]{0,39}$")
    producer_minimum: Decimal
    producer_maximum: Decimal
    consumer_minimum: Decimal
    consumer_maximum: Decimal

    @model_validator(mode="after")
    def ranges_are_ordered(self) -> InterfaceQuantityConstraint:
        if self.producer_minimum > self.producer_maximum:
            raise ValueError("producer quantity interval must be ordered")
        if self.consumer_minimum > self.consumer_maximum:
            raise ValueError("consumer quantity interval must be ordered")
        return self


class InterfaceContract(BaseModel):
    """One typed, proposal-stage boundary from a producing module to a consumer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
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
    version: int = Field(ge=1)
    evidence_refs: tuple[str, ...] = Field(max_length=128)
    verification_status: InterfaceVerificationStatus

    @model_validator(mode="after")
    def boundary_is_between_two_modules_and_has_unique_references(self) -> InterfaceContract:
        if self.producer_module_id == self.consumer_module_id:
            raise ValueError("InterfaceContract cannot connect a module to itself")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("InterfaceContract evidence_refs must be unique")
        if len(set(self.environmental_conditions)) != len(self.environmental_conditions):
            raise ValueError("InterfaceContract environmental_conditions must be unique")
        metrics = [item.metric_key for item in self.quantity_constraints]
        if len(set(metrics)) != len(metrics):
            raise ValueError("InterfaceContract quantity constraint metrics must be unique")
        return self


class SolutionElement(BaseModel):
    """One structured proposal component scoped to exactly one system module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    element_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    module_id: UUID
    responsibility: str = Field(min_length=1, max_length=4_000)
    selected_candidate_ref: str = Field(min_length=1, max_length=500)
    configuration_ref: str = Field(min_length=1, max_length=500)
    input_interface_ids: tuple[UUID, ...] = Field(max_length=64)
    output_interface_ids: tuple[UUID, ...] = Field(max_length=64)
    constraint_refs: tuple[str, ...] = Field(max_length=64)
    evidence_refs: tuple[str, ...] = Field(max_length=128)
    decision_refs: tuple[str, ...] = Field(max_length=64)
    unresolved_refs: tuple[str, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def element_references_are_unique_and_directional(self) -> SolutionElement:
        collections = (
            ("input_interface_ids", self.input_interface_ids),
            ("output_interface_ids", self.output_interface_ids),
            ("constraint_refs", self.constraint_refs),
            ("evidence_refs", self.evidence_refs),
            ("decision_refs", self.decision_refs),
            ("unresolved_refs", self.unresolved_refs),
        )
        for field_name, values in collections:
            if len(set(values)) != len(values):
                raise ValueError(f"SolutionElement {field_name} must be unique")
        if set(self.input_interface_ids) & set(self.output_interface_ids):
            raise ValueError("SolutionElement cannot consume and produce the same interface")
        return self


class SolutionElementSet(BaseModel):
    """A normalized proposal projection with one element per selected module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    elements: tuple[SolutionElement, ...] = Field(min_length=1, max_length=64)
    interfaces: tuple[InterfaceContract, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def references_are_closed_and_modules_are_unique(self) -> SolutionElementSet:
        modules = [item.module_id for item in self.elements]
        if len(set(modules)) != len(modules):
            raise ValueError("SolutionElementSet can contain only one element per module")
        interface_ids = {item.id for item in self.interfaces}
        element_by_module = {item.module_id: item for item in self.elements}
        for interface in self.interfaces:
            if interface.producer_module_id not in element_by_module:
                raise ValueError("InterfaceContract producer is outside SolutionElementSet")
            if interface.consumer_module_id not in element_by_module:
                raise ValueError("InterfaceContract consumer is outside SolutionElementSet")
        for element in self.elements:
            references = (*element.input_interface_ids, *element.output_interface_ids)
            if not set(references) <= interface_ids:
                raise ValueError("SolutionElement references an unknown InterfaceContract")
        return self


__all__ = [
    "InterfaceContract",
    "InterfaceQuantityConstraint",
    "InterfaceType",
    "InterfaceVerificationStatus",
    "SolutionElement",
    "SolutionElementSet",
]
