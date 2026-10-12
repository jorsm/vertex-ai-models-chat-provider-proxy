# 8. Observability and usage analytics <!-- omit in toc -->

```text
Proxy -> OpenTelemetry spans -> Cloud Trace
      -> JSON on stdout      -> Cloud Logging
                                  |-> log-based metrics, dashboard, alerts
                                  |-> dedicated sink -> BigQuery table -> reporting view
```

- [The usage event](#the-usage-event)
- [BigQuery](#bigquery)
- [Metrics, dashboard and alerts](#metrics-dashboard-and-alerts)
- [Cloud Trace](#cloud-trace)
- [Verification](#verification)
- [Vertex AI audit logs](#vertex-ai-audit-logs)
- [Running locally](#running-locally)

The proxy never calls BigQuery. It writes structured JSON to stdout; Cloud Logging routes and aggregates it. For every inference it handles the proxy emits exactly one `event_kind = inference_completed` event, including authentication/governance rejections, upstream failures and interrupted streams. Health checks, discovery and unknown paths are excluded. If the process is killed before the final log is written, no event is produced; platform logs and metrics still cover that case.

## The usage event

Fields are scalars directly queryable under `jsonPayload`. Only variable-shaped objects are serialized as JSON strings: `effective_labels_json`, `model_metadata_json`, `provider_usage_json`. Prompts, generated text and tool payloads are never included.

| Group | Examples |
| --- | --- |
| Event and correlation | `event_id`, `request_id`, `trace_id`, `provider_response_id`, `schema_version` (4), `request_started_at`, `response_completed_at` |
| Identity and workspace | `principal_email`, `principal_subject`, `principal_display_name`, `client_user_label`, `workspace_label` |
| Provider and model | `provider`, `model_requested`, `model_upstream`, `model_effort`, `model_family`, `model_version`, `vertex_region`, `endpoint_action`, `max_input_tokens`, `max_output_tokens` |
| Timing | `duration_ms`, `time_to_first_chunk_ms`, `time_to_first_token_ms` |
| Outcome | `http_status`, `outcome`, `stream_completed`, `usage_complete` |
| Tokens | `input_tokens_total`, `input_tokens_uncached`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` (and `_5m`/`_1h`), `thinking_tokens`, `tool_use_tokens`, `billable_output_tokens`, `total_tokens` |
| Pricing | `currency`, `input_price_per_million`, `output_price_per_million`, `cache_read_price_per_million`, `cache_write_price_per_million`, `pricing_source`, `pricing_rule_id` |
| Estimated cost | `estimated_input_cost`, `estimated_output_cost`, `estimated_cache_read_cost`, `estimated_cache_write_cost`, `estimated_total_cost` |
| Technical | `event_kind`, `telemetry_schema_version`, `service_version` |

`outcome` is one of `completed`, `usage_missing`, `upstream_error`, `upstream_connection_error`, `stream_error`, `client_disconnected`, `authentication_rejected`, `governance_rejected`, `context_limit_exceeded`.

Notes on semantics:

- **Missing values are omitted** (BigQuery `NULL`); zero is kept.
- `model_effort` is the effective top-level named effort after validation and default injection, normalized to lowercase. It is a bounded scalar from the model's declared choices and is nullable for requests without an applicable validated effort (including explicit `countTokens`). The normalized view exposes it under `model.model_effort`; message-level overrides stay in the request and are not logged individually.
- `estimated_*_cost` are floating-point numbers for metrics. The matching `estimated_*_cost_decimal` fields hold the exact decimal string; prices (`*_price_per_million`) are decimal strings too. Use `SAFE_CAST(... AS NUMERIC)` for accounting queries.
- Costs are **estimates** computed from the catalog pricing and the provider's reported usage. They are not invoiced amounts: reconcile with Cloud Billing.
- `principal_display_name` is the `name` claim of the verified token and may be absent.
- `duration_ms` runs until the response has been consumed. `time_to_first_chunk_ms` is the first upstream body chunk. `time_to_first_token_ms` is the first complete SSE event with Gemini or Claude content (including thinking and tool calls); heartbeats and metadata are ignored, and it is absent for non-streaming responses.
- Rejected requests use `unknown` as the model in metrics, so unvalidated client input never becomes a metric label.

Upstream HTTP errors (4xx/5xx) additionally produce a separate log with `event_kind = upstream_error_response`, `http_status`, trace/span IDs and `invocation_event_id` (to join with the usage event). It contains the JSON error returned to the client (`error_response`) or the text body when it is not JSON (`error_response_text`), capped at 128 KiB (`error_response_bytes`, `error_response_truncated`, `error_response_complete`).

## BigQuery

A Cloud Logging sink exports the proxy's stdout events that carry an `event_id` into a day-partitioned table, `run_googleapis_com_stdout`, in the usage dataset. The raw table keeps application fields under `jsonPayload` (timestamps are ISO strings).

The view **`ai_usage_events_normalized`** presents the same data grouped into nine STRUCTs: `event`, `identity`, `model`, `timing`, `result`, `tokens`, `pricing`, `costs`, `versions`, plus `logged_at`. It projects, builds STRUCTs and applies `SAFE_CAST` (costs and prices to `NUMERIC`, counts to `INT64`, application timestamps to `TIMESTAMP`, latencies to `FLOAT64`). Optional `model_effort` uses JSON extraction to tolerate older raw schemas. The view does not join or deduplicate.

Export is asynchronous and at-least-once, so the table can contain duplicates: deduplicate on `event_id` when accuracy matters, and always filter by time to limit the partitions scanned. Events logged before the sink existed are not back-filled.

```sql
SELECT
  logged_at,
  event.event_id,
  model.model_upstream,
  identity.principal_email,
  identity.workspace_label,
  tokens.total_tokens,
  costs.estimated_total_cost
FROM `MY_PROJECT.ai_usage.ai_usage_events_normalized`
WHERE logged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)
QUALIFY ROW_NUMBER() OVER (PARTITION BY event.event_id ORDER BY logged_at DESC) = 1
```

Objects stored as JSON strings can be read with `JSON_VALUE`, `JSON_QUERY` or `SAFE.PARSE_JSON` on the `*_json` fields.

Terraform bootstraps all report columns in the raw table schema so the view can be created before the first event. Cloud Logging adds further platform fields on its own; Terraform ignores those schema additions. If you reuse a table created with an older, narrower schema, add the missing nullable columns (for example with `bq update`) before applying the view.

## Metrics, dashboard and alerts

Log-based metrics (prefix `ai_proxy_`), labelled by upstream model, provider, region and outcome:

- **Counters:** requests, errors, rejections, interrupted streams, missing usage.
- **Distributions:** duration, time to first chunk, time to first token, each token counter, and the estimated cost components in USD.

The **dashboard** "Google Agent Platform for Copilot Chat - Proxy — Usage and Reliability" charts five-minute counts, p50/p95 latencies and mean tokens and cost per invocation. Email addresses, workspaces and IDs stay in the full payload and are not metric labels.

| Alert | Variable | Condition |
| --- | --- | --- |
| Errors | `proxy_error_count_threshold` | More than N upstream/stream errors per model in five minutes |
| Missing usage | `proxy_missing_usage_count_threshold` | More than N responses without usage per model in five minutes |
| Time to first token | `proxy_ttft_p95_threshold_ms` | p95 above the threshold for five minutes |
| Sink export errors | n/a | Cloud Logging reports errors exporting usage logs (`logging.googleapis.com/exports/error_count`) |

Alerts notify `notification_emails`. With no emails, incidents remain visible in Cloud Monitoring. The sink alert cannot detect logs that were never emitted; also check `sink_error` logs and any `export_errors` tables in the dataset.

## Cloud Trace

OpenTelemetry spans are exported to Cloud Trace, never including prompts, responses, tokens, emails or exception messages and stacks. Terraform enables the Trace APIs, creates the `_Trace` observability bucket in `proxy_trace_storage_location`, and grants `roles/cloudtrace.agent` to the service account.

In the Google Cloud console open **Trace > Trace Explorer**, choose the proxy project, and look for the span `proxy.inference` or the service `copilot-chat-proxy`.

| Span | Measures |
| --- | --- |
| `proxy.inference` | The whole request, through the end of streaming and usage finalization |
| `authentication` | Caller identity validation |
| `request.parsing` | Reading the body and parsing JSON |
| `governance` | Allowlists, label validation and injection |
| `request.encoding` | Body serialization and header preparation |
| `upstream` | Preparing the call and receiving upstream headers |
| `upstream.credentials` | Reading or refreshing the service-account token |
| `upstream.client` | Acquiring the shared connection pool |
| `upstream.request_preparation` | Routing, headers and request construction |
| `upstream.response_headers` | HTTP send and wait for Vertex AI headers |
| `upstream.connect_tcp` | TCP connect including DNS (only when a connection is opened) |
| `upstream.tls` | TLS handshake (absent when a connection is reused) |
| `upstream.send_headers`, `upstream.send_body` | Sending request headers and payload |
| `upstream.wait_response_headers` | Waiting for response headers after sending |
| `upstream.first_chunk_wait` | Reading the first non-empty body chunk |
| `upstream.streaming` | Consuming the stream, including waits between chunks; carries `first_chunk` and `first_content` events |
| `upstream.response_close` | Closing the response and releasing the connection |
| `usage.finalization` | Usage normalization, catalog lookup, cost estimate and final log |

Parent spans include their children's time, so durations must not be summed. The wait for a free connection from the pool has no span of its own, and time spent inside Vertex AI cannot be separated from network time with proxy-side instrumentation alone. No span is created per chunk, to keep overhead low.

Log entries carry the active trace and span IDs, and the same trace ID is in the BigQuery view. A W3C `traceparent` header takes precedence over `X-Cloud-Trace-Context`; if neither is valid a new trace starts. Health, discovery and OpenAPI requests produce no application spans.

Tracing settings:

- `proxy_trace_enabled`: master switch for export.
- `proxy_trace_sampling_ratio`: fraction of inference traces exported (1 collects all). The local ratio applies even if the incoming context is marked unsampled.
- `proxy_trace_export_timeout_ms`: RPC deadline and the maximum wait for the final flush. Batches are exported asynchronously, and the proxy flushes before closing the response because Cloud Run can throttle background CPU once a response completes. The flush can add up to the timeout to the end of a response but does not lengthen spans that already ended.

Initialization or export failures are logged as warnings and never fail the request. Trace complements, and does not replace, logs, log-based metrics and the BigQuery export.

## Verification

After deploying, find the event in Cloud Logging:

```text
resource.type="cloud_run_revision"
resource.labels.service_name="copilot-chat-proxy"
jsonPayload.component="copilot-chat-proxy"
jsonPayload.event_kind="inference_completed"
jsonPayload.event_id:*
```

then confirm the row in BigQuery and that no sink errors are reported. Metrics and export can take a few minutes to appear.

## Vertex AI audit logs

This setup does not configure Vertex AI Data Access audit logs or a sink for them. Google audit logs describe API calls and IAM checks (including direct calls made around the proxy) and are not equivalent to the application events above. Admin Activity audit logs are always on, and Data Access logs enabled at the folder or organization level continue to apply.

## Running locally

Set `TRACE_ENABLED=false`, or set all three `TRACE_*` variables and use credentials with Cloud Trace permission. The test suite uses an in-memory exporter and sends no spans.
