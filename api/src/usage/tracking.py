"""Incremental provider usage parsing and cost estimation.

Only provider metadata is retained. Generated text, prompts and tool payloads
are never copied into the analytics event.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except TypeError, ValueError:
        return 0


def _utc_iso(timestamp: datetime | None = None) -> str:
    return (timestamp or datetime.now(UTC)).isoformat()


class UsageAccumulator:
    """Parses SSE incrementally and keeps only the latest usage snapshots."""

    def __init__(self, provider: str, content_type: str) -> None:
        """Create an accumulator for one upstream response of the given provider and content type."""
        self.provider = provider
        self.content_type = content_type.lower()
        self._line_buffer = b""
        self._json_buffer = bytearray()
        self._gemini_usage: dict[str, Any] | None = None
        self._claude_usage: dict[str, Any] = {}
        self.provider_response_id: str | None = None
        self.first_content_ms: float | None = None
        self._elapsed_ms: float | None = None

    @property
    def is_sse(self) -> bool:
        """Whether the response is a server-sent event stream."""
        return "text/event-stream" in self.content_type

    def feed(self, chunk: bytes, elapsed_ms: float | None = None) -> None:
        """Consume a response chunk observed `elapsed_ms` after the request started."""
        self._elapsed_ms = elapsed_ms
        if self.is_sse:
            self._line_buffer += chunk
            while b"\n" in self._line_buffer:
                raw_line, self._line_buffer = self._line_buffer.split(b"\n", 1)
                self._consume_sse_line(raw_line.rstrip(b"\r"))
        else:
            self._json_buffer.extend(chunk)

    def finish(self) -> dict[str, Any]:
        """Flush buffered data and return the normalized usage snapshot."""
        if self.is_sse:
            if self._line_buffer:
                self._consume_sse_line(self._line_buffer.rstrip(b"\r"))
                self._line_buffer = b""
        elif self._json_buffer:
            try:
                payload = json.loads(self._json_buffer)
                if isinstance(payload, list):
                    for item in payload:
                        self._consume_payload(item)
                else:
                    self._consume_payload(payload)
            except UnicodeDecodeError, json.JSONDecodeError:
                pass
            finally:
                self._json_buffer.clear()

        return self._normalized_usage()

    def _consume_sse_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            self._consume_payload(json.loads(data))
        except UnicodeDecodeError, json.JSONDecodeError:
            return

    def _consume_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return

        # Measure the first decoded content event, not SSE comments, usage or
        # message-start metadata. Only SSE has an observable first-token time.
        if self.is_sse and self.first_content_ms is None:
            parts = []
            candidates = payload.get("candidates")
            for candidate in candidates if isinstance(candidates, list) else []:
                content = candidate.get("content") if isinstance(candidate, dict) else None
                candidate_parts = content.get("parts") if isinstance(content, dict) else None
                if isinstance(candidate_parts, list):
                    parts.extend(part for part in candidate_parts if isinstance(part, dict))
            delta = payload.get("delta") or {}
            block = payload.get("content_block") or {}
            has_content = (
                any(part.get("text") or part.get("functionCall") or part.get("inlineData") for part in parts)
                or (payload.get("type") == "content_block_delta" and isinstance(delta, dict) and any(delta.get(key) for key in ("text", "thinking", "partial_json")))
                or (payload.get("type") == "content_block_start" and isinstance(block, dict) and any(block.get(key) for key in ("text", "thinking", "name")))
            )
            if has_content:
                self.first_content_ms = self._elapsed_ms

        usage = payload.get("usageMetadata")
        if isinstance(usage, dict):
            self._gemini_usage = usage
            self.provider_response_id = str(payload.get("responseId") or "") or self.provider_response_id

        message = payload.get("message")
        if isinstance(message, dict):
            self.provider_response_id = str(message.get("id") or "") or self.provider_response_id
            self._merge_claude_usage(message.get("usage"))

        self.provider_response_id = str(payload.get("id") or "") or self.provider_response_id
        self._merge_claude_usage(payload.get("usage"))

    def _merge_claude_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, dict):
                current = self._claude_usage.setdefault(key, {})
                if isinstance(current, dict):
                    current.update(value)
            elif value is not None:
                self._claude_usage[key] = value

    def _normalized_usage(self) -> dict[str, Any]:
        if self._gemini_usage is not None:
            raw = self._gemini_usage
            prompt = _int(raw.get("promptTokenCount"))
            cached = _int(raw.get("cachedContentTokenCount"))
            tool_use = _int(raw.get("toolUsePromptTokenCount"))
            output = _int(raw.get("candidatesTokenCount"))
            thinking = _int(raw.get("thoughtsTokenCount"))
            return {
                "input_tokens_total": prompt + tool_use,
                "input_tokens_uncached": max(prompt - cached, 0) + tool_use,
                "cache_read_tokens": cached,
                "cache_write_tokens": 0,
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0,
                "output_tokens": output,
                "thinking_tokens": thinking,
                "tool_use_tokens": tool_use,
                "billable_output_tokens": output + thinking,
                "total_tokens": _int(raw.get("totalTokenCount")) or prompt + tool_use + output + thinking,
                "usage_complete": True,
                "provider_usage": raw,
            }

        if self._claude_usage:
            raw = self._claude_usage
            uncached = _int(raw.get("input_tokens"))
            cache_read = _int(raw.get("cache_read_input_tokens"))
            cache_write = _int(raw.get("cache_creation_input_tokens"))
            output = _int(raw.get("output_tokens"))
            cache_detail = raw.get("cache_creation") or {}
            cache_5m = _int(cache_detail.get("ephemeral_5m_input_tokens"))
            cache_1h = _int(cache_detail.get("ephemeral_1h_input_tokens"))
            return {
                "input_tokens_total": uncached + cache_read + cache_write,
                "input_tokens_uncached": uncached,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "cache_write_5m_tokens": cache_5m,
                "cache_write_1h_tokens": cache_1h,
                "output_tokens": output,
                "thinking_tokens": None,
                "tool_use_tokens": None,
                "billable_output_tokens": output,
                "total_tokens": uncached + cache_read + cache_write + output,
                "usage_complete": True,
                "provider_usage": raw,
            }

        return {
            "input_tokens_total": None,
            "input_tokens_uncached": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "cache_write_5m_tokens": None,
            "cache_write_1h_tokens": None,
            "output_tokens": None,
            "thinking_tokens": None,
            "tool_use_tokens": None,
            "billable_output_tokens": None,
            "total_tokens": None,
            "usage_complete": False,
            "provider_usage": None,
        }


def provider_for_model(model_spec: dict[str, Any] | None) -> str:
    """Return the catalog vendor, or "unknown" for requests rejected before model resolution."""
    return model_spec["vendor"] if model_spec is not None else "unknown"


def upstream_model_for(model_id: str, model_spec: dict[str, Any] | None = None) -> str:
    """Return the catalog's literal backend version without changing model names."""
    return model_spec["version"] if model_spec is not None else model_id


