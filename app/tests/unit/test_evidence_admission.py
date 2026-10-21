from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.artifacts.contracts import ArtifactMetadata, ArtifactStatus
from aidison.domain.models import EvidenceStatus
from aidison.research.evidence_admission import (
    EvidenceAdmissionInput,
    EvidenceAdmissionPolicy,
    EvidenceAdmissionStatus,
    EvidenceCandidate,
    EvidenceInvalidationTarget,
    EvidenceLifecycle,
    EvidenceRelation,
    admit_evidence_candidate,
    derive_evidence_invalidation,
    materialize_admitted_binding,
    to_legacy_domain_evidence_binding,
)
from aidison.research.source_observations import (
    ObservationOutcome,
    SourceIdentity,
    SourceKind,
    SourceObservation,
    SourceSnapshot,
    SourceSpan,
    SourceSpanKind,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _candidate(
    *,
    project_id=None,
    module_id=None,
    basis_hash=None,
) -> tuple[EvidenceCandidate, str]:
    project_id = project_id or uuid4()
    module_id = module_id or uuid4()
    basis_hash = basis_hash or _hash("frozen-basis")
    source = SourceIdentity(
        kind=SourceKind.WEB,
        provider="official-site",
        canonical_locator="https://example.test/motor-specification",
    )
    document = "Motor specification: 35A peak current at 12V."
    quote = "35A peak current"
    start = document.index(quote)
    content_hash = _hash(document)
    snapshot = SourceSnapshot(
        source_id=source.id,
        content_hash=content_hash,
        artifact_ref=f"artifact+sha256://{content_hash}/{uuid4()}",
        media_type="text/plain",
        representation="normalized-document-v1",
        parser_revision="text-normalizer-v1",
    )
    observation = SourceObservation(
        agent_run_id=uuid4(),
        task_id=uuid4(),
        basis_hash=basis_hash,
        requested_source_id=source.id,
        resolved_source_id=source.id,
        outcome=ObservationOutcome.FETCHED,
        observed_at=datetime(2026, 9, 2, 9, 0, tzinfo=UTC),
        http_status=200,
        snapshot_id=snapshot.id,
    )
    return (
        EvidenceCandidate(
            project_id=project_id,
            module_id=module_id,
            basis_hash=basis_hash,
            source=source,
            observation=observation,
            snapshot=snapshot,
            span=SourceSpan(
                snapshot_id=snapshot.id,
                artifact_ref=snapshot.artifact_ref,
                content_hash=content_hash,
                kind=SourceSpanKind.HTML_TEXT,
                structural_locator="text:specification-paragraph",
                start_char=start,
                end_char=start + len(quote),
                quote_text=quote,
            ),
            relation=EvidenceRelation.SUPPORTS,
            claim="The motor peak current is 35A at 12V.",
            applicability=("hardware-revision-a",),
        ),
        document,
    )


def _artifact(candidate: EvidenceCandidate, *, status=ArtifactStatus.PRESENT) -> ArtifactMetadata:
    artifact_hash, artifact_id = candidate.snapshot.artifact_ref.removeprefix(
        "artifact+sha256://"
    ).split("/")
    return ArtifactMetadata(
        id=artifact_id,
        project_id=candidate.project_id,
        agent_run_id=uuid4(),
        basis_hash=_hash("producing-run-basis"),
        kind="normalized_source_document",
        content_hash=artifact_hash,
        size_bytes=1024,
        media_type=candidate.snapshot.media_type,
        storage_key=f"sha256/{artifact_hash[:2]}/{artifact_hash[2:4]}/{artifact_hash}",
        status=status,
    )


def _input(
    candidate: EvidenceCandidate,
    document: str,
    *,
    status=ArtifactStatus.PRESENT,
    allowed_source_kinds=(SourceKind.WEB,),
) -> EvidenceAdmissionInput:
    return EvidenceAdmissionInput(
        candidate=candidate,
        artifact=_artifact(candidate, status=status),
        normalized_document=document,
        active_module_ids=(candidate.module_id,),
        observed_before=datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
        policy=EvidenceAdmissionPolicy(
            allowed_source_kinds=allowed_source_kinds,
            max_observation_age=timedelta(days=7),
        ),
    )


def test_admitted_candidate_materializes_current_domain_binding_without_writing_it() -> None:
    candidate, document = _candidate()
    decision = admit_evidence_candidate(_input(candidate, document))

    assert decision.status is EvidenceAdmissionStatus.ACCEPTED
    binding = materialize_admitted_binding(decision, binding_id=uuid4())
    legacy = to_legacy_domain_evidence_binding(binding)
    assert binding.project_id == candidate.project_id
    assert binding.module_id == candidate.module_id
    assert binding.source_url == candidate.source.canonical_locator
    assert binding.source_span_id == candidate.span.id
    assert binding.lifecycle is EvidenceLifecycle.ACTIVE
    assert legacy.snapshot_hash == candidate.snapshot.content_hash
    assert legacy.span_text == candidate.span.quote_text
    assert legacy.status is EvidenceStatus.SUPPORTED


def test_quarantined_artifact_never_becomes_evidence_even_when_candidate_is_well_formed() -> None:
    candidate, document = _candidate()
    decision = admit_evidence_candidate(
        _input(candidate, document, status=ArtifactStatus.QUARANTINED)
    )

    assert decision.status is EvidenceAdmissionStatus.REJECTED
    assert decision.reason_codes == ("artifact_not_present",)
    assert decision.candidate_id == candidate.id


def test_disallowed_source_kind_is_rejected_before_a_binding_can_be_materialized() -> None:
    candidate, document = _candidate()
    decision = admit_evidence_candidate(
        _input(candidate, document, allowed_source_kinds=(SourceKind.REPOSITORY,))
    )

    assert decision.status is EvidenceAdmissionStatus.REJECTED
    assert decision.reason_codes == ("source_kind_not_allowed",)


def test_stale_observation_or_text_drift_cannot_be_admitted() -> None:
    candidate, document = _candidate()
    stale = EvidenceAdmissionInput(
        candidate=candidate,
        artifact=_artifact(candidate),
        normalized_document=document,
        active_module_ids=(candidate.module_id,),
        observed_before=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
        policy=EvidenceAdmissionPolicy(
            allowed_source_kinds=(SourceKind.WEB,),
            max_observation_age=timedelta(days=7),
        ),
    )
    drifted = _input(candidate, document.replace("35A", "40A"))

    assert admit_evidence_candidate(stale).reason_codes == ("observation_stale",)
    assert admit_evidence_candidate(drifted).reason_codes == ("span_quote_not_rehydratable",)


def test_rejected_candidate_cannot_be_materialized_as_domain_evidence() -> None:
    candidate, document = _candidate()
    rejected = admit_evidence_candidate(
        _input(candidate, document, status=ArtifactStatus.QUARANTINED)
    )

    with pytest.raises(ValueError, match="rejected evidence candidate"):
        materialize_admitted_binding(rejected)


def test_unusable_artifact_emits_deterministic_invalidation_for_all_downstream_projections(
) -> None:
    candidate, document = _candidate()
    accepted = admit_evidence_candidate(_input(candidate, document))
    binding = materialize_admitted_binding(accepted, binding_id=uuid4())
    event = derive_evidence_invalidation(
        binding,
        artifact_status=ArtifactStatus.CORRUPT,
        affected_coverage_keys=("power.compatibility", "power.current", "power.current"),
    )

    assert event is not None
    assert event.project_id == candidate.project_id
    assert event.basis_hash == candidate.basis_hash
    assert event.lifecycle is EvidenceLifecycle.QUARANTINED
    assert event.affected_coverage_keys == ("power.compatibility", "power.current")
    assert event.targets == (
        EvidenceInvalidationTarget.COVERAGE,
        EvidenceInvalidationTarget.VERIFICATION,
        EvidenceInvalidationTarget.IMPACT,
    )


def test_present_artifact_never_emits_false_invalidation() -> None:
    candidate, document = _candidate()
    accepted = admit_evidence_candidate(_input(candidate, document))
    binding = materialize_admitted_binding(accepted, binding_id=uuid4())

    assert (
        derive_evidence_invalidation(
            binding,
            artifact_status=ArtifactStatus.PRESENT,
            affected_coverage_keys=("power.current",),
        )
        is None
    )
