"""Manual OpenTelemetry spans, including the lifetime of streamed responses."""

import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import GoogleAPICallError, RetryError
from google.auth.exceptions import GoogleAuthError
from google.cloud.trace_v2 import TraceServiceClient
from google.cloud.trace_v2.types import BatchWriteSpansRequest
from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.trace import SpanKind, StatusCode

from config import settings
from telemetry.logger import log_event
from telemetry.trace_context import incoming_context

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from contextlib import AbstractContextManager

    from flask import Request, Response

_log = logging.getLogger(__name__)

# Failures a telemetry backend can raise; tracing must never fail inference.
_TELEMETRY_ERRORS = (GoogleAPICallError, RetryError, GoogleAuthError, OSError, ValueError)


@dataclass
class TracingState:
    """Lazily initialized tracer provider shared by all requests of this worker."""

    provider: TracerProvider | None = None
    tracer: trace.Tracer = field(default_factory=lambda: trace.NoOpTracerProvider().get_tracer(__name__))
    lock: threading.Lock = field(default_factory=threading.Lock)


state = TracingState()


class WarningCloudTraceSpanExporter(CloudTraceSpanExporter):
    """Drop failed batches with one warning; tracing must not fail inference."""

    def export(self, spans: Any) -> SpanExportResult:
        """Write a span batch to Cloud Trace, discarding it on failure."""
        try:
            self.client.batch_write_spans(
                request=BatchWriteSpansRequest(
                    name=f"projects/{self.project_id}",
                    spans=self._translate_to_cloud_trace(spans),
                ),
            )
        except _TELEMETRY_ERRORS as error:
            # The base exporter logs a multiline stacktrace: keep its FAILURE result
            # without logging payloads or retrying.
            log_event(
                "WARNING",
                "Cloud Trace export failed; batch discarded without retry",
                extra={"error_type": type(error).__name__, "span_count": len(spans)},
            )
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS


class _BoundedTraceClient(TraceServiceClient):
    """Cloud Trace client whose writes are time-bounded and never retried."""

    def batch_write_spans(self, request: BatchWriteSpansRequest, **kwargs: Any) -> None:
        """Write spans with the configured timeout and no retry."""
        super().batch_write_spans(
            request=request,
            timeout=settings.trace_export_timeout_ms / 1000,
            retry=None,
            **kwargs,
        )


def _build_provider() -> TracerProvider:
    """Create a tracer provider exporting sampled spans to Cloud Trace."""
    provider = TracerProvider(
        sampler=TraceIdRatioBased(settings.trace_sampling_ratio),
        resource=Resource.create(
            {
                "service.name": "copilot-chat-proxy",
                "service.version": os.getenv("K_REVISION") or "unknown",
                "cloud.provider": "gcp",
                "cloud.account.id": settings.hub_project_id,
            },
        ),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            WarningCloudTraceSpanExporter(
                project_id=settings.hub_project_id,
                client=_BoundedTraceClient(),
                resource_regex=r"^(service\.|cloud\.)",
            ),
            schedule_delay_millis=500,
        ),
    )
    return provider


def _get_tracer() -> trace.Tracer:
    """Return the shared tracer, initializing the provider on first use."""
    if not settings.trace_enabled:
        return trace.NoOpTracerProvider().get_tracer(__name__)
    if state.provider is None:
        with state.lock:
            if state.provider is None:
                try:
                    provider = _build_provider()
                except _TELEMETRY_ERRORS:
                    # Telemetry must never prevent inference, or expose credentials.
                    _log.warning("Cloud Trace initialization failed; request continues without export")
                else:
                    state.tracer = provider.get_tracer("copilot-chat-proxy")
                    state.provider = provider
    return state.tracer


def _mark_error(span: trace.Span, error_type: str) -> None:
    """Flag a span as failed; exception messages and stacks may contain payloads or credentials."""
    span.set_attribute("error.type", error_type)
    span.set_status(StatusCode.ERROR)


