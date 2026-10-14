"""Offline discovery, native effort authorization and transport regression tests."""

import base64
import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never

import httpx
import pytest
from checks import expect, expect_equal, expect_is, expect_not_in

from catalog import discovery
from config import settings
from inference import handler
from main import app
from security.governance import GovernanceError, enforce_request_policy
from vertex.client import build_vertex_url

if TYPE_CHECKING:
    from collections.abc import Callable

CATALOG = json.loads((Path(__file__).parent / "fixtures" / "catalog.json").read_text(encoding="utf-8"))
REAL_LOADER = discovery.load_master_catalog
LABELS = {"vscode-vertex-ai-project": "effort-test", "vscode-vertex-ai-user": "test-user"}
HEADERS = {"X-Vertex-AI-Labels": base64.b64encode(json.dumps(LABELS).encode()).decode()}
PROXY_PATH = "/v1/projects/client-project/locations/global/publishers/{publisher}/models/{version}:{action}"
EFFORT_MODELS = [model for model in CATALOG["candidateModels"] if model["vendor"] in ("google", "anthropic") and "effort" in model]
CASES = [(model, effort) for model in EFFORT_MODELS for effort in [*model["effort"]["values"], None]]


def route(model: dict[str, Any], version: str | None = None, publisher: str | None = None, action: str | None = None) -> str:
    """Build the literal Vertex SDK route for a catalog model."""
    return PROXY_PATH.format(publisher=publisher or model["vendor"], version=version or model["version"], action=action or ("streamRawPredict" if model["vendor"] == "anthropic" else "streamGenerateContent"))


def request_body(model: dict[str, Any], effort: str | None = None) -> dict[str, Any]:
    """Include opaque signatures and provider options that policy must preserve."""
    if model["vendor"] == "anthropic":
        body = {
            "messages": [
                {"role": "user", "content": "Original request"},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "private", "signature": "opaque-signature"}, {"type": "redacted_thinking", "data": "opaque-data"}, {"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {"query": "value"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}]},
            ],
            "thinking": {"type": "adaptive", "display": "omitted"},
            "tools": [{"name": "lookup", "description": "Lookup", "input_schema": {"type": "object"}}],
            "output_config": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
            "anthropic_version": "vertex-2023-10-16",
            "max_tokens": 4096,
        }
        if effort is not None:
            body["output_config"]["effort"] = effort
    else:
        body = {
            "contents": [{"role": "model", "parts": [{"functionCall": {"name": "lookup", "args": {}}, "thoughtSignature": "opaque-signature"}]}, {"role": "user", "parts": [{"functionResponse": {"name": "lookup", "response": {"result": "ok"}}}]}],
            "tools": [{"functionDeclarations": [{"name": "lookup", "parameters": {"type": "object"}}]}],
            "generationConfig": {"maxOutputTokens": 1000, "thinkingConfig": {"includeThoughts": True}},
        }
        if effort is not None:
            body["generationConfig"]["thinkingConfig"]["thinkingLevel"] = effort.upper()
    return body


@pytest.mark.parametrize(("model", "effort"), CASES, ids=[f"{model['id']}:{effort or 'default'}" for model, effort in CASES])
def test_every_advertised_effort_and_omission_default(model: dict[str, Any], effort: str | None) -> None:
    """Every catalog choice/default reaches the unchanged native model identity."""
    body = request_body(model, effort)
    original = deepcopy(body)
    governed = enforce_request_policy(route(model), body, "verified@example.invalid", HEADERS)
    forwarded, headers = governed.body, governed.headers
    effective = effort or model["effort"]["default"]
    expect_equal(governed.model_id, model["version"])
    for key in ("messages", "contents", "tools", "thinking", "anthropic_version", "max_tokens"):
        if key in original:
            expect_equal(forwarded[key], original[key])
    if model["vendor"] == "anthropic":
        expect_equal(forwarded["output_config"]["effort"], effective)
        expect_equal(forwarded["output_config"]["format"], original["output_config"]["format"])
        expect_not_in("labels", forwarded)
    else:
        expect_equal(forwarded["generationConfig"]["thinkingConfig"]["thinkingLevel"], effective.upper())
        expect(forwarded["generationConfig"]["thinkingConfig"]["includeThoughts"] is True)
        expect_equal(forwarded["generationConfig"]["maxOutputTokens"], 1000)
        expect_equal(forwarded["labels"]["proxy-auth-user"], "verified_example_invalid")
    expect_equal(json.loads(base64.b64decode(headers["X-Vertex-AI-Labels"]))["proxy-auth-user"], "verified_example_invalid")
    expect(build_vertex_url(route(model)).endswith(route(model)))


