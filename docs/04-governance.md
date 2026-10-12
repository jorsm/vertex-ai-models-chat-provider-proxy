# 4. Governance <!-- omit in toc -->

- [Model allowlist](#model-allowlist)
- [Thinking effort](#thinking-effort)
- [Project allowlist](#project-allowlist)
- [Region allowlist](#region-allowlist)
- [Cost-attribution labels](#cost-attribution-labels)
  - [How labels reach Vertex AI](#how-labels-reach-vertex-ai)
- [Context-window limit](#context-window-limit)
- [Discovery catalog](#discovery-catalog)
- [What governance does not do](#what-governance-does-not-do)

Governance rules are applied to every inference request, after authentication and before anything is sent to Vertex AI. A violation returns `HTTP 400` with `status: FAILED_PRECONDITION` and an explanatory message.

## Model allowlist

`ALLOWED_MODELS` contains exact canonical IDs from the configured model catalog, for example `claude-sonnet-5-5` and `gemini-3.8-flash`. An entry missing from the catalog grants no access.

The request path must use an allowed catalog entry's literal `version` and matching `vendor` under `/publishers/{vendor}/models/{version}:{action}`. The supported publishers are `google` and `anthropic`. The catalog ID and backend version may differ; clients use the advertised `version` for inference.

Matching is exact: dots, hyphens, prefixes and suffixes are not normalized, and the proxy does not rewrite the backend model name. Legacy effort aliases such as `claude-sonnet-5-5-high` grant no access unless they are literal catalog entries. An unavailable catalog with no valid cached copy denies inference.

## Thinking effort

Models can declare `effort: {"values": [...], "default": "..."}` in the catalog. This policy is included in discovery and enforced on inference before the context check or any upstream request. Omission selects the declared default; malformed fields and unsupported values return `400 FAILED_PRECONDITION`.

| Provider | Native request field | Forwarded value |
| --- | --- | --- |
| Gemini | `generationConfig.thinkingConfig.thinkingLevel` | Catalog choice in uppercase, for example `HIGH` |
| Claude | `output_config.effort` | Exact lowercase catalog choice, for example `high` |

On Gemini models declaring named effort, `thinkingBudget` is rejected; choose an advertised `thinkingLevel`. Claude's optional message-level `output_config.effort` overrides are also validated, without adding defaults to historical messages. Models without an effort declaration reject named effort controls.

The proxy preserves other thinking options (including Claude adaptive or enabled modes), output formats, signed thinking history and tool payloads. Explicit `:countTokens` requests skip effort validation and default injection, while authentication, model/project/region policy and label enforcement still apply.

## Project allowlist

The Vertex AI project is read from the path (`/projects/{project}/locations/...`). It must be in `ALLOWED_VERTEX_PROJECTS`, otherwise the request is rejected with `400`. When the setting is not provided, only the proxy project (`PROXY_PROJECT_ID`) is allowed. In a wider setup, list every project that clients may consume Vertex AI from: the client picks one per request, typically per workspace. The proxy service account needs `roles/aiplatform.user` on each of them. Terraform grants it on the proxy project only, and only when the proxy project is an allowed project: for every other listed project, grant the role to the service account (`terraform output proxy_service_account_email`) yourself.

## Region allowlist

The Vertex AI location is read from the path (`/locations/{region}/`). It must be in `ALLOWED_REGIONS` (for example `global`), otherwise the request is rejected. Project and region are chosen by the client; the two allowlists bound the choice.

## Cost-attribution labels

Label names are configured twice, because the name the chat client uses is not necessarily the name you want in the billing data.

**Received from the chat client** (the keys the client sends, used only to read the values):

| Setting | Meaning |
| --- | --- |
| `CHAT_USER_LABEL_KEY` | Key carrying the client-declared user |
| `CHAT_PROJECT_LABEL_KEY` | Key carrying the workspace or repository being worked on |

> **Don't touch these two.** The chat extension sends `vscode-vertex-ai-user` and `vscode-vertex-ai-project` today; if they do not match, requests are rejected or lose their attribution.

**Written to billing** (the label keys on the Vertex AI request, chosen freely):

| Setting | Meaning | Source of the value |
| --- | --- | --- |
| `BILLING_USER_LABEL_KEY` | Client-declared user (descriptive) | Remapped from `CHAT_USER_LABEL_KEY` |
| `BILLING_PROJECT_LABEL_KEY` | Workspace or repository being worked on | Remapped from `CHAT_PROJECT_LABEL_KEY` |
| `BILLING_AUTH_USER_LABEL_KEY` | Authenticated caller | **Derived from the verified token** (no client counterpart) |

The client labels are read under the chat keys and sent to Vertex AI under the billing keys only; the original chat-key labels are removed. Keep the three billing keys different from each other.

A fourth label, `managed-by = copilot-chat-proxy`, marks traffic that went through the proxy.

- `REQUIRE_USER_LABEL` / `REQUIRE_PROJECT_LABEL` decide whether a missing client label is an error (`Missing required label '<key>'`) or is replaced by the value `default`.
- Label values are sanitized to the Google Cloud label rules: lowercase letters, digits, `_` and `-`; at most 63 characters; they must start with a letter (a `v_` prefix is added otherwise); empty input becomes `unknown`. For example `jane.doe@example.com` becomes `jane_doe_example_com`.

The proxy looks for client labels, using the chat keys, in this order, in: a header named like the label key, the base64 JSON `X-Vertex-AI-Labels` header, the body's `config.labels`, and the body's root `labels`.

### How labels reach Vertex AI

| Provider | Transport |
| --- | --- |
| Gemini | Root `labels` object of the JSON body. A client `config` wrapper is unwrapped (labels merged, `generationConfig`, `systemInstruction`, `tools`, `safetySettings` lifted to the root) and the unsupported `model` field is removed. |
| Claude (Anthropic) | `X-Vertex-AI-Labels` header containing base64-encoded JSON. Anthropic rejects body labels, so any labels in the body are removed. |

## Context-window limit

`MAX_CONTEXT_TOKENS` bounds the size of a request to contain cost. For Gemini the proxy asks Vertex AI's `countTokens`; when that is unavailable (other publishers, or a failure) it estimates `bytes / 4`. If the result is greater than the limit the request is rejected with `400 FAILED_PRECONDITION`, including the detected and maximum token counts and a hint to start a new conversation or compact the context. A value `<= 0` disables the check.

## Discovery catalog

`GET /discovery` returns the model catalog the client may show:

- The master catalog is downloaded from `MODELS_CATALOG_URL` and cached in memory for 5 minutes after validating routing, native input capacity and optional effort metadata. If a refresh fails, only a previously valid copy from the same URL can be served. Changing the URL clears the cache.
- Only exact catalog IDs listed in `ALLOWED_MODELS` are returned; unlisted models are never synthesized.
- Optional `effort` metadata is included without a capability header or negotiation marker.
- `DISCOVERY_MAX_INPUT_TOKENS` (> 0) caps each model's advertised `maxInputTokens` at the smaller of its native capacity and the configured policy value.
- `regionPriority` is filtered to `ALLOWED_REGIONS`.

The same catalog provides the **pricing** used to estimate costs (see [Observability](08-observability.md)).

## What governance does not do

- It does not manage who may invoke the proxy: that is Cloud Run IAM ([Security and IAM](03-security-and-iam.md)).
- There is no per-user or per-project budget enforcement and no per-user model profile: the allowlist is global. Budgets are monitored (billing budget, alerts), not enforced per request.
