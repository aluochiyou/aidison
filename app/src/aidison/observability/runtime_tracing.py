"""Optional, fail-safe Langfuse traces for live Aidison runtime boundaries.

The PostgreSQL event stream and Artifact Store remain the source of truth.
This module emits a deliberately small diagnostic projection only: correlation
keys, stable component names, outcomes, and scalar timing data.  It never
accepts prompt text, source content, model output, credentials, or private
LangGraph state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from contextvars import ContextVar, Token
from threading import RLock
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from langchain_core.callbacks import BaseCallbackHandler
from pydantic import AliasChoices, Field, SecretStr

from aidison.config import AidisonSettings
from aidison.observability.contracts import TelemetryCorrelation

_TraceKind = Literal["span", "agent", "tool", "chain", "retriever"]
_TraceScalar = str | int | float | bool | None
_SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "aidison.component",
        "aidison.event",
        "aidison.graph_name",
        "aidison.graph_revision",
        "aidison.attempt_count",
        "aidison.fallback_used",
        "aidison.outcome",
        "aidison.project_id",
        "aidison.run_id",
        "aidison.task_id",
        "error.class",
        "http.method",
        "http.status_code",
        "runtime.binding",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.usage.total_tokens",
    }
)
_current_trace_id: ContextVar[str | None] = ContextVar("aidison_runtime_trace_id", default=None)


class RuntimeTracingSettings(AidisonSettings):
    """Secret-backed configuration for optional live Langfuse runtime traces."""

    enabled: bool = Field(default=False, validation_alias="AIDISON_RUNTIME_LANGFUSE_ENABLED")
    project: str = Field(
        default="aidison-runtime",
        validation_alias="AIDISON_RUNTIME_LANGFUSE_PROJECT",
        min_length=1,
        max_length=200,
    )
    public_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AIDISON_RUNTIME_LANGFUSE_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY"
        ),
    )
    secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AIDISON_RUNTIME_LANGFUSE_SECRET_KEY", "LANGFUSE_SECRET_KEY"
        ),
    )
    base_url: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("AIDISON_RUNTIME_LANGFUSE_BASE_URL", "LANGFUSE_BASE_URL"),
        min_length=1,
        max_length=500,
    )
    environment: str = Field(
        default="development",
        validation_alias=AliasChoices(
            "AIDISON_RUNTIME_LANGFUSE_ENVIRONMENT", "LANGFUSE_ENVIRONMENT"
        ),
        min_length=1,
        max_length=100,
    )
    release: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AIDISON_RUNTIME_LANGFUSE_RELEASE", "LANGFUSE_RELEASE"),
        max_length=200,
    )


class RuntimeTracer(Protocol):
    """Small product-owned boundary; callers never use the SDK directly."""

    @property
    def enabled(self) -> bool: ...

    def span(
        self,
        *,
        name: str,
        correlation: TelemetryCorrelation,
        kind: _TraceKind = "span",
        attributes: Mapping[str, _TraceScalar] | None = None,
    ) -> AbstractContextManager[None]: ...

    def flush(self) -> None: ...


class _NoopTraceSpan(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None


class DisabledRuntimeTracer:
    """The default: tracing adds no SDK construction, network, or runtime cost."""

    enabled = False

    def span(
        self,
        *,
        name: str,
        correlation: TelemetryCorrelation,
        kind: _TraceKind = "span",
        attributes: Mapping[str, _TraceScalar] | None = None,
    ) -> AbstractContextManager[None]:
        del name, correlation, kind, attributes
        return _NoopTraceSpan()

    def flush(self) -> None:
        return None


class _LangfuseTraceSpan(AbstractContextManager[None]):
    def __init__(
        self,
        *,
        tracer: LangfuseRuntimeTracer,
        trace_id: str,
        name: str,
        kind: _TraceKind,
        metadata: dict[str, _TraceScalar],
    ) -> None:
        self._tracer = tracer
        self._trace_id = trace_id
        self._name = name
        self._kind = kind
        self._metadata = metadata
        self._scope: Any | None = None
        self._token: Token[str | None] | None = None

    def __enter__(self) -> None:
        self._token = _current_trace_id.set(self._trace_id)
        try:
            client = self._tracer._client_or_none()
            if client is not None:
                self._scope = client.start_as_current_observation(
                    trace_context={"trace_id": self._trace_id},
                    name=self._name,
                    as_type=self._kind,
                    metadata=self._metadata,
                    version=self._tracer.release,
                )
                self._scope.__enter__()
        except Exception:
            # Telemetry cannot make a request, graph, or database command fail.
            self._scope = None
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self._scope is not None:
                self._scope.__exit__(exc_type, exc, traceback)
        except Exception:
            pass
        finally:
            if self._token is not None:
                _current_trace_id.reset(self._token)
        return None


class LangfuseRuntimeTracer:
    """Lazy Langfuse exporter which cannot alter product control flow."""

    enabled = True

    def __init__(
        self,
        *,
        project: str,
        public_key: str,
        secret_key: str,
        base_url: str,
        environment: str,
        release: str | None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._project = project
        self._public_key = public_key
        self._secret_key = secret_key
        self._base_url = base_url
        self._environment = environment
        self.release = release
        self._client_factory = client_factory or _default_client
        self._client: Any | None = None
        self._client_failed = False

    def span(
        self,
        *,
        name: str,
        correlation: TelemetryCorrelation,
        kind: _TraceKind = "span",
        attributes: Mapping[str, _TraceScalar] | None = None,
    ) -> AbstractContextManager[None]:
        metadata = _safe_metadata(correlation=correlation, attributes=attributes)
        if metadata is None:
            return _NoopTraceSpan()
        # A Langfuse project is selected by credentials rather than a per-call
        # SDK argument. Preserve the configured logical project as a safe
        # deployment label so traces from several Aidison environments can be
        # distinguished without exporting request/model payloads.
        metadata["aidison.service"] = self._project
        return _LangfuseTraceSpan(
            tracer=self,
            trace_id=_trace_id_for(correlation),
            name=name,
            kind=kind,
            metadata=metadata,
        )

    def flush(self) -> None:
        try:
            client = self._client_or_none()
            if client is not None:
                client.flush()
        except Exception:
            return None

    def _client_or_none(self) -> Any | None:
        if self._client_failed:
            return None
        if self._client is None:
            try:
                self._client = self._client_factory(
                    public_key=self._public_key,
                    secret_key=self._secret_key,
                    base_url=self._base_url,
                    environment=self._environment,
                    release=self.release,
                )
            except Exception:
                self._client_failed = True
                return None
        return self._client


class LangGraphRuntimeCallback(BaseCallbackHandler):
    """Emit one safe span per LangGraph callback without reading graph payloads.

    Callback ``inputs`` and ``outputs`` may contain prompts, web content, model
    text, or private checkpoint state.  They are intentionally ignored.  The
    AgentRun identity is injected by the product runtime, not inferred from a
    LangChain callback identifier.
    """

    raise_error = False

    def __init__(
        self,
        *,
        tracer: RuntimeTracer,
        correlation: TelemetryCorrelation,
        graph_name: str,
        graph_revision: str,
    ) -> None:
        if correlation.run_id is None:
            raise ValueError("LangGraph runtime callback requires an AgentRun correlation")
        self._tracer = tracer
        self._correlation = correlation
        self._graph_name = graph_name
        self._graph_revision = graph_revision
        self._spans: dict[UUID, AbstractContextManager[None]] = {}
        self._lock = RLock()

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del serialized, inputs, parent_run_id, kwargs
        correlation = self._correlation.model_copy(update={"invocation_id": run_id})
        scope = self._tracer.span(
            name="aidison.langgraph.node",
            correlation=correlation,
            kind="chain",
            attributes={
                "aidison.event": "chain_started",
                "aidison.graph_name": self._graph_name,
                "aidison.graph_revision": self._graph_revision,
            },
        )
        try:
            scope.__enter__()
            with self._lock:
                previous = self._spans.pop(run_id, None)
                self._spans[run_id] = scope
            if previous is not None:
                previous.__exit__(None, None, None)
        except Exception:
            return None

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del outputs, parent_run_id, kwargs
        self._close_span(run_id)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        del parent_run_id, kwargs
        self._close_span(run_id, error=error)
        try:
            with self._tracer.span(
                name="aidison.langgraph.node_error",
                correlation=self._correlation.model_copy(update={"invocation_id": run_id}),
                kind="chain",
                attributes={
                    "aidison.event": "chain_failed",
                    "aidison.graph_name": self._graph_name,
                    "aidison.graph_revision": self._graph_revision,
                    "error.class": type(error).__name__,
                },
            ):
                pass
        except Exception:
            return None

    def close(self) -> None:
        """Close orphaned SDK scopes after a graph-level crash or cancellation."""

        with self._lock:
            pending = tuple(self._spans.values())
            self._spans.clear()
        for scope in pending:
            try:
                scope.__exit__(None, None, None)
            except Exception:
                continue

    def _close_span(self, run_id: UUID, error: BaseException | None = None) -> None:
        with self._lock:
            scope = self._spans.pop(run_id, None)
        if scope is None:
            return
        try:
            scope.__exit__(type(error) if error is not None else None, error, None)
        except Exception:
            return None


def build_runtime_tracer(settings: RuntimeTracingSettings) -> RuntimeTracer:
    """Create no exporter until tracing is explicitly and completely configured."""

    public_key = settings.public_key.get_secret_value() if settings.public_key else None
    secret_key = settings.secret_key.get_secret_value() if settings.secret_key else None
    if not settings.enabled or not public_key or not secret_key:
        return DisabledRuntimeTracer()
    return LangfuseRuntimeTracer(
        project=settings.project,
        public_key=public_key,
        secret_key=secret_key,
        base_url=settings.base_url,
        environment=settings.environment,
        release=settings.release,
    )


def _trace_id_for(correlation: TelemetryCorrelation) -> str:
    if correlation.run_id is not None:
        return correlation.run_id.hex
    current = _current_trace_id.get()
    if current is not None:
        return current
    if correlation.request_id is not None:
        return uuid5(NAMESPACE_URL, f"aidison-request:{correlation.request_id}").hex
    return uuid4().hex


def _safe_metadata(
    *,
    correlation: TelemetryCorrelation,
    attributes: Mapping[str, _TraceScalar] | None,
) -> dict[str, _TraceScalar] | None:
    if attributes is not None:
        for key, value in attributes.items():
            if key not in _SAFE_ATTRIBUTE_KEYS or not isinstance(
                value, (str, int, float, bool, type(None))
            ):
                return None
    metadata: dict[str, _TraceScalar] = {
        "aidison.project": "runtime",
        "aidison.component": "runtime",
    }
    if correlation.request_id is not None:
        metadata["aidison.request_id"] = correlation.request_id
    if correlation.project_id is not None:
        metadata["aidison.project_id"] = str(correlation.project_id)
    if correlation.run_id is not None:
        metadata["aidison.run_id"] = str(correlation.run_id)
    if correlation.task_id is not None:
        metadata["aidison.task_id"] = correlation.task_id
    if correlation.invocation_id is not None:
        metadata["aidison.invocation_id"] = str(correlation.invocation_id)
    if correlation.decision_request_id is not None:
        metadata["aidison.decision_request_id"] = str(correlation.decision_request_id)
    if attributes:
        metadata.update(attributes)
    return metadata


def _default_client(**kwargs: Any) -> Any:
    from langfuse import Langfuse

    return Langfuse(**kwargs)


__all__ = [
    "DisabledRuntimeTracer",
    "LangGraphRuntimeCallback",
    "LangfuseRuntimeTracer",
    "RuntimeTracer",
    "RuntimeTracingSettings",
    "build_runtime_tracer",
]