def test_http_discovery_advertises_effort_without_negotiation(offline_proxy_policy: dict[str, Any]) -> None:
    """The current extension sends only authentication on GET /discovery."""
    original = deepcopy(offline_proxy_policy)
    response = app.test_client().get("/discovery")
    expect_equal(response.status_code, 200)
    expect_not_in("catalogCapabilities", response.json)
    models = response.json["candidateModels"]
    expect_equal(len(models), len(CATALOG["candidateModels"]))
    for model in models:
        source = next(item for item in original["candidateModels"] if item["id"] == model["id"])
        expect_equal(model.get("effort"), source.get("effort"))
        expect_equal(model["version"], source["version"])
        expect_equal(model["maxInputTokens"], min(250000, source["maxInputTokens"]))
    copy = discovery.get_discovery_catalog()
    copy["candidateModels"][0]["pricing"]["input"] = 999
    copy["candidateModels"][0]["effort"]["values"].clear()
    expect_equal(offline_proxy_policy, original)


def test_discovery_limit_never_increases_native_capacity(offline_proxy_policy: dict[str, Any]) -> None:
    """A configured policy cap cannot overstate a smaller native input budget."""
    offline_proxy_policy["candidateModels"][0]["maxInputTokens"] = 1000
    expect_equal(discovery.get_discovery_catalog()["candidateModels"][0]["maxInputTokens"], 1000)


def _never_upstream(*_args: object, **_kwargs: object) -> Never:
    pytest.fail("Rejected policy input reached token counting or Vertex inference")


@pytest.mark.parametrize("allowed", ["gemini-4-argon", "gemini-3.8-flash", "gemini-4-argon-high", "gemini-4.argon"])
def test_argon_requires_exact_allowlist_membership(monkeypatch: pytest.MonkeyPatch, allowed: str) -> None:
    """Argon appears and accepts requests only with its exact canonical ID approved."""
    monkeypatch.setenv("ALLOWED_MODELS", allowed)
    model = next(model for model in EFFORT_MODELS if model["id"] == "gemini-4-argon")
    response = app.test_client().get("/discovery")
    expect_equal(response.status_code, 200)
    ids = [entry["id"] for entry in response.json["candidateModels"]]
    if allowed == model["id"]:
        expect_equal(ids, [model["id"]])
        governed = enforce_request_policy(route(model), request_body(model), "verified@example.invalid", HEADERS)
        expect_equal(governed.model_id, model["version"])
        expect_equal(governed.body["generationConfig"]["thinkingConfig"]["thinkingLevel"], "MEDIUM")
    else:
        expect_not_in(model["id"], ids)
        monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
        monkeypatch.setattr(handler, "forward_to_vertex", _never_upstream)
        monkeypatch.setattr(handler, "count_tokens_vertex", _never_upstream)
        response = app.test_client().post(route(model), json=request_body(model), headers=HEADERS)
        expect_equal(response.status_code, 400)


@pytest.mark.parametrize("vendor", ["google", "anthropic"])
@pytest.mark.parametrize("value", ["ultra", " high ", "", None, 3, True, {}, [], {"private": "must-not-log"}])
def test_rejected_effort_never_reaches_upstream(monkeypatch: pytest.MonkeyPatch, vendor: str, value: object) -> None:
    """Malformed choices return 400 before any cloud request, without echoing content."""
    monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
    monkeypatch.setattr(handler, "forward_to_vertex", _never_upstream)
    monkeypatch.setattr(handler, "count_tokens_vertex", _never_upstream)
    model = next(model for model in EFFORT_MODELS if model["vendor"] == vendor)
    body = request_body(model)
    container, key = (body["output_config"], "effort") if vendor == "anthropic" else (body["generationConfig"]["thinkingConfig"], "thinkingLevel")
    container[key] = value
    response = app.test_client().post(route(model), json=body, headers=HEADERS)
    expect_equal(response.status_code, 400)
    expect_not_in("must-not-log", response.data.decode())


