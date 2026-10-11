"""Upstream Vertex AI communication and streaming proxy engine.

Handles Google Cloud authentication via runtime Service Account credentials
and streams raw chunks (SSE / chunked encoding) with zero memory buffering.
"""

import atexit
import re
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookiejar import Cookie, CookieJar
from typing import TYPE_CHECKING, Any

import google.auth
import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleAuthRequest
from opentelemetry.trace import SpanKind, StatusCode

from telemetry.tracing import network_trace, stage_span

if TYPE_CHECKING:
    from collections.abc import Generator

    from google.auth.credentials import Credentials

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_COUNT_TOKENS_FIELDS = ("contents", "systemInstruction", "system_instruction", "tools")
_HIGH_SUFFIX = "-high"
_PROJECT_LOCATION = re.compile(r"^v1/projects/[^/]+/locations/([a-z0-9-]+)/")
_auth_request = GoogleAuthRequest()


@dataclass
class ClientState:
    """Per-worker credentials and HTTP connection pool, created lazily and guarded by locks."""

    credentials: Credentials | None = None
    credentials_lock: threading.Lock = field(default_factory=threading.Lock)
    http_client: httpx.Client | None = None
    http_client_lock: threading.Lock = field(default_factory=threading.Lock)


state = ClientState()


class _NoCookies(CookieJar):
    """The stateless Vertex proxy must not persist cookies across requests."""

    def set_cookie(self, cookie: Cookie) -> None:
        """Discard the cookie."""


def get_http_client() -> httpx.Client:
    """Return the worker's thread-safe connection pool; request state stays local."""
    with state.http_client_lock:
        if state.http_client is None:
            state.http_client = httpx.Client(
                timeout=httpx.Timeout(300.0, connect=10.0),
                cookies=_NoCookies(),
            )
        return state.http_client


def _close_http_client() -> None:
    """Close the pool at shutdown only: never close another request's active connection pool."""
    if state.http_client is not None:
        state.http_client.close()


atexit.register(_close_http_client)


def get_vertex_access_token() -> str:
    """Acquire and refresh the Google OAuth2 access token of the runtime Service Account."""
    # ADC credentials and the refresh transport also carry mutable state.
    with state.credentials_lock:
        if state.credentials is None:
            state.credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
        if not state.credentials.valid:
            state.credentials.refresh(_auth_request)
        return state.credentials.token


def build_vertex_url(path: str) -> str:
    """Construct the target Vertex AI endpoint URL.

    Project and region are those sent by the client; governance has already
    checked the region is allowed.
    """
    clean_path = path.lstrip("/")

    # Resolve -high thinking alias to base model for Vertex AI endpoints
    if f"{_HIGH_SUFFIX}:" in clean_path:
        clean_path = clean_path.replace(f"{_HIGH_SUFFIX}:", ":")
    elif clean_path.endswith(_HIGH_SUFFIX):
        clean_path = clean_path.removesuffix(_HIGH_SUFFIX)

    if not clean_path.startswith("v1/"):
        clean_path = f"v1/{clean_path}"

    match = _PROJECT_LOCATION.match(clean_path)
    if not match:
        raise ValueError("Vertex path must include a project and a valid location.")
    effective_region = match.group(1)

    base_endpoint = "https://aiplatform.googleapis.com" if effective_region == "global" else f"https://{effective_region}-aiplatform.googleapis.com"

    return f"{base_endpoint}/{clean_path}"


def _count_tokens_path(path: str) -> str | None:
    """Return the countTokens variant of a generateContent path, or None if unsupported."""
    clean_path = path.lstrip("/")
    for action in (":streamGenerateContent", ":generateContent"):
        if action in clean_path:
            return clean_path.replace(action, ":countTokens")
    return None


def count_tokens_vertex(path: str, body_json: dict[str, Any]) -> int | None:
    """Invoke the Vertex AI countTokens endpoint for supported models (Gemini / Google publishers).

    Returns the token count, or None if unsupported, unavailable, or failed.
    """
    count_path = _count_tokens_path(path)
    if count_path is None or not isinstance(body_json, dict):
        return None
    # countTokens accepts only the content-related fields of the original request.
    payload = {key: body_json[key] for key in _COUNT_TOKENS_FIELDS if key in body_json}
    if not payload:
        return None

    try:
        response = get_http_client().post(
            build_vertex_url(count_path),
            headers={
                "Authorization": f"Bearer {get_vertex_access_token()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        data = response.json() if response.status_code == HTTPStatus.OK else None
    except ValueError, GoogleAuthError, httpx.HTTPError:
        return None

    if isinstance(data, dict) and "totalTokens" in data:
        return int(data["totalTokens"])
    return None


def forward_to_vertex(
    method: str,
    path: str,
    query_params: dict[str, Any],
    headers: dict[str, str],
    content: bytes,
) -> tuple[int, dict[str, str], Generator[bytes]]:
    """Execute a streaming request against Google Vertex AI.

    Returns:
      (status_code, response_headers, chunk_generator).

    """
    # Acquire Service Account token
    with stage_span("upstream.credentials"):
        token = get_vertex_access_token()

    with stage_span("upstream.client"):
        client = get_http_client()

    with stage_span("upstream.request_preparation"):
        target_url = build_vertex_url(path)
        upstream_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": headers.get("content-type", "application/json"),
            "User-Agent": "Vertex-AI-Companion-Proxy/1.0",
            "Accept-Encoding": "identity",
        }
        for header_name in ("x-vertex-ai-labels", "accept"):
            if header_name in headers:
                upstream_headers[header_name] = headers[header_name]
        upstream_req = client.build_request(
            method=method,
            url=target_url,
            params=query_params,
            headers=upstream_headers,
            content=content,
        )

    with stage_span("upstream.response_headers", kind=SpanKind.CLIENT) as span, network_trace() as callback:
        upstream_req.extensions["trace"] = callback
        upstream_resp = client.send(upstream_req, stream=True)
        span.set_attribute("http.response.status_code", upstream_resp.status_code)
        if upstream_resp.status_code >= HTTPStatus.BAD_REQUEST:
            span.set_status(StatusCode.ERROR)

    resp_headers = {
        "Content-Type": upstream_resp.headers.get("content-type", "application/json"),
    }

    def response_stream_generator() -> Generator[bytes]:
        try:
            chunks = iter(upstream_resp.iter_raw())
            with stage_span("upstream.first_chunk_wait"):
                first = next(chunks, None)
                while first is not None and not first:
                    first = next(chunks, None)
            if first is not None:
                yield first
            for chunk in chunks:
                if chunk:
                    yield chunk
        finally:
            with stage_span("upstream.response_close"):
                upstream_resp.close()

    return upstream_resp.status_code, resp_headers, response_stream_generator()
