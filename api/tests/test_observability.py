"""Lossless usage serialization and stable fields for the Logging sink."""

import json
from typing import TYPE_CHECKING

from checks import expect, expect_equal, expect_not_in

from telemetry.observability import log_inference_summary

if TYPE_CHECKING:
    import pytest


def test_complete_event_preserves_precise_costs_and_variable_metadata(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify complete event preserves precise costs and variable metadata."""
    monkeypatch.setenv("K_REVISION", "proxy-revision-123")
    event = {
        "event_id": "event-123",
        "schema_version": 4,
        "http_status": 200,
        "outcome": "completed",
        "duration_ms": 10.0,
        "estimated_total_cost": "0.123456789123456789",
        "total_tokens": 0,
        "thinking_tokens": None,
        "principal_subject": "subject-123",
        "principal_display_name": "Zoë Müller",
        "effective_labels": {"workspace.with-dots": "project-a"},
        "model_metadata": {"pricing": {"input": 1.25}, "capabilities": {"new": True}},
        "provider_usage": {"tokenDetails": [{"modality": "TEXT", "tokenCount": 3}]},
    }
    log_inference_summary(event, None)
    entry = json.loads(capsys.readouterr().out)
    expect_equal(entry["estimated_total_cost_decimal"], event["estimated_total_cost"])
    expect_equal(entry["principal_subject"], event["principal_subject"])
    expect_equal(entry["principal_display_name"], event["principal_display_name"])
    for key in ("effective_labels", "model_metadata", "provider_usage"):
        expect_equal(json.loads(entry[f"{key}_json"]), event[key])
    expect_not_in("event_json", entry)
    expect_equal(entry["estimated_total_cost"], float(event["estimated_total_cost"]))
    expect_equal(entry["total_tokens"], 0)
    expect_not_in("thinking_tokens", entry)
    expect_not_in("effective_labels", entry)
    expect_not_in("model_metadata", entry)
    expect_not_in("provider_usage", entry)
    expect_equal(entry["service_version"], "proxy-revision-123")


def test_dynamic_metadata_can_change_types_without_changing_export_schema(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify dynamic metadata can change types without changing export schema."""
    for metadata in [{"details": "text"}, {"details": [{"new-key": 1}]}]:
        log_inference_summary(
            {
                "event_id": "event",
                "http_status": 200,
                "outcome": "completed",
                "duration_ms": 1.0,
                "provider_usage": metadata,
            },
            None,
        )
    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    expect_equal(entries[0].keys(), entries[1].keys())
    expect(all(isinstance(entry["provider_usage_json"], str) for entry in entries))
    expect(isinstance(json.loads(entries[0]["provider_usage_json"])["details"], str))
    expect(isinstance(json.loads(entries[1]["provider_usage_json"])["details"], list))
