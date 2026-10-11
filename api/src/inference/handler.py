"""Authenticated Vertex inference handler for the proxy service."""

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx
from flask import Request, Response, jsonify, stream_with_context
from google.auth.exceptions import GoogleAuthError
from opentelemetry import trace

from catalog.discovery import get_model_spec
from config import settings
from security.auth import AuthError, authenticate_caller, display_name_from_claims
from security.governance import (
    GovernanceError,
    extract_model_id,
    extract_region,
    validate_model_and_enforce_policy,
)
from telemetry.logger import ErrorResponseCapture, log_event
from telemetry.observability import log_inference_summary
from telemetry.trace_context import current_trace_id
from telemetry.tracing import stage_span, traced_inference
from usage.tracking import (
    UsageAccumulator,
    pricing_fields,
    provider_for_model,
    timestamp_iso,
    upstream_model_for,
)
from vertex.client import count_tokens_vertex, forward_to_vertex

if TYPE_CHECKING:
    from collections.abc import Iterator

_EVENT_SCHEMA_VERSION = 4
_CHARS_PER_TOKEN_ESTIMATE = 4
_UPSTREAM_ERRORS = (httpx.HTTPError, GoogleAuthError, ValueError, OSError)


def _logged_region(path: str) -> str | None:
    """Return the request region only when policy allows it; client values are unbounded."""
    region = extract_region(path)
    return region if region in settings.allowed_regions else None


def _effective_labels(modified_body: Any, extra_headers: dict[str, str]) -> dict[str, Any]:
    """Return enforced governance labels without retaining request content."""
    labels = modified_body.get("labels") if isinstance(modified_body, dict) else None
    if isinstance(labels, dict):
        return labels

    encoded = extra_headers.get("X-Vertex-AI-Labels")
    if encoded:
        try:
            decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except ValueError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


class _EarlyResponseError(Exception):
    """Abort request processing with a ready-made error response."""

    def __init__(self, response: Response) -> None:
        """Store the response to return to the client."""
        super().__init__(response.status_code)
        self.response = response


def _error_response(code: int, status: str, message: str, **extra: Any) -> tuple[Response, dict[str, Any]]:
    """Build a Google-style JSON error response and its payload for logging."""
    payload = {"error": {"code": code, "status": status, "message": message, **extra}}
    response = jsonify(payload)
    response.status_code = code
    return response, payload


@dataclass
class _InferenceContext:
    """State shared by the stages of one inference request."""

    request: Request
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request_started_at: str = field(default_factory=timestamp_iso)
    start_time: float = field(default_factory=time.perf_counter)
    claims: dict[str, Any] = field(default_factory=dict)
    user_email: str | None = None

    def elapsed_ms(self) -> float:
        """Milliseconds elapsed since the request started."""
        return round((time.perf_counter() - self.start_time) * 1000, 2)

    def base_event(self, response_completed_at: str) -> dict[str, Any]:
        """Return the identity and timing fields common to every summary event."""
        return {
            "event_id": self.event_id,
            "schema_version": _EVENT_SCHEMA_VERSION,
            "request_id": self.request.headers.get("X-Request-Id") or self.request.headers.get("Function-Execution-Id"),
            "trace_id": current_trace_id(self.request),
            "request_started_at": self.request_started_at,
            "response_completed_at": response_completed_at,
            "principal_email": self.user_email,
            "principal_subject": self.claims.get("sub"),
            "principal_display_name": display_name_from_claims(self.claims),
        }

    def log_early_failure(self, outcome: str, status: int, model_id: str | None = None) -> None:
        """Emit the terminal summary of a request rejected before streaming; model names stay unvalidated."""
        log_inference_summary(
            {
                **self.base_event(timestamp_iso()),
                "provider": provider_for_model(model_id or ""),
                "model_requested": model_id or "unknown",
                "model_upstream": upstream_model_for(model_id) if model_id else "unknown",
                "vertex_region": _logged_region(self.request.path),
                "http_status": status,
                "outcome": outcome,
                "stream_completed": False,
                "usage_complete": False,
                "duration_ms": self.elapsed_ms(),
            },
            self.request,
        )


@dataclass
class _Upstream:
    """Upstream response headers and its not yet consumed body stream."""

    status_code: int
    headers: dict[str, str]
    chunks: Iterator[bytes]


def _authenticate(ctx: _InferenceContext) -> None:
    """Authenticate the caller, or abort with a 401/403 response."""
    try:
        with stage_span("authentication"):
            ctx.user_email, ctx.claims = authenticate_caller(ctx.request.headers.get("Authorization"))
    except AuthError as auth_err:
        ctx.log_early_failure("authentication_rejected", auth_err.status_code)
        status = "UNAUTHENTICATED" if auth_err.status_code == HTTPStatus.UNAUTHORIZED else "PERMISSION_DENIED"
        response, payload = _error_response(auth_err.status_code, status, auth_err.message)
        log_event(
            "WARNING",
            f"Authentication failure: {auth_err.message}",
            request=ctx.request,
            status_code=auth_err.status_code,
            error_response=payload,
        )
        raise _EarlyResponseError(response) from auth_err

    log_event(
        "INFO",
        f"User successfully authenticated: {ctx.user_email}",
        request=ctx.request,
        user_email=ctx.user_email,
        extra={"principal_display_name": display_name_from_claims(ctx.claims)},
    )