@contextmanager
def stage_span(name: str, kind: SpanKind = SpanKind.INTERNAL) -> Iterator[trace.Span]:
    """Open a child span that records only the exception type on failure."""
    with _get_tracer().start_as_current_span(
        name,
        kind=kind,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            _mark_error(span, type(error).__name__)
            raise


# HTTPX/httpcore transport events. Only names/durations are exported: info can
# contain authorization headers, prompts, raw responses and SSL objects.
_NETWORK_STAGES = {
    "connection.connect_tcp": "upstream.connect_tcp",
    "connection.start_tls": "upstream.tls",
    "http11.send_request_headers": "upstream.send_headers",
    "http11.send_request_body": "upstream.send_body",
    "http11.receive_response_headers": "upstream.wait_response_headers",
    "http2.send_request_headers": "upstream.send_headers",
    "http2.send_request_body": "upstream.send_body",
    "http2.receive_response_headers": "upstream.wait_response_headers",
}


@contextmanager
def network_trace() -> Iterator[Callable[[str, dict[str, Any]], None]]:
    """Yield an HTTPX trace callback and span registry belonging only to this HTTP request."""
    active: dict[str, trace.Span] = {}
    parent = trace.set_span_in_context(trace.get_current_span())
    tracer = _get_tracer()

    def callback(event_name: str, info: dict[str, Any]) -> None:
        event, _, phase = event_name.rpartition(".")
        name = _NETWORK_STAGES.get(event)
        if name is None:
            return
        if phase == "started":
            active[event] = tracer.start_span(name, context=parent)
        elif phase in ("complete", "failed"):
            span = active.pop(event, None)
            if span is not None:
                if phase == "failed":
                    _mark_error(span, type(info.get("exception")).__name__)
                span.end()

    try:
        yield callback
    finally:
        # Also close spans when the transport aborts without a matching event.
        for span in active.values():
            _mark_error(span, "transport_interrupted")
            span.end()


def _is_inference_request(request: Request) -> bool:
    """Return whether the request is a POST to a Vertex inference path."""
    path = request.path.strip("/")
    return request.method == "POST" and path.startswith(("v1/projects/", "projects/"))


def _use_span(span: trace.Span) -> AbstractContextManager[trace.Span]:
    """Attach a span to the current context without ending it or recording exceptions."""
    return trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False)


def _end_and_flush(span: trace.Span) -> None:
    """End the request span and drain the batch queue within a bounded wait."""
    span.end()
    # Cloud Run can suspend background work after the response completes.
    if state.provider is None or not span.get_span_context().trace_flags.sampled:
        return
    try:
        if not state.provider.force_flush(timeout_millis=settings.trace_export_timeout_ms):
            _log.warning("Cloud Trace export did not finish within the configured timeout")
    except _TELEMETRY_ERRORS:
        _log.warning("Cloud Trace flush failed; request continues")


def _run_once(action: Callable[[], None]) -> Callable[[], None]:
    """Return a callable that runs `action` only on its first invocation."""
    done = False

    def run() -> None:
        nonlocal done
        if not done:
            done = True
            action()

    return run


def _close_upstream(original: Iterator[bytes], streaming: trace.Span) -> None:
    """Close the wrapped response iterator inside the streaming span, then end it."""
    try:
        with _use_span(streaming):
            close = getattr(original, "close", None)
            if close:
                close()
    finally:
        streaming.end()


def _stream_within_span(original: Iterator[bytes], tracer: trace.Tracer, span: trace.Span, finish: Callable[[], None]) -> Generator[bytes]:
    """Re-yield streamed chunks while a child span measures the stream's lifetime."""
    streaming = tracer.start_span("upstream.streaming", context=trace.set_span_in_context(span))
    try:
        while True:
            # Attach only while advancing the generator: never leave a request's
            # context attached while yielding to the WSGI server.
            with _use_span(streaming):
                chunk = next(original, None)
            if chunk is None:
                break
            yield chunk
    except BaseException as error:
        _mark_error(streaming, type(error).__name__)
        _mark_error(span, type(error).__name__)
        raise
    finally:
        try:
            _close_upstream(original, streaming)
        finally:
            finish()


def traced_inference(handler: Callable[[Request], Response]) -> Callable[[Request], Response]:
    """Wrap an inference handler with a server span that lasts until the response is fully sent."""

    @wraps(handler)
    def wrapped(request: Request) -> Response:
        if not _is_inference_request(request):
            return handler(request)
        tracer = _get_tracer()
        span = tracer.start_span(
            "proxy.inference",
            context=incoming_context(request),
            kind=SpanKind.SERVER,
            attributes={"http.request.method": request.method},
        )
        finish = _run_once(lambda: _end_and_flush(span))
        try:
            with _use_span(span):
                response = handler(request)
                span.set_attribute("http.response.status_code", response.status_code)
                if response.status_code >= HTTPStatus.BAD_REQUEST:
                    _mark_error(span, f"http_{response.status_code}")
        except BaseException as error:
            _mark_error(span, type(error).__name__)
            finish()
            raise
        if not response.is_streamed:
            finish()
            return response

        response.response = _stream_within_span(iter(response.response), tracer, span, finish)
        response.call_on_close(finish)
        return response

    return wrapped
