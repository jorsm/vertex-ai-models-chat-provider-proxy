"""Unit tests for Google Agent Platform for Copilot Chat - Proxy (Python 3.14)."""

import base64
import json
from typing import TYPE_CHECKING, Any, NamedTuple, Never

import httpx
import pytest
from checks import expect, expect_equal, expect_greater, expect_greater_equal, expect_in, expect_is, expect_is_not, expect_not_in

from catalog import discovery
from config import settings
from inference import handler as inference_handler
from main import app
from security.auth import AuthError, extract_bearer_token
from security.governance import (
    GovernanceError,
    _is_model_allowed,
    extract_model_id,
    sanitize_gcp_label,
    validate_model_and_enforce_policy,
)
from telemetry.logger import ErrorResponseCapture
from vertex.client import build_vertex_url

if TYPE_CHECKING:
    from collections.abc import Iterator

    from flask.testing import FlaskClient


class FailureCase(NamedTuple):
    """Expected behavior when a request fails at one pipeline stage."""

    failure: str
    expected_status: int
    expected_outcome: str


type UpstreamResult = tuple[int, dict[str, str], Iterator[bytes]]


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the required environment variables for test execution."""
    monkeypatch.setenv("HUB_PROJECT_ID", "test-hub-project")
    monkeypatch.setenv("ALLOWED_VERTEX_PROJECTS", "test-hub-project,user-project,test-project,my-proj,p")
    monkeypatch.setenv("ALLOWED_REGIONS", "global")
    monkeypatch.setenv("CHAT_USER_LABEL_KEY", "vscode-vertex-ai-user")
    monkeypatch.setenv("CHAT_PROJECT_LABEL_KEY", "vscode-vertex-ai-project")
    monkeypatch.setenv("BILLING_USER_LABEL_KEY", "vscode-vertex-ai-user")
    monkeypatch.setenv("BILLING_PROJECT_LABEL_KEY", "vscode-vertex-ai-project")
    monkeypatch.setenv("BILLING_AUTH_USER_LABEL_KEY", "proxy-auth-user")
    monkeypatch.setenv(
        "ALLOWED_MODELS",
        "claude-opus-5-5,claude-opus-5-5-high,claude-opus-5-5-max,claude-sonnet-5-5-high,claude-sonnet-5-5-medium,claude-sonnet-5-5-max,gemini-3.8-flash,gemini-3.8-flash-high",
    )
    monkeypatch.setenv(
        "MODELS_CATALOG_URL",
        "https://raw.githubusercontent.com/jorsm/vertex-ai-models-chat-provider/main/src/models.json",
    )
    monkeypatch.setenv("PROXY_BASE_URL", "https://proxy.example.test")
    monkeypatch.setenv("REQUIRE_PROJECT_LABEL", "true")
    monkeypatch.setenv("REQUIRE_USER_LABEL", "true")
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "500000")
    monkeypatch.setenv("DISCOVERY_MAX_INPUT_TOKENS", "250000")


def _path(model: str, action: str = "streamGenerateContent", region: str = "global", publisher: str = "google") -> str:
    return f"/v1/projects/user-project/locations/{region}/publishers/{publisher}/models/{model}:{action}"


GEMINI_PATH = _path("gemini-3.8-flash")


def test_sanitize_gcp_label() -> None:
    """Verify sanitize gcp label."""
    expect_equal(sanitize_gcp_label("john.smith@example.com"), "john_smith_example_com")
    expect(sanitize_gcp_label("123user@example.com").startswith("v_"))
    expect_equal(len(sanitize_gcp_label("a" * 100)), 63)
    expect_equal(sanitize_gcp_label(""), "unknown")


def test_extract_model_id() -> None:
    """Verify extract model id."""
    path = "/v1/projects/my-proj/locations/global/publishers/google/models/gemini-3.8-flash:streamGenerateContent"
    expect_equal(extract_model_id(path, None), "gemini-3.8-flash")

    body = {"model": "claude-sonnet-5-5"}
    expect_equal(extract_model_id("/v1/endpoints/openapi/chat/completions", body), "claude-sonnet-5-5")


def test_missing_project_label_rejects_call() -> None:
    """Verify missing project label rejects call."""
    path = GEMINI_PATH
    # Request body with user label but missing project label
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
        "config": {"labels": {"vscode-vertex-ai-user": "dev_custom_nickname"}},
    }
    with pytest.raises(GovernanceError) as excinfo:
        validate_model_and_enforce_policy(path, body, "jane.doe@example.com")
    expect_equal(excinfo.value.status_code, 400)
    expect_in("Missing required label 'vscode-vertex-ai-project'", excinfo.value.message)


def test_missing_user_label_rejects_call() -> None:
    """Verify missing user label rejects call."""
    path = GEMINI_PATH
    # Request body with project label but missing user label
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
        "config": {"labels": {"vscode-vertex-ai-project": "my-client-project"}},
    }
    with pytest.raises(GovernanceError) as excinfo:
        validate_model_and_enforce_policy(path, body, "jane.doe@example.com")
    expect_equal(excinfo.value.status_code, 400)
    expect_in("Missing required label 'vscode-vertex-ai-user'", excinfo.value.message)


def test_three_labels_enforced_strictly() -> None:
    """Verify three labels enforced strictly."""
    path = GEMINI_PATH
    # Client sends its own custom user nickname and project label
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
        "config": {
            "labels": {
                "vscode-vertex-ai-project": "ecommerce-frontend",
                "vscode-vertex-ai-user": "my_team_nickname",
            },
        },
    }
    # Authenticated caller on the proxy is jane.doe@example.com
    _model_id, modified_body, extra_headers = validate_model_and_enforce_policy(
        path,
        body,
        "jane.doe@example.com",
    )
    # 1. Client user label is preserved (sanitized) at root labels
    expect_equal(modified_body["labels"]["vscode-vertex-ai-user"], "my_team_nickname")
    # 2. Client project label is preserved (sanitized) at root labels
    expect_equal(modified_body["labels"]["vscode-vertex-ai-project"], "ecommerce-frontend")
    # 3. Third label (proxy-auth-user) strictly contains the authenticated caller's email
    expect_equal(modified_body["labels"]["proxy-auth-user"], "jane_doe_example_com")

    # In Anthropic header, all 3 labels must be present
    decoded_claude = json.loads(
        base64.b64decode(extra_headers["X-Vertex-AI-Labels"]).decode("utf-8"),
    )
    expect_equal(decoded_claude["vscode-vertex-ai-user"], "my_team_nickname")
    expect_equal(decoded_claude["vscode-vertex-ai-project"], "ecommerce-frontend")
    expect_equal(decoded_claude["proxy-auth-user"], "jane_doe_example_com")


def test_validate_allowed_model() -> None:
    """Verify validate allowed model."""
    path = GEMINI_PATH
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
        "config": {
            "labels": {
                "vscode-vertex-ai-project": "crm-backend",
                "vscode-vertex-ai-user": "frontend-dev",
            },
        },
    }
    model_id, modified_body, extra_headers = validate_model_and_enforce_policy(
        path,
        body,
        "jane.doe@example.com",
    )
    expect_equal(model_id, "gemini-3.8-flash")
    expect_equal(modified_body["labels"]["vscode-vertex-ai-user"], "frontend-dev")
    expect_equal(modified_body["labels"]["vscode-vertex-ai-project"], "crm-backend")
    expect_equal(modified_body["labels"]["proxy-auth-user"], "jane_doe_example_com")
    expect_in("X-Vertex-AI-Labels", extra_headers)

    # Also test high-thinking variant
    path_high = _path("gemini-3.8-flash-high")
    model_high, _, _ = validate_model_and_enforce_policy(
        path_high,
        body,
        "jane.doe@example.com",
    )
    expect_equal(model_high, "gemini-3.8-flash-high")

    # Also test Claude Sonnet 5.5 and Opus 5.5 with header-based labels
    headers = {
        "vscode-vertex-ai-project": "mobile-app",
        "vscode-vertex-ai-user": "mobile-coder",
    }
    path_sonnet = _path(
        "claude-sonnet-5-5-high",
        "streamRawPredict",
        publisher="anthropic",
    )
    model_sonnet, _, headers_sonnet = validate_model_and_enforce_policy(
        path_sonnet,
        {},
        "jane.doe@example.com",
        headers=headers,
    )
    expect_equal(model_sonnet, "claude-sonnet-5-5-high")
    decoded_sonnet = json.loads(
        base64.b64decode(headers_sonnet["X-Vertex-AI-Labels"]).decode("utf-8"),
    )
    expect_equal(decoded_sonnet["vscode-vertex-ai-project"], "mobile-app")
    expect_equal(decoded_sonnet["vscode-vertex-ai-user"], "mobile-coder")
    expect_equal(decoded_sonnet["proxy-auth-user"], "jane_doe_example_com")

    # Anthropic's SDK sends the shared upstream version, not the catalog's
    # policy-profile ID (for example, the selected "-medium" alias).
    path_sonnet_upstream = _path(
        "claude-sonnet-5-5",
        "streamRawPredict",
        publisher="anthropic",
    )
    claude_body = {"messages": [{"role": "user", "content": "Hello"}]}
    model_sonnet_upstream, modified_sonnet_body, _ = validate_model_and_enforce_policy(
        path_sonnet_upstream,
        claude_body,
        "jane.doe@example.com",
        headers=headers,
    )
    expect_equal(model_sonnet_upstream, "claude-sonnet-5-5")
    expect_equal(modified_sonnet_body, {"messages": [{"role": "user", "content": "Hello"}]})
    expect_not_in("labels", modified_sonnet_body)

    path_opus = _path("claude-opus-5-5", "streamRawPredict", publisher="anthropic")
    model_opus, _, _ = validate_model_and_enforce_policy(
        path_opus,
        {},
        "jane.doe@example.com",
        headers=headers,
    )
    expect_equal(model_opus, "claude-opus-5-5")


def test_validate_unauthorized_region() -> None:
    """Verify validate unauthorized region."""
    body = {
        "config": {
            "labels": {
                "vscode-vertex-ai-project": "test-proj",
                "vscode-vertex-ai-user": "test-user",
            },
        },
    }
    with pytest.raises(GovernanceError) as excinfo:
        validate_model_and_enforce_policy(
            _path("gemini-3.8-flash", region="us-central1"),
            body,
            "user@example.com",
        )
    expect_equal(excinfo.value.status_code, 400)
    expect_in("us-central1", excinfo.value.message)

    with pytest.raises(GovernanceError):
        validate_model_and_enforce_policy(
            "/v1/models/gemini-3.8-flash:generateContent",
            body,
            "user@example.com",
        )


def test_region_allowlist_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify region allowlist is configurable."""
    monkeypatch.setenv("ALLOWED_REGIONS", "global,europe-west1")
    body = {
        "config": {
            "labels": {"vscode-vertex-ai-project": "p", "vscode-vertex-ai-user": "u"},
        },
    }
    model_id, _, _ = validate_model_and_enforce_policy(
        _path("gemini-3.8-flash", region="europe-west1"),
        body,
        "user@example.com",
    )
    expect_equal(model_id, "gemini-3.8-flash")