def _parse_body(request: Request) -> tuple[bytes, Any]:
    """Return the raw body and its parsed JSON (None if absent or invalid)."""
    with stage_span("request.parsing"):
        raw_body = request.get_data()
        body_json = None
        if raw_body:
            try:
                body_json = json.loads(raw_body.decode("utf-8"))
            except ValueError, RecursionError:
                body_json = None
    return raw_body, body_json


def _enforce_governance(ctx: _InferenceContext, body_json: Any) -> tuple[str, Any, dict[str, str]]:
    """Apply model, region and label policy, or abort with a 400 response."""
    try:
        with stage_span("governance"):
            result = validate_model_and_enforce_policy(
                ctx.request.path,
                body_json,
                ctx.user_email,
                headers=dict(ctx.request.headers),
            )
    except GovernanceError as gov_err:
        ctx.log_early_failure("governance_rejected", gov_err.status_code)
        response, payload = _error_response(gov_err.status_code, "FAILED_PRECONDITION", gov_err.message)
        log_event(
            "WARNING",
            f"Governance check failed: {gov_err.message}",
            request=ctx.request,
            user_email=ctx.user_email,
            model_id=extract_model_id(ctx.request.path, body_json),
            status_code=gov_err.status_code,
            error_response=payload,
        )
        raise _EarlyResponseError(response) from gov_err
    return result


def _enforce_context_limit(ctx: _InferenceContext, model_id: str, body: Any, content: bytes) -> None:
    """Reject requests whose context exceeds the configured limit, to contain cloud costs."""
    limit = settings.max_context_tokens
    if limit <= 0 or body is None:
        return
    detected_tokens = count_tokens_vertex(ctx.request.path, body)
    if detected_tokens is None and content:
        detected_tokens = len(content) // _CHARS_PER_TOKEN_ESTIMATE
    if detected_tokens is None or detected_tokens <= limit:
        return

    ctx.log_early_failure("context_limit_exceeded", HTTPStatus.BAD_REQUEST, model_id)
    message = f"Context limit exceeded ({detected_tokens:,} estimated tokens out of a maximum allowed of {limit:,}). This restriction is enforced to contain cloud costs. Please start a new conversation ('+'), clear the existing chat ('/clear'), or compact the context ('/compact')."
    response, payload = _error_response(
        HTTPStatus.BAD_REQUEST,
        "FAILED_PRECONDITION",
        message,
        context_tokens=detected_tokens,
        max_context_tokens=limit,
    )
    log_event(
        "WARNING",
        f"Context limit exceeded for user {ctx.user_email}: {detected_tokens} > {limit}",
        request=ctx.request,
        user_email=ctx.user_email,
        model_id=model_id,
        status_code=HTTPStatus.BAD_REQUEST,
        error_response=payload,
    )
    raise _EarlyResponseError(response)


def _forward(ctx: _InferenceContext, model_id: str, headers: dict[str, str], content: bytes) -> _Upstream:
    """Send the request to Vertex AI, or abort with a 502 response."""
    request = ctx.request
    try:
        with stage_span("upstream") as upstream_span:
            status_code, upstream_headers, chunks = forward_to_vertex(
                method=request.method,
                path=request.path,
                query_params=dict(request.args),
                headers=headers,
                content=content,
            )
            upstream_span.set_attribute("http.response.status_code", status_code)
            if status_code >= HTTPStatus.BAD_REQUEST:
                upstream_span.set_status(trace.StatusCode.ERROR)
    except _UPSTREAM_ERRORS as upstream_err:
        ctx.log_early_failure("upstream_connection_error", HTTPStatus.BAD_GATEWAY, model_id)
        response, payload = _error_response(
            HTTPStatus.BAD_GATEWAY,
            "BAD_GATEWAY",
            f"Error communicating with Vertex AI upstream: {upstream_err}",
        )
        log_event(
            "ERROR",
            f"Upstream communication failure with Vertex AI: {upstream_err}",
            request=request,
            user_email=ctx.user_email,
            model_id=model_id,
            status_code=HTTPStatus.BAD_GATEWAY,
            error_response=payload,
        )
        raise _EarlyResponseError(response) from upstream_err
    return _Upstream(status_code, upstream_headers, chunks)


