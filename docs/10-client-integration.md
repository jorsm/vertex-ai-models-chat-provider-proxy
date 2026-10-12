# 10. Client integration <!-- omit in toc -->

- [VS Code extension](#vs-code-extension)
- [Obtaining an ID token](#obtaining-an-id-token)
- [Example: calling Gemini through the proxy](#example-calling-gemini-through-the-proxy)
- [Example: Claude labels](#example-claude-labels)
- [Handling errors](#handling-errors)

The proxy speaks the native Vertex AI REST protocol on `/v1/projects/...`, so any client that can change its base URL and attach a Google ID token can use it. The reference client is the open-source [Google Agent Platform for Copilot Chat](https://marketplace.visualstudio.com/items?itemName=jorsm.vertex-ai-models-chat-provider) extension for VS Code, which exposes a setting for this purpose.

## VS Code extension

Set the extension's proxy URL to the base URL of your proxy (`terraform output proxy_service_uri`, or your custom domain) in the **user** settings (the setting is machine-scoped, not workspace-scoped):

```json
{
  "vertexAiChat.proxyUrl": "https://copilot-chat-proxy.example.com",
  "vertexAiChat.projectId": "my-copilot-chat-proxy"
}
```

With `proxyUrl` set, the extension:

1. Requests the model catalog from `GET /discovery` instead of using a built-in list.
2. Authenticates with the user's personal `gcloud auth login` identity and sends a Google **ID token** as `Authorization: Bearer ...` on every call. There is no direct fallback to Vertex AI while `proxyUrl` is set.
3. Sends Gemini and Claude inference requests to `/v1/projects/{projectId}/locations/{region}/publishers/{publisher}/models/{model}:{action}` on the proxy rather than on `aiplatform.googleapis.com`.
4. Attaches the user and workspace labels (`vscode-vertex-ai-user`, `vscode-vertex-ai-project`) when label support is enabled in the extension, so they must match `CHAT_USER_LABEL_KEY` and `CHAT_PROJECT_LABEL_KEY` (the proxy remaps them to your billing label names).
5. Retries only transient failures (429, 502, 503 and network errors) and shows other errors, including the proxy's `400` and `403` messages, to the user immediately.

`vertexAiChat.projectId` must be a project the **proxy service account** can use for Vertex AI calls (normally the proxy project). Refer to the extension's own documentation for the exact current behavior and for providers that do not yet support proxy mode.

Discovery includes each model's optional effort choices and default without negotiation. Keep the canonical model ID for selection, use its advertised `version` and `vendor` in the inference path, and send effort in the native provider payload. Changing effort does not select a different model ID. See [Thinking effort](04-governance.md#thinking-effort).

## Obtaining an ID token

For command-line tests and other clients:

```bash
gcloud auth login
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" https://copilot-chat-proxy.example.com/discovery
```

Libraries such as `google-auth` (`id_token.fetch_id_token`) or `google-auth-library` (`getIdTokenClient`) obtain tokens programmatically. The audience must be accepted by Cloud Run IAM (the service URL, or a custom audience configured on the service).

## Example: calling Gemini through the proxy

```bash
curl -N -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://copilot-chat-proxy.example.com/v1/projects/my-copilot-chat-proxy/locations/global/publishers/google/models/gemini-3.8-flash:streamGenerateContent?alt=sse" \
  -d '{
        "contents": [{"role": "user", "parts": [{"text": "Say hello"}]}],
        "generationConfig": {"thinkingConfig": {"thinkingLevel": "HIGH"}},
        "labels": {"vscode-vertex-ai-user": "jane", "vscode-vertex-ai-project": "my-repo"}
      }'
```

Use a level advertised by discovery; omit `thinkingLevel` to use the catalog default. The proxy adds the verified-caller label and `managed-by`, forwards the request to Vertex AI and streams the answer back.

## Example: Claude labels

Anthropic models take labels from a header, as base64-encoded JSON:

```bash
LABELS=$(printf '{"vscode-vertex-ai-user":"jane","vscode-vertex-ai-project":"my-repo"}' | base64 -w0)
# send:  X-Vertex-AI-Labels: $LABELS
```

For Claude, put a lowercase advertised effort in `output_config`, for example `"output_config": {"effort": "high"}`, or omit it to use the catalog default. Keep the model's other native options, including `thinking`, as required by the client.

## Handling errors

| Status | Meaning | Client behavior |
| --- | --- | --- |
| 400 | Model/publisher, project, region, effort or label policy violated, or context too large | Show `error.message`; do not retry |
| 401 / 403 | Not authenticated or not authorized to invoke the proxy | Ask the user to re-authenticate (`gcloud auth login`) or request access |
| 404 / 405 | Wrong path or method | Check the base URL and path shape |
| 429 / 502 / 503 | Rate limit, upstream or proxy failure | May be retried with backoff (the extension does) |

See the [API reference](05-api-reference.md) for the exact error format.
