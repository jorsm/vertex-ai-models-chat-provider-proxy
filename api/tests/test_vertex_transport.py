"""Real local HTTP transport: pooling, concurrent isolation and network spans."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Never

import httpx
import pytest
from checks import expect, expect_equal, expect_greater_equal, expect_not_in
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from telemetry import tracing
from vertex import client as vertex_client

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

type TransportFixture = tuple[Callable[[int], dict[str, Any]], InMemorySpanExporter, list[tuple[str, int]]]

SERVICE_CREDENTIAL = "service-token"


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[TransportFixture]:
    """Serve a local HTTP upstream and expose a caller, span exporter and connection log."""
    monkeypatch.setenv("TRACE_ENABLED", "true")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing.state, "provider", provider)
    monkeypatch.setattr(tracing.state, "tracer", provider.get_tracer("transport-test"))
    monkeypatch.setattr(vertex_client.state, "http_client", None)
    local = threading.local()
    monkeypatch.setattr(vertex_client, "get_vertex_access_token", lambda: local.token)
    connections: list[tuple[str, int]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            connections.append(self.client_address)
            data = self.rfile.read(int(self.headers["Content-Length"]))
            result = json.dumps(
                {
                    "body": data.decode(),
                    "label": self.headers["x-vertex-ai-labels"],
                    "auth": self.headers["Authorization"],
                    "cookie": self.headers.get("Cookie"),
                },
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.send_header("Set-Cookie", "private=must-not-leak; Path=/")
            self.end_headers()
            self.wfile.write(result)
            self.wfile.flush()

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    monkeypatch.setattr(vertex_client, "build_vertex_url", lambda *_args, **_kwargs: url)

    def call(index: int) -> dict[str, Any]:
        local.token = f"token-{index}"
        with tracing.stage_span("test.request"):
            status, _, stream = vertex_client.forward_to_vertex(
                "POST",
                "/",
                {},
                {"x-vertex-ai-labels": str(index)},
                str(index).encode(),
            )
            expect_equal(status, 200)
            return json.loads(b"".join(stream))

    yield call, exporter, connections
    if vertex_client.state.http_client is not None:
        vertex_client.state.http_client.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    provider.shutdown()


def test_connections_reused_without_persisting_cookies(transport: TransportFixture) -> None:
    """Verify connections reused without persisting cookies."""
    call, exporter, connections = transport
    expect_equal(
        call(1),
        {
            "body": "1",
            "label": "1",
            "auth": "Bearer token-1",
            "cookie": None,
        },
    )
    expect_equal(
        call(2),
        {
            "body": "2",
            "label": "2",
            "auth": "Bearer token-2",
            "cookie": None,
        },
    )
    expect_equal(connections[0], connections[1])
    spans = exporter.get_finished_spans()
    expect_equal(sum(span.name == "upstream.connect_tcp" for span in spans), 1)
    expect_equal(sum(span.name == "upstream.wait_response_headers" for span in spans), 2)
    expect(not vertex_client.get_http_client().is_closed)


def test_concurrent_requests_keep_payload_labels_auth_and_traces_separate(transport: TransportFixture) -> None:
    """Verify concurrent requests keep payload labels auth and traces separate."""
    call, exporter, _ = transport
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(call, range(24)))
    for index, result in enumerate(results):
        expect_equal(
            result,
            {
                "body": str(index),
                "label": str(index),
                "auth": f"Bearer token-{index}",
                "cookie": None,
            },
        )
    spans = exporter.get_finished_spans()
    roots = [span for span in spans if span.name == "test.request"]
    expect_equal(len(roots), 24)
    expect_equal(len({span.context.trace_id for span in roots}), 24)
    for root in roots:
        request_spans = [span for span in spans if span.context.trace_id == root.context.trace_id]
        headers = next(span for span in request_spans if span.name == "upstream.response_headers")
        for span in request_spans:
            if span.name in {
                "upstream.send_headers",
                "upstream.send_body",
                "upstream.wait_response_headers",
                "upstream.connect_tcp",
            }:
                expect_equal(span.parent.span_id, headers.context.span_id)
        expect_greater_equal(root.end_time, max(span.end_time for span in request_spans))
    expect_not_in("token-", str([(span.attributes, span.events) for span in spans]))


def test_send_failure_ends_network_span_and_does_not_close_shared_client(
    transport: TransportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify send failure ends network span and does not close shared client."""
    call, exporter, _ = transport
    original_send = vertex_client.get_http_client().send

    def fail(request: httpx.Request, **_kwargs: object) -> Never:
        callback = request.extensions["trace"]
        callback("connection.start_tls.started", {"secret": "private-token"})
        error = httpx.ConnectError("private-token")
        callback("connection.start_tls.failed", {"exception": error})
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(vertex_client.get_http_client(), "send", fail)
        with pytest.raises(httpx.ConnectError):
            call(0)
    expect_equal(vertex_client.get_http_client().send, original_send)
    expect(not vertex_client.get_http_client().is_closed)
    expect_equal(call(1)["body"], "1")
    tls = next(span for span in exporter.get_finished_spans() if span.name == "upstream.tls")
    expect_equal(tls.status.status_code, StatusCode.ERROR)
    expect_equal(tls.attributes["error.type"], "ConnectError")
    expect_not_in("private-token", str(tls.attributes))
    expect(not tls.events)


def test_credential_initialization_and_refresh_are_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify credential initialization and refresh are serialized."""
    calls = []

    class Credentials:
        valid = False
        token = None

        def refresh(self, _request: object) -> None:
            calls.append("refresh")
            self.token = SERVICE_CREDENTIAL
            self.valid = True

    credentials = Credentials()

    def default(**_kwargs: object) -> tuple[Credentials, str]:
        calls.append("default")
        return credentials, "project"

    monkeypatch.setattr(vertex_client.state, "credentials", None)
    monkeypatch.setattr(vertex_client.google.auth, "default", default)
    with ThreadPoolExecutor(max_workers=8) as executor:
        expect_equal(
            list(
                executor.map(
                    lambda _: vertex_client.get_vertex_access_token(),
                    range(24),
                ),
            ),
            [SERVICE_CREDENTIAL] * 24,
        )
    expect_equal(calls, ["default", "refresh"])
