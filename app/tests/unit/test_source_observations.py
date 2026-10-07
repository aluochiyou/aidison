from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.research.source_observations import (
    ObservationOutcome,
    SourceIdentity,
    SourceKind,
    SourceObservation,
    SourceSnapshot,
    SourceSpan,
    SourceSpanKind,
    source_origin_key,
)


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _source(locator: str = "https://example.test/specification") -> SourceIdentity:
    return SourceIdentity(
        kind=SourceKind.WEB,
        provider="official-site",
        canonical_locator=locator,
    )


def _snapshot(source: SourceIdentity | None = None) -> SourceSnapshot:
    content_hash = _hash("35A peak current")
    return SourceSnapshot(
        source_id=(source or _source()).id,
        content_hash=content_hash,
        artifact_ref=f"artifact+sha256://{content_hash}/{uuid4()}",
        media_type="text/html",
        representation="normalized-document-v1",
        parser_revision="html-normalizer-v1",
    )


def test_source_identity_is_content_addressable_and_does_not_depend_on_construction_order() -> None:
    first = _source()
    second = _source()

    assert first.id == second.id
    assert first.id == _hash(
        '{"canonical_locator":"https://example.test/specification",'
        '"kind":"web","provider":"official-site","schema_version":"source-identity-v1"}'
    )


def test_source_origin_key_collapses_pages_on_the_same_web_origin() -> None:
    first = _source("https://docs.example.test/specification")
    second = _source("https://docs.example.test/product")
    third = _source("https://lab.example.test/review")

    assert source_origin_key(first) == "web-origin:https://docs.example.test"
    assert source_origin_key(first) == source_origin_key(second)
    assert source_origin_key(first) != source_origin_key(third)


def test_not_modified_observation_records_a_new_check_without_copying_snapshot() -> None:
    source = _source()
    snapshot = _snapshot(source)
    observation = SourceObservation(
        agent_run_id=uuid4(),
        task_id=uuid4(),
        basis_hash=_hash("frozen basis"),
        requested_source_id=source.id,
        resolved_source_id=source.id,
        outcome=ObservationOutcome.NOT_MODIFIED,
        observed_at=datetime(2026, 9, 2, 9, 30, tzinfo=UTC),
        http_status=304,
        snapshot_id=snapshot.id,
        response_validator="etag:abc",
    )

    assert observation.snapshot_id == snapshot.id
    assert observation.outcome is ObservationOutcome.NOT_MODIFIED


def test_observation_does_not_confuse_failed_retrieval_with_a_usable_snapshot() -> None:
    source = _source()
    common = {
        "agent_run_id": uuid4(),
        "task_id": uuid4(),
        "basis_hash": _hash("frozen basis"),
        "requested_source_id": source.id,
        "resolved_source_id": source.id,
        "observed_at": datetime(2026, 9, 2, 9, 30, tzinfo=UTC),
    }

    with pytest.raises(ValidationError, match="successful observation"):
        SourceObservation(outcome=ObservationOutcome.FETCHED, **common)

    with pytest.raises(ValidationError, match="failed observation"):
        SourceObservation(
            outcome=ObservationOutcome.FAILED,
            snapshot_id=uuid4(),
            failure_code="network_timeout",
            **common,
        )


def test_source_span_binds_exact_quote_hash_to_one_snapshot_artifact() -> None:
    snapshot = _snapshot()
    span = SourceSpan(
        snapshot_id=snapshot.id,
        artifact_ref=snapshot.artifact_ref,
        content_hash=snapshot.content_hash,
        kind=SourceSpanKind.HTML_TEXT,
        structural_locator="css:main > section:nth-of-type(2) > p:nth-of-type(1)",
        start_char=17,
        end_char=33,
        quote_text="35A peak current",
    )

    assert span.quote_hash == _hash("35A peak current")
    assert span.end_char - span.start_char == len(span.quote_text)


def test_source_contract_rejects_invalid_snapshot_and_span_bindings() -> None:
    snapshot = _snapshot()

    with pytest.raises(ValidationError, match="artifact_ref"):
        SourceSnapshot(
            source_id=_source().id,
            content_hash=snapshot.content_hash,
            artifact_ref=f"artifact+sha256://{_hash('other')}/{uuid4()}",
            media_type="text/html",
            representation="normalized-document-v1",
        )

    with pytest.raises(ValidationError, match="parser_revision"):
        SourceSnapshot(
            source_id=_source().id,
            content_hash=snapshot.content_hash,
            artifact_ref=snapshot.artifact_ref,
            media_type="text/html",
            representation="normalized-document-v1",
        )

    with pytest.raises(ValidationError, match="span character offsets"):
        SourceSpan(
            snapshot_id=snapshot.id,
            artifact_ref=snapshot.artifact_ref,
            content_hash=snapshot.content_hash,
            kind=SourceSpanKind.HTML_TEXT,
            structural_locator="css:main > p",
            start_char=0,
            end_char=3,
            quote_text="too long",
        )