def _project_path(project: str) -> str:
    return f"/v1/projects/{project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"


def test_project_outside_allowlist_is_rejected() -> None:
    """Verify a Vertex project that is not allowlisted is rejected."""
    body = {"labels": {"vscode-vertex-ai-project": "p", "vscode-vertex-ai-user": "u"}}
    with pytest.raises(GovernanceError) as excinfo:
        validate_model_and_enforce_policy(_project_path("other-project"), body, "user@example.com")
    expect_equal(excinfo.value.status_code, 400)
    expect_in("other-project", excinfo.value.message)
    expect_in("Approved projects", excinfo.value.message)


def test_vertex_projects_default_to_hub_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the hub project is the only allowed project when the setting is unset."""
    monkeypatch.delenv("ALLOWED_VERTEX_PROJECTS")
    expect_equal(settings.allowed_vertex_projects, ["test-hub-project"])
    body = {"labels": {"vscode-vertex-ai-project": "p", "vscode-vertex-ai-user": "u"}}
    validate_model_and_enforce_policy(_project_path("test-hub-project"), body, "user@example.com")
    with pytest.raises(GovernanceError):
        validate_model_and_enforce_policy(_project_path("user-project"), body, "user@example.com")


def test_vertex_projects_accept_comma_list_and_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify both list formats are parsed."""
    monkeypatch.setenv("ALLOWED_VERTEX_PROJECTS", "proj-a, proj-b")
    expect_equal(settings.allowed_vertex_projects, ["proj-a", "proj-b"])
    monkeypatch.setenv("ALLOWED_VERTEX_PROJECTS", '["proj-a", "proj-b"]')
    expect_equal(settings.allowed_vertex_projects, ["proj-a", "proj-b"])


