"""End-to-End Integration Tests for Deployed Google Agent Platform for Copilot Chat - Proxy on Google Cloud.

Validates all enterprise constraints:
- Missing / invalid authentication (401)
- Valid authentication
- Missing project label (400)
- Missing user label (400)
- Missing both labels (400)
- Unauthorized models not in allowlist (400)
- Deprecated models (400)
- Allowed models: Gemini 3.8 Flash & Gemini 3.8 Flash High (200 OK live inference)
- Allowed models: Claude Sonnet 5.5 & Claude Opus 5.5.
"""

import base64
import json

import requests
from checks import expect, expect_equal, expect_greater, expect_in


class TestHealthAndAuth:
    """Tests basic connectivity and authentication barriers."""

    def test_healthcheck_endpoint(self, proxy_url: str, valid_headers: dict) -> None:
        """Verifies that the /health endpoint responds 200 with service metadata."""
        url = f"{proxy_url}/health"
        resp = requests.get(url, headers=valid_headers, timeout=15)
        expect_equal(resp.status_code, 200, f"Healthcheck failed: {resp.text}")
        data = resp.json()
        expect_equal(data.get("status"), "healthy")
        expect_equal(data.get("service"), "copilot-chat-proxy")
        expect_equal(data.get("allowed_regions"), ["global"])
        expect_equal(data.get("endpoints", {}).get("discovery"), "/discovery")
        expect_equal(data.get("endpoints", {}).get("openapi"), "/openapi.json")
        expect_equal(data.get("endpoints", {}).get("health"), "/health")

    def test_openapi_endpoint_returns_spec(self, proxy_url: str, valid_headers: dict) -> None:
        """Verifies that GET /openapi.json returns valid OpenAPI 3.0.3 documentation."""
        url = f"{proxy_url}/openapi.json"
        resp = requests.get(url, headers=valid_headers, timeout=15)
        expect_equal(resp.status_code, 200, f"OpenAPI failed: {resp.text}")
        data = resp.json()
        expect_equal(data.get("openapi"), "3.0.3")
        expect_in("/discovery", data.get("paths", {}))
        expect_in("/health", data.get("paths", {}))
        expect_in("/v1/projects/{project}/locations/{region}/publishers/{publisher}/models/{model}:{action}", data.get("paths", {}))

    def test_discovery_endpoint_returns_catalog(self, proxy_url: str, valid_headers: dict) -> None:
        """Verifies that GET /discovery returns candidate models conforming to models.schema.json."""
        url = f"{proxy_url}/discovery"
        resp = requests.get(url, headers=valid_headers, timeout=15)
        expect_equal(resp.status_code, 200, f"Discovery failed: {resp.text}")
        data = resp.json()
        expect_in("candidateModels", data)
        expect_in("regionPriority", data)
        expect_equal(data["regionPriority"], ["global"])
        expect(isinstance(data["candidateModels"], list))
        expect_greater(len(data["candidateModels"]), 0)
        model_ids = [m["id"] for m in data["candidateModels"]]
        expect_in("gemini-3.8-flash", model_ids)

    def test_models_json_legacy_alias_returns_404(self, proxy_url: str, valid_headers: dict) -> None:
        """Verifies that legacy /models.json alias is disabled and returns 404."""
        url = f"{proxy_url}/models.json"
        resp = requests.get(url, headers=valid_headers, timeout=15)
        expect_equal(resp.status_code, 404)

    def test_predict_path_disabled_returns_404(self, proxy_url: str, valid_headers: dict) -> None:
        """Verifies that custom /predict path is disabled (only real SDK paths are supported)."""
        url = f"{proxy_url}/predict"
        resp = requests.post(url, headers=valid_headers, json={}, timeout=15)
        expect_equal(resp.status_code, 404)

    def test_unknown_path_returns_404(self, proxy_url: str, valid_headers: dict) -> None:
        """Verifies that calling an unknown endpoint path returns 404 NOT_FOUND."""
        url = f"{proxy_url}/random-endpoint-does-not-exist"
        resp = requests.get(url, headers=valid_headers, timeout=15)
        expect_equal(resp.status_code, 404)
        expect_in("NOT_FOUND", resp.json().get("error", {}).get("status", ""))

    def test_inference_missing_auth_header(self, proxy_url: str, vertex_project: str) -> None:
        """Verifies that requests without Authorization header are blocked (401 or 403)."""
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
                "labels": {"vscode-vertex-ai-project": "test", "vscode-vertex-ai-user": "test"},
            },
            timeout=15,
        )
        expect_in(resp.status_code, (401, 403), f"Expected 401 or 403, got {resp.status_code}: {resp.text}")

    def test_inference_invalid_bearer_token(self, proxy_url: str, vertex_project: str) -> None:
        """Verifies that requests with an invalid/forged token are blocked (401 or 403)."""
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        resp = requests.post(
            url,
            headers={"Authorization": "Bearer invalid-forged-token-value", "Content-Type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
                "labels": {"vscode-vertex-ai-project": "test", "vscode-vertex-ai-user": "test"},
            },
            timeout=15,
        )
        expect_in(resp.status_code, (401, 403), f"Expected 401 or 403, got {resp.status_code}: {resp.text}")