@pytest.mark.parametrize(
    ("vendor", "key", "value"),
    [("google", "generationConfig", None), ("google", "generationConfig", []), ("google", "generationConfig", {"thinkingConfig": None}), ("google", "generationConfig", {"thinkingConfig": []}), ("anthropic", "output_config", None), ("anthropic", "output_config", [])],
)
def test_malformed_effort_containers_are_rejected_before_upstream(monkeypatch: pytest.MonkeyPatch, vendor: str, key: str, value: object) -> None:
    """Explicit invalid containers must not be silently replaced with defaults."""
    monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
    monkeypatch.setattr(handler, "forward_to_vertex", _never_upstream)
    monkeypatch.setattr(handler, "count_tokens_vertex", _never_upstream)
    model = next(model for model in EFFORT_MODELS if model["vendor"] == vendor)
    body = request_body(model)
    body[key] = value
    expect_equal(app.test_client().post(route(model), json=body, headers=HEADERS).status_code, 400)


@pytest.mark.parametrize("value", ["low", "LOW", "LoW"])
def test_google_native_levels_are_canonicalized(value: str) -> None:
    """Accept enum casing consistently and forward the SDK's uppercase form."""
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "google")
    body = request_body(model)
    body["generationConfig"]["thinkingConfig"]["thinkingLevel"] = value
    forwarded = enforce_request_policy(route(model), body, "test@example.invalid", HEADERS).body
    expect_equal(forwarded["generationConfig"]["thinkingConfig"]["thinkingLevel"], "LOW")


def test_claude_effort_is_a_literal_catalog_choice() -> None:
    """Claude's lowercase names are validated without trimming or alias conversion."""
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "anthropic")
    with pytest.raises(GovernanceError):
        enforce_request_policy(route(model), request_body(model, "LOW"), "test@example.invalid", HEADERS)


@pytest.mark.parametrize("include_level", [False, True])
def test_thinking_budget_cannot_bypass_advertised_levels(include_level: bool) -> None:
    """Gemini 3 named-level policy rejects budgets even with a permitted level."""
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "google")
    body = request_body(model, "low" if include_level else None)
    body["generationConfig"]["thinkingConfig"]["thinkingBudget"] = 9999
    with pytest.raises(GovernanceError, match="thinkingBudget"):
        enforce_request_policy(route(model), body, "test@example.invalid", HEADERS)


@pytest.mark.parametrize("message_effort", ["low", "max"])
def test_claude_per_message_effort_preserves_history(message_effort: str) -> None:
    """Explicit message choices are allowed, preserving signatures and unchanged turns."""
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "anthropic")
    body = request_body(model, "medium")
    body["messages"].append({"role": "system", "content": "", "output_config": {"effort": message_effort}})
    original = deepcopy(body["messages"])
    forwarded = enforce_request_policy(route(model), body, "test@example.invalid", HEADERS).body
    expect_equal(forwarded["messages"], original)
    expect_equal(forwarded["output_config"]["effort"], "medium")


@pytest.mark.parametrize("message_config", [None, [], {"effort": "ultra"}, {"effort": None}, {"effort": {"private": "must-not-log"}}])
def test_rejected_per_message_effort_never_reaches_upstream(monkeypatch: pytest.MonkeyPatch, message_config: object) -> None:
    """Per-message configuration cannot bypass the top-level catalog policy."""
    monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
    monkeypatch.setattr(handler, "forward_to_vertex", _never_upstream)
    monkeypatch.setattr(handler, "count_tokens_vertex", _never_upstream)
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "anthropic")
    body = request_body(model, "low")
    body["messages"].append({"role": "system", "content": "", "output_config": message_config})
    response = app.test_client().post(route(model), json=body, headers=HEADERS)
    expect_equal(response.status_code, 400)
    expect_not_in("must-not-log", response.data.decode())