def test_validate_unauthorized_model() -> None:
    """Verify validate unauthorized model."""
    path = _path("unauthorized-gpt-custom")
    body = {
        "config": {
            "labels": {
                "vscode-vertex-ai-project": "test-proj",
                "vscode-vertex-ai-user": "test-user",
            },
        },
    }
    with pytest.raises(GovernanceError) as excinfo:
        validate_model_and_enforce_policy(path, body, "user@example.com")
    expect_equal(excinfo.value.status_code, 400)
    expect_in("allowlist", excinfo.value.message)


def test_extract_bearer_token() -> None:
    """Verify extract bearer token."""
    expect_equal(extract_bearer_token("Bearer my-secret-token"), "my-secret-token")

    with pytest.raises(AuthError):
        extract_bearer_token(None)

    with pytest.raises(AuthError):
        extract_bearer_token("Basic dXNlcjpwYXNz")


def test_dynamic_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify dynamic env var override."""
    monkeypatch.setenv("ALLOWED_MODELS", "brand-new-custom-model,gemini-3.7-flash")

    expect_in("brand-new-custom-model", settings.allowed_models)

    path = _path("brand-new-custom-model")
    body = {
        "contents": [],
        "config": {
            "labels": {
                "vscode-vertex-ai-project": "custom-corp-project",
                "vscode-vertex-ai-user": "custom-user-tag",
            },
        },
    }
    model_id, modified_body, _extra_headers = validate_model_and_enforce_policy(
        path,
        body,
        "john.smith@example.com",
    )
    expect_equal(model_id, "brand-new-custom-model")
    expect_equal(modified_body["labels"]["vscode-vertex-ai-project"], "custom-corp-project")
    expect_equal(modified_body["labels"]["vscode-vertex-ai-user"], "custom-user-tag")
    expect_equal(modified_body["labels"]["proxy-auth-user"], "john_smith_example_com")


def test_billing_label_keys_remap_chat_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify labels are read under the chat keys and written under the billing keys only."""
    monkeypatch.setenv("BILLING_USER_LABEL_KEY", "custom-developer-id")
    monkeypatch.setenv("BILLING_PROJECT_LABEL_KEY", "custom-repo-name")
    monkeypatch.setenv("BILLING_AUTH_USER_LABEL_KEY", "custom-auth-caller")

    body = {
        "contents": [],
        "config": {
            "labels": {
                "vscode-vertex-ai-project": "ecommerce-frontend",
                "vscode-vertex-ai-user": "Custom Alias!",
            },
        },
    }
    _model_id, modified_body, extra_headers = validate_model_and_enforce_policy(
        GEMINI_PATH,
        body,
        "jane.doe@example.com",
    )
    expected = {
        "custom-repo-name": "ecommerce-frontend",
        "custom-developer-id": "custom_alias_",
        "custom-auth-caller": "jane_doe_example_com",
    }
    for key, value in expected.items():
        expect_equal(modified_body["labels"][key], value)
    expect_not_in("vscode-vertex-ai-user", modified_body["labels"])
    expect_not_in("vscode-vertex-ai-project", modified_body["labels"])

    decoded = json.loads(
        base64.b64decode(extra_headers["X-Vertex-AI-Labels"]).decode("utf-8"),
    )
    for key, value in expected.items():
        expect_equal(decoded[key], value)
    expect_not_in("vscode-vertex-ai-user", decoded)


