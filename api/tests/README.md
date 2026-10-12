# Tests

Unit tests use the committed catalog fixture and mocked inference/token-counting transports, so no model requests are billed. Catalog loader tests mock HTTP downloads explicitly. The fixture is the upstream extension catalog at `5fec9c5` (2026-10-12).

`test_effort_contract.py` covers every advertised native effort and default, plain discovery without capability headers, exact catalog/backend/publisher authorization, signed tool-history preservation, per-message Claude effort, malformed/unsupported controls, Gemini budget rejection, countTokens payloads, catalog validation/cache isolation, and effort usage logs.

Unit tests run offline. `test_integration_proxy.py` calls a deployed proxy and is skipped unless configured.

```bash
pip install -r api/requirements.txt -r api/tests/requirements.txt
cd api && pytest tests
```

To run the live integration tests, set the proxy URL and the proxy project, and authenticate with an identity that holds `roles/run.invoker`:

```bash
gcloud auth login
export PROXY_SERVICE_URI=https://your-proxy.example.com PROXY_PROJECT_ID=my-copilot-chat-proxy
pytest api/tests/test_integration_proxy.py -v
```

`PROXY_AUTH_TOKEN` (or `GCP_ID_TOKEN`) can supply an ID token instead of `gcloud auth print-identity-token`.

See [Development and testing](../../docs/09-development-and-testing.md) for the full test suite description, the live test cases and the quality gates.
