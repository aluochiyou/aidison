"""Deterministic, bounded private-context assembly for Capability invocations.

This module deliberately assembles a *derived* prompt projection.  It neither
owns Project facts nor turns a cache hit into evidence admission or permission.
The rendered text remains invocation-private; callers persist its manifest and
bytes through the normal Artifact boundary when they need replayability.
"""

from __future__ import annotations

import json
import math
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.research.langgraph_contracts import TaskEnvelope
from aidison.security.secrets import SecretExposureError, assert_secret_free


class ContextAssemblyError(RuntimeError):
    """Base error for deterministic context compilation."""


class ContextTooLargeError(ContextAssemblyError):
    """The minimum safe context cannot fit the frozen model budget."""


class ContextLayer(StrEnum):
    POLICY = "policy"
    PROFILE = "profile"
    OUTPUT = "output"
    CANONICAL = "canonical"
    ADMITTED = "admitted"
    TASK = "task"
    MEMORY = "memory"
    UNTRUSTED = "untrusted"


class ContextPriority(StrEnum):
    MUST = "must"
    SHOULD = "should"
    MAY = "may"


class ContextTransform(StrEnum):
    EXACT = "exact"
    EXCERPT = "excerpt"


_LAYER_ORDER = {
    ContextLayer.POLICY: 0,
    ContextLayer.PROFILE: 1,
    ContextLayer.OUTPUT: 2,
    ContextLayer.CANONICAL: 3,
    ContextLayer.ADMITTED: 4,
    ContextLayer.TASK: 5,
    ContextLayer.MEMORY: 6,
    ContextLayer.UNTRUSTED: 7,
}
_PRIORITY_ORDER = {
    ContextPriority.MUST: 0,
    ContextPriority.SHOULD: 1,
    ContextPriority.MAY: 2,
}
_STABLE_LAYERS = {
    ContextLayer.POLICY,
    ContextLayer.PROFILE,
    ContextLayer.OUTPUT,
    ContextLayer.CANONICAL,
    ContextLayer.ADMITTED,
}


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ContextAssemblyPolicy(BaseModel):
    """Pinned context-window policy for a single compiled model input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_context_window_tokens: int = Field(gt=0, le=10_000_000)
    max_output_tokens: int = Field(ge=0, le=10_000_000)
    tool_loop_reserve_tokens: int = Field(ge=0, le=10_000_000)
    safety_margin_tokens: int = Field(ge=0, le=10_000_000)
    policy_version: str = Field(min_length=1, max_length=120)
    authorization_scope_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tokenizer_binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    renderer_version: str = Field(min_length=1, max_length=120)
    provider_cache_policy_hash: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )

    @property
    def input_token_budget(self) -> int:
        return (
            self.model_context_window_tokens
            - self.max_output_tokens
            - self.tool_loop_reserve_tokens
            - self.safety_margin_tokens
        )

    @model_validator(mode="after")
    def input_budget_is_positive(self) -> ContextAssemblyPolicy:
        if self.input_token_budget <= 0:
            raise ValueError("ContextAssemblyPolicy leaves no input token budget")
        return self


class ContextCandidate(BaseModel):
    """One already-authorized context fragment, not a retrieval request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_ref: str = Field(min_length=1, max_length=500)
    layer: ContextLayer
    priority: ContextPriority
    authority: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=2_000_000)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    excerpt: str | None = Field(default=None, min_length=1, max_length=2_000_000)
    excerpt_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_basis_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    stable_prefix: bool = False

    @model_validator(mode="after")
    def fragment_is_bound_and_hashed(self) -> ContextCandidate:
        if _sha256_text(self.content) != self.content_hash:
            raise ValueError("context candidate content_hash does not match content")
        if (self.excerpt is None) != (self.excerpt_hash is None):
            raise ValueError("context candidate excerpt and excerpt_hash must appear together")
        if self.excerpt is not None and _sha256_text(self.excerpt) != self.excerpt_hash:
            raise ValueError("context candidate excerpt_hash does not match excerpt")
        if self.stable_prefix and self.layer not in _STABLE_LAYERS:
            raise ValueError("only stable server/admitted layers may enter a prompt prefix")
        if (
            self.layer
            in {
                ContextLayer.POLICY,
                ContextLayer.PROFILE,
                ContextLayer.OUTPUT,
            }
            and not self.stable_prefix
        ):
            raise ValueError("policy, profile, and output contracts must be stable prefix material")
        if self.layer is ContextLayer.UNTRUSTED and self.authority != "untrusted":
            raise ValueError("untrusted context must be labelled with untrusted authority")
        if self.layer is not ContextLayer.UNTRUSTED and self.authority == "untrusted":
            raise ValueError("trusted context cannot claim untrusted authority")
        return self