def test_missing_label_error_names_the_chat_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the rejection tells clients which label to send, not the billing name."""
    monkeypatch.setenv("BILLING_PROJECT_LABEL_KEY", "custom-repo-name")
    with pytest.raises(GovernanceError) as excinfo:
        validate_model_and_enforce_policy(
            GEMINI_PATH,
            {"labels": {"vscode-vertex-ai-user": "u"}},
            "jane.doe@example.com",
        )
    expect_in("vscode-vertex-ai-project", excinfo.value.message)
    expect_not_in("custom-repo-name", excinfo.value.message)


@pytest.mark.parametrize(
    ("env_name", "attribute"),
    [
        ("ALLOWED_MODELS", "allowed_models"),
        ("ALLOWED_REGIONS", "allowed_regions"),
        ("CHAT_USER_LABEL_KEY", "chat_user_label_key"),
        ("CHAT_PROJECT_LABEL_KEY", "chat_project_label_key"),
        ("BILLING_USER_LABEL_KEY", "billing_user_label_key"),
        ("BILLING_PROJECT_LABEL_KEY", "billing_project_label_key"),
        ("BILLING_AUTH_USER_LABEL_KEY", "billing_auth_user_label_key"),
        ("MODELS_CATALOG_URL", "models_catalog_url"),
        ("REQUIRE_PROJECT_LABEL", "require_project_label"),
        ("REQUIRE_USER_LABEL", "require_user_label"),
        ("MAX_CONTEXT_TOKENS", "max_context_tokens"),
        ("DISCOVERY_MAX_INPUT_TOKENS", "discovery_max_input_tokens"),
    ],
)
def test_missing_required_env_var_raises_error(monkeypatch: pytest.MonkeyPatch, env_name: str, attribute: str) -> None:
    """Verify missing required env var raises error."""
    monkeypatch.delenv(env_name, raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        getattr(settings, attribute)
    expect_in("Missing required configuration", str(excinfo.value))
    expect_in(env_name, str(excinfo.value))


def test_optional_project_label_when_configured_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify optional project label when configured false."""
    monkeypatch.setenv("REQUIRE_PROJECT_LABEL", "false")
    monkeypatch.setenv("REQUIRE_USER_LABEL", "true")

    path = "/v1/projects/p/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
    # User label present, project label omitted
    body = {"contents": [], "labels": {"vscode-vertex-ai-user": "test-dev"}}
    # Should not raise exception
    _model_id, modified_body, _ = validate_model_and_enforce_policy(
        path,
        body,
        "user@example.com",
    )
    expect_equal(modified_body["labels"]["vscode-vertex-ai-project"], "default")
    expect_equal(modified_body["labels"]["vscode-vertex-ai-user"], "test-dev")


def test_optional_user_label_when_configured_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify optional user label when configured false."""
    monkeypatch.setenv("REQUIRE_PROJECT_LABEL", "true")
    monkeypatch.setenv("REQUIRE_USER_LABEL", "false")

    path = "/v1/projects/p/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
    # Project label present, user label omitted
    body = {"contents": [], "labels": {"vscode-vertex-ai-project": "my-project"}}
    # Should not raise exception
    _model_id, modified_body, _ = validate_model_and_enforce_policy(
        path,
        body,
        "user@example.com",
    )
    expect_equal(modified_body["labels"]["vscode-vertex-ai-project"], "my-project")
    expect_equal(modified_body["labels"]["vscode-vertex-ai-user"], "default")


# ---------------------------------------------------------------------------
# Flask WSGI application tests
# ---------------------------------------------------------------------------


@pytest.fixture
def flask_client() -> FlaskClient:
    """Return a test client for the production Flask application."""
    return app.test_client()


@pytest.fixture
def discovery_catalog(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Discovery tests use a fixed catalog instead of the remote GitHub URL."""
    catalog = {
        "regionPriority": ["global", "europe-west1"],
        "candidateModels": [
            {
                "id": "gemini-3.8-flash",
                "vendor": "google",
                "displayName": "Gemini",
                "family": "gemini",
                "version": "3.8",
                "maxInputTokens": 1000,
                "maxOutputTokens": 100,
                "capabilities": {"imageInput": True, "toolCalling": True},
                "pricing": {"input": 1, "output": 4},
            },
        ],
    }
    monkeypatch.setattr(discovery, "load_master_catalog", lambda: catalog)
    return catalog