@pytest.mark.parametrize("message_override", [False, True])
def test_no_effort_model_rejects_fabricated_controls(message_override: bool) -> None:
    """Absent metadata permits neither top-level nor message-level effort."""
    model = next(model for model in CATALOG["candidateModels"] if model["id"] == "claude-haiku-4-5")
    body = request_body(model)
    if message_override:
        body["messages"].append({"role": "system", "content": "", "output_config": {"effort": "high"}})
    else:
        body["output_config"]["effort"] = "high"
    with pytest.raises(GovernanceError, match="does not declare"):
        enforce_request_policy(route(model), body, "test@example.invalid", HEADERS)


def test_claude_default_does_not_change_thinking_mode() -> None:
    """Effort works with or without thinking; the upstream owns mode compatibility."""
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "anthropic")
    body = {"messages": [{"role": "user", "content": "OK"}]}
    forwarded = enforce_request_policy(route(model), body, "test@example.invalid", HEADERS).body
    expect_equal(forwarded["output_config"]["effort"], model["effort"]["default"])
    expect_not_in("thinking", forwarded)


@pytest.mark.parametrize("config", [None, [], {"thinkingConfig": None}])
def test_count_tokens_does_not_inject_or_validate_generation_effort(monkeypatch: pytest.MonkeyPatch, config: object) -> None:
    """Counting preserves native payload/errors independently of generation options."""
    monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "0")
    forwarded: list[dict[str, Any]] = []

    def upstream(**kwargs: Any) -> tuple[int, dict[str, str], Any]:
        forwarded.append(json.loads(kwargs["content"]))
        return 400, {"Content-Type": "application/json"}, iter([b'{"error":{"message":"Invalid counting payload"}}'])

    monkeypatch.setattr(handler, "forward_to_vertex", upstream)
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "google")
    body = {"contents": [], "generationConfig": config}
    response = app.test_client().post(route(model, action="countTokens"), json=body, headers=HEADERS)
    expect_equal(response.status_code, 400)
    expect_equal(response.json["error"]["message"], "Invalid counting payload")
    expect_equal(forwarded[0]["generationConfig"], config)


def test_literal_backend_and_publisher_match_the_allowlisted_catalog(monkeypatch: pytest.MonkeyPatch, offline_proxy_policy: dict[str, Any]) -> None:
    """Catalog ID and backend version can differ, and a real -high suffix stays literal."""
    custom = deepcopy(EFFORT_MODELS[0])
    custom.update(id="company-model", version="company-backend-high")
    offline_proxy_policy["candidateModels"] = [custom]
    monkeypatch.setenv("ALLOWED_MODELS", custom["id"])
    expect_equal(discovery.get_discovery_catalog()["candidateModels"][0]["version"], custom["version"])
    expect_equal(enforce_request_policy(route(custom), request_body(custom, "low"), "test@example.invalid", HEADERS).model_id, custom["version"])
    expect(build_vertex_url(route(custom)).endswith("company-backend-high:streamRawPredict"))
    for path in [route(custom, version=custom["id"]), route(custom, version="company-backend"), route(custom, publisher="google")]:
        with pytest.raises(GovernanceError, match="approved allowlist"):
            enforce_request_policy(path, request_body(custom), "test@example.invalid", HEADERS)


def test_empty_catalog_denies_even_an_allowlisted_model(offline_proxy_policy: dict[str, Any]) -> None:
    """A catalog outage cannot restore policy authorization through name heuristics."""
    offline_proxy_policy["candidateModels"] = []
    with pytest.raises(GovernanceError, match="approved allowlist"):
        enforce_request_policy(route(EFFORT_MODELS[0]), {}, "test@example.invalid", HEADERS)


@pytest.mark.parametrize("effort", ["low", None])
def test_summary_logs_effective_native_effort(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], effort: str | None) -> None:
    """Usage telemetry distinguishes explicit effort from the injected catalog default."""
    monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "google")
    monkeypatch.setattr(handler, "forward_to_vertex", lambda **_: (200, {"Content-Type": "application/json"}, iter([b'{"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":1}}'])))
    response = app.test_client().post(route(model), json=request_body(model, effort), headers=HEADERS)
    expect_equal(response.status_code, 200)
    expect(response.data)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    summary = next(event for event in events if event.get("event_kind") == "inference_completed")
    expect_equal(summary["model_effort"], effort or model["effort"]["default"])
    expect_equal(summary["model_upstream"], model["version"])
    expect_equal(summary["provider"], model["vendor"])
    expect_not_in("opaque-signature", str(summary))


