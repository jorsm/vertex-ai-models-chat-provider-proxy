"""Provider usage normalization and pricing tests."""

import json
from decimal import Decimal

from checks import expect, expect_equal, expect_is

from usage.tracking import UsageAccumulator, pricing_fields


def test_gemini_sse_usage_is_parsed_across_chunk_boundaries() -> None:
    """Verify gemini sse usage is parsed across chunk boundaries."""
    accumulator = UsageAccumulator("google", "text/event-stream")
    payload = {
        "responseId": "gemini-response-1",
        "usageMetadata": {
            "promptTokenCount": 100,
            "cachedContentTokenCount": 40,
            "toolUsePromptTokenCount": 10,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 5,
            "totalTokenCount": 135,
        },
    }
    encoded = f"data: {json.dumps(payload)}\r\n\r\n".encode()
    accumulator.feed(encoded[:17])
    accumulator.feed(encoded[17:])

    usage = accumulator.finish()

    expect_equal(accumulator.provider_response_id, "gemini-response-1")
    expect_equal(usage["input_tokens_total"], 110)
    expect_equal(usage["input_tokens_uncached"], 70)
    expect_equal(usage["cache_read_tokens"], 40)
    expect_equal(usage["output_tokens"], 20)
    expect_equal(usage["thinking_tokens"], 5)
    expect_equal(usage["billable_output_tokens"], 25)
    expect_equal(usage["total_tokens"], 135)
    expect(usage["usage_complete"] is True)


def test_claude_stream_merges_start_and_delta_usage() -> None:
    """Verify claude stream merges start and delta usage."""
    accumulator = UsageAccumulator("anthropic", "text/event-stream; charset=utf-8")
    start = {
        "type": "message_start",
        "message": {
            "id": "msg_123",
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 5,
                    "ephemeral_1h_input_tokens": 15,
                },
            },
        },
    }
    delta = {"type": "message_delta", "usage": {"output_tokens": 40}}
    accumulator.feed(f"event: message_start\ndata: {json.dumps(start)}\n\n".encode())
    accumulator.feed(f"event: message_delta\ndata: {json.dumps(delta)}\n\n".encode())

    usage = accumulator.finish()

    expect_equal(accumulator.provider_response_id, "msg_123")
    expect_equal(usage["input_tokens_total"], 150)
    expect_equal(usage["input_tokens_uncached"], 100)
    expect_equal(usage["cache_read_tokens"], 30)
    expect_equal(usage["cache_write_tokens"], 20)
    expect_equal(usage["cache_write_5m_tokens"], 5)
    expect_equal(usage["cache_write_1h_tokens"], 15)
    expect_equal(usage["output_tokens"], 40)
    expect_equal(usage["total_tokens"], 190)


def test_non_streaming_json_usage_and_cost_snapshot() -> None:
    """Verify non streaming json usage and cost snapshot."""
    accumulator = UsageAccumulator("google", "application/json")
    accumulator.feed(
        json.dumps(
            {
                "usageMetadata": {
                    "promptTokenCount": 1000,
                    "cachedContentTokenCount": 400,
                    "candidatesTokenCount": 200,
                    "thoughtsTokenCount": 50,
                    "totalTokenCount": 1250,
                },
            },
        ).encode(),
    )
    usage = accumulator.finish()

    pricing = pricing_fields(
        {
            "pricing": {
                "input": 2,
                "output": 10,
                "cache_read": 0.2,
                "cache_create": 3,
            },
        },
        usage,
    )

    expect_equal(Decimal(pricing["estimated_input_cost"]), Decimal("0.0012"))
    expect_equal(Decimal(pricing["estimated_output_cost"]), Decimal("0.0025"))
    expect_equal(Decimal(pricing["estimated_cache_read_cost"]), Decimal("0.00008"))
    expect_equal(Decimal(pricing["estimated_total_cost"]), Decimal("0.00378"))


def test_missing_usage_is_null_not_zero() -> None:
    """Verify missing usage is null not zero."""
    accumulator = UsageAccumulator("google", "text/event-stream")
    accumulator.feed(b'data: {"candidates": []}\n\n')

    usage = accumulator.finish()

    expect(usage["usage_complete"] is False)
    expect_is(usage["total_tokens"], None)
    expect_is(usage["input_tokens_total"], None)
    expect_is(usage["provider_usage"], None)


def test_first_content_ignores_metadata_and_waits_for_complete_sse_record() -> None:
    """Verify first content ignores metadata and waits for complete sse record."""
    accumulator = UsageAccumulator("google", "text/event-stream")
    accumulator.feed(
        b': heartbeat\n\ndata: {"usageMetadata": {"promptTokenCount": 1}}\n\n',
        10,
    )
    expect_is(accumulator.first_content_ms, None)
    content = b'data: {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}\n\n'
    accumulator.feed(content[:40], 20)
    expect_is(accumulator.first_content_ms, None)
    accumulator.feed(content[40:], 30)
    expect_equal(accumulator.first_content_ms, 30)
    accumulator.feed(content, 40)
    expect_equal(accumulator.first_content_ms, 30)


def test_claude_first_content_ignores_start_and_empty_delta() -> None:
    """Verify claude first content ignores start and empty delta."""
    accumulator = UsageAccumulator("anthropic", "text/event-stream")
    accumulator.feed(
        b'data: {"type": "message_start", "message": {"id": "msg"}}\n\n',
        10,
    )
    accumulator.feed(
        b'data: {"type": "content_block_delta", "delta": {"text": ""}}\n\n',
        20,
    )
    expect_is(accumulator.first_content_ms, None)
    accumulator.feed(
        b'data: {"type": "content_block_delta", "delta": {"thinking": "reason"}}\n\n',
        30,
    )
    expect_equal(accumulator.first_content_ms, 30)


def test_non_streaming_content_has_no_first_token_measurement() -> None:
    """Verify non streaming content has no first token measurement."""
    accumulator = UsageAccumulator("google", "application/json")
    accumulator.feed(
        b'{"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}',
        10,
    )
    accumulator.finish()
    expect_is(accumulator.first_content_ms, None)
