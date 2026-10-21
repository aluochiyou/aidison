"""Deterministic Coverage Contract compilation and orchestration shape routing.

R2 starts by separating what a Run must answer from how it will execute.  This
module is deliberately pure: it creates no tasks, performs no model calls and
does not mutate Domain or Control state.
"""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.domain.models import Module, ResearchStrategyTask


class CoveragePriority(StrEnum):
    """The contractual importance of a research question."""

    MUST = "must"
    SHOULD = "should"
    MAY = "may"


class CoverageKey(BaseModel):
    """One stable question that must be answered or explicitly left unresolved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    question: str = Field(min_length=1, max_length=4_000)
    priority: CoveragePriority
    module_ids: tuple[str, ...] = Field(max_length=16)
    required_source_kinds: tuple[str, ...] = Field(max_length=16)
    min_distinct_sources: int = Field(default=1, ge=1, le=5)
    min_distinct_origins: int = Field(default=1, ge=1, le=5)
    requires_independent_verification: bool = False

    @model_validator(mode="before")
    @classmethod
    def null_legacy_source_minimum_uses_safe_default(cls, value: Any) -> Any:
        """Treat a pre-field-addition JSON ``null`` as the old one-source rule.

        This is only a read compatibility rule for already-frozen Coverage
        Contracts.  New compilation always writes an explicit integer, and no
        runtime or task state is migrated.
        """

        if isinstance(value, dict) and value.get("min_distinct_sources") is None:
            return {key: item for key, item in value.items() if key != "min_distinct_sources"}
        return value


class CoverageContract(BaseModel):
    """Immutable, content-addressable coverage scope for one frozen Run basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "coverage-contract-v1"
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    objective: str = Field(min_length=1, max_length=2_000)
    context_lines: tuple[str, ...] = Field(default=(), max_length=12)
    keys: tuple[CoverageKey, ...] = Field(min_length=1, max_length=128)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="before")
    @classmethod
    def legacy_null_source_minimum_recomputes_logical_contract_hash(cls, value: Any) -> Any:
        """Keep old serialized contracts readable after adding the source rule.

        Artifact byte integrity is verified by ``ContentAddressedArtifactStore``.
        The embedded logical hash, however, was calculated when the field was
        serialized as ``null``.  Drop only that stale logical hash so the
        normalized contract can calculate its current semantic hash.
        """

        if not isinstance(value, dict):
            return value
        keys = value.get("keys")
        has_legacy_null_minimum = isinstance(keys, (list, tuple)) and any(
            isinstance(item, dict) and item.get("min_distinct_sources") is None
            for item in keys
        )
        if "context_lines" in value and not has_legacy_null_minimum:
            return value
        return {key: item for key, item in value.items() if key != "content_hash"}

    @model_validator(mode="after")
    def has_unique_stable_keys_and_hash(self) -> CoverageContract:
        if len({item.key for item in self.keys}) != len(self.keys):
            raise ValueError("CoverageContract keys must be unique")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_hash = sha256(canonical.encode()).hexdigest()
        if self.content_hash is not None and self.content_hash != expected_hash:
            raise ValueError("content_hash does not match CoverageContract content")
        object.__setattr__(self, "content_hash", expected_hash)
        return self


class CoverageCompileInput(BaseModel):
    """Only approved basis material accepted by the deterministic compiler."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    objective: str = Field(min_length=1, max_length=2_000)
    modules: tuple[Module, ...] = Field(min_length=1, max_length=64)
    strategy_tasks: tuple[ResearchStrategyTask, ...] = Field(default=())
    context_lines: tuple[str, ...] = Field(default=(), max_length=12)
    requires_independent_verification: bool = False
    minimum_evidence_sources_for_must: int = Field(default=1, ge=1, le=5)
    minimum_evidence_origins_for_must: int = Field(default=1, ge=1, le=5)

    @model_validator(mode="after")
    def module_keys_are_unique(self) -> CoverageCompileInput:
        if len({item.key for item in self.modules}) != len(self.modules):
            raise ValueError("CoverageCompileInput module keys must be unique")
        if any(not item.strip() or len(item) > 300 for item in self.context_lines):
            raise ValueError("Coverage context lines must be non-empty and at most 300 characters")
        module_ids = {item.id for item in self.modules}
        if any(
            len(item.module_ids) != 1 or item.module_ids[0] not in module_ids
            for item in self.strategy_tasks
        ):
            raise ValueError("research strategy task must target one scoped module")
        return self


class ModuleStrategy(StrEnum):
    SINGLE = "single"
    BATCHED = "batched"
    PARALLEL = "parallel"


class ModuleDepth(StrEnum):
    DIRECT = "direct"
    DECOMPOSED = "decomposed"


class VerificationMode(StrEnum):
    ADMISSION_ONLY = "admission_only"
    INDEPENDENT = "independent"
    INTEGRATION = "integration"


class OrchestrationSignals(BaseModel):
    """Observable planning signals, not an opaque model-generated complexity score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_key_count: int = Field(ge=1, le=128)
    module_scope_count: int = Field(ge=1, le=64)
    source_strategy_count: int = Field(ge=1, le=16)
    max_concurrency: int = Field(ge=1, le=16)
    estimated_context_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_sandbox_isolation: bool = False
    requires_independent_verification: bool = False
    cross_module_compatibility: bool = False


