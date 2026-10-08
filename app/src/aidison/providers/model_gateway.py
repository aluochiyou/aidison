"""Run-frozen model invocation with visible retry, fallback, quota and circuit gates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.runtime.agent_runs import AgentRunClaim


class ProviderFailureClass(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_OVERFLOW = "context_overflow"
    CONTENT_POLICY_BLOCKED = "content_policy_blocked"
    RATE_LIMITED = "rate_limited"
    TRANSIENT_UPSTREAM = "transient_upstream"
    CONNECTION_PRE_DISPATCH = "connection_pre_dispatch"
    TIMEOUT_AFTER_DISPATCH = "timeout_after_dispatch"
    MALFORMED_RESPONSE = "malformed_response"
    CANCELLED = "cancelled"
    QUOTA_UNAVAILABLE = "quota_unavailable"
    CIRCUIT_OPEN = "circuit_open"
    UNKNOWN_USAGE_OR_EFFECT = "unknown_usage_or_effect"


class FallbackPolicy(StrEnum):
    NONE = "none"
    AVAILABILITY_ONLY = "availability_only"


class ProviderFailure(RuntimeError):
    def __init__(
        self, failure_class: ProviderFailureClass, *, retry_after_seconds: float | None = None
    ):
        self.failure_class = failure_class
        self.retry_after_seconds = retry_after_seconds
        super().__init__(failure_class.value)


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_attempts_per_target: int = Field(ge=1, le=8)
    base_backoff_seconds: float = Field(ge=0, le=60)


class ModelTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=120)
    credential_pool_id: str = Field(min_length=1, max_length=200)
    quota_group: str = Field(min_length=1, max_length=120)
    capabilities: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}:{self.credential_pool_id}:{self.quota_group}"


class ModelBudgetContext(BaseModel):
    """Run-frozen authority to account for every physical model attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: UUID
    claim: AgentRunClaim
    logical_step: str = Field(min_length=1, max_length=200)
    idempotency_prefix: str = Field(min_length=1, max_length=260)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reserved_tokens: int = Field(ge=1)
    request_artifact_ref: str | None = Field(default=None, max_length=500)


class ModelInvocationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    logical_invocation_id: UUID
    run_id: UUID
    project_id: UUID | None = None
    task_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_ref: str = Field(min_length=1, max_length=500)
    required_capabilities: tuple[str, ...]
    targets: tuple[ModelTarget, ...] = Field(min_length=1, max_length=4)
    retry_policy: RetryPolicy
    fallback_policy: FallbackPolicy = FallbackPolicy.NONE
    deadline: datetime
    budget_context: ModelBudgetContext | None = None
    # This is invocation-private provider input (for example, rendered chat
    # messages). It is deliberately excluded from durable request dumps; the
    # replayable prompt is the separately persisted ``prompt_ref`` Artifact.
    provider_payload: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def targets_meet_required_capabilities(self) -> ModelInvocationRequest:
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        required = set(self.required_capabilities)
        if any(not required.issubset(target.capabilities) for target in self.targets):
            raise ValueError("every frozen target must satisfy required capabilities")
        if self.budget_context is not None and self.budget_context.claim.run_id != self.run_id:
            raise ValueError("ModelBudgetContext claim must belong to this AgentRun")
        return self


class PhysicalAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target: ModelTarget
    ordinal: int = Field(ge=1)
    failure: ProviderFailureClass | None = None


class ModelInvocationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: str
    actual_target: ModelTarget | None = None
    response_ref: str | None = None
    provider_request_id: str | None = None
    failure: ProviderFailureClass | None = None
    fallback_from: ModelTarget | None = None
    usage_tokens: int | None = Field(default=None, ge=0)
    attempts: tuple[PhysicalAttempt, ...]


