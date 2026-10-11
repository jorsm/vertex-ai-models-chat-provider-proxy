"""Structured JSON logger for Google Cloud Logging and Cloud Trace.

Follows Google Cloud structured logging specifications:
- Correlates logs using LOG_EXECUTION_ID and Cloud Trace contexts
- Strictly avoids logging sensitive user prompts or source code
- In case of error, logs the full response payload sent to the user for fast troubleshooting.
"""

import contextlib
import json
import sys
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from config import settings
from telemetry.trace_context import trace_log_fields

if TYPE_CHECKING:
    from flask import Request

_TRUTHY_FIELDS = {"user_email": "user_email", "model_id": "model_id", "labels": "labels"}
_KNOWN_FIELDS = frozenset({*_TRUTHY_FIELDS, "status_code", "duration_ms", "error_response", "extra"})


def _optional_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Map the optional `log_event` keyword fields to their log entry keys."""
    entry = {key: fields[name] for name, key in _TRUTHY_FIELDS.items() if fields.get(name)}
    if fields.get("status_code") is not None:
        entry["http_status"] = fields["status_code"]
    if fields.get("duration_ms") is not None:
        entry["duration_ms"] = round(fields["duration_ms"], 2)
    if fields.get("error_response") is not None:
        entry["error_response"] = fields["error_response"]
    return entry


def log_event(severity: str, message: str, request: Request | None = None, **fields: Any) -> None:
    """Emit a structured JSON log entry to stdout for Google Cloud Logging.

    Prompt contents and source code must never be passed in. Optional keyword fields:
    `user_email`, `model_id`, `labels`, `status_code`, `duration_ms`,
    `error_response` (logged for immediate debugging) and `extra` (merged verbatim).
    """
    unknown = set(fields) - _KNOWN_FIELDS
    if unknown:
        raise TypeError(f"Unexpected log fields: {sorted(unknown)}")

    log_entry: dict[str, Any] = {
        "severity": severity.upper(),
        "message": message,
        "component": "ai-companion-proxy",
        "timestamp": time.time(),
    }

    if request:
        exec_id = request.headers.get("Function-Execution-Id") or request.headers.get("X-Request-Id")
        if settings.log_execution_id and exec_id:
            log_entry["execution_id"] = exec_id
        log_entry["http_method"] = request.method
        log_entry["http_path"] = request.path

    log_entry.update(trace_log_fields(request))
    log_entry.update(_optional_fields(fields))
    log_entry.update(fields.get("extra") or {})

    sys.stdout.write(json.dumps(log_entry) + "\n")
    sys.stdout.flush()


def _error_excerpt(payload: Any, text: str | None, limit: int = 500) -> str:
    """Return a one-line error summary so the Logs Explorer row is readable without expanding it."""
    item = payload[0] if isinstance(payload, list) and payload else payload
    detail = item.get("error", item) if isinstance(item, dict) else None
    if isinstance(detail, dict):
        summary = " ".join(str(part) for part in (detail.get("status"), detail.get("message")) if part)
    else:
        summary = text or ""
    summary = " ".join(summary.split())
    return summary if len(summary) <= limit else summary[:limit] + "..."


class ErrorResponseCapture:
    """Copy only failed HTTP response bytes, with bounded memory/log size."""

    MAX_BYTES = 128 * 1024

    def __init__(self) -> None:
        """Start with an empty capture buffer."""
        self.body = bytearray()
        self.total_bytes = 0

    def feed(self, chunk: bytes) -> None:
        """Record a response chunk, keeping at most `MAX_BYTES` bytes."""
        self.total_bytes += len(chunk)
        remaining = self.MAX_BYTES - len(self.body)
        if remaining > 0:
            self.body.extend(chunk[:remaining])

    def log(self, status_code: int, request: Request | None = None, **metadata: Any) -> None:
        """Log the captured error response together with the given metadata."""
        truncated = self.total_bytes > len(self.body)
        fields = {
            "event_kind": "upstream_error_response",
            "error_response_bytes": self.total_bytes,
            "error_response_truncated": truncated,
            **metadata,
        }
        payload = None
        if not truncated:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                payload = json.loads(self.body)
        text = None
        if payload is None:
            text = self.body.decode("utf-8", errors="replace")
            fields["error_response_text"] = text
        excerpt = _error_excerpt(payload, text)
        log_event(
            "ERROR" if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR else "WARNING",
            f"Upstream returned HTTP {status_code}" + (f": {excerpt}" if excerpt else ""),
            request=request,
            status_code=status_code,
            error_response=payload,
            extra=fields,
        )
