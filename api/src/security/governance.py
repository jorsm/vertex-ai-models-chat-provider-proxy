"""Governance and label enforcement engine.

Validates requested models against enterprise allowlist
and injects sanitized governance labels into payloads and headers.
"""

import base64
import json
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from config import settings

if TYPE_CHECKING:
    from collections.abc import Callable

_MODEL_POLICY_SUFFIXES = ("-high", "-medium", "-max")
_HIGH_SUFFIX = "-high"
_MAX_LABEL_LENGTH = 63
_MANAGED_BY = "copilot-chat-proxy"
_CONFIG_PASSTHROUGH_KEYS = ("generationConfig", "systemInstruction", "tools", "safetySettings")


def _is_model_allowed(model_id: str, allowed_models: list[str]) -> bool:
    """Match both catalog aliases and the upstream model selected by an SDK.

    Claude catalog entries such as ``claude-sonnet-5-5-medium`` are policy
    profiles. The Anthropic SDK sends their shared Vertex model version
    (``claude-sonnet-5-5``) in the URL, so an authorized profile must also
    authorize that exact upstream version. Only the known policy suffixes are
    removed; arbitrary prefixes or sibling model versions remain denied.
    """
    model_id = model_id.replace(".", "-")
    for allowed_model in allowed_models:
        allowed = allowed_model.replace(".", "-")
        if model_id == allowed:
            return True
        for suffix in _MODEL_POLICY_SUFFIXES:
            if allowed.endswith(suffix) and model_id == allowed.removesuffix(suffix):
                return True
    return False


class GovernanceError(Exception):
    """Raised when a request violates enterprise policy; carries the HTTP status to return."""

    def __init__(self, status_code: int, message: str) -> None:
        """Store the HTTP status code and the client-facing message."""
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def sanitize_gcp_label(value: str) -> str:
    """Sanitize a string to conform to Google Cloud Resource Label requirements.

    - Lowercase letters, international characters, numbers, underscores (_), and hyphens (-)
    - Maximum 63 characters
    - Must start with a lowercase letter.
    """
    if not value:
        return "unknown"

    sanitized = re.sub(r"[^a-z0-9_-]", "_", value.lower().strip())
    # Ensure it starts with a lowercase letter
    if sanitized and not sanitized[0].isalpha():
        sanitized = f"v_{sanitized}"

    return sanitized[:_MAX_LABEL_LENGTH] if sanitized else "unknown"


def extract_model_id(path: str, body_json: dict[str, Any] | None) -> str:
    """Extract the model ID from the Vertex AI REST path or JSON body as sent by the VS Code extension.

    - Path format: /models/{model_id}:action (Gemini & Claude SDKs)
    - Body format: body["model"] (OpenAI-compatible / Grok).
    """
    path_match = re.search(r"/models/([^/:]+)", path)
    if path_match:
        return path_match.group(1)

    if body_json and isinstance(body_json, dict) and "model" in body_json:
        return str(body_json["model"])

    return ""


def extract_project(path: str) -> str:
    """Extract the Vertex project from /projects/{project}/locations/... paths."""
    match = re.search(r"/projects/([^/]+)/locations/", path)
    return match.group(1) if match else ""


def extract_region(path: str) -> str:
    """Extract the Vertex location from /projects/{project}/locations/{region}/... paths."""
    match = re.search(r"/locations/([^/]+)/", path)
    return match.group(1) if match else ""


def _first_label(labels: object, key: str) -> str | None:
    """Return the stripped, non-empty value of `key` (or its lowercase form) in a labels mapping."""
    if isinstance(labels, dict):
        value = labels.get(key) or labels.get(key.lower())
        if value and str(value).strip():
            return str(value).strip()
    return None


def _label_from_headers(key: str, headers: dict[str, str]) -> str | None:
    """Look up a label in a direct header or in the base64 JSON `X-Vertex-AI-Labels` header."""
    norm_headers = {name.lower(): value for name, value in headers.items()}

    direct = norm_headers.get(key.lower(), "").strip()
    if direct:
        return direct

    # Anthropic Claude base64 header
    raw_b64 = norm_headers.get("x-vertex-ai-labels", "").strip()
    if raw_b64:
        try:
            parsed_labels = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
        except ValueError:
            return None
        return _first_label(parsed_labels, key)
    return None


def extract_label_from_request(
    key: str,
    body_json: dict[str, Any] | None,
    headers: dict[str, str] | None = None,
) -> str | None:
    """Extract a label value from incoming headers or JSON body.

    Checks:
    1. HTTP headers (e.g. X-Vertex-AI-Labels base64 JSON, direct header matching key)
    2. Gemini config.labels (body.config.labels[key])
    3. Root labels (body.labels[key]).
    """
    if headers:
        value = _label_from_headers(key, headers)
        if value:
            return value

    if body_json and isinstance(body_json, dict):
        config_obj = body_json.get("config")
        config_labels = config_obj.get("labels") if isinstance(config_obj, dict) else None
        for labels in (config_labels, body_json.get("labels")):
            value = _first_label(labels, key)
            if value:
                return value

    return None