class _MeteredStream:
    """Pass the upstream body through while measuring latency and token usage."""

    def __init__(self, ctx: _InferenceContext, model_id: str, upstream: _Upstream, labels: dict[str, Any]) -> None:
        """Prepare accumulators for one upstream response."""
        self.ctx = ctx
        self.model_id = model_id
        self.upstream = upstream
        self.labels = labels
        self.provider = provider_for_model(model_id)
        self.accumulator = UsageAccumulator(
            provider=self.provider,
            content_type=upstream.headers.get("Content-Type", "application/json"),
        )
        failed = upstream.status_code >= HTTPStatus.BAD_REQUEST
        self.error_capture = ErrorResponseCapture() if failed else None
        self.outcome = "upstream_error" if failed else "completed"
        self.first_chunk_ms: float | None = None
        self.stream_completed = False

    def stream(self) -> Iterator[bytes]:
        """Yield upstream chunks, then log the usage summary however the stream ends."""
        try:
            for chunk in self.upstream.chunks:
                self._observe(chunk)
                yield chunk
            self.stream_completed = True
        except GeneratorExit:
            self.outcome = "client_disconnected"
            raise
        except Exception:
            self.outcome = "stream_error"
            raise
        finally:
            with stage_span("usage.finalization"):
                self._finalize()

    def _observe(self, chunk: bytes) -> None:
        """Record timing and usage information carried by a chunk."""
        if self.error_capture is not None:
            self.error_capture.feed(chunk)
        elapsed_ms = self.ctx.elapsed_ms()
        if self.first_chunk_ms is None:
            self.first_chunk_ms = elapsed_ms
            trace.get_current_span().add_event("first_chunk")
        previous_content_ms = self.accumulator.first_content_ms
        self.accumulator.feed(chunk, elapsed_ms=elapsed_ms)
        if previous_content_ms is None and self.accumulator.first_content_ms is not None:
            trace.get_current_span().add_event("first_content")

    def _finalize(self) -> None:
        """Log upstream errors and emit the final invocation summary."""
        ctx = self.ctx
        if self.error_capture is not None:
            self.error_capture.log(
                self.upstream.status_code,
                request=ctx.request,
                model_id=self.model_id,
                user_email=ctx.user_email,
                invocation_event_id=ctx.event_id,
                error_response_complete=self.stream_completed,
            )
        usage = self.accumulator.finish()
        if not usage["usage_complete"] and self.outcome == "completed":
            self.outcome = "usage_missing"

        model_spec = get_model_spec(self.model_id)
        spec = model_spec or {}
        path = ctx.request.path
        log_inference_summary(
            {
                **ctx.base_event(timestamp_iso()),
                "provider_response_id": self.accumulator.provider_response_id,
                "client_user_label": self.labels.get(settings.billing_user_label_key),
                "workspace_label": self.labels.get(settings.billing_project_label_key),
                "effective_labels": self.labels,
                "provider": self.provider,
                "model_requested": self.model_id,
                "model_upstream": upstream_model_for(self.model_id),
                "model_family": spec.get("family"),
                "model_version": spec.get("version"),
                "max_input_tokens": spec.get("maxInputTokens"),
                "max_output_tokens": spec.get("maxOutputTokens"),
                "model_metadata": model_spec,
                "vertex_region": _logged_region(path),
                "endpoint_action": path.rsplit(":", 1)[-1] if ":" in path else "",
                "http_status": self.upstream.status_code,
                "outcome": self.outcome,
                "stream_completed": self.stream_completed,
                "duration_ms": ctx.elapsed_ms(),
                "time_to_first_chunk_ms": self.first_chunk_ms,
                "time_to_first_token_ms": self.accumulator.first_content_ms,
                "pricing_source": settings.models_catalog_url,
                "pricing_rule_id": spec.get("id"),
                **usage,
                **pricing_fields(model_spec, usage),
            },
            ctx.request,
        )


@traced_inference
def handle_inference(request: Request) -> Response:
    """Validate, forward, and meter a Vertex AI inference request."""
    ctx = _InferenceContext(request)
    try:
        _authenticate(ctx)
        raw_body, body_json = _parse_body(request)
        model_id, modified_body, extra_headers = _enforce_governance(ctx, body_json)

        with stage_span("request.encoding"):
            content = json.dumps(modified_body).encode("utf-8") if modified_body is not None else raw_body
            headers = {key.lower(): value for key, value in request.headers.items()}
            headers.update({key.lower(): value for key, value in extra_headers.items()})

        _enforce_context_limit(ctx, model_id, modified_body, content)
        upstream = _forward(ctx, model_id, headers, content)
    except _EarlyResponseError as early:
        return early.response

    labels = _effective_labels(modified_body, extra_headers)
    if upstream.status_code < HTTPStatus.BAD_REQUEST:
        severity = "INFO"
    elif upstream.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
        severity = "WARNING"
    else:
        severity = "ERROR"
    log_event(
        severity,
        f"Upstream response started with status {upstream.status_code} for model '{model_id}'",
        request=request,
        user_email=ctx.user_email,
        model_id=model_id,
        labels=labels,
        status_code=upstream.status_code,
    )

    return Response(
        stream_with_context(_MeteredStream(ctx, model_id, upstream, labels).stream()),
        status=upstream.status_code,
        headers=upstream.headers,
        content_type=upstream.headers.get("Content-Type", "application/json"),
    )