class OrchestrationShape(BaseModel):
    """Smallest sufficient execution shape chosen by deterministic policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module_strategy: ModuleStrategy
    module_depth: ModuleDepth
    verification: VerificationMode
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)


def _normalise_question(value: str) -> str:
    return " ".join(value.split())


def compile_coverage_contract(value: CoverageCompileInput) -> CoverageContract:
    """Compile approved module acceptance and questions without model discretion.

    Acceptance criteria are always ``MUST``.  Open questions are ``SHOULD``;
    no caller can downgrade a hard constraint by changing an orchestration
    preference.  Input order does not influence the emitted contract or hash.
    """

    must_evidence_source_minimum = max(
        value.minimum_evidence_sources_for_must,
        2 if value.requires_independent_verification else 1,
    )
    must_evidence_origin_minimum = max(
        value.minimum_evidence_origins_for_must,
        2 if value.requires_independent_verification else 1,
    )
    keys: list[CoverageKey] = [
        CoverageKey(
            key="project.objective",
            question=_normalise_question(value.objective),
            priority=CoveragePriority.MUST,
            module_ids=(),
            required_source_kinds=("project_basis",),
        )
    ]
    context_lines = tuple(sorted({_normalise_question(item) for item in value.context_lines}))
    if context_lines:
        keys.append(
            CoverageKey(
                key="project.requirements",
                question="Candidate options must satisfy the frozen project requirements.",
                priority=CoveragePriority.MUST,
                module_ids=(),
                required_source_kinds=("project_basis",),
            )
        )
    for module in sorted(value.modules, key=lambda item: item.key):
        module_id = str(module.id)
        acceptance = tuple(_normalise_question(item) for item in module.acceptance if item.strip())
        if acceptance:
            for index, question in enumerate(acceptance, start=1):
                keys.append(
                    CoverageKey(
                        key=f"{module.key}.acceptance.{index:02d}",
                        question=question,
                        priority=CoveragePriority.MUST,
                        module_ids=(module_id,),
                        required_source_kinds=("evidence", "project_basis"),
                        min_distinct_sources=must_evidence_source_minimum,
                        min_distinct_origins=must_evidence_origin_minimum,
                        requires_independent_verification=value.requires_independent_verification,
                    )
                )
        else:
            keys.append(
                CoverageKey(
                    key=f"{module.key}.responsibility",
                    question=_normalise_question(module.responsibility),
                    priority=CoveragePriority.MUST,
                    module_ids=(module_id,),
                    required_source_kinds=("evidence", "project_basis"),
                    min_distinct_sources=must_evidence_source_minimum,
                    min_distinct_origins=must_evidence_origin_minimum,
                    requires_independent_verification=value.requires_independent_verification,
                )
            )
        for index, question in enumerate(
            (item for item in module.open_questions if item.strip()), start=1
        ):
            keys.append(
                CoverageKey(
                    key=f"{module.key}.question.{index:02d}",
                    question=_normalise_question(question),
                    priority=CoveragePriority.SHOULD,
                    module_ids=(module_id,),
                    required_source_kinds=("evidence",),
                )
            )
    for task in sorted(value.strategy_tasks, key=lambda item: item.task_key):
        keys.append(
            CoverageKey(
                key=f"strategy.{task.task_key}",
                question=_normalise_question(task.objective),
                priority=CoveragePriority(task.priority),
                module_ids=(str(task.module_ids[0]),),
                required_source_kinds=("evidence", "project_basis"),
                min_distinct_sources=(
                    must_evidence_source_minimum if task.priority == "must" else 1
                ),
                min_distinct_origins=(
                    must_evidence_origin_minimum if task.priority == "must" else 1
                ),
                requires_independent_verification=(
                    value.requires_independent_verification and task.priority == "must"
                ),
            )
        )
    return CoverageContract(
        basis_hash=value.basis_hash,
        objective=_normalise_question(value.objective),
        context_lines=context_lines,
        keys=tuple(sorted(keys, key=lambda item: item.key)),
    )


def select_orchestration_shape(signals: OrchestrationSignals) -> OrchestrationShape:
    """Choose a bounded shape from explainable hard and soft policy triggers."""

    reasons: list[str] = []
    depth = ModuleDepth.DIRECT
    verification = VerificationMode.ADMISSION_ONLY

    if signals.estimated_context_ratio >= 0.70:
        reasons.append("context_isolation")
        depth = ModuleDepth.DECOMPOSED
    if signals.requires_sandbox_isolation:
        reasons.append("sandbox_isolation")
        depth = ModuleDepth.DECOMPOSED
    if signals.requires_independent_verification:
        reasons.append("independent_verification")
        verification = VerificationMode.INDEPENDENT
    if signals.cross_module_compatibility:
        reasons.append("cross_module_integration")
        depth = ModuleDepth.DECOMPOSED
        verification = VerificationMode.INTEGRATION

    soft_reasons: list[str] = []
    if signals.coverage_key_count >= 2:
        soft_reasons.append("coverage_fanout")
    if signals.module_scope_count >= 2:
        soft_reasons.append("module_separation")
    if signals.source_strategy_count >= 2:
        soft_reasons.append("source_diversity")

    strategy = ModuleStrategy.SINGLE
    if len(soft_reasons) >= 2:
        reasons.extend(soft_reasons)
        depth = ModuleDepth.DECOMPOSED
        if signals.max_concurrency > 1:
            strategy = ModuleStrategy.PARALLEL
        else:
            strategy = ModuleStrategy.BATCHED
            reasons.append("concurrency_capped")

    if not reasons:
        reasons.append("default_minimal")
    return OrchestrationShape(
        module_strategy=strategy,
        module_depth=depth,
        verification=verification,
        reason_codes=tuple(reasons),
    )


__all__ = [
    "CoverageCompileInput",
    "CoverageContract",
    "CoverageKey",
    "CoveragePriority",
    "ModuleDepth",
    "ModuleStrategy",
    "OrchestrationShape",
    "OrchestrationSignals",
    "VerificationMode",
    "compile_coverage_contract",
    "select_orchestration_shape",
]