class ContextItem(BaseModel):
    """One selected, auditable fragment in the compiled prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_ref: str
    layer: ContextLayer
    priority: ContextPriority
    authority: str
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    transform: ContextTransform
    selection_reason: str
    stable_prefix: bool
    original_token_estimate: int = Field(ge=0)
    included_token_estimate: int = Field(ge=0)
    rendered_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ContextOmission(BaseModel):
    """An explicit non-selection so context loss is observable and replayable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_ref: str
    layer: ContextLayer
    priority: ContextPriority
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason_code: str


class ContextManifest(BaseModel):
    """Compact selection projection; rendered bytes remain private/Artifact-backed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    task_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    context_policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    items: tuple[ContextItem, ...]
    omitted_items: tuple[ContextOmission, ...]
    input_token_estimate: int = Field(ge=0)
    output_reserve_tokens: int = Field(ge=0)
    tool_loop_reserve_tokens: int = Field(ge=0)
    safety_margin_tokens: int = Field(ge=0)
    prefix_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    cache_lane_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ContextAssemblyInput(BaseModel):
    """Resolved, authorized fragments supplied to a private capability only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ContextAssemblyPolicy
    candidates: tuple[ContextCandidate, ...] = Field(max_length=256)
    profile_definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ContextAssembly(BaseModel):
    """Private result of deterministic selection and prompt rendering."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: ContextManifest
    rendered_context: str
    stable_prefix: str
    cache_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    cache_hit: bool = False


class TokenEstimator(Protocol):
    """Tokenizer adapter; estimates are never a source of billing truth."""

    def estimate(self, text: str) -> int: ...


class ApproximateTokenEstimator:
    """Small process-local fallback until a frozen provider tokenizer is wired."""

    def __init__(self) -> None:
        self._by_content_hash: dict[str, int] = {}

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        content_hash = _sha256_text(text)
        cached = self._by_content_hash.get(content_hash)
        if cached is not None:
            return cached
        # A conservative, deterministic fallback.  The policy pins the tokenizer
        # binding so production composition can replace this with a provider
        # tokenizer without changing the selection contract.
        estimate = max(1, math.ceil(len(text) / 4))
        self._by_content_hash[content_hash] = estimate
        return estimate


class ContextAssemblyEngine:
    """Build bounded context with an in-process, disposable assembly cache."""

    def __init__(self, *, token_estimator: TokenEstimator | None = None) -> None:
        self._token_estimator = token_estimator or ApproximateTokenEstimator()
        self._cache: dict[str, ContextAssembly] = {}

    def assemble(
        self,
        *,
        task: TaskEnvelope,
        policy: ContextAssemblyPolicy,
        candidates: tuple[ContextCandidate, ...],
        profile_definition_hash: str = "0" * 64,
        model_binding_hash: str = "0" * 64,
    ) -> ContextAssembly:
        self._validate_candidates(task=task, candidates=candidates)
        cache_key = self._cache_key(
            task=task,
            policy=policy,
            candidates=candidates,
            profile_definition_hash=profile_definition_hash,
            model_binding_hash=model_binding_hash,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(update={"cache_hit": True})

        selected: list[tuple[ContextCandidate, ContextTransform, str, int, int]] = []
        omitted: list[ContextOmission] = []
        used_tokens = 0
        for candidate in sorted(candidates, key=self._selection_key):
            exact = self._render_candidate(candidate=candidate, content=candidate.content)
            exact_tokens = self._token_estimator.estimate(exact)
            original_tokens = exact_tokens
            selected_transform = ContextTransform.EXACT
            rendered = exact
            included_tokens = exact_tokens
            if used_tokens + exact_tokens > policy.input_token_budget:
                if candidate.excerpt is not None:
                    excerpt = self._render_candidate(candidate=candidate, content=candidate.excerpt)
                    excerpt_tokens = self._token_estimator.estimate(excerpt)
                    if used_tokens + excerpt_tokens <= policy.input_token_budget:
                        selected_transform = ContextTransform.EXCERPT
                        rendered = excerpt
                        included_tokens = excerpt_tokens
                    else:
                        self._omit_or_raise(
                            candidate=candidate,
                            omitted=omitted,
                            reason_code="budget_exhausted",
                        )
                        continue
                else:
                    self._omit_or_raise(
                        candidate=candidate,
                        omitted=omitted,
                        reason_code="budget_exhausted",
                    )
                    continue
            used_tokens += included_tokens
            selected.append(
                (candidate, selected_transform, rendered, original_tokens, included_tokens)
            )

        prefix_parts: list[str] = []
        suffix_parts: list[str] = []
        items: list[ContextItem] = []
        for candidate, transform, rendered, original_tokens, included_tokens in sorted(
            selected,
            key=lambda item: self._physical_key(item[0]),
        ):
            if candidate.stable_prefix:
                prefix_parts.append(rendered)
            else:
                suffix_parts.append(rendered)
            items.append(
                ContextItem(
                    source_ref=candidate.source_ref,
                    layer=candidate.layer,
                    priority=candidate.priority,
                    authority=candidate.authority,
                    source_hash=candidate.content_hash,
                    transform=transform,
                    selection_reason="must"
                    if candidate.priority is ContextPriority.MUST
                    else "budget_ranked",
                    stable_prefix=candidate.stable_prefix,
                    original_token_estimate=original_tokens,
                    included_token_estimate=included_tokens,
                    rendered_hash=_sha256_text(rendered),
                )
            )

        stable_prefix = "\n\n".join(prefix_parts)
        rendered_context = "\n\n".join((*prefix_parts, *suffix_parts))
        context_policy_hash = _sha256_text(_canonical_json(policy.model_dump(mode="json")))
        cache_lane_hash = _sha256_text(
            _canonical_json(
                {
                    "profile_definition_hash": profile_definition_hash,
                    "model_binding_hash": model_binding_hash,
                    "policy_version": policy.policy_version,
                    "renderer_version": policy.renderer_version,
                    "authorization_scope_hash": policy.authorization_scope_hash,
                    "provider_cache_policy_hash": policy.provider_cache_policy_hash,
                    "stable_source_hashes": [
                        item.source_hash for item in items if item.stable_prefix
                    ],
                }
            )
        )
        manifest_values = {
            "run_id": task.run_id,
            "task_id": task.id,
            "basis_hash": task.basis_hash,
            "profile_definition_hash": profile_definition_hash,
            "model_binding_hash": model_binding_hash,
            "context_policy_hash": context_policy_hash,
            "items": tuple(items),
            "omitted_items": tuple(
                sorted(omitted, key=lambda item: (_LAYER_ORDER[item.layer], item.source_ref))
            ),
            "input_token_estimate": used_tokens,
            "output_reserve_tokens": policy.max_output_tokens,
            "tool_loop_reserve_tokens": policy.tool_loop_reserve_tokens,
            "safety_margin_tokens": policy.safety_margin_tokens,
            "prefix_hash": _sha256_text(stable_prefix),
            "cache_lane_hash": cache_lane_hash,
        }
        manifest = ContextManifest.model_validate(
            {
                **manifest_values,
                "manifest_hash": _sha256_text(_canonical_json(self._jsonable(manifest_values))),
            }
        )
        assembly = ContextAssembly(
            manifest=manifest,
            rendered_context=rendered_context,
            stable_prefix=stable_prefix,
            cache_key=cache_key,
        )
        self._cache[cache_key] = assembly
        return assembly

    @staticmethod
    def _cache_key(
        *,
        task: TaskEnvelope,
        policy: ContextAssemblyPolicy,
        candidates: tuple[ContextCandidate, ...],
        profile_definition_hash: str,
        model_binding_hash: str,
    ) -> str:
        """Key only derived computation, never a durable authorization shortcut."""

        return _sha256_text(
            _canonical_json(
                {
                    "task_envelope": task.model_dump(mode="json"),
                    "basis_hash": task.basis_hash,
                    "profile_definition_hash": profile_definition_hash,
                    "model_binding_hash": model_binding_hash,
                    "context_policy": policy.model_dump(mode="json"),
                    "ordered_sources": [
                        {
                            "source_ref": candidate.source_ref,
                            "content_hash": candidate.content_hash,
                            "excerpt_hash": candidate.excerpt_hash,
                            "layer": candidate.layer.value,
                            "priority": candidate.priority.value,
                            "authority": candidate.authority,
                            "source_basis_hash": candidate.source_basis_hash,
                            "stable_prefix": candidate.stable_prefix,
                        }
                        for candidate in sorted(candidates, key=lambda item: item.source_ref)
                    ],
                }
            )
        )

    def _validate_candidates(
        self, *, task: TaskEnvelope, candidates: tuple[ContextCandidate, ...]
    ) -> None:
        refs = [candidate.source_ref for candidate in candidates]
        if len(refs) != len(set(refs)):
            raise ContextAssemblyError("context candidates must have unique source_ref values")
        for candidate in candidates:
            try:
                assert_secret_free(candidate.content, boundary="context")
                if candidate.excerpt is not None:
                    assert_secret_free(candidate.excerpt, boundary="context")
            except SecretExposureError as exc:
                raise ContextAssemblyError(
                    "context candidate contains secret-like material"
                ) from exc
            if _sha256_text(candidate.content) != candidate.content_hash:
                raise ContextAssemblyError("context candidate content hash does not match content")
            if (
                candidate.excerpt is not None
                and _sha256_text(candidate.excerpt) != candidate.excerpt_hash
            ):
                raise ContextAssemblyError("context candidate excerpt hash does not match excerpt")
            if (
                candidate.layer in {ContextLayer.CANONICAL, ContextLayer.ADMITTED}
                and candidate.source_basis_hash != task.basis_hash
            ):
                raise ContextAssemblyError("canonical/admitted context must match task basis")

    @staticmethod
    def _selection_key(candidate: ContextCandidate) -> tuple[int, int, str]:
        return (
            _PRIORITY_ORDER[candidate.priority],
            _LAYER_ORDER[candidate.layer],
            candidate.source_ref,
        )

    @staticmethod
    def _physical_key(candidate: ContextCandidate) -> tuple[int, int, str]:
        return (
            0 if candidate.stable_prefix else 1,
            _LAYER_ORDER[candidate.layer],
            candidate.source_ref,
        )

    @staticmethod
    def _omit_or_raise(
        *,
        candidate: ContextCandidate,
        omitted: list[ContextOmission],
        reason_code: str,
    ) -> None:
        if candidate.priority is ContextPriority.MUST:
            raise ContextTooLargeError("must_context_exceeds_budget")
        omitted.append(
            ContextOmission(
                source_ref=candidate.source_ref,
                layer=candidate.layer,
                priority=candidate.priority,
                source_hash=candidate.content_hash,
                reason_code=reason_code,
            )
        )

    @staticmethod
    def _render_candidate(*, candidate: ContextCandidate, content: str) -> str:
        label = candidate.layer.value.upper()
        header = f"[{label} source_ref={candidate.source_ref} authority={candidate.authority}]"
        if candidate.layer is ContextLayer.UNTRUSTED:
            payload = _canonical_json(
                {
                    "content": content,
                    "source_ref": candidate.source_ref,
                    "untrusted": True,
                }
            )
            return f"{header}\n{payload}\n[/{label}]"
        return f"{header}\n{content}\n[/{label}]"

    @staticmethod
    def _jsonable(value: object) -> object:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, tuple):
            return [ContextAssemblyEngine._jsonable(item) for item in value]
        if isinstance(value, list):
            return [ContextAssemblyEngine._jsonable(item) for item in value]
        if isinstance(value, dict):
            return {key: ContextAssemblyEngine._jsonable(item) for key, item in value.items()}
        return value


__all__ = [
    "ApproximateTokenEstimator",
    "ContextAssembly",
    "ContextAssemblyEngine",
    "ContextAssemblyError",
    "ContextAssemblyInput",
    "ContextAssemblyPolicy",
    "ContextCandidate",
    "ContextItem",
    "ContextLayer",
    "ContextManifest",
    "ContextOmission",
    "ContextPriority",
    "ContextTooLargeError",
    "ContextTransform",
    "TokenEstimator",
]