class ModelInvocationObservation(BaseModel):
    """Payload-free summary exposed to diagnostics only after settlement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_invocation_id: UUID
    run_id: UUID
    project_id: UUID | None = None
    task_id: UUID
    status: str = Field(min_length=1, max_length=40)
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    attempt_count: int = Field(ge=0)
    fallback_used: bool
    usage_tokens: int | None = Field(default=None, ge=0)
    failure: ProviderFailureClass | None = None

    @classmethod
    def from_result(
        cls,
        *,
        request: ModelInvocationRequest,
        result: ModelInvocationResult,
    ) -> ModelInvocationObservation:
        target = _observed_target(request=request, result=result)
        return cls(
            logical_invocation_id=request.logical_invocation_id,
            run_id=request.run_id,
            project_id=request.project_id,
            task_id=request.task_id,
            status=result.status,
            provider=target.provider,
            model=target.model,
            attempt_count=len(result.attempts),
            fallback_used=result.fallback_from is not None,
            usage_tokens=result.usage_tokens,
            failure=result.failure,
        )


class ProviderAdapter(Protocol):
    async def invoke(
        self, *, request: ModelInvocationRequest, target: ModelTarget
    ) -> dict[str, Any]: ...


class ProviderStreamAdapter(Protocol):
    def stream(
        self,
        *,
        request: ModelInvocationRequest,
        target: ModelTarget,
    ) -> AsyncIterator[dict[str, Any]]: ...


class QuotaPermit(Protocol):
    async def release(self) -> None: ...


class ProviderQuota(Protocol):
    async def acquire(self, *, bucket_key: str, deadline: datetime) -> QuotaPermit: ...


class ProviderCircuit(Protocol):
    async def allow_request(self, *, key: str, deadline: datetime) -> bool: ...
    async def record_success(self, *, key: str) -> None: ...
    async def record_failure(self, *, key: str, failure: ProviderFailureClass) -> None: ...


class ModelAttemptBudgetPort(Protocol):
    """Storage-neutral boundary for one physical model attempt's cost state."""

    async def reserve_attempt(
        self,
        *,
        context: ModelBudgetContext,
        target: ModelTarget,
        ordinal: int,
    ) -> object: ...

    async def mark_dispatched(
        self, *, operation_id: object, context: ModelBudgetContext
    ) -> None: ...

    async def settle(
        self,
        *,
        operation_id: object,
        usage_tokens: int,
        provider_request_id: str | None,
        response_ref: str,
    ) -> None: ...

    async def release_before_dispatch(
        self,
        *,
        operation_id: object,
        context: ModelBudgetContext,
        normalized_error: str,
    ) -> None: ...

    async def mark_ambiguous(self, *, operation_id: object, normalized_error: str) -> None: ...


class ModelInvocationObserver(Protocol):
    """Fail-open sink for one completed non-streaming logical invocation."""

    def observe(self, observation: ModelInvocationObservation) -> None: ...