def _label_value(
    key: str,
    body_json: dict[str, Any] | None,
    headers: dict[str, str] | None,
    required: Callable[[], bool],
) -> str:
    """Return the sanitized client label, or "default" when absent and not required by policy."""
    raw = extract_label_from_request(key, body_json, headers)
    if not raw or not raw.strip():
        if required():
            raise GovernanceError(HTTPStatus.BAD_REQUEST, f"Missing required label '{key}'.")
        return "default"
    return sanitize_gcp_label(raw)


def _ensure_dict(container: dict[str, Any], key: str) -> dict[str, Any]:
    """Return `container[key]`, replacing it with an empty dict if it is not one."""
    value = container.get(key)
    if not isinstance(value, dict):
        value = container[key] = {}
    return value


def _unwrap_config(body_json: dict[str, Any]) -> None:
    """Move labels and standard fields out of a client-supplied `config` object."""
    if not isinstance(body_json.get("config"), dict):
        return
    config = body_json.pop("config")
    labels = _ensure_dict(body_json, "labels")
    if isinstance(config.get("labels"), dict):
        labels.update(config["labels"])
    for key in _CONFIG_PASSTHROUGH_KEYS:
        if key in config and key not in body_json:
            body_json[key] = config[key]


def _enforce_gemini_body(body_json: dict[str, Any], model_id: str, labels: dict[str, str]) -> None:
    """Inject the governance labels (and thinking level for -high aliases) into a Gemini body."""
    _unwrap_config(body_json)
    body_labels = _ensure_dict(body_json, "labels")
    # Client labels are remapped to the billing keys: drop the raw ones so unsanitized values never reach Vertex AI
    for chat_key in (settings.chat_user_label_key, settings.chat_project_label_key):
        if chat_key not in labels:
            body_labels.pop(chat_key, None)
            body_labels.pop(chat_key.lower(), None)
    body_labels.update(labels)

    # Thinking model alias (-high): inject thinkingConfig HIGH into generationConfig
    if model_id.endswith(_HIGH_SUFFIX):
        generation_config = _ensure_dict(body_json, "generationConfig")
        generation_config.setdefault("thinkingConfig", {"thinkingLevel": "HIGH"})

    # Vertex AI rejects "Unknown field: model" for Gemini
    body_json.pop("model", None)


def _strip_client_labels(body_json: dict[str, Any]) -> None:
    """Remove client labels from a body whose provider rejects them (Anthropic)."""
    body_json.pop("labels", None)
    config_obj = body_json.get("config")
    if isinstance(config_obj, dict):
        config_obj.pop("labels", None)
        if not config_obj:
            body_json.pop("config", None)


def validate_model_and_enforce_policy(
    path: str,
    body_json: dict[str, Any] | None,
    user_email: str,
    headers: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, str]]:
    """Validate model, project and region allowlists & deprecations and enforce governance labels.

    Enforces presence of required labels according to configuration.
    Injects a third strict label containing the authenticated user email from Google OIDC.

    Returns:
      (model_id, modified_body_json, extra_headers)

    Raises:
      GovernanceError(400) if model, project or region is not allowed or if required labels are missing.

    """
    model_id = extract_model_id(path, body_json)
    if not model_id:
        raise GovernanceError(HTTPStatus.BAD_REQUEST, "No model specified in the request.")

    if not _is_model_allowed(model_id, settings.allowed_models):
        raise GovernanceError(
            HTTPStatus.BAD_REQUEST,
            f"Model '{model_id}' is not in the approved allowlist. Approved models: {settings.allowed_models}",
        )

    # Project and region are chosen by the client; the allowlists bound the choice
    project = extract_project(path)
    if not project or project not in settings.allowed_vertex_projects:
        raise GovernanceError(
            HTTPStatus.BAD_REQUEST,
            f"Project '{project}' is not in the approved list. Approved projects: {settings.allowed_vertex_projects}",
        )

    region = extract_region(path)
    if not region or region not in settings.allowed_regions:
        raise GovernanceError(
            HTTPStatus.BAD_REQUEST,
            f"Region '{region}' is not in the approved list. Approved regions: {settings.allowed_regions}",
        )

    chat_project_key = settings.chat_project_label_key
    chat_user_key = settings.chat_user_label_key
    # Values are read under the chat keys and written under the billing keys
    labels = {
        settings.billing_project_label_key: _label_value(chat_project_key, body_json, headers, lambda: settings.require_project_label),
        settings.billing_user_label_key: _label_value(chat_user_key, body_json, headers, lambda: settings.require_user_label),
        # Strictly the authenticated caller email from the Google OIDC token (non-forgeable)
        settings.billing_auth_user_label_key: sanitize_gcp_label(user_email),
        "managed-by": _MANAGED_BY,
    }

    # Gemini accepts root JSON labels. Anthropic rejects that field, so Claude
    # receives the same enforced labels only through X-Vertex-AI-Labels below.
    if body_json and isinstance(body_json, dict):
        if model_id.lower().startswith("claude"):
            _strip_client_labels(body_json)
        else:
            _enforce_gemini_body(body_json, model_id, labels)

    encoded_labels = base64.b64encode(json.dumps(labels).encode("utf-8")).decode("utf-8")
    return model_id, body_json, {"X-Vertex-AI-Labels": encoded_labels}
