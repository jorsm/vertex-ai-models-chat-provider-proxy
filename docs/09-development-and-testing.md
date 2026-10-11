# 9. Development and testing <!-- omit in toc -->

- [Setup](#setup)
- [Run the proxy locally](#run-the-proxy-locally)
- [Quality gates](#quality-gates)
- [Test suite](#test-suite)
- [Live integration tests](#live-integration-tests)
- [Contributing guidelines](#contributing-guidelines)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r api/requirements.txt -r api/tests/requirements.txt
```

Python 3.14 is the target runtime (`ruff.toml` sets `target-version = "py314"`).

## Run the proxy locally

All required environment variables must be set (see [Configuration](06-configuration.md)); `LOCAL_DEV_BYPASS=true` removes the need for a real Google token, and `TRACE_ENABLED=false` avoids Cloud Trace calls.

```bash
export HUB_PROJECT_ID=my-ai-companion-hub ALLOWED_REGIONS=global \
       ALLOWED_MODELS=gemini-3.8-flash USER_LABEL_KEY=vscode-vertex-ai-user \
       PROJECT_LABEL_KEY=vscode-vertex-ai-project AUTH_USER_LABEL_KEY=proxy-auth-user \
       REQUIRE_USER_LABEL=true REQUIRE_PROJECT_LABEL=true \
       MODELS_CATALOG_URL=https://raw.githubusercontent.com/jorsm/vertex-ai-models-chat-provider/main/src/models.json \
       PROXY_BASE_URL=http://localhost:8080 MAX_CONTEXT_TOKENS=500000 \
       DISCOVERY_MAX_INPUT_TOKENS=250000 TRACE_ENABLED=false LOCAL_DEV_BYPASS=true

gunicorn --chdir api/src --bind 127.0.0.1:8080 --workers 1 --worker-class gthread --threads 8 --timeout 0 main:app
```

In bypass mode the caller is `local-developer@example.com`. Forwarding to Vertex AI still needs Application Default Credentials with Vertex AI access (`gcloud auth application-default login`).

## Quality gates

```bash
cd api
ruff check .            # lint: all rules enabled, see ruff.toml
ruff format --check .   # formatting
pytest tests            # unit tests; live tests are skipped without configuration
```

`ruff.toml` selects `ALL` rules with a short, documented ignore list. The code is expected to pass without per-line suppressions.

## Test suite

| File | Scope |
| --- | --- |
| `test_unit_proxy.py` | Governance, label sanitization, settings, discovery, URL building and the Flask routes with a mocked upstream |
| `test_usage_tracking.py` | Streaming usage parsing for Gemini and Claude, cost estimation |
| `test_observability.py` | Schema of the emitted usage event |
| `test_tracing.py` | Span lifetimes across streams, absence of sensitive data, trace correlation, exporter failure handling |
| `test_vertex_transport.py` | Real local HTTP server: connection reuse, concurrent isolation, cookie handling, credential refresh |
| `test_integration_proxy.py` | Live end-to-end tests against a deployed proxy |

Assertions use the helpers in `tests/checks.py` (`expect`, `expect_equal`, `expect_in`, ...), which raise `AssertionError` with a descriptive message.

## Live integration tests

`test_integration_proxy.py` calls a deployed proxy and **sends real requests to Vertex AI** (a few short prompts). It is skipped unless configured:

| Variable | Meaning |
| --- | --- |
| `PROXY_SERVICE_URI` | Base URL of the deployed proxy (required) |
| `HUB_PROJECT_ID` | Project used in Vertex AI request paths; must be one the proxy service account can use (required) |
| `PROXY_AUTH_TOKEN` or `GCP_ID_TOKEN` | Optional ID token. Without it the tests run `gcloud auth print-identity-token` |

```bash
gcloud auth login
export PROXY_SERVICE_URI=https://your-proxy.example.com HUB_PROJECT_ID=my-ai-companion-hub
pytest api/tests/test_integration_proxy.py -v
```

The identity must hold `roles/run.invoker`. The tests assume the sample governance configuration (for example `gemini-3.8-flash` allowed, `gemini-3.7-flash` not, region `global`); adapt them if your allowlist differs.

| Class | Verifies |
| --- | --- |
| `TestHealthAndAuth` | `/health`, `/openapi.json`, `/discovery`, disabled legacy paths, unknown paths (404), missing or forged token (401/403) |
| `TestLabelGovernanceConstraints` | Missing project label, missing user label, empty labels (400) |
| `TestModelGovernanceConstraints` | Model outside the allowlist (400) |
| `TestLiveInferenceAllowedModels` | Real Gemini inference (plain and high-effort), Claude-style labels header, SDK-shaped path |

## Contributing guidelines

- Keep new settings required and explicit; do not add silent defaults for governance behavior.
- Never log prompts, responses or credentials; add only metadata to events and spans.
- Add or update tests with each behavior change, and keep `ruff check` and `ruff format --check` clean.