class ModelGateway:
    def __init__(
        self,
        *,
        adapter: ProviderAdapter,
        quota: ProviderQuota,
        circuit: ProviderCircuit,
        budget: ModelAttemptBudgetPort | None = None,
        observer: ModelInvocationObserver | None = None,
    ) -> None:
        self._adapter, self._quota, self._circuit, self._budget = adapter, quota, circuit, budget
        self._observer = observer

    async def invoke(self, request: ModelInvocationRequest) -> ModelInvocationResult:
        attempts: list[PhysicalAttempt] = []
        for target_index, target in enumerate(request.targets):
            if not await self._circuit.allow_request(key=target.key, deadline=request.deadline):
                attempts.append(
                    PhysicalAttempt(
                        target=target,
                        ordinal=len(attempts) + 1,
                        failure=ProviderFailureClass.CIRCUIT_OPEN,
                    )
                )
                if (
                    request.fallback_policy is FallbackPolicy.AVAILABILITY_ONLY
                    and target_index < len(request.targets) - 1
                ):
                    continue
                return self._observed(
                    request,
                    ModelInvocationResult(
                        status="failed",
                        failure=ProviderFailureClass.CIRCUIT_OPEN,
                        attempts=tuple(attempts),
                    ),
                )
            for _ in range(request.retry_policy.max_attempts_per_target):
                if datetime.now(UTC) >= request.deadline:
                    return self._observed(
                        request,
                        ModelInvocationResult(
                            status="failed",
                            failure=ProviderFailureClass.CANCELLED,
                            attempts=tuple(attempts),
                        ),
                    )
                ordinal = len(attempts) + 1
                operation_id = await self._reserve_budget_attempt(
                    request=request,
                    target=target,
                    ordinal=ordinal,
                )
                try:
                    permit = await self._quota.acquire(
                        bucket_key=target.key,
                        deadline=request.deadline,
                    )
                except ProviderFailure as failure:
                    await self._release_budget_before_dispatch(
                        request=request,
                        operation_id=operation_id,
                        normalized_error=failure.failure_class.value,
                    )
                    attempts.append(
                        PhysicalAttempt(
                            target=target,
                            ordinal=ordinal,
                            failure=failure.failure_class,
                        )
                    )
                    await self._circuit.record_failure(
                        key=target.key,
                        failure=failure.failure_class,
                    )
                    break
                try:
                    await self._mark_budget_dispatched(request=request, operation_id=operation_id)
                    response = self._validate_response(
                        await self._adapter.invoke(request=request, target=target)
                    )
                except ProviderFailure as failure:
                    await self._mark_budget_ambiguous(
                        operation_id=operation_id,
                        normalized_error=failure.failure_class.value,
                    )
                    attempts.append(
                        PhysicalAttempt(
                            target=target, ordinal=ordinal, failure=failure.failure_class
                        )
                    )
                    await self._circuit.record_failure(
                        key=target.key, failure=failure.failure_class
                    )
                    retry = failure.failure_class in {
                        ProviderFailureClass.RATE_LIMITED,
                        ProviderFailureClass.TRANSIENT_UPSTREAM,
                        ProviderFailureClass.CONNECTION_PRE_DISPATCH,
                    }
                    if (
                        retry
                        and len([item for item in attempts if item.target == target])
                        < request.retry_policy.max_attempts_per_target
                    ):
                        await asyncio.sleep(
                            failure.retry_after_seconds
                            if failure.retry_after_seconds is not None
                            else request.retry_policy.base_backoff_seconds
                        )
                        continue
                    break
                else:
                    budget_settled = await self._settle_or_quarantine_budget(
                        request=request,
                        operation_id=operation_id,
                        response=response,
                    )
                    attempts.append(PhysicalAttempt(target=target, ordinal=ordinal))
                    if not budget_settled:
                        return self._observed(
                            request,
                            ModelInvocationResult(
                                status="failed",
                                failure=ProviderFailureClass.UNKNOWN_USAGE_OR_EFFECT,
                                attempts=tuple(attempts),
                            ),
                        )
                    await self._circuit.record_success(key=target.key)
                    return self._observed(
                        request,
                        ModelInvocationResult(
                            status="succeeded",
                            actual_target=target,
                            response_ref=response["response_ref"],
                            provider_request_id=response.get("provider_request_id"),
                            fallback_from=request.targets[0] if target_index else None,
                            usage_tokens=_reported_usage_tokens(response),
                            attempts=tuple(attempts),
                        ),
                    )
                finally:
                    await permit.release()
            last_failure = attempts[-1].failure if attempts else None
            availability_failure = last_failure in {
                ProviderFailureClass.RATE_LIMITED,
                ProviderFailureClass.TRANSIENT_UPSTREAM,
                ProviderFailureClass.CONNECTION_PRE_DISPATCH,
                ProviderFailureClass.CIRCUIT_OPEN,
                ProviderFailureClass.QUOTA_UNAVAILABLE,
            }
            if (
                request.fallback_policy is not FallbackPolicy.AVAILABILITY_ONLY
                or not availability_failure
            ):
                break
        failure_class = attempts[-1].failure if attempts else ProviderFailureClass.QUOTA_UNAVAILABLE
        return self._observed(
            request,
            ModelInvocationResult(
                status="failed",
                failure=failure_class,
                attempts=tuple(attempts),
            ),
        )

    def _observed(
        self,
        request: ModelInvocationRequest,
        result: ModelInvocationResult,
    ) -> ModelInvocationResult:
        """Notify diagnostics after product state is resolved, never before."""

        if self._observer is not None:
            try:
                self._observer.observe(
                    ModelInvocationObservation.from_result(request=request, result=result)
                )
            except Exception:
                # Observability is not part of the provider/effect transaction.
                # It must never turn a settled invocation into a retry.
                pass
        return result

    async def stream(
        self,
        request: ModelInvocationRequest,
        *,
        stream_adapter: ProviderStreamAdapter,
    ) -> AsyncIterator[dict[str, Any]]:
        """Keep the physical quota permit until the provider iterator closes."""

        target = request.targets[0]
        if not await self._circuit.allow_request(key=target.key, deadline=request.deadline):
            raise ProviderFailure(ProviderFailureClass.CIRCUIT_OPEN)
        operation_id = await self._reserve_budget_attempt(request=request, target=target, ordinal=1)
        try:
            permit = await self._quota.acquire(bucket_key=target.key, deadline=request.deadline)
        except ProviderFailure as failure:
            await self._release_budget_before_dispatch(
                request=request,
                operation_id=operation_id,
                normalized_error=failure.failure_class.value,
            )
            raise
        budget_resolved = False
        usage_tokens: int | None = None
        try:
            await self._mark_budget_dispatched(request=request, operation_id=operation_id)
            async for event in stream_adapter.stream(request=request, target=target):
                if datetime.now(UTC) >= request.deadline:
                    raise ProviderFailure(ProviderFailureClass.CANCELLED)
                candidate_usage = event.get("usage_tokens")
                if isinstance(candidate_usage, int) and candidate_usage >= 0:
                    usage_tokens = candidate_usage
                yield event
        except ProviderFailure as failure:
            await self._circuit.record_failure(key=target.key, failure=failure.failure_class)
            await self._mark_budget_ambiguous(
                operation_id=operation_id,
                normalized_error=failure.failure_class.value,
            )
            budget_resolved = True
            raise
        else:
            await self._circuit.record_success(key=target.key)
            if usage_tokens is None:
                await self._mark_budget_ambiguous(
                    operation_id=operation_id,
                    normalized_error="stream_completed_without_usage",
                )
                budget_resolved = True
            else:
                await self._settle_budget(
                    request=request,
                    operation_id=operation_id,
                    usage_tokens=usage_tokens,
                    provider_request_id=None,
                    response_ref="stream://completed",
                )
                budget_resolved = True
        finally:
            if not budget_resolved:
                await self._mark_budget_ambiguous(
                    operation_id=operation_id,
                    normalized_error="stream_terminated_before_usage",
                )
            await permit.release()

    async def _reserve_budget_attempt(
        self,
        *,
        request: ModelInvocationRequest,
        target: ModelTarget,
        ordinal: int,
    ) -> object | None:
        if self._budget is None or request.budget_context is None:
            return None
        return await self._budget.reserve_attempt(
            context=request.budget_context,
            target=target,
            ordinal=ordinal,
        )

    async def _mark_budget_dispatched(
        self, *, request: ModelInvocationRequest, operation_id: object | None
    ) -> None:
        if (
            self._budget is not None
            and request.budget_context is not None
            and operation_id is not None
        ):
            await self._budget.mark_dispatched(
                operation_id=operation_id,
                context=request.budget_context,
            )

    async def _release_budget_before_dispatch(
        self,
        *,
        request: ModelInvocationRequest,
        operation_id: object | None,
        normalized_error: str,
    ) -> None:
        if (
            self._budget is not None
            and request.budget_context is not None
            and operation_id is not None
        ):
            await self._budget.release_before_dispatch(
                operation_id=operation_id,
                context=request.budget_context,
                normalized_error=normalized_error,
            )

    async def _mark_budget_ambiguous(
        self, *, operation_id: object | None, normalized_error: str
    ) -> None:
        if self._budget is not None and operation_id is not None:
            await self._budget.mark_ambiguous(
                operation_id=operation_id,
                normalized_error=normalized_error,
            )

    async def _settle_or_quarantine_budget(
        self,
        *,
        request: ModelInvocationRequest,
        operation_id: object | None,
        response: dict[str, Any],
    ) -> bool:
        if request.budget_context is None:
            return True
        usage_tokens = response.get("usage_tokens")
        if (
            not isinstance(usage_tokens, int)
            or usage_tokens < 0
            or usage_tokens > request.budget_context.reserved_tokens
        ):
            await self._mark_budget_ambiguous(
                operation_id=operation_id,
                normalized_error="missing_or_unbounded_provider_usage",
            )
            return False
        await self._settle_budget(
            request=request,
            operation_id=operation_id,
            usage_tokens=usage_tokens,
            provider_request_id=response.get("provider_request_id"),
            response_ref=response["response_ref"],
        )
        return True

    async def _settle_budget(
        self,
        *,
        request: ModelInvocationRequest,
        operation_id: object | None,
        usage_tokens: int,
        provider_request_id: str | None,
        response_ref: str,
    ) -> None:
        if (
            self._budget is not None
            and request.budget_context is not None
            and operation_id is not None
        ):
            await self._budget.settle(
                operation_id=operation_id,
                usage_tokens=usage_tokens,
                provider_request_id=provider_request_id,
                response_ref=response_ref,
            )

    @staticmethod
    def _validate_response(response: dict[str, Any]) -> dict[str, Any]:
        response_ref = response.get("response_ref")
        provider_request_id = response.get("provider_request_id")
        if not isinstance(response_ref, str) or not response_ref:
            raise ProviderFailure(ProviderFailureClass.MALFORMED_RESPONSE)
        if provider_request_id is not None and not isinstance(provider_request_id, str):
            raise ProviderFailure(ProviderFailureClass.MALFORMED_RESPONSE)
        return response


def _reported_usage_tokens(response: dict[str, Any]) -> int | None:
    value = response.get("usage_tokens")
    return value if isinstance(value, int) and value >= 0 else None


def _observed_target(
    *,
    request: ModelInvocationRequest,
    result: ModelInvocationResult,
) -> ModelTarget:
    if result.actual_target is not None:
        return result.actual_target
    if result.attempts:
        return result.attempts[-1].target
    return request.targets[0]
