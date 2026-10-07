"""Typed, immutable source lineage before Evidence Admission.

The source chain deliberately separates what was accessed (``SourceIdentity``),
when it was checked (``SourceObservation``), immutable bytes or normalized
content (``SourceSnapshot``), and a rehydratable cited fragment
(``SourceSpan``).  None of these objects asserts that a claim is true or that
the current project may use it as evidence; R2-07 owns that Admission boundary.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_ARTIFACT_REF = re.compile(r"^artifact\+sha256://([a-f0-9]{64})/([0-9a-fA-F-]{36})$")


class SourceKind(StrEnum):
    """Stable source categories used by Coverage policy, not Evidence status."""

    WEB = "web"
    DOCUMENT = "document"
    REPOSITORY = "repository"
    DATASET = "dataset"
    PROJECT_FILE = "project_file"
    USER_UPLOAD = "user_upload"


class ObservationOutcome(StrEnum):
    """The result of one retrieval attempt, independent of snapshot contents."""

    FETCHED = "fetched"
    NOT_MODIFIED = "not_modified"
    FAILED = "failed"


class SourceSpanKind(StrEnum):
    """A formal citation locator, intentionally distinct from a retrieval chunk."""

    HTML_TEXT = "html_text"
    PDF_TEXT = "pdf_text"
    MARKDOWN_TEXT = "markdown_text"
    TABLE_CELL = "table_cell"
    CODE_LINES = "code_lines"
    JSON_VALUE = "json_value"


class SourceIdentity(BaseModel):
    """Canonical identity of source material, excluding mutable retrieved content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "source-identity-v1"
    kind: SourceKind
    provider: str = Field(min_length=1, max_length=120)
    canonical_locator: str = Field(min_length=1, max_length=4_000)
    id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def owns_a_stable_content_identity(self) -> SourceIdentity:
        payload = self.model_dump(mode="json", exclude={"id"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = sha256(canonical.encode()).hexdigest()
        if self.id is not None and self.id != expected:
            raise ValueError("source identity id does not match canonical source content")
        object.__setattr__(self, "id", expected)
        return self


def source_origin_key(source: SourceIdentity) -> str:
    """Return the bounded retrieval origin used for diversity accounting.

    This models a URL origin, not a legal publisher identity: separate
    subdomains may still have the same publisher. It is nevertheless stronger
    than treating two pages on one origin as independent corroboration.
    Non-web locators stay scoped to their provider and canonical locator.
    """

    parsed = urlsplit(source.canonical_locator)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        hostname = parsed.hostname.rstrip(".").lower()
        try:
            port = parsed.port
        except ValueError:
            # A malformed port must not make quality projection fail. Keep this
            # source distinct through its full canonical locator instead.
            return f"{source.kind.value}-origin:{source.provider}:{source.canonical_locator}"
        default_port = 443 if parsed.scheme == "https" else 80
        port_suffix = "" if port is None or port == default_port else f":{port}"
        return f"{source.kind.value}-origin:{parsed.scheme.lower()}://{hostname}{port_suffix}"
    return f"{source.kind.value}-origin:{source.provider}:{source.canonical_locator}"


class SourceSnapshot(BaseModel):
    """One immutable artifact representation of a source's observed content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    source_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_ref: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=200)
    representation: str = Field(min_length=1, max_length=160)
    parser_revision: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("artifact_ref")
    @classmethod
    def is_content_addressed_artifact_ref(cls, value: str) -> str:
        if _ARTIFACT_REF.fullmatch(value) is None:
            raise ValueError("artifact_ref must be a content-addressed artifact reference")
        return value

    @model_validator(mode="after")
    def artifact_ref_matches_snapshot_content(self) -> SourceSnapshot:
        matched = _ARTIFACT_REF.fullmatch(self.artifact_ref)
        assert matched is not None
        if matched.group(1) != self.content_hash:
            raise ValueError("artifact_ref content hash does not match snapshot content_hash")
        if self.representation.startswith("normalized-") and self.parser_revision is None:
            raise ValueError("normalized snapshot requires parser_revision")
        return self


class SourceObservation(BaseModel):
    """An append-only record that one Run task checked a source at a known time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    agent_run_id: UUID
    task_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    requested_source_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    resolved_source_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcome: ObservationOutcome
    observed_at: datetime
    http_status: int | None = Field(default=None, ge=100, le=599)
    snapshot_id: UUID | None = None
    response_validator: str | None = Field(default=None, min_length=1, max_length=1_000)
    failure_code: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def outcome_has_a_non_ambiguous_snapshot_contract(self) -> SourceObservation:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.outcome is ObservationOutcome.FAILED:
            if self.snapshot_id is not None:
                raise ValueError("failed observation cannot reference a snapshot")
            if self.failure_code is None:
                raise ValueError("failed observation requires failure_code")
        else:
            if self.snapshot_id is None:
                raise ValueError("successful observation requires a snapshot reference")
            if self.failure_code is not None:
                raise ValueError("successful observation cannot carry failure_code")
        if self.outcome is ObservationOutcome.NOT_MODIFIED and self.http_status != 304:
            raise ValueError("not_modified observation requires HTTP 304")
        if self.outcome is ObservationOutcome.FETCHED and self.http_status is not None:
            if not 200 <= self.http_status < 300:
                raise ValueError("fetched observation requires a 2xx HTTP status")
        return self


class SourceSpan(BaseModel):
    """A rehydratable exact quote in one immutable Snapshot Artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    snapshot_id: UUID
    artifact_ref: str = Field(min_length=1, max_length=512)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: SourceSpanKind
    structural_locator: str = Field(min_length=1, max_length=2_000)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=1)
    quote_text: str = Field(min_length=1, max_length=16_000)
    quote_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("artifact_ref")
    @classmethod
    def is_content_addressed_artifact_ref(cls, value: str) -> str:
        if _ARTIFACT_REF.fullmatch(value) is None:
            raise ValueError("artifact_ref must be a content-addressed artifact reference")
        return value

    @model_validator(mode="after")
    def binds_quote_and_artifact_without_claiming_admission(self) -> SourceSpan:
        matched = _ARTIFACT_REF.fullmatch(self.artifact_ref)
        assert matched is not None
        if matched.group(1) != self.content_hash:
            raise ValueError("artifact_ref content hash does not match span content_hash")
        if (
            self.end_char <= self.start_char
            or self.end_char - self.start_char != len(self.quote_text)
        ):
            raise ValueError("span character offsets must exactly match quote_text length")
        expected = sha256(self.quote_text.encode()).hexdigest()
        if self.quote_hash is not None and self.quote_hash != expected:
            raise ValueError("quote_hash does not match quote_text")
        object.__setattr__(self, "quote_hash", expected)
        return self
