"""Safe, typed observability contracts for Aidison product and runtime events."""

from aidison.observability.contracts import (
    DurableProductEvent,
    DurableProductEventType,
    EphemeralLiveEvent,
    EphemeralLiveEventType,
    LogLevel,
    MetricPoint,
    StructuredLogRecord,
    TelemetryCorrelation,
    append_durable_product_event,
)

__all__ = [
    "DurableProductEvent",
    "DurableProductEventType",
    "EphemeralLiveEvent",
    "EphemeralLiveEventType",
    "LogLevel",
    "MetricPoint",
    "StructuredLogRecord",
    "TelemetryCorrelation",
    "append_durable_product_event",
]
