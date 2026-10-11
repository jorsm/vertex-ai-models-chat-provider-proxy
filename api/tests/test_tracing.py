"""Verify streamed trace lifetimes, safe attributes, and incoming correlation."""

import json
from http import HTTPStatus
from types import ModuleType
from typing import TYPE_CHECKING, Any, Never

import httpx
import pytest
from checks import expect, expect_equal, expect_greater_equal, expect_in, expect_not_in
from flask import request as flask_request
from flask.testing import FlaskClient
from google.api_core import exceptions
from google.api_core.exceptions import DeadlineExceeded
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, StatusCode, TraceFlags

import main
from inference import handler as inference_handler
from security.auth import AuthError
from security.governance import GovernanceError
from telemetry import tracing
from telemetry.logger import log_event
from telemetry.tracing import WarningCloudTraceSpanExporter
from vertex import client as vertex_client

if TYPE_CHECKING:
    from collections.abc import Iterator


class Upstream:
    """Fake streaming upstream response whose behavior tests adjust per case."""

    def __init__(self) -> None:
        """Start as a successful event-stream response."""
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}
        self.closed = False
        self.fail = False
        self.chunks: list[bytes] | None = None

    def iter_raw(self) -> Iterator[bytes]:
        """Yield the configured chunks, or a default two-event stream."""
        if self.chunks is not None:
            yield from self.chunks
            return
        yield b'data: {"candidates":[{"content":{"parts":[{"text":"private output"}]}}]}\n\n'
        if self.fail:
            raise RuntimeError("private prompt and secret-token")
        yield b'data: {"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":2}}\n\n'

    def close(self) -> None:
        """Record that the response was closed."""
        self.closed = True


type TraceClient = tuple[FlaskClient, InMemorySpanExporter, Upstream, ModuleType]


