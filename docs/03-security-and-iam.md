# 3. Security and IAM <!-- omit in toc -->

The central principle: **the user's identity ends at the proxy; access to Vertex AI happens only through the proxy's identity.**

- [Authentication flow](#authentication-flow)
  - [Local development bypass](#local-development-bypass)
- [Anti-spoofing](#anti-spoofing)
- [IAM matrix](#iam-matrix)
  - [Making the proxy the only entry point](#making-the-proxy-the-only-entry-point)
- [Third-party model terms](#third-party-model-terms)
- [Data handling](#data-handling)
- [Hardening checklist](#hardening-checklist)

## Authentication flow

1. The client obtains a Google **ID token** (OIDC) for the proxy URL, for example with `gcloud auth print-identity-token`, and sends it as `Authorization: Bearer <token>`.
2. **Cloud Run** verifies the token signature and checks that the caller holds `roles/run.invoker` on the service. Unauthorized callers never reach the container.
3. When Cloud Run forwards the request it replaces the token signature with the marker `_REMOVED_BY_GOOGLE`, because the signature has already been verified at the edge. The proxy decodes the payload of such tokens.
4. A token that still carries a real signature (for example when the service is reached some other way) is verified by the proxy itself, first with Google's `tokeninfo` endpoint and then with Google's published signing keys (JWKS).
5. The proxy validates the claims:
   - `iss` is `accounts.google.com` or `https://accounts.google.com`;
   - `exp` has not passed;
   - `aud` matches `EXPECTED_AUDIENCE`, when that setting is configured;
   - an `email` claim is present. The lower-cased email becomes the authenticated caller.

Failures return `401` with `status: UNAUTHENTICATED` (see [API reference](05-api-reference.md)).

The optional `name` claim is logged as `principal_display_name`. Google does not guarantee that claim: with `gcloud auth print-identity-token` it can be missing, in which case the field is simply omitted. `principal_subject` (the `sub` claim) is the stable identifier.

### Local development bypass

With `LOCAL_DEV_BYPASS=true` the proxy skips token validation and acts as `local-developer@example.com`. This is meant for local runs and tests only. Never set it on a deployed service.

## Anti-spoofing

The cost-attribution label that identifies the caller (`AUTH_USER_LABEL_KEY`, `proxy-auth-user` in the sample configuration) is derived **only** from the verified token. Labels the client sends, such as `vscode-vertex-ai-user`, are kept as descriptive metadata but never decide who is billed. A client cannot attribute its usage to someone else.

## IAM matrix

| Identity | Role | Resource | Purpose |
| --- | --- | --- | --- |
| Developers (`proxy_invoker_identities`) | `roles/run.invoker` | The Cloud Run proxy service | Invoke the proxy. Nothing else. |
| Developers | *none* on Vertex AI | Hub project (and any governed project) | They must not be able to call Vertex AI directly. |
| Proxy service account | `roles/aiplatform.user` | Hub project when it is an allowed Vertex project (Terraform); every other project in `allowed_vertex_projects` (manual) | Call Gemini, Claude and other Vertex AI models. |
| Proxy service account | `roles/logging.logWriter` | Hub project | Write application logs. |
| Proxy service account | `roles/cloudtrace.agent` | Hub project | Export OpenTelemetry spans to Cloud Trace. |
| Cloud Logging sink writer identity | `roles/bigquery.dataEditor` | Usage dataset | Deliver usage events to BigQuery. |

The proxy itself has no BigQuery permissions: usage reaches BigQuery through the Cloud Logging sink.

### Making the proxy the only entry point

Terraform grants the invoker role and the service-account roles above. It does **not** remove anything users already have. To force all traffic through the proxy, make sure users hold no equivalent permission through any other path:

- Remove `roles/aiplatform.user` (or any role containing `aiplatform.endpoints.predict`) from users on every project you want to govern.
- Check inherited grants from folders and the organization, group memberships, custom roles and broad roles such as Editor or Owner.
- Do not give users keys for, or permission to impersonate, the proxy service account.

Configuring a client to use the proxy only changes how that client routes requests; the effective IAM policy is what makes the proxy mandatory.

## Third-party model terms

Because Vertex AI calls are made with the proxy service account in the hub project, publisher terms for third-party models (for example Anthropic Claude in Model Garden) are accepted once by a cloud administrator in the hub project. Developers do nothing in the Google Cloud console.

## Data handling

- Prompts, generated text and tool payloads are **never** copied into logs, metrics, traces or BigQuery. Only metadata is recorded: identities, model, labels, token counts, timings and estimated costs.
- Upstream HTTP errors are the one exception for response bodies: when Vertex AI returns a 4xx/5xx the (bounded, at most 128 KiB) error body is logged to help troubleshooting. Error bodies describe the failure and are not model output.
- Exception messages and stack traces are never attached to trace spans, because they can include upstream payloads or credentials.
- The HTTP client does not persist cookies between requests.

## Hardening checklist

- [ ] Only the intended identities hold `roles/run.invoker`.
- [ ] No user holds Vertex AI inference permissions in governed projects.
- [ ] `LOCAL_DEV_BYPASS` is unset (or `false`) on the deployed service.
- [ ] `allowed_models`, `allowed_vertex_projects`, `allowed_vertex_regions` and the label requirements are set explicitly in `terraform.tfvars`.
- [ ] `terraform.tfvars` and Terraform state are not committed to version control (use a remote state backend).
- [ ] Notification emails for budgets and alerts are configured.
