"""Incoming trace context parsing and Cloud Logging trace correlation fields."""

import re
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from config import settings

if TYPE_CHECKING:
    from flask import Request
    from opentelemetry.context import Context

_CLOUD_TRACE_HEADER = re.compile(r"([0-9a-fA-F]{32})/([0-9]+)(?:;o=([01]))?")
_MAX_TRACE_ID = 2**128
_MAX_SPAN_ID = 2**64


def incoming_context(request: Request) -> Context | None:
    """Return the caller's trace context from W3C traceparent or Google's decimal-span-ID header."""
    if request.headers.get("traceparent"):
        # Flask's header mapping uses title case; propagators expect lowercase.
        context = TraceContextTextMapPropagator().extract(
            {key.lower(): value for key, value in request.headers.items()},
        )
        if trace.get_current_span(context).get_span_context().is_valid:
            return context
    header = request.headers.get("X-Cloud-Trace-Context", "")
    match = _CLOUD_TRACE_HEADER.fullmatch(header)
    if match:
        trace_id, span_id = int(match[1], 16), int(match[2])
        if 0 < trace_id < _MAX_TRACE_ID and 0 < span_id < _MAX_SPAN_ID:
            parent = SpanContext(
                trace_id,
                span_id,
                is_remote=True,
                trace_flags=TraceFlags(int(match[3] or 0)),
            )
            return trace.set_span_in_context(NonRecordingSpan(parent))
    return None


def trace_log_fields(request: Request | None = None) -> dict[str, str | bool]:
    """Return Cloud Logging fields correlating a log entry with the active or incoming trace."""
    context = trace.get_current_span().get_span_context()
    if request is not None:
        parent_context = incoming_context(request)
        if parent_context is not None:
            incoming_span = trace.get_current_span(parent_context).get_span_context()
            if incoming_span.is_valid and (not context.is_valid or context.trace_id != incoming_span.trace_id):
                context = incoming_span
    if not context.is_valid:
        return {}
    return {
        "logging.googleapis.com/trace": f"projects/{settings.hub_project_id}/traces/{context.trace_id:032x}",
        "logging.googleapis.com/spanId": f"{context.span_id:016x}",
        "logging.googleapis.com/trace_sampled": bool(context.trace_flags.sampled),
    }


def current_trace_id(request: Request | None = None) -> str | None:
    """Return the 32-hex trace ID of the active or incoming trace, if any."""
    fields = trace_log_fields(request)
    return str(fields.get("logging.googleapis.com/trace", "")).rsplit("/", 1)[-1] or None