@pytest.fixture
def trace_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TraceClient]:
    """Provide the Flask client with an in-memory tracer and a fake Vertex upstream."""
    for key, value in {
        "TRACE_ENABLED": "true",
        "TRACE_SAMPLING_RATIO": "1",
        "TRACE_EXPORT_TIMEOUT_MS": "1000",
        "PROXY_PROJECT_ID": "test-proxy",
        "ALLOWED_REGIONS": "global",
        "CHAT_USER_LABEL_KEY": "user",
        "CHAT_PROJECT_LABEL_KEY": "project",
        "BILLING_USER_LABEL_KEY": "user",
        "BILLING_PROJECT_LABEL_KEY": "project",
        "MODELS_CATALOG_URL": "https://catalog.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    provider = TracerProvider(sampler=TraceIdRatioBased(1))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing.state, "provider", provider)
    monkeypatch.setattr(tracing.state, "tracer", provider.get_tracer("test"))

    app = main.app
    monkeypatch.setattr(
        inference_handler,
        "authenticate_caller",
        lambda _: ("private@example.com", {"sub": "private-subject"}),
    )
    monkeypatch.setattr(
        inference_handler,
        "validate_model_and_enforce_policy",
        lambda *_args, **_kwargs: ("gemini-test", {}, {}),
    )
    monkeypatch.setattr(inference_handler, "get_model_spec", lambda _: None)
    monkeypatch.setattr(
        vertex_client,
        "get_vertex_access_token",
        lambda: "secret-token",
    )

    upstream = Upstream()

    class HttpClient:
        closed = False

        def __init__(self, **_kwargs: object) -> None:
            pass

        def build_request(self, **kwargs: Any) -> httpx.Request:
            return httpx.Request(**kwargs)

        def send(self, *_args: object, **_kwargs: object) -> Upstream:
            return upstream

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(vertex_client.state, "http_client", None)
    monkeypatch.setattr(vertex_client.httpx, "Client", HttpClient)
    yield app.test_client(), exporter, upstream, main
    provider.shutdown()


PATH = "/v1/projects/user-project/locations/global/publishers/google/models/gemini-test:streamGenerateContent"


@pytest.mark.parametrize(
    "name",
    ["  Zoë Müller  ", None, "", "   ", {"unexpected": "value"}],
)
@pytest.mark.parametrize("rejected", [False, True])
def test_display_name_in_usage_comes_from_authenticated_claims(
    trace_client: TraceClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str | dict[str, str] | None,
    rejected: bool,
) -> None:
    """Verify display name in usage comes from authenticated claims."""
    client, _, _, _ = trace_client

    claims = {"sub": "subject-123"}
    if name is not None:
        claims["name"] = name
    monkeypatch.setattr(
        inference_handler,
        "authenticate_caller",
        lambda _: ("user@example.com", claims),
    )
    if rejected:

        def reject(*_args: object, **_kwargs: object) -> Never:
            raise GovernanceError(400, "Missing required label")

        monkeypatch.setattr(
            inference_handler,
            "validate_model_and_enforce_policy",
            reject,
        )
    response = client.post(PATH, json={"name": "Forged client name"}, buffered=True)
    expect_equal(response.status_code, 400 if rejected else 200)
    response.close()
    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    summary = next(entry for entry in entries if entry.get("event_kind") == "inference_completed")
    expect_equal(summary["principal_subject"], "subject-123")
    expect_equal(summary["principal_email"], "user@example.com")
    if isinstance(name, str) and name.strip():
        expect_equal(summary["principal_display_name"], "Zoë Müller")
    else:
        expect_not_in("principal_display_name", summary)


def test_trace_stays_open_until_stream_finishes_and_logs_match(trace_client: TraceClient, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify trace stays open until stream finishes and logs match."""
    client, exporter, upstream, _main = trace_client
    trace_id = "1234567890abcdef1234567890abcdef"
    response = client.post(
        PATH,
        json={"prompt": "private prompt"},
        headers={"X-Cloud-Trace-Context": f"{trace_id}/42;o=0"},
        buffered=False,
    )
    expect_not_in("proxy.inference", {span.name for span in exporter.get_finished_spans()})
    expect(not trace.get_current_span().get_span_context().is_valid)
    expect_in(b"private output", response.data)
    response.close()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    expect_equal(
        set(spans),
        {
            "proxy.inference",
            "authentication",
            "governance",
            "upstream",
            "upstream.credentials",
            "upstream.client",
            "upstream.request_preparation",
            "upstream.response_headers",
            "upstream.streaming",
            "usage.finalization",
            "request.parsing",
            "request.encoding",
            "upstream.first_chunk_wait",
            "upstream.response_close",
        },
    )
    root = spans["proxy.inference"]
    expect_equal(root.parent.span_id, 42)
    expect_equal(root.context.trace_id, int(trace_id, 16))
    expect(root.context.trace_flags.sampled)  # Local ratio is independent of Google's incoming sampling.
    expect_equal(spans["upstream.credentials"].parent.span_id, spans["upstream"].context.span_id)
    expect_equal(spans["upstream.response_headers"].parent.span_id, spans["upstream"].context.span_id)
    expect_equal(spans["usage.finalization"].parent.span_id, spans["upstream.streaming"].context.span_id)
    expect_greater_equal(root.end_time, spans["usage.finalization"].end_time)
    expect_equal(
        {event.name for event in spans["upstream.streaming"].events},
        {
            "first_chunk",
            "first_content",
        },
    )
    expect(upstream.closed)
    expect(not trace.get_current_span().get_span_context().is_valid)
    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    summary = next(entry for entry in entries if entry.get("event_kind") == "inference_completed")
    expect_equal(summary["trace_id"], trace_id)
    expect_equal(summary["logging.googleapis.com/spanId"], f"{spans['usage.finalization'].context.span_id:016x}")
    expect_not_in(
        "private",
        str(
            [(span.attributes, span.events) for span in spans.values()],
        ),
    )
    expect_not_in(
        "secret-token",
        str(
            [(span.attributes, span.events) for span in spans.values()],
        ),
    )


@pytest.mark.parametrize("failure", ["stream", "disconnect"])
def test_stream_failure_or_disconnect_ends_spans_without_sensitive_exceptions(
    trace_client: TraceClient,
    failure: str,
) -> None:
    """Verify stream failure or disconnect ends spans without sensitive exceptions."""
    client, exporter, upstream, _ = trace_client
    upstream.fail = failure == "stream"
    response = client.post(PATH, json={}, buffered=False)
    if failure == "stream":
        with pytest.raises(RuntimeError):
            _ = response.data
    response.close()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    expect_equal(spans["proxy.inference"].status.status_code, StatusCode.ERROR)
    expect_equal(spans["upstream.streaming"].status.status_code, StatusCode.ERROR)
    expect(all(not span.events or all(event.name != "exception" for event in span.events) for span in spans.values()))
    expect_not_in(
        "secret-token",
        str(
            [(span.attributes, span.events) for span in spans.values()],
        ),
    )
    expect(upstream.closed)
    expect(not trace.get_current_span().get_span_context().is_valid)


def test_authentication_rejection_has_an_error_span_and_no_upstream(
    trace_client: TraceClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify authentication rejection has an error span and no upstream."""
    client, exporter, _, _main = trace_client

    def reject(_: object) -> Never:
        raise AuthError(401, "private credential")

    monkeypatch.setattr(inference_handler, "authenticate_caller", reject)
    expect_equal(client.post(PATH, json={}).status_code, 401)
    spans = {span.name: span for span in exporter.get_finished_spans()}
    expect_equal(set(spans), {"proxy.inference", "authentication"})
    expect_equal(spans["authentication"].status.status_code, StatusCode.ERROR)
    expect_equal(spans["proxy.inference"].status.status_code, StatusCode.ERROR)
    expect_not_in(
        "private credential",
        str(
            [(span.attributes, span.events) for span in spans.values()],
        ),
    )


def test_w3c_context_and_health_exclusion(trace_client: TraceClient) -> None:
    """Verify w3c context and health exclusion."""
    client, exporter, _, _ = trace_client
    expect_equal(client.get("/health").status_code, 200)
    expect(not exporter.get_finished_spans())
    trace_id = "abcdefabcdefabcdefabcdefabcdefab"
    response = client.post(
        PATH,
        json={},
        headers={"traceparent": f"00-{trace_id}-000000000000002a-01"},
    )
    _ = response.data
    response.close()
    root = next(span for span in exporter.get_finished_spans() if span.name == "proxy.inference")
    expect_equal(root.context.trace_id, int(trace_id, 16))
    expect_equal(root.parent.span_id, 42)


def test_log_correlation_prefers_incoming_trace_header(trace_client: TraceClient, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify log correlation prefers incoming trace header."""
    _, _, _, main = trace_client
    incoming_trace_id = "1234567890abcdef1234567890abcdef"
    unrelated_span = NonRecordingSpan(
        SpanContext(
            trace_id=0xABCDEF,
            span_id=0x1234,
            is_remote=False,
            trace_flags=TraceFlags(1),
        ),
    )

    with (
        main.app.test_request_context(
            "/health",
            headers={"X-Cloud-Trace-Context": f"{incoming_trace_id}/42;o=1"},
        ),
        trace.use_span(unrelated_span, end_on_exit=False),
    ):
        log_event("INFO", "Trace correlation test", request=flask_request)

    entry = json.loads(capsys.readouterr().out)
    expect_equal(entry["logging.googleapis.com/trace"], f"projects/test-proxy/traces/{incoming_trace_id}")
    expect_equal(entry["logging.googleapis.com/spanId"], "000000000000002a")
    expect(entry["logging.googleapis.com/trace_sampled"] is True)


@pytest.mark.parametrize("status", [400, 429, 503])
def test_upstream_error_payload_is_logged_without_changing_response(
    trace_client: TraceClient,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    """Verify upstream error payload is logged without changing response."""
    client, _exporter, upstream, _ = trace_client
    payload = {
        "error": {"code": status, "message": "Errore quota: richiesta rifiutata"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    upstream.status_code = status
    upstream.headers = {"content-type": "application/json"}
    upstream.chunks = [body[:20], body[20:35], body[35:]]
    response = client.post(PATH, json={"prompt": "never log this prompt"})
    expect_equal(response.status_code, status)
    expect_equal(response.data, body)
    response.close()
    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    entry = next(log for log in logs if log.get("event_kind") == "upstream_error_response")
    expect_equal(entry["error_response"], payload)
    expect_equal(entry["severity"], "ERROR" if status >= HTTPStatus.INTERNAL_SERVER_ERROR else "WARNING")
    expect_equal(entry["http_status"], status)
    expect(entry["error_response_complete"] is True)
    expect(entry["error_response_truncated"] is False)
    expect_equal(entry["error_response_bytes"], len(body))
    expect(entry["logging.googleapis.com/trace"])
    expect_not_in("never log this prompt", str(logs))
    expect(upstream.closed)


@pytest.mark.parametrize(
    "body",
    [b"", b"<html>Bad gateway</html>", b"x" * (128 * 1024 + 100)],
    ids=["empty", "html", "large"],
)
def test_non_json_and_large_errors_preserve_response_and_bound_logs(
    trace_client: TraceClient,
    capsys: pytest.CaptureFixture[str],
    body: bytes,
) -> None:
    """Verify non json and large errors preserve response and bound logs."""
    client, _, upstream, _ = trace_client
    upstream.status_code = 502
    upstream.chunks = [body[:100], body[100:]]
    response = client.post(PATH, json={})
    expect_equal(response.data, body)
    response.close()
    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    entry = next(log for log in entries if log.get("event_kind") == "upstream_error_response")
    expect_equal(entry["error_response_text"], body[: 128 * 1024].decode())
    expect_equal(entry["error_response_truncated"], len(body) > 128 * 1024)
    expect_equal(entry["error_response_bytes"], len(body))


def test_successful_response_content_is_not_logged(trace_client: TraceClient, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify successful response content is not logged."""
    client, _, _, _ = trace_client
    response = client.post(PATH, json={})
    _ = response.data
    response.close()
    logs = capsys.readouterr().out
    expect_not_in("upstream_error_response", logs)
    expect_not_in("private output", logs)


@pytest.mark.parametrize("path", ["/health", "/discovery", "/openapi.json"])
def test_method_error_logs_the_same_payload_as_response(trace_client: TraceClient, capsys: pytest.CaptureFixture[str], path: str) -> None:
    """Verify method error logs the same payload as response."""
    client, _, _, _ = trace_client
    response = client.post(path, json={})
    expect_equal(response.status_code, 405)
    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    expect(any(log.get("error_response") == response.get_json() for log in logs))


@pytest.mark.parametrize(
    "error_name",
    ["DeadlineExceeded", "ServiceUnavailable", "PermissionDenied", None],
)
def test_cloud_trace_export_warns_without_stacktrace_or_retry(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    error_name: str | None,
) -> None:
    """Verify cloud trace export warns without stacktrace or retry."""
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with provider.get_tracer("test").start_as_current_span("test.export"):
        pass
    spans = memory.get_finished_spans()
    calls = []

    class Client:
        def batch_write_spans(self, request: object) -> None:
            calls.append(request)
            if error_name:
                raise getattr(exceptions, error_name)(
                    "private credentials must not be logged",
                )

    exporter = WarningCloudTraceSpanExporter(project_id="test-proxy", client=Client())
    try:
        result = exporter.export(spans)
    finally:
        provider.shutdown()
    expect_equal(len(calls), 1)
    expect_equal(calls[0].name, "projects/test-proxy")
    expect_equal(len(calls[0].spans), 1)
    output = capsys.readouterr().out
    expect_not_in("Traceback", output + caplog.text)
    expect_not_in("private credentials", output + caplog.text)
    expect(not caplog.records)
    if error_name:
        expect_equal(result, SpanExportResult.FAILURE)
        entry = json.loads(output)
        expect_equal(entry["severity"], "WARNING")
        expect_equal(entry["error_type"], error_name)
        expect_equal(entry["span_count"], 1)
    else:
        expect_equal(result, SpanExportResult.SUCCESS)
        expect_equal(output, "")


def test_failed_cloud_trace_export_does_not_interrupt_inference(trace_client: TraceClient, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify failed cloud trace export does not interrupt inference."""

    class Client:
        def batch_write_spans(self, **_kwargs: object) -> Never:
            raise DeadlineExceeded("export unavailable")

    exporter = tracing.WarningCloudTraceSpanExporter(
        project_id="test-proxy",
        client=Client(),
    )
    tracing.state.provider.add_span_processor(SimpleSpanProcessor(exporter))
    client, _, upstream, _ = trace_client
    response = client.post(PATH, json={}, buffered=True)
    expect_equal(response.status_code, 200)
    expect_in(b"private output", response.data)
    response.close()
    expect(upstream.closed)
    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    summary = next(entry for entry in entries if entry.get("event_kind") == "inference_completed")
    expect_equal(summary["outcome"], "completed")
    expect(summary["stream_completed"] is True)
    expect(any(entry.get("error_type") == "DeadlineExceeded" and entry["severity"] == "WARNING" for entry in entries))
