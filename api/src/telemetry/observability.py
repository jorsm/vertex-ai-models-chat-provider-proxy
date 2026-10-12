"""Stable, bounded operational log schema derived from usage events."""

import json
import os
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from telemetry.logger import log_event

if TYPE_CHECKING:
    from flask import Request

SCALAR_FIELDS = (
    "event_id",
    "request_id",
    "trace_id",
    "provider_response_id",
    "schema_version",
    "request_started_at",
    "response_completed_at",
    "principal_subject",
    "principal_email",
    "principal_display_name",
    "client_user_label",
    "workspace_label",
    "provider",
    "model_requested",
    "model_upstream",
    "model_effort",
    "vertex_region",
    "model_family",
    "model_version",
    "max_input_tokens",
    "max_output_tokens",
    "endpoint_action",
    "pricing_source",
    "pricing_rule_id",
    "input_price_per_million",
    "output_price_per_million",
    "cache_read_price_per_million",
    "cache_write_price_per_million",
    "http_status",
    "outcome",
    "stream_completed",
    "usage_complete",
    "duration_ms",
    "time_to_first_chunk_ms",
    "time_to_first_token_ms",
    "input_tokens_total",
    "input_tokens_uncached",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "thinking_tokens",
    "tool_use_tokens",
    "billable_output_tokens",
    "total_tokens",
    "currency",
    "estimated_input_cost",
    "estimated_output_cost",
    "estimated_cache_read_cost",
    "estimated_cache_write_cost",
    "estimated_total_cost",
)

METADATA_FIELDS = ("effective_labels", "model_metadata", "provider_usage")


def log_inference_summary(event: dict[str, Any], request: Request | None) -> None:
    """Emit queryable scalars, exact cost strings and serialized metadata.

    Dynamic objects remain strings to avoid BigQuery schema conflicts. Numeric
    cost fields feed metrics; *_decimal fields preserve accounting precision.
    The caller supplies metadata only, never prompts or generated content.
    """
    fields = {key: event[key] for key in SCALAR_FIELDS if event.get(key) is not None}
    for key in SCALAR_FIELDS:
        if key.startswith("estimated_") and key in fields:
            fields[f"{key}_decimal"] = str(fields[key])
            fields[key] = float(fields[key])
    for key in METADATA_FIELDS:
        if event.get(key) is not None:
            fields[f"{key}_json"] = json.dumps(
                event[key],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
    fields.update(
        {
            "event_kind": "inference_completed",
            "telemetry_schema_version": 1,
            "service_version": os.getenv("K_REVISION") or "unknown",
        },
    )
    outcome = event["outcome"]
    severity = "INFO" if outcome == "completed" else "WARNING"
    if event["http_status"] >= HTTPStatus.INTERNAL_SERVER_ERROR or outcome == "stream_error":
        severity = "ERROR"
    log_event(
        severity,
        "Inference invocation finished",
        request=request,
        model_id=event.get("model_requested"),
        status_code=event["http_status"],
        duration_ms=event["duration_ms"],
        extra=fields,
    )
