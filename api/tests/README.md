# Tests

Unit tests run offline. `test_integration_proxy.py` calls a deployed proxy and is skipped unless configured.

```bash
pip install -r api/requirements.txt -r api/tests/requirements.txt
cd api && pytest tests
```

To run the live integration tests, set the proxy URL and the hub project, and authenticate with an identity that holds `roles/run.invoker`:

```bash
gcloud auth login
export PROXY_SERVICE_URI=https://your-proxy.example.com HUB_PROJECT_ID=my-copilot-chat-proxy-hub
pytest api/tests/test_integration_proxy.py -v
```

`PROXY_AUTH_TOKEN` (or `GCP_ID_TOKEN`) can supply an ID token instead of `gcloud auth print-identity-token`.

See [Development and testing](../../docs/09-development-and-testing.md) for the full test suite description, the live test cases and the quality gates.
