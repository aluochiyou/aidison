"""Optional, fail-safe Langfuse traces for live Aidison runtime boundaries.

The PostgreSQL event stream and Artifact Store remain the source of truth.
This module emits a deliberately small diagnostic projection only: correlation
keys, stable component names, outcomes, and scalar timing data.  It never
accepts prompt text, source content, model output, credentials, or private
LangGraph state.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from threading import RLock
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from langchain_core.callbacks import BaseCallbackHandler
from pydantic import AliasChoices, Field, SecretStr

from aidison.config import AidisonSettings
from aidison.observability.contracts import TelemetryCorrelation

_TraceKind = Literal["span", "agent", "tool", "chain", "retriever", "generation"]
_TraceScalar = str | int | float | bool | None
_SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "aidison.component",
        "aidison.event",
        "aidison.graph_name",
        "aidison.graph_revision",
        "aidison.node_name",
        "aidison.run_kind",
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
        "tool.name",
        "tool.provider",
    }
)


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
        validation_alias=AliasChoices("AIDISON_RUNTIME_LANGFUSE_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY"),
    )
    secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AIDISON_RUNTIME_LANGFUSE_SECRET_KEY", "LANGFUSE_SECRET_KEY"),
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
        parent_observation_id: str | None = None,
    ) -> AbstractContextManager[None]: ...

    def flush(self) -> None: ...

    def health_snapshot(self) -> dict[str, _TraceScalar]: ...

    def probe_connection(self) -> bool: ...


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
        parent_observation_id: str | None = None,
    ) -> AbstractContextManager[None]:
        del name, correlation, kind, attributes, parent_observation_id
        return _NoopTraceSpan()

    def flush(self) -> None:
        return None

    def health_snapshot(self) -> dict[str, _TraceScalar]:
        return {
            "enabled": False,
            "configured": False,
            "endpoint": None,
            "environment": None,
            "release": None,
        }

    def probe_connection(self) -> bool:
        return False


class _LangfuseTraceSpan(AbstractContextManager[None]):
    def __init__(
        self,
        *,
        tracer: LangfuseRuntimeTracer,
        trace_id: str,
        name: str,
        kind: _TraceKind,
        metadata: dict[str, _TraceScalar],
        correlation: TelemetryCorrelation,
        parent_observation_id: str | None,
        is_run_root: bool,
    ) -> None:
        self._tracer = tracer
        self._trace_id = trace_id
        self._name = name
        self._kind = kind
        self._metadata = metadata
        self._correlation = correlation
        self._parent_observation_id = parent_observation_id
        self._is_run_root = is_run_root
        self._observation: Any | None = None

    @property
    def observation_id(self) -> str | None:
        value = getattr(self._observation, "id", None)
        return value if isinstance(value, str) else None

    def __enter__(self) -> None:
        try:
            client = self._tracer._client_or_none()
            if client is not None:
                parent_observation_id = self._parent_observation_id
                if parent_observation_id is None and self._correlation.run_id is not None:
                    parent_observation_id = self._tracer._run_root_id(self._correlation.run_id)
                trace_context = {"trace_id": self._trace_id}
                if parent_observation_id is not None:
                    trace_context["parent_span_id"] = parent_observation_id
                self._observation = client.start_observation(
                    trace_context=trace_context,
                    name=self._name,
                    as_type=self._kind,
                    metadata=self._metadata,
                    version=self._tracer.release,
                )
                if self._is_run_root and self._correlation.run_id is not None:
                    observation_id = self.observation_id
                    if observation_id is not None:
                        self._tracer._register_run_root(self._correlation.run_id, observation_id)
        except Exception:
            # Telemetry cannot make a request, graph, or database command fail.
            self._observation = None
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self._observation is not None:
                if exc is not None:
                    self._observation.update(
                        level="ERROR",
                        status_message=type(exc).__name__,
                    )
                self._observation.end()
        except Exception:
            pass
        finally:
            if self._is_run_root and self._correlation.run_id is not None:
                self._tracer._unregister_run_root(self._correlation.run_id, self.observation_id)
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
        self._run_roots: dict[UUID, str] = {}
        self._lock = RLock()

    def span(
        self,
        *,
        name: str,
        correlation: TelemetryCorrelation,
        kind: _TraceKind = "span",
        attributes: Mapping[str, _TraceScalar] | None = None,
        parent_observation_id: str | None = None,
    ) -> AbstractContextManager[None]:
        metadata = _safe_metadata(correlation=correlation, attributes=attributes)
        if metadata is None:
            return _NoopTraceSpan()
        # A Langfuse project is selected by credentials rather than a per-call
        # SDK argument. Preserve the configured logical project as a safe
        # deployment label so traces from several Aidison environments can be
        # distinguished without exporting request/model payloads.
        metadata["aidison.service"] = self._project
        is_run_root = bool(
            correlation.run_id is not None
            and attributes is not None
            and attributes.get("aidison.event") == "agent_run_started"
        )
        return _LangfuseTraceSpan(
            tracer=self,
            trace_id=_trace_id_for(correlation),
            name=name,
            kind=kind,
            metadata=metadata,
            correlation=correlation,
            parent_observation_id=parent_observation_id,
            is_run_root=is_run_root,
        )

    def _run_root_id(self, run_id: UUID) -> str | None:
        with self._lock:
            return self._run_roots.get(run_id)

    def _register_run_root(self, run_id: UUID, observation_id: str) -> None:
        with self._lock:
            self._run_roots[run_id] = observation_id

    def _unregister_run_root(self, run_id: UUID, observation_id: str | None) -> None:
        with self._lock:
            if self._run_roots.get(run_id) == observation_id:
                self._run_roots.pop(run_id, None)

    def flush(self) -> None:
        try:
            client = self._client_or_none()
            if client is not None:
                client.flush()
        except Exception:
            return None

    def health_snapshot(self) -> dict[str, _TraceScalar]:
        """Return only non-secret deployment metadata for the developer UI."""

        parsed = urlsplit(self._base_url)
        endpoint = parsed.netloc or parsed.path.split("/")[0]
        return {
            "enabled": True,
            "configured": True,
            "endpoint": endpoint or "unknown",
            "environment": self._environment,
            "release": self.release,
        }

    def probe_connection(self) -> bool:
        """Verify credentials and network reachability without exporting data."""

        try:
            client = self._client_or_none()
            return bool(client is not None and client.auth_check())
        except Exception:
            return False

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
        del serialized, inputs
        node_name = _safe_observation_name(kwargs.get("name"), fallback="node")
        correlation = self._correlation.model_copy(update={"invocation_id": run_id})
        with self._lock:
            parent_scope = self._spans.get(parent_run_id) if parent_run_id else None
        parent_observation_id = getattr(parent_scope, "observation_id", None)
        scope = self._tracer.span(
            name=f"aidison.langgraph.{self._graph_name}.{node_name}",
            correlation=correlation,
            kind="chain",
            attributes={
                "aidison.event": "chain_started",
                "aidison.graph_name": self._graph_name,
                "aidison.graph_revision": self._graph_revision,
                "aidison.node_name": node_name,
            },
            parent_observation_id=parent_observation_id,
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


def _safe_observation_name(value: object, *, fallback: str) -> str:
    """Keep code-owned LangGraph labels useful without exporting graph payloads."""

    candidate = str(value or fallback).strip()
    candidate = re.sub(r"[^A-Za-z0-9_.:-]+", "_", candidate).strip("_.:-")
    return (candidate or fallback)[:120]


def _trace_id_for(correlation: TelemetryCorrelation) -> str:
    if correlation.run_id is not None:
        return correlation.run_id.hex
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
