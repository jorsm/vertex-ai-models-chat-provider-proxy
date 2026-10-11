# 4. Governance <!-- omit in toc -->

- [Model allowlist](#model-allowlist)
- [Region allowlist](#region-allowlist)
- [Cost-attribution labels](#cost-attribution-labels)
  - [How labels reach Vertex AI](#how-labels-reach-vertex-ai)
- [Context-window limit](#context-window-limit)
- [Discovery catalog](#discovery-catalog)
- [What governance does not do](#what-governance-does-not-do)

Governance rules are applied to every inference request, after authentication and before anything is sent to Vertex AI. A violation returns `HTTP 400` with `status: FAILED_PRECONDITION` and an explanatory message.

## Model allowlist

Only models listed in `ALLOWED_MODELS` can be used. The model ID is taken from the request path (`/models/{model}:{action}`, used by the Gemini and Claude SDKs) or, for OpenAI-compatible calls, from the `model` field of the body.

Matching rules:

- Dots and hyphens are interchangeable: `gemini-3.8-flash` equals `gemini-3-8-flash`.
- **Effort variants must be listed explicitly.** `-high`, `-medium` and `-max` entries are separate models. Allowing `gemini-3.8-flash` does not allow `gemini-3.8-flash-high`.
- Allowing a variant also allows its **upstream model ID**, because SDKs send the underlying version in the URL. For example `claude-sonnet-5-5-medium` authorizes requests for `claude-sonnet-5-5`, but not `claude-sonnet-5-5-high` or `claude-sonnet-5-6`.
- Anything else, including arbitrary prefixes and sibling versions, is rejected.

Variants are resolved before the upstream call: a `-high` suffix is stripped from the path sent to Vertex AI. For Gemini, `-high` additionally injects `generationConfig.thinkingConfig.thinkingLevel = "HIGH"` unless the client already set a `thinkingConfig`.

## Project allowlist

The Vertex AI project is read from the path (`/projects/{project}/locations/...`). It must be in `ALLOWED_VERTEX_PROJECTS`, otherwise the request is rejected with `400`. When the setting is not provided, only the hub project (`HUB_PROJECT_ID`) is allowed. In a wider setup, list every project that clients may consume Vertex AI from: the client picks one per request, typically per workspace. The proxy service account needs `roles/aiplatform.user` on each of them. Terraform grants it on the hub project only, and only when the hub is an allowed project: for every other listed project, grant the role to the service account (`terraform output proxy_service_account_email`) yourself.

## Region allowlist

The Vertex AI location is read from the path (`/locations/{region}/`). It must be in `ALLOWED_REGIONS` (for example `global`), otherwise the request is rejected. Project and region are chosen by the client; the two allowlists bound the choice.

## Cost-attribution labels

Three labels are enforced, using key names taken from configuration:

| Setting | Meaning | Source of the value |
| --- | --- | --- |
| `USER_LABEL_KEY` | Client-declared user (descriptive) | Sent by the client |
| `PROJECT_LABEL_KEY` | Workspace or repository being worked on | Sent by the client |
| `AUTH_USER_LABEL_KEY` | Authenticated caller | **Derived from the verified token** |

A fourth label, `managed-by = copilot-chat-proxy`, marks traffic that went through the proxy.

- `REQUIRE_USER_LABEL` / `REQUIRE_PROJECT_LABEL` decide whether a missing client label is an error (`Missing required label '<key>'`) or is replaced by the value `default`.
- Label values are sanitized to the Google Cloud label rules: lowercase letters, digits, `_` and `-`; at most 63 characters; they must start with a letter (a `v_` prefix is added otherwise); empty input becomes `unknown`. For example `jane.doe@example.com` becomes `jane_doe_example_com`.

The proxy looks for client labels, in this order, in: a header named like the label key, the base64 JSON `X-Vertex-AI-Labels` header, the body's `config.labels`, and the body's root `labels`.

### How labels reach Vertex AI

| Provider | Transport |
| --- | --- |
| Gemini | Root `labels` object of the JSON body. A client `config` wrapper is unwrapped (labels merged, `generationConfig`, `systemInstruction`, `tools`, `safetySettings` lifted to the root) and the unsupported `model` field is removed. |
| Claude (Anthropic) | `X-Vertex-AI-Labels` header containing base64-encoded JSON. Anthropic rejects body labels, so any labels in the body are removed. |

## Context-window limit

`MAX_CONTEXT_TOKENS` bounds the size of a request to contain cost. For Gemini the proxy asks Vertex AI's `countTokens`; when that is unavailable (other publishers, or a failure) it estimates `bytes / 4`. If the result is greater than the limit the request is rejected with `400 FAILED_PRECONDITION`, including the detected and maximum token counts and a hint to start a new conversation or compact the context. A value `<= 0` disables the check.

## Discovery catalog

`GET /discovery` returns the model catalog the client may show:

- The master catalog is downloaded from `MODELS_CATALOG_URL` (a JSON document following the `models.schema.json` format of the VS Code extension) and cached in memory for 5 minutes. If a refresh fails, the previous copy is served.
- Only models listed in `ALLOWED_MODELS` are returned; unlisted models are never synthesized.
- `DISCOVERY_MAX_INPUT_TOKENS` (> 0) overrides `maxInputTokens` for every model so clients stay below the context limit.
- `regionPriority` is filtered to `ALLOWED_REGIONS`.

The same catalog provides the **pricing** used to estimate costs (see [Observability](08-observability.md)).

## What governance does not do

- It does not manage who may invoke the proxy: that is Cloud Run IAM ([Security and IAM](03-security-and-iam.md)).
- There is no per-user or per-project budget enforcement and no per-user model profile: the allowlist is global. Budgets are monitored (billing budget, alerts), not enforced per request.