def test_loader_keeps_valid_cache_and_resets_on_catalog_url_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid refreshes cannot replace valid policy or transfer it to a new URL."""
    monkeypatch.setattr(discovery, "_cache", discovery._CatalogCache())  # noqa: SLF001 - exercise worker cache reset.
    payload = deepcopy(CATALOG)
    calls: list[str] = []

    def download(url: str, **_kwargs: object) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(discovery.httpx, "get", download)
    expect_equal(REAL_LOADER(), CATALOG)
    expect_equal(REAL_LOADER(), CATALOG)
    expect_equal(len(calls), 1)
    payload["candidateModels"][0]["effort"]["default"] = "unsupported"
    monkeypatch.setattr(discovery._cache, "fetched_at", 0)  # noqa: SLF001 - force cache expiry without a wait.
    expect_equal(REAL_LOADER(), CATALOG)
    monkeypatch.setenv("MODELS_CATALOG_URL", "https://catalog.example.invalid/other/models.json")
    expect_equal(REAL_LOADER()["candidateModels"], [])
    expect_equal(len(calls), 3)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda catalog: catalog["candidateModels"].append(deepcopy(catalog["candidateModels"][0])),
        lambda catalog: catalog["candidateModels"][0].pop("version"),
        lambda catalog: catalog["candidateModels"][0].update(version="../other"),
        lambda catalog: catalog["candidateModels"][0].update(version="https://example.invalid/model"),
        lambda catalog: catalog["candidateModels"][0].update(effort=None),
        lambda catalog: catalog["candidateModels"][0]["effort"].update(values=["high", "high"]),
        lambda catalog: catalog["candidateModels"][0]["effort"].update(values=[3]),
        lambda catalog: catalog["candidateModels"][0]["effort"].update(default="invalid"),
    ],
)
def test_malformed_routing_or_effort_metadata_is_not_cached(monkeypatch: pytest.MonkeyPatch, mutation: Callable[[dict[str, Any]], object]) -> None:
    """Downloaded catalogs must pass routing/effort validation before caching."""
    catalog = deepcopy(CATALOG)
    mutation(catalog)
    monkeypatch.setattr(discovery.httpx, "get", lambda url, **_: httpx.Response(200, json=catalog, request=httpx.Request("GET", url)))
    expect_is(discovery._download_catalog("https://catalog.example.invalid/models.json"), None)  # noqa: SLF001 - validate downloader boundary.


@pytest.mark.parametrize(
    "alter",
    [
        lambda path: f"{path}/extra",
        lambda path: path.replace("/models/", "/models/../models/"),
        lambda path: path.replace("/projects/client-project/", "/projects/other-project/../client-project/"),
        lambda path: path.replace("/locations/global/", "/locations/global/extra/"),
        lambda path: path.replace("/publishers/anthropic/", "/publishers/google/../anthropic/"),
        lambda path: path.replace(":streamRawPredict", ":streamRawPredict/../../other:rawPredict"),
    ],
)
def test_nonliteral_vertex_routes_cannot_bypass_catalog_policy(alter: Callable[[str], str]) -> None:
    """Path traversal and trailing resources must fail before URL normalization."""
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "anthropic")
    with pytest.raises(GovernanceError):
        enforce_request_policy(alter(route(model)), request_body(model), "test@example.invalid", HEADERS)


def test_http_trailing_resource_is_rejected_before_any_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Flask catch-all route still enforces the complete native model route."""
    monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
    monkeypatch.setattr(handler, "forward_to_vertex", _never_upstream)
    monkeypatch.setattr(handler, "count_tokens_vertex", _never_upstream)
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "anthropic")
    expect_equal(app.test_client().post(f"{route(model)}/extra", json=request_body(model), headers=HEADERS).status_code, 400)