class TestFlaskHttp:
    """Tests the Flask routes used by the container entrypoint."""

    def test_http_healthcheck(self, flask_client: FlaskClient) -> None:
        """Verify http healthcheck."""
        resp = flask_client.get("/health")
        expect_equal(resp.status_code, 200)
        data = resp.get_json()
        expect_equal(data["status"], "healthy")
        expect_equal(data["service"], "copilot-chat-proxy")
        expect_equal(data["allowed_regions"], ["global"])
        expect_not_in("vertex_project", data)

    def test_http_no_cors_headers(self, flask_client: FlaskClient) -> None:
        """Verify responses carry no CORS headers."""
        resp = flask_client.get("/health")
        expect_equal(resp.status_code, 200)
        expect_not_in("Access-Control-Allow-Origin", resp.headers)

    def test_http_missing_auth_header(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify http missing auth header."""
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "false")
        sdk_path = "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        resp = flask_client.post(
            sdk_path,
            json={
                "contents": [],
                "labels": {
                    "vscode-vertex-ai-project": "p",
                    "vscode-vertex-ai-user": "u",
                },
            },
        )
        expect_equal(resp.status_code, 401)
        err = resp.get_json()["error"]
        expect_equal(err["code"], 401)
        expect_in("Missing Authorization header", err["message"])

    def test_http_missing_project_label(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify http missing project label."""
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
        sdk_path = "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        resp = flask_client.post(
            sdk_path,
            json={"contents": [], "labels": {"vscode-vertex-ai-user": "my-user"}},
        )
        expect_equal(resp.status_code, 400)
        err = resp.get_json()["error"]
        expect_equal(err["code"], 400)
        expect_in("vscode-vertex-ai-project", err["message"])

    def test_http_missing_user_label(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify http missing user label."""
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
        sdk_path = "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        resp = flask_client.post(
            sdk_path,
            json={"contents": [], "labels": {"vscode-vertex-ai-project": "my-project"}},
        )
        expect_equal(resp.status_code, 400)
        err = resp.get_json()["error"]
        expect_equal(err["code"], 400)
        expect_in("vscode-vertex-ai-user", err["message"])

    def test_http_unauthorized_model(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify http unauthorized model."""
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
        sdk_path = "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.7-flash:generateContent"
        resp = flask_client.post(
            sdk_path,
            json={
                "contents": [],
                "labels": {
                    "vscode-vertex-ai-project": "p",
                    "vscode-vertex-ai-user": "u",
                },
            },
        )
        expect_equal(resp.status_code, 400)
        err = resp.get_json()["error"]
        expect_in("allowlist", err["message"].lower())

    def test_http_successful_request_with_mock_upstream(
        self,
        flask_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify http successful request with mock upstream."""
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")

        # Mock forward_to_vertex to return a mock generator
        def mock_forward(*_args: object, **_kwargs: object) -> UpstreamResult:
            def gen() -> Iterator[bytes]:
                yield b'{"candidates": [{"content": {"parts": [{"text": "MOCK_OK"}]}}]}'

            return 200, {"Content-Type": "application/json"}, gen()

        monkeypatch.setattr(inference_handler, "forward_to_vertex", mock_forward)

        sdk_path = "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        resp = flask_client.post(
            sdk_path,
            json={
                "contents": [{"role": "user", "parts": [{"text": "test"}]}],
                "labels": {
                    "vscode-vertex-ai-project": "p",
                    "vscode-vertex-ai-user": "u",
                },
            },
        )
        expect_equal(resp.status_code, 200)
        expect_in(b"MOCK_OK", resp.data)

        captured = capsys.readouterr()
        expect_in("User successfully authenticated: local-developer@example.com", captured.out)

    def test_complete_usage_event_is_logged_after_stream_completion(
        self,
        flask_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify complete usage event is logged after stream completion."""
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
        usage_payload = json.dumps(
            {
                "responseId": "response-123",
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "cachedContentTokenCount": 25,
                    "candidatesTokenCount": 20,
                    "totalTokenCount": 120,
                },
            },
        )

        def mock_forward(*_args: object, **_kwargs: object) -> UpstreamResult:
            def gen() -> Iterator[bytes]:
                encoded = f"data: {usage_payload}\n\n".encode()
                yield encoded[:11]
                yield encoded[11:]

            return 200, {"Content-Type": "text/event-stream"}, gen()

        monkeypatch.setattr(inference_handler, "forward_to_vertex", mock_forward)
        monkeypatch.setattr(
            inference_handler,
            "get_model_spec",
            lambda _: {
                "id": "gemini-3.8-flash",
                "vendor": "google",
                "family": "gemini",
                "version": "3.8",
                "maxInputTokens": 1000000,
                "maxOutputTokens": 64000,
                "pricing": {
                    "input": 1.0,
                    "output": 4.0,
                    "cache_read": 0.1,
                    "cache_create": 0.0,
                },
            },
        )

        sdk_path = "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.8-flash:streamGenerateContent"
        resp = flask_client.post(
            sdk_path,
            json={
                "contents": [{"parts": [{"text": "test"}]}],
                "labels": {
                    "vscode-vertex-ai-project": "project-a",
                    "vscode-vertex-ai-user": "developer-a",
                },
            },
        )

        expect_equal(resp.status_code, 200)
        expect(resp.data)
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        summaries = [entry for entry in logs if entry.get("event_kind") == "inference_completed"]
        expect_equal(len(summaries), 1)
        summary = summaries[0]
        event = summary
        expect(event["stream_completed"] is True)
        expect_equal(event["outcome"], "completed")
        expect_equal(event["principal_email"], "local-developer@example.com")
        expect_equal(event["workspace_label"], "project-a")
        expect_equal(event["client_user_label"], "developer-a")
        expect_equal(event["provider_response_id"], "response-123")
        expect_equal(event["input_tokens_total"], 100)
        expect_equal(event["input_tokens_uncached"], 75)
        expect_equal(event["cache_read_tokens"], 25)
        expect_equal(event["output_tokens"], 20)
        expect(event["usage_complete"] is True)
        expect_equal(event["estimated_total_cost_decimal"], "0.0001575")
        expect_equal(event["schema_version"], 4)
        expect_is_not(event["time_to_first_chunk_ms"], None)
        expect_is(event.get("time_to_first_token_ms"), None)  # Usage-only response.
        expect_equal(summary["event_id"], event["event_id"])
        expect_equal(summary["input_tokens_total"], event["input_tokens_total"])
        expect_equal(summary["estimated_total_cost"], float(event["estimated_total_cost"]))
        expect_equal(summary["provider"], "google")
        expect_not_in("provider_usage", summary)
        expect_not_in("model_metadata", summary)
        expect_equal(json.loads(event["model_metadata_json"])["pricing"]["input"], 1.0)
        expect_equal(json.loads(event["provider_usage_json"])["promptTokenCount"], 100)
        expect_equal(event["input_price_per_million"], "1.0")
        expect_not_in("contents", event)
        expect(not hasattr(inference_handler, "write_usage_event"))

    @pytest.mark.parametrize(
        "case",
        [
            FailureCase("auth", 401, "authentication_rejected"),
            FailureCase("governance", 400, "governance_rejected"),
            FailureCase("upstream", 502, "upstream_connection_error"),
        ],
        ids=lambda case: case.failure,
    )
    def test_early_failures_emit_one_terminal_summary(
        self,
        flask_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        case: FailureCase,
    ) -> None:
        """Verify early failures emit one terminal summary."""
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
        monkeypatch.setattr(inference_handler, "get_model_spec", lambda _: None)

        def fail(*_args: object, **_kwargs: object) -> Never:
            if case.failure == "auth":
                raise AuthError(401, "rejected")
            if case.failure == "governance":
                raise GovernanceError(400, "rejected")
            raise httpx.ConnectError("unavailable")

        target = {
            "auth": "authenticate_caller",
            "governance": "validate_model_and_enforce_policy",
            "upstream": "forward_to_vertex",
        }[case.failure]
        monkeypatch.setattr(inference_handler, target, fail)
        resp = flask_client.post(
            "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent",
            json={
                "contents": [],
                "labels": {
                    "vscode-vertex-ai-project": "p",
                    "vscode-vertex-ai-user": "u",
                },
            },
        )
        expect_equal(resp.status_code, case.expected_status)
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        summaries = [entry for entry in logs if entry.get("event_kind") == "inference_completed"]
        expect_equal(len(summaries), 1)
        expect_equal(summaries[0]["outcome"], case.expected_outcome)
        expect_not_in("total_tokens", summaries[0])
        expect_not_in("estimated_total_cost", summaries[0])
        event = summaries[0]
        expect_equal(event["schema_version"], 4)
        expect(event["request_started_at"])
        expect(event["response_completed_at"])
        if case.failure != "upstream":
            expect_equal(summaries[0]["model_upstream"], "unknown")

    @pytest.mark.parametrize(
        ("mode", "outcome"),
        [
            ("error", "stream_error"),
            ("disconnect", "client_disconnected"),
            ("missing", "usage_missing"),
            ("http_error", "upstream_error"),
        ],
    )
    def test_stream_outcomes_are_logged_without_bigquery(
        self,
        flask_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        mode: str,
        outcome: str,
    ) -> None:
        """Verify stream outcomes are logged without bigquery."""
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
        monkeypatch.setattr(inference_handler, "get_model_spec", lambda _: None)

        def forward(*_args: object, **_kwargs: object) -> UpstreamResult:
            def stream() -> Iterator[bytes]:
                yield b'data: {"candidates": [{"content": {"parts": [{"text": "content"}]}}]}\n\n'
                if mode == "error":
                    raise RuntimeError("broken stream")
                yield b": heartbeat\n\n"

            return (
                (503 if mode == "http_error" else 200),
                {"Content-Type": "text/event-stream"},
                stream(),
            )

        monkeypatch.setattr(inference_handler, "forward_to_vertex", forward)
        resp = flask_client.post(
            "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.8-flash:streamGenerateContent",
            json={
                "contents": [],
                "labels": {
                    "vscode-vertex-ai-project": "p",
                    "vscode-vertex-ai-user": "u",
                },
            },
            buffered=False,
        )
        if mode == "disconnect":
            resp.close()
        elif mode == "error":
            with pytest.raises(RuntimeError, match="broken stream"):
                resp.get_data()
        else:
            resp.get_data()
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
        summaries = [entry for entry in logs if entry.get("event_kind") == "inference_completed"]
        expect_equal(len(summaries), 1)
        summary = summaries[0]
        expect_equal(summary["outcome"], outcome)
        expect_not_in("usage_export_status", summary)
        expect_equal(summary["outcome"], outcome)
        expect_greater_equal(summary["time_to_first_token_ms"], summary["time_to_first_chunk_ms"])

    def test_http_inference_get_method_not_allowed(self, flask_client: FlaskClient) -> None:
        """Verify http inference get method not allowed."""
        sdk_path = "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        resp = flask_client.get(sdk_path)
        expect_equal(resp.status_code, 405)
        err = resp.get_json()["error"]
        expect_in("METHOD_NOT_ALLOWED", err["status"])

    def test_http_unknown_path_not_found(self, flask_client: FlaskClient) -> None:
        """Verify http unknown path not found."""
        resp = flask_client.get("/random-non-existent-path")
        expect_equal(resp.status_code, 404)
        err = resp.get_json()["error"]
        expect_in("NOT_FOUND", err["status"])

    def test_http_predict_custom_alias_not_found(self, flask_client: FlaskClient) -> None:
        """Verify http predict custom alias not found.

        Verify that custom alias /predict is eliminated and returns 404.
        """
        resp = flask_client.post("/predict")
        expect_equal(resp.status_code, 404)

    def test_http_legacy_models_path_not_found(self, flask_client: FlaskClient) -> None:
        """Verify http legacy models path not found.

        Verify that legacy alias /v1/models/... is completely disabled (404).
        """
        resp = flask_client.post("/v1/models/gemini-3.8-flash:generateContent")
        expect_equal(resp.status_code, 404)

    def test_inference_path_without_v1_prefix_not_found(self, flask_client: FlaskClient) -> None:
        """Reject the former route alias without the Vertex API version prefix."""
        response = flask_client.post(
            "/projects/test-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent",
        )
        expect_equal(response.status_code, 404)

    def test_http_sdk_full_path_successful(self, flask_client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify http sdk full path successful.

        Verify that the full path invoked by VS Code SDKs (/v1/projects/...) works.
        """
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")

        captured = {}

        def mock_forward(*args: object, **kwargs: object) -> UpstreamResult:
            captured["kwargs"] = kwargs
            captured["args"] = args

            def gen() -> Iterator[bytes]:
                yield b'{"result": "SDK_PATH_OK"}'

            return 200, {"Content-Type": "application/json"}, gen()

        monkeypatch.setattr(inference_handler, "forward_to_vertex", mock_forward)

        sdk_path = "/v1/projects/test-hub-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        resp = flask_client.post(
            sdk_path,
            json={
                "contents": [{"parts": [{"text": "Hello"}]}],
                "labels": {
                    "vscode-vertex-ai-project": "proj-a",
                    "vscode-vertex-ai-user": "dev-b",
                },
            },
        )
        expect_equal(resp.status_code, 200)
        expect_in(b"SDK_PATH_OK", resp.data)

    @pytest.mark.usefixtures("discovery_catalog")
    def test_http_discovery_endpoint_returns_valid_schema(
        self,
        flask_client: FlaskClient,
    ) -> None:
        """Verify http discovery endpoint returns valid schema."""
        resp = flask_client.get("/discovery")
        expect_equal(resp.status_code, 200)
        data = resp.get_json()
        expect_in("candidateModels", data)
        expect_in("regionPriority", data)
        # Region filter: only ["global"] is returned, other regions are filtered out
        expect_equal(data["regionPriority"], ["global"])
        expect(isinstance(data["candidateModels"], list))
        expect_greater(len(data["candidateModels"]), 0)

        # Validate that each model spec conforms to the required fields of models.schema.json
        required_fields = [
            "id",
            "vendor",
            "displayName",
            "family",
            "version",
            "maxInputTokens",
            "maxOutputTokens",
            "capabilities",
            "pricing",
        ]
        for model in data["candidateModels"]:
            for field in required_fields:
                expect_in(field, model, f"Missing required field '{field}' in model {model.get('id')}")
            expect_in(model["vendor"], ("google", "anthropic", "grok"))
            expect_in("imageInput", model["capabilities"])
            expect_in("toolCalling", model["capabilities"])
            expect_in("input", model["pricing"])
            expect_in("output", model["pricing"])
            expect_equal(model["maxInputTokens"], 250000)

    @pytest.mark.usefixtures("discovery_catalog")
    def test_discovery_sets_max_input_tokens_from_env(
        self,
        flask_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify discovery sets max input tokens from env."""
        monkeypatch.setenv("DISCOVERY_MAX_INPUT_TOKENS", "250000")
        resp = flask_client.get("/discovery")
        expect_equal(resp.status_code, 200)
        data = resp.get_json()
        for model in data["candidateModels"]:
            expect_equal(model["maxInputTokens"], 250000)

    @pytest.mark.usefixtures("discovery_catalog")
    def test_discovery_preserves_catalog_max_input_tokens_when_disabled(
        self,
        flask_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify discovery preserves catalog max input tokens when disabled."""
        monkeypatch.setenv("DISCOVERY_MAX_INPUT_TOKENS", "0")
        resp = flask_client.get("/discovery")
        expect_equal(resp.status_code, 200)
        data = resp.get_json()
        for model in data["candidateModels"]:
            expect_equal(model["maxInputTokens"], 1000)

    def test_http_discovery_post_method_not_allowed(self, flask_client: FlaskClient) -> None:
        """Verify http discovery post method not allowed."""
        resp = flask_client.post("/discovery")
        expect_equal(resp.status_code, 405)
        err = resp.get_json()["error"]
        expect_in("METHOD_NOT_ALLOWED", err["status"])

    def test_http_models_json_legacy_alias_not_found(self, flask_client: FlaskClient) -> None:
        """Verify http models json legacy alias not found.

        Verify /models.json is not exposed (only /discovery).
        """
        resp = flask_client.get("/models.json")
        expect_equal(resp.status_code, 404)

    def test_http_openapi_json_endpoint(self, flask_client: FlaskClient) -> None:
        """Verify http openapi json endpoint."""
        resp = flask_client.get("/openapi.json")
        expect_equal(resp.status_code, 200)
        data = resp.get_json()
        expect_equal(data.get("openapi"), "3.0.3")
        expect_equal(
            data["servers"],
            [
                {
                    "url": "https://proxy.example.test",
                    "description": "Cloud Run proxy service",
                },
            ],
        )
        expect_in("paths", data)
        expect_in("/discovery", data["paths"])
        expect_in("/health", data["paths"])
        expect_in("/openapi.json", data["paths"])
        expect_in("/v1/projects/{project}/locations/{region}/publishers/{publisher}/models/{model}:{action}", data["paths"])
        expect_in("components", data)
        expect_in("GoogleBearerAuth", data["components"]["securitySchemes"])

    def test_http_openapi_post_method_not_allowed(self, flask_client: FlaskClient) -> None:
        """Verify http openapi post method not allowed."""
        resp = flask_client.post("/openapi.json")
        expect_equal(resp.status_code, 405)
        err = resp.get_json()["error"]
        expect_in("METHOD_NOT_ALLOWED", err["status"])

    @pytest.mark.usefixtures("discovery_catalog")
    def test_unlisted_model_omitted_without_heuristics(
        self,
        flask_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify unlisted model omitted without heuristics.

        Verify that only verified models in catalog are returned, and unlisted ones are NOT guessed/fabricated.
        """
        monkeypatch.setenv(
            "ALLOWED_MODELS",
            "gemini-3.8-flash,unknown-future-model-xyz",
        )
        resp = flask_client.get("/discovery")
        expect_equal(resp.status_code, 200)
        data = resp.get_json()
        models_by_id = {m["id"]: m for m in data["candidateModels"]}

        # Allowed model present in catalog is returned
        expect_in("gemini-3.8-flash", models_by_id)
        # Unlisted model without catalog entry is NOT synthesized with fake values
        expect_not_in("unknown-future-model-xyz", models_by_id)

    def test_context_window_limit_exceeded_blocks_request(
        self,
        flask_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify context window limit exceeded blocks request.

        Configure a low threshold to trigger limit.
        """
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
        monkeypatch.setenv("MAX_CONTEXT_TOKENS", "100")
        monkeypatch.setattr(
            "inference.handler.count_tokens_vertex",
            lambda _path, _body: 150,
        )

        path = GEMINI_PATH
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "Very long conversation exceeding limit"}],
                },
            ],
            "labels": {
                "vscode-vertex-ai-project": "my-project",
                "vscode-vertex-ai-user": "dev-user",
            },
        }
        resp = flask_client.post(
            path,
            json=body,
            headers={"Authorization": "Bearer mock-valid-token"},
        )
        expect_equal(resp.status_code, 400)
        data = resp.get_json()
        expect_in("error", data)
        expect_equal(data["error"]["status"], "FAILED_PRECONDITION")
        expect_in("Context limit exceeded", data["error"]["message"])
        expect_in("cloud costs", data["error"]["message"])
        expect_in("'+'", data["error"]["message"])
        expect_equal(data["error"]["context_tokens"], 150)
        expect_equal(data["error"]["max_context_tokens"], 100)


def test_build_vertex_url_sdk_routes() -> None:
    """Verify build vertex url sdk routes.

    1. Client project and location are both forwarded.
    """
    sdk_path = "/v1/projects/user-project/locations/global/publishers/google/models/gemini-3.8-flash:streamGenerateContent"
    url_sdk = build_vertex_url(sdk_path)
    expect_equal(url_sdk, "https://aiplatform.googleapis.com/v1/projects/user-project/locations/global/publishers/google/models/gemini-3.8-flash:streamGenerateContent")

    # 2. Suffix -high thinking alias is stripped for Vertex AI upstream endpoint
    sdk_high = "/v1/projects/user-project/locations/global/publishers/google/models/gemini-3.8-flash-high:generateContent"
    url_high = build_vertex_url(sdk_high)
    expect_equal(url_high, "https://aiplatform.googleapis.com/v1/projects/user-project/locations/global/publishers/google/models/gemini-3.8-flash:generateContent")

    # 3. Regional endpoint
    sdk_regional = "/v1/projects/user-project/locations/europe-west1/publishers/anthropic/models/claude-sonnet-5.5:rawPredict"
    url_regional = build_vertex_url(sdk_regional)
    expect_equal(url_regional, "https://europe-west1-aiplatform.googleapis.com/v1/projects/user-project/locations/europe-west1/publishers/anthropic/models/claude-sonnet-5.5:rawPredict")


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        ("gemini-3-flash-preview", ["gemini-3-flash-preview"]),
        (
            "gemini-3-flash-preview,gemini-3-flash-preview-high",
            ["gemini-3-flash-preview", "gemini-3-flash-preview-high"],
        ),
        ("gemini-3-flash", []),
        ("gemini-3.flash-preview", ["gemini-3.flash-preview"]),
    ],
)
def test_discovery_requires_explicit_variants(monkeypatch: pytest.MonkeyPatch, allowed: str, expected: list[str]) -> None:
    """Verify discovery requires explicit variants."""
    catalog = {
        "candidateModels": [
            {"id": "gemini-3-flash-preview", "version": "gemini-3-flash-preview"},
            {
                "id": "gemini-3-flash-preview-high",
                "version": "gemini-3-flash-preview-high",
            },
        ],
    }
    monkeypatch.setattr(discovery, "load_master_catalog", lambda: catalog)
    monkeypatch.setenv("ALLOWED_MODELS", allowed)
    models = discovery.get_discovery_catalog()["candidateModels"]
    expect_equal([model["id"] for model in models], expected)
    expect_equal(catalog["candidateModels"][0]["id"], "gemini-3-flash-preview")


@pytest.mark.parametrize(
    ("model", "allowed", "expected"),
    [
        ("gemini-3-flash-preview-high", ["gemini-3-flash-preview"], False),
        ("gemini-3-flash-preview-next", ["gemini-3-flash-preview"], False),
        ("gemini-3-flash-preview", ["gemini-3-flash-preview"], True),
        ("gemini-3-flash-preview-high", ["gemini-3-flash-preview-high"], True),
        ("claude-sonnet-5-5", ["claude-sonnet-5-5-medium"], True),
        ("claude-sonnet-5-5-high", ["claude-sonnet-5-5-medium"], False),
        ("claude-sonnet-5-6", ["claude-sonnet-5-5-medium"], False),
        ("gemini-3.8-flash", ["gemini-3-8-flash"], True),
    ],
)
def test_model_allowlist_requires_explicit_variants(model: str, allowed: list[str], expected: bool) -> None:
    """Verify model allowlist requires explicit variants."""
    expect_is(_is_model_allowed(model, allowed), expected)


# ---------------------------------------------------------------------------
# Upstream error logging
# ---------------------------------------------------------------------------


def test_upstream_error_message_includes_excerpt(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify upstream error message includes excerpt."""
    capture = ErrorResponseCapture()
    capture.feed(
        json.dumps(
            [
                {
                    "error": {
                        "code": 403,
                        "status": "PERMISSION_DENIED",
                        "message": "Permission 'aiplatform.endpoints.predict' denied",
                    },
                },
            ],
        ).encode(),
    )
    capture.log(403)
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    expect_equal(entry["message"], "Upstream returned HTTP 403: PERMISSION_DENIED Permission 'aiplatform.endpoints.predict' denied")
    expect_equal(entry["error_response"][0]["error"]["status"], "PERMISSION_DENIED")


def test_upstream_non_json_error_is_logged_as_text(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify upstream non json error is logged as text."""
    capture = ErrorResponseCapture()
    capture.feed(b"<html>Bad gateway</html>")
    capture.log(502)
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    expect_equal(entry["message"], "Upstream returned HTTP 502: <html>Bad gateway</html>")
    expect_equal(entry["error_response_text"], "<html>Bad gateway</html>")