def pricing_fields(
    model_spec: dict[str, Any] | None,
    usage: dict[str, Any],
) -> dict[str, Any]:
    """Compute per-million prices and estimated costs from model pricing and token usage."""
    pricing = model_spec.get("pricing", {}) if model_spec else {}

    def rate(name: str) -> Decimal | None:
        value = pricing.get(name)
        return Decimal(str(value)) if value is not None else None

    input_rate = rate("input")
    output_rate = rate("output")
    cache_read_rate = rate("cache_read")
    cache_write_rate = rate("cache_create")

    def component(tokens_name: str, unit_rate: Decimal | None) -> Decimal | None:
        tokens = usage.get(tokens_name)
        if tokens is None or unit_rate is None:
            return None
        return Decimal(tokens) * unit_rate / Decimal(1_000_000)

    input_cost = component("input_tokens_uncached", input_rate)
    output_cost = component("billable_output_tokens", output_rate)
    cache_read_cost = component("cache_read_tokens", cache_read_rate)
    cache_write_cost = component("cache_write_tokens", cache_write_rate)
    components = [input_cost, output_cost, cache_read_cost, cache_write_cost]
    total = sum((value for value in components if value is not None), Decimal(0)) if any(value is not None for value in components) else None

    def number(value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None

    return {
        "currency": "USD" if pricing else None,
        "input_price_per_million": number(input_rate),
        "output_price_per_million": number(output_rate),
        "cache_read_price_per_million": number(cache_read_rate),
        "cache_write_price_per_million": number(cache_write_rate),
        "estimated_input_cost": number(input_cost),
        "estimated_output_cost": number(output_cost),
        "estimated_cache_read_cost": number(cache_read_cost),
        "estimated_cache_write_cost": number(cache_write_cost),
        "estimated_total_cost": number(total),
    }


def timestamp_iso(timestamp: datetime | None = None) -> str:
    """Return the given (or current) UTC time as an ISO 8601 string."""
    return _utc_iso(timestamp)