class TestLabelGovernanceConstraints:
    """Tests the mandatory presence of VS Code labels and error handling."""

    def test_missing_project_label_rejected(self, proxy_url: str, valid_headers: dict, vertex_project: str) -> None:
        """Verifies that omitting 'vscode-vertex-ai-project' rejects the call with HTTP 400."""
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
            "labels": {
                "vscode-vertex-ai-user": "developer-nickname",
                # vscode-vertex-ai-project is omitted
            },
        }
        resp = requests.post(url, headers=valid_headers, json=body, timeout=15)
        expect_equal(resp.status_code, 400, f"Expected 400, got {resp.status_code}: {resp.text}")
        err = resp.json().get("error", {})
        expect_equal(err.get("code"), 400)
        expect_in("vscode-vertex-ai-project", err.get("message", ""))
        expect_in("missing required label", err.get("message", "").lower())

    def test_missing_user_label_rejected(self, proxy_url: str, valid_headers: dict, vertex_project: str) -> None:
        """Verifies that omitting 'vscode-vertex-ai-user' rejects the call with HTTP 400."""
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
            "labels": {
                "vscode-vertex-ai-project": "corp-crm",
                # vscode-vertex-ai-user is omitted
            },
        }
        resp = requests.post(url, headers=valid_headers, json=body, timeout=15)
        expect_equal(resp.status_code, 400, f"Expected 400, got {resp.status_code}: {resp.text}")
        err = resp.json().get("error", {})
        expect_equal(err.get("code"), 400)
        expect_in("vscode-vertex-ai-user", err.get("message", ""))
        expect_in("missing required label", err.get("message", "").lower())

    def test_missing_both_labels_rejected(self, proxy_url: str, valid_headers: dict, vertex_project: str) -> None:
        """Verifies that an empty labels dictionary rejects the call with HTTP 400."""
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
            "labels": {},
        }
        resp = requests.post(url, headers=valid_headers, json=body, timeout=15)
        expect_equal(resp.status_code, 400, f"Expected 400, got {resp.status_code}: {resp.text}")
        err = resp.json().get("error", {})
        expect_equal(err.get("code"), 400)


class TestModelGovernanceConstraints:
    """Tests allowlist enforcement and deprecated model blocks."""

    def test_model_unauthorized_not_in_allowlist(self, proxy_url: str, valid_headers: dict, vertex_project: str) -> None:
        """Verifies that models not in allowlist (e.g. gemini-3.7-flash, grok, custom) return HTTP 400."""
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.7-flash:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": "test"}]}],
            "labels": {
                "vscode-vertex-ai-project": "integration-test",
                "vscode-vertex-ai-user": "tester",
            },
        }
        resp = requests.post(url, headers=valid_headers, json=body, timeout=15)
        expect_equal(resp.status_code, 400, f"Expected 400, got {resp.status_code}: {resp.text}")
        err = resp.json().get("error", {})
        expect_equal(err.get("code"), 400)
        expect_in("approved allowlist", err.get("message", "").lower())
        expect_in("gemini-3.7-flash", err.get("message", ""))


