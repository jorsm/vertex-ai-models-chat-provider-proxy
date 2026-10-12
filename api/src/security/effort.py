"""Enforce the catalog's named effort choices in native provider payloads."""

from typing import Any


class EffortError(ValueError):
    """A native effort field does not conform to the catalog policy."""


def _object_field(body: dict[str, Any], key: str) -> dict[str, Any]:
    """Read an optional object without silently repairing malformed input."""
    value = body.get(key, {})
    if not isinstance(value, dict):
        raise EffortError(f"{key} must be an object.")
    return value


def _validate_value(model: dict[str, Any], container: dict[str, Any], key: str, *, default: bool) -> None:
    """Validate an explicit value or inject the declared default into a provider field."""
    policy = model.get("effort")
    if policy is None:
        if key in container:
            raise EffortError(f"Model '{model['id']}' does not declare named effort support.")
        return
    if key not in container and not default:
        return
    value = container.get(key, policy["default"])
    if key == "thinkingLevel" and isinstance(value, str):
        value = value.lower()
    if not isinstance(value, str) or value not in policy["values"]:
        raise EffortError(f"Model '{model['id']}': effort is not permitted. Allowed: {policy['values']}")
    container[key] = value.upper() if key == "thinkingLevel" else value


def enforce_model_effort(model: dict[str, Any], body: dict[str, Any]) -> None:
    """Validate native effort controls and apply the catalog default on omission.

    Preserve provider options, messages, signatures, tools and thinking mode.
    Claude message-level overrides are checked without adding message defaults.
    """
    if model["vendor"] == "google":
        generation = _object_field(body, "generationConfig")
        thinking = _object_field(generation, "thinkingConfig")
        if model.get("effort") is not None and "thinkingBudget" in thinking:
            raise EffortError("Use a catalog thinkingLevel instead of thinkingBudget for this model.")
        _validate_value(model, thinking, "thinkingLevel", default=True)
        if model.get("effort") is not None:
            generation["thinkingConfig"] = thinking
            body["generationConfig"] = generation
        return

    output = _object_field(body, "output_config")
    _validate_value(model, output, "effort", default=True)
    if model.get("effort") is not None:
        body["output_config"] = output
    messages = body.get("messages", [])
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and "output_config" in message:
                _validate_value(model, _object_field(message, "output_config"), "effort", default=False)


def effective_model_effort(model: dict[str, Any], body: dict[str, Any]) -> str | None:
    """Read the validated request's top-level effort for bounded usage telemetry."""
    if model.get("effort") is None:
        return None
    if model["vendor"] == "google":
        value = body.get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingLevel")
    else:
        value = body.get("output_config", {}).get("effort")
    return value.lower() if isinstance(value, str) else None