def test_gemini_config_wrapper_cannot_hide_invalid_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrapped native generation options are checked after label/config unwrapping."""
    monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
    monkeypatch.setattr(handler, "forward_to_vertex", _never_upstream)
    monkeypatch.setattr(handler, "count_tokens_vertex", _never_upstream)
    model = next(model for model in EFFORT_MODELS if model["vendor"] == "google")
    body = {"contents": [], "config": {"labels": LABELS, "generationConfig": {"thinkingConfig": {"thinkingLevel": "ULTRA"}}}}
    expect_equal(app.test_client().post(route(model), json=body).status_code, 400)


def test_google_without_named_effort_preserves_legacy_budget(monkeypatch: pytest.MonkeyPatch, offline_proxy_policy: dict[str, Any]) -> None:
    """A catalog with no named choices delegates token-budget support to Vertex."""
    model = deepcopy(next(model for model in EFFORT_MODELS if model["vendor"] == "google"))
    model.pop("effort")
    offline_proxy_policy["candidateModels"] = [model]
    monkeypatch.setenv("ALLOWED_MODELS", model["id"])
    body = {"contents": [], "generationConfig": {"thinkingConfig": {"thinkingBudget": 1024}}}
    forwarded = enforce_request_policy(route(model), body, "test@example.invalid", HEADERS).body
    expect_equal(forwarded["generationConfig"], {"thinkingConfig": {"thinkingBudget": 1024}})
    forwarded["generationConfig"]["thinkingConfig"]["thinkingLevel"] = "HIGH"
    with pytest.raises(GovernanceError, match="does not declare"):
        enforce_request_policy(route(model), forwarded, "test@example.invalid", HEADERS)


def test_no_effort_model_leaves_options_and_message_configuration_unchanged() -> None:
    """Omission on a model without metadata inserts no top-level or message effort."""
    model = next(model for model in CATALOG["candidateModels"] if model["id"] == "claude-haiku-4-5")
    body = request_body(model)
    body["messages"].append({"role": "system", "content": "", "output_config": {"format": {"type": "json_schema"}}})
    original = deepcopy(body)
    forwarded = enforce_request_policy(route(model), body, "test@example.invalid", HEADERS).body
    expect_equal(forwarded, original)


def test_successful_request_uses_one_stale_refresh_and_authorized_metadata_snapshot(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A failed refresh and concurrent cache change cannot delay or alter usage metadata."""
    monkeypatch.setenv("LOCAL_DEV_BYPASS", "true")
    cached = deepcopy(CATALOG)
    model = next(model for model in cached["candidateModels"] if model["vendor"] == "google")
    authorized = deepcopy(model)
    worker_cache = discovery._CatalogCache(catalog=cached, fetched_at=0, source_url=settings.models_catalog_url)  # noqa: SLF001 - force a stale worker snapshot.
    monkeypatch.setattr(discovery, "_cache", worker_cache)
    monkeypatch.setattr(discovery, "load_master_catalog", REAL_LOADER)
    downloads: list[str] = []

    def unavailable(url: str, **_kwargs: object) -> httpx.Response:
        downloads.append(url)
        return httpx.Response(503, request=httpx.Request("GET", url))

    def upstream(**kwargs: Any) -> tuple[int, dict[str, str], Any]:
        forwarded = json.loads(kwargs["content"])
        expect_equal(forwarded["generationConfig"]["thinkingConfig"]["thinkingLevel"], authorized["effort"]["default"].upper())
        model["version"] = "changed-after-authorization"
        model["effort"]["default"] = "high"
        model["pricing"]["input"] = 999
        return 200, {"Content-Type": "application/json"}, iter([b'{"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":1}}'])

    monkeypatch.setattr(discovery.httpx, "get", unavailable)
    monkeypatch.setattr(handler, "forward_to_vertex", upstream)
    response = app.test_client().post(route(authorized), json=request_body(authorized), headers=HEADERS)
    expect_equal(response.status_code, 200)
    expect(response.data)
    expect_equal(downloads, [settings.models_catalog_url])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    summary = next(event for event in events if event.get("event_kind") == "inference_completed")
    expect_equal(summary["model_upstream"], authorized["version"])
    expect_equal(summary["model_effort"], authorized["effort"]["default"])
    expect_equal(json.loads(summary["model_metadata_json"]), authorized)