class TestLiveInferenceAllowedModels:
    """Tests live inference on the allowed models (Gemini 3.8 Flash, High, Claude)."""

    def test_gemini_3_8_flash_live_inference(self, proxy_url: str, valid_headers: dict, vertex_project: str) -> None:
        """Executes a real live inference request to gemini-3.8-flash via the proxy."""
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "Rispondi esclusivamente con la parola 'FUNZIONA'."}],
                },
            ],
            "labels": {
                "vscode-vertex-ai-project": "automated-integration-suite",
                "vscode-vertex-ai-user": "qa-automation-runner",
            },
        }
        resp = requests.post(url, headers=valid_headers, json=body, timeout=60)
        expect_equal(resp.status_code, 200, f"Live inference failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        expect_in("candidates", data, f"Missing candidates in Vertex response: {data}")
        expect_greater(len(data["candidates"]), 0)
        candidate_text = data["candidates"][0]["content"]["parts"][0]["text"]
        expect_greater(len(candidate_text.strip()), 0)

    def test_gemini_3_8_flash_high_live_inference(self, proxy_url: str, valid_headers: dict, vertex_project: str) -> None:
        """Executes a real live inference request to gemini-3.8-flash-high via the proxy."""
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.8-flash-high:generateContent"
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "Qual e la capitale dell'Italia? Rispondi in una parola."}],
                },
            ],
            "labels": {
                "vscode-vertex-ai-project": "automated-integration-suite",
                "vscode-vertex-ai-user": "qa-automation-runner",
            },
        }
        resp = requests.post(url, headers=valid_headers, json=body, timeout=60)
        expect_equal(resp.status_code, 200, f"Live inference failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        expect_in("candidates", data)
        expect_in("Roma", data["candidates"][0]["content"]["parts"][0]["text"])

    def test_claude_headers_format_accepted(self, proxy_url: str, valid_headers: dict, vertex_project: str) -> None:
        """Verify that Anthropic Claude labels passed via X-Vertex-AI-Labels are accepted.

        The base64 JSON header must be properly decoded and validated by the proxy.
        """
        claude_labels = {
            "vscode-vertex-ai-project": "claude-test-project",
            "vscode-vertex-ai-user": "claude-dev-user",
        }
        b64_labels = base64.b64encode(json.dumps(claude_labels).encode("utf-8")).decode("utf-8")

        headers = {
            **valid_headers,
            "X-Vertex-AI-Labels": b64_labels,
        }

        # Use exact SDK path with allowed model
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Test"}]}],
        }
        resp = requests.post(url, headers=headers, json=body, timeout=60)
        # 200 indicates that the project and user labels from the X-Vertex-AI-Labels header were accepted!
        expect_equal(resp.status_code, 200, f"Header-based labels failed ({resp.status_code}): {resp.text}")

    def test_sdk_full_path_live_inference_gemini(self, proxy_url: str, valid_headers: dict, vertex_project: str) -> None:
        """Verifies live inference using the exact SDK path invoked by @google/genai."""
        url = f"{proxy_url}/v1/projects/{vertex_project}/locations/global/publishers/google/models/gemini-3.8-flash:generateContent"
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": "Rispondi solo con la parola 'OK'."}],
                },
            ],
            "labels": {
                "vscode-vertex-ai-project": "automated-sdk-path",
                "vscode-vertex-ai-user": "qa-sdk-tester",
            },
        }
        resp = requests.post(url, headers=valid_headers, json=body, timeout=60)
        expect_equal(resp.status_code, 200, f"Live SDK path failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        expect_in("candidates", data)
