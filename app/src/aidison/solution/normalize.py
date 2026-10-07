"""Deterministically normalize admitted solution drafts into typed proposal objects."""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.solution.contracts import SolutionContract
from aidison.solution.elements import (
    InterfaceContract,
    InterfaceVerificationStatus,
    SolutionElement,
    SolutionElementSet,
)
from aidison.solution.payloads import SolutionCompositionPayload


class SolutionNormalizationArtifactRefs(BaseModel):
    """Application-owned Artifact refs for one untrusted element draft."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    element_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    configuration_ref: str = Field(min_length=1, max_length=500)
    unresolved_ref: str | None = Field(default=None, max_length=500)


class SolutionNormalizationInput(BaseModel):
    """Only an already-admitted draft plus server-created Artifact references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: SolutionContract
    payload: SolutionCompositionPayload
    artifact_refs: tuple[SolutionNormalizationArtifactRefs, ...] = Field(
        min_length=1, max_length=64
    )

    @model_validator(mode="after")
    def element_artifact_keys_are_unique(self) -> SolutionNormalizationInput:
        keys = [item.element_key for item in self.artifact_refs]
        if len(keys) != len(set(keys)):
            raise ValueError("Solution normalization artifact element keys must be unique")
        return self


def _stable_id(*, run_id: UUID, kind: str, key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"aidison://solution-run/{run_id}/{kind}/{key}")


def normalize_solution_composition(value: SolutionNormalizationInput) -> SolutionElementSet:
    """Create an idempotent typed projection without assigning verification success.

    Caller responsibility: material admission must already have accepted the
    payload.  This function only turns stable keys and application-owned
    Artifact refs into proposal-stage types.  Interface verification always
    starts ``UNVERIFIED`` regardless of model claims.
    """

    drafts_by_key = {item.element_key: item for item in value.payload.elements}
    refs_by_key = {item.element_key: item for item in value.artifact_refs}
    if set(drafts_by_key) != set(refs_by_key):
        raise ValueError("each solution element requires exactly one configuration Artifact ref")

    interfaces_by_key = {
        item.interface_key: InterfaceContract(
            id=_stable_id(
                run_id=value.contract.solution_run_id,
                kind="interface",
                key=item.interface_key,
            ),
            interface_key=item.interface_key,
            producer_module_id=item.producer_module_id,
            consumer_module_id=item.consumer_module_id,
            interface_type=item.interface_type,
            schema_or_unit=item.schema_or_unit,
            direction=item.direction,
            range_or_capacity=item.range_or_capacity,
            quantity_constraints=item.quantity_constraints,
            protocol_or_format=item.protocol_or_format,
            timing_or_frequency=item.timing_or_frequency,
            environmental_conditions=item.environmental_conditions,
            version=1,
            evidence_refs=tuple(
                f"evidence-binding://{evidence_id}" for evidence_id in item.evidence_binding_ids
            ),
            verification_status=InterfaceVerificationStatus.UNVERIFIED,
        )
        for item in value.payload.interfaces
    }
    elements: list[SolutionElement] = []
    for key in sorted(drafts_by_key):
        draft = drafts_by_key[key]
        refs = refs_by_key[key]
        if draft.unresolved and refs.unresolved_ref is None:
            raise ValueError("solution element with unknowns requires an unresolved Artifact ref")
        if not draft.unresolved and refs.unresolved_ref is not None:
            raise ValueError(
                "solution element without unknowns cannot retain an unresolved Artifact ref"
            )
        elements.append(
            SolutionElement(
                id=_stable_id(run_id=value.contract.solution_run_id, kind="element", key=key),
                element_key=draft.element_key,
                module_id=draft.module_id,
                responsibility=draft.responsibility,
                selected_candidate_ref=f"candidate://{draft.selected_candidate_id}",
                configuration_ref=refs.configuration_ref,
                input_interface_ids=tuple(
                    interfaces_by_key[interface_key].id
                    for interface_key in draft.input_interface_keys
                ),
                output_interface_ids=tuple(
                    interfaces_by_key[interface_key].id
                    for interface_key in draft.output_interface_keys
                ),
                constraint_refs=(value.contract.constraint_set_ref,),
                evidence_refs=tuple(
                    f"evidence-binding://{evidence_id}"
                    for evidence_id in draft.evidence_binding_ids
                ),
                decision_refs=value.contract.accepted_decision_refs,
                unresolved_refs=(refs.unresolved_ref,) if refs.unresolved_ref is not None else (),
            )
        )
    return SolutionElementSet(
        elements=tuple(elements),
        interfaces=tuple(interfaces_by_key[key] for key in sorted(interfaces_by_key)),
    )


__all__ = [
    "SolutionNormalizationArtifactRefs",
    "SolutionNormalizationInput",
    "normalize_solution_composition",
]
