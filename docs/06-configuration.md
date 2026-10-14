# 6. Configuration <!-- omit in toc -->

- [Proxy environment variables](#proxy-environment-variables)
- [Terraform variables](#terraform-variables)
  - [Model allowlist](#model-allowlist)
  - [Environment and project](#environment-and-project)
  - [Governance](#governance)
  - [Usage analytics, budgets and alerts](#usage-analytics-budgets-and-alerts)
  - [Tracing](#tracing)
- [Terraform outputs](#terraform-outputs)

Configuration lives in two places that map onto each other: Terraform **variables** (set in `terraform.tfvars`) and the **environment variables** Terraform writes onto the Cloud Run service. When you run the proxy yourself (Docker, local) you set the environment variables directly.

The Terraform variables intentionally have **no defaults**: every value is stated explicitly in `terraform.tfvars`. Start from [`terraform/terraform.tfvars.example`](../terraform/terraform.tfvars.example).

## Proxy environment variables

Variables marked *required* have no default: the service refuses to use a missing setting and raises an error naming it.

| Variable | Required | Meaning | Terraform source |
| --- | :---: | --- | --- |
| `PROXY_PROJECT_ID` | yes | Project hosting the proxy; used for telemetry and trace correlation, and as the default Vertex AI project | `project_id` |
| `ALLOWED_VERTEX_PROJECTS` | no | Comma-separated list (or JSON array) of Vertex AI project IDs clients may target; defaults to `PROXY_PROJECT_ID` | `allowed_vertex_projects` (empty list = proxy project) |
| `ALLOWED_REGIONS` | yes | Comma-separated list (or JSON array) of permitted Vertex AI locations, for example `global` | `allowed_vertex_regions` |
| `ALLOWED_MODELS` | yes | Comma-separated list (or JSON array) of exact permitted canonical catalog IDs; effort is a request field | `allowed_models` |
| `CHAT_USER_LABEL_KEY` | yes | Key under which the chat client sends the user (don't touch: fixed by the extension) | `chat_user_label_key` |
| `CHAT_PROJECT_LABEL_KEY` | yes | Key under which the chat client sends the workspace (don't touch: fixed by the extension) | `chat_project_label_key` |
| `BILLING_USER_LABEL_KEY` | yes | Billing label key that carries the client-declared user | `billing_user_label_key` |
| `BILLING_PROJECT_LABEL_KEY` | yes | Billing label key that carries the client-declared workspace | `billing_project_label_key` |
| `BILLING_AUTH_USER_LABEL_KEY` | yes | Billing label key that carries the verified caller | `billing_auth_user_label_key` |
| `REQUIRE_USER_LABEL` | yes | `true`/`false`: reject requests without the user label | `require_user_label` |
| `REQUIRE_PROJECT_LABEL` | yes | `true`/`false`: reject requests without the project label | `require_project_label` |
| `MODELS_CATALOG_URL` | yes | URL of the validated model catalog used for authorization, discovery, effort and pricing | `models_catalog_url` |
| `PROXY_BASE_URL` | yes | Public base URL, published in `/openapi.json` | `https://<custom_domain>` |
| `MAX_CONTEXT_TOKENS` | yes | Request size limit in tokens; `<= 0` disables | `max_context_tokens` |
| `DISCOVERY_MAX_INPUT_TOKENS` | yes | Cap on each model's native `maxInputTokens` in `/discovery`; `<= 0` keeps catalog values | `discovery_max_input_tokens` |
| `TRACE_ENABLED` | yes | `true`/`false`: export spans to Cloud Trace | `proxy_trace_enabled` |
| `TRACE_SAMPLING_RATIO` | yes | Fraction (0 to 1) of inference traces exported | `proxy_trace_sampling_ratio` |
| `TRACE_EXPORT_TIMEOUT_MS` | yes | Export RPC deadline and final flush wait in ms (> 0) | `proxy_trace_export_timeout_ms` |
| `EXPECTED_AUDIENCE` | no | If set, the token `aud` claim must equal it | not set by Terraform |
| `LOG_EXECUTION_ID` | no | Include request/execution IDs in logs (default `true`) | set to `true` |
| `LOCAL_DEV_BYPASS` | no | `true` skips token validation. **Local use only** (default `false`) | not set |
| `PORT` | no | Listening port inside the container (`8080` in the Dockerfile) | Cloud Run |
| `K_REVISION` | no | Provided by Cloud Run; logged as `service_version` | Cloud Run |

## Terraform variables

### Model allowlist

`allowed_models` becomes the Cloud Run `ALLOWED_MODELS` environment variable. The proxy downloads `models_catalog_url` and caches valid catalogs in memory for five minutes, retaining the last valid catalog if a refresh fails. `/discovery` returns the intersection of the allowlist and catalog `id` values, in allowlist order. Missing IDs are omitted; wildcards, effort suffixes, and model-name normalization are not supported.

Inference uses that same approved subset: the request route must match an allowed entry's exact `version` and `vendor` (Google or Anthropic). A client cannot bypass the subset by calling a hidden model directly; the proxy returns `400` before token counting or forwarding. Project, region, label and request-size policies are checked independently.

Thinking effort comes from each allowed model's catalog metadata. For example, `gemini-4-argon` permits Low/Medium/High, defaults to Medium when omitted, and rejects unsupported levels. `allowed_models` selects models; it does not restrict their individual effort choices. Discovery also caps advertised input tokens using `discovery_max_input_tokens`.

Update the actual `terraform.tfvars` and apply the reviewed plan to change the deployed subset; editing the example or the upstream catalog alone does not enable a model.

### Environment and project

| Variable | Description |
| --- | --- |
| `folder_id` | Folder in which the proxy project is created |
| `project_id` | ID of the proxy project |
| `billing_account_id` | Billing account linked to the proxy project and used for the budget |
| `region` | Region of the Cloud Run service and regional resources |
| `custom_domain` | Domain mapped to the Cloud Run service; the proxy base URL is `https://<custom_domain>`. Domain mapping is a Preview feature limited to some regions, and the domain must be verified in Search Console first: see [Deployment](07-deployment.md#3-custom-domain) |
| `proxy_invoker_identities` | IAM members (`user:`, `group:`, `serviceAccount:`) granted `roles/run.invoker` |

### Governance

`allowed_models`, `allowed_vertex_projects`, `allowed_vertex_regions`, `chat_user_label_key`, `chat_project_label_key`, `billing_user_label_key`, `billing_project_label_key`, `billing_auth_user_label_key`, `require_project_label`, `require_user_label`, `models_catalog_url`, `max_context_tokens`, `discovery_max_input_tokens` correspond one-to-one to the environment variables above.

### Usage analytics, budgets and alerts

| Variable | Description |
| --- | --- |
| `usage_bigquery_dataset_id` | Dataset that receives usage events |
| `bigquery_dataset_location` | Dataset location, for example `EU` |
| `budget_amount`, `budget_currency` | Monthly billing budget of the proxy project (alerts at 50%, 90%, 100% actual and 100% forecast) |
| `notification_emails` | Recipients shared by budgets and Monitoring alerts |
| `proxy_error_count_threshold` | Alert if upstream/stream errors per model exceed this count in five minutes |
| `proxy_missing_usage_count_threshold` | Alert if responses without usage metadata per model exceed this count in five minutes |
| `proxy_ttft_p95_threshold_ms` | Alert if p95 time-to-first-token per model stays above this for five minutes |

### Tracing

| Variable | Description |
| --- | --- |
| `proxy_trace_enabled` | Enable span export |
| `proxy_trace_sampling_ratio` | Sampling fraction, between 0 and 1 |
| `proxy_trace_export_timeout_ms` | Export deadline and final flush wait |
| `proxy_trace_storage_location` | Location of the Cloud Trace observability bucket, for example `eu` |

## Terraform outputs

| Output | Meaning |
| --- | --- |
| `proxy_project_id`, `proxy_project_number` | Identifiers of the proxy project |
| `proxy_service_uri` | HTTPS URL of the Cloud Run service |
| `proxy_service_account_email` | Service account the proxy runs as |
| `custom_domain_url`, `custom_domain_dns_records` | Custom domain URL and the DNS records you must create |
| `proxy_invoker_identities` | Identities allowed to invoke the proxy |
| `usage_bigquery_dataset_id`, `usage_bigquery_table`, `usage_bigquery_view` | Where usage data lands and the reporting view |
| `usage_log_sink_name` | Cloud Logging sink exporting usage events |
| `notification_channels` | Monitoring notification channel IDs |
| `proxy_project_budget` | Billing budget resource name |
