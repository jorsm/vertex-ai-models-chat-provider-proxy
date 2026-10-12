SELECT
  timestamp AS logged_at,
  STRUCT(
    jsonPayload.event_id AS event_id,
    jsonPayload.request_id AS request_id,
    jsonPayload.trace_id AS trace_id,
    jsonPayload.provider_response_id AS provider_response_id
  ) AS event,
  STRUCT(
    jsonPayload.principal_subject AS principal_subject,
    jsonPayload.principal_email AS principal_email,
    jsonPayload.principal_display_name AS principal_display_name,
    jsonPayload.client_user_label AS client_user_label,
    jsonPayload.workspace_label AS workspace_label,
    jsonPayload.effective_labels_json AS effective_labels_json
  ) AS identity,
  STRUCT(
    jsonPayload.provider AS provider,
    jsonPayload.vertex_region AS vertex_region,
    jsonPayload.endpoint_action AS endpoint_action,
    jsonPayload.model_requested AS model_requested,
    jsonPayload.model_upstream AS model_upstream,
    -- Logging adds this optional field after the first effort-aware event. JSON extraction
    -- lets this view deploy before that addition and returns NULL for older rows.
    JSON_VALUE(TO_JSON_STRING(jsonPayload), '$.model_effort') AS model_effort,
    jsonPayload.model_family AS model_family,
    jsonPayload.model_version AS model_version,
    SAFE_CAST(jsonPayload.max_input_tokens AS INT64) AS max_input_tokens,
    SAFE_CAST(jsonPayload.max_output_tokens AS INT64) AS max_output_tokens,
    jsonPayload.model_metadata_json AS model_metadata_json
  ) AS model,
  STRUCT(
    SAFE_CAST(jsonPayload.request_started_at AS TIMESTAMP) AS request_started_at,
    SAFE_CAST(jsonPayload.response_completed_at AS TIMESTAMP) AS response_completed_at,
    SAFE_CAST(jsonPayload.duration_ms AS FLOAT64) AS duration_ms,
    SAFE_CAST(jsonPayload.time_to_first_chunk_ms AS FLOAT64) AS time_to_first_chunk_ms,
    SAFE_CAST(jsonPayload.time_to_first_token_ms AS FLOAT64) AS time_to_first_token_ms
  ) AS timing,
  STRUCT(
    SAFE_CAST(jsonPayload.http_status AS INT64) AS http_status,
    jsonPayload.outcome AS outcome,
    SAFE_CAST(jsonPayload.stream_completed AS BOOL) AS stream_completed,
    SAFE_CAST(jsonPayload.usage_complete AS BOOL) AS usage_complete
  ) AS result,
  STRUCT(
    SAFE_CAST(jsonPayload.input_tokens_total AS INT64) AS input_tokens_total,
    SAFE_CAST(jsonPayload.input_tokens_uncached AS INT64) AS input_tokens_uncached,
    SAFE_CAST(jsonPayload.cache_read_tokens AS INT64) AS cache_read_tokens,
    SAFE_CAST(jsonPayload.cache_write_tokens AS INT64) AS cache_write_tokens,
    SAFE_CAST(jsonPayload.cache_write_5m_tokens AS INT64) AS cache_write_5m_tokens,
    SAFE_CAST(jsonPayload.cache_write_1h_tokens AS INT64) AS cache_write_1h_tokens,
    SAFE_CAST(jsonPayload.output_tokens AS INT64) AS output_tokens,
    SAFE_CAST(jsonPayload.thinking_tokens AS INT64) AS thinking_tokens,
    SAFE_CAST(jsonPayload.tool_use_tokens AS INT64) AS tool_use_tokens,
    SAFE_CAST(jsonPayload.billable_output_tokens AS INT64) AS billable_output_tokens,
    SAFE_CAST(jsonPayload.total_tokens AS INT64) AS total_tokens,
    jsonPayload.provider_usage_json AS provider_usage_json
  ) AS tokens,
  STRUCT(
    jsonPayload.currency AS currency,
    jsonPayload.pricing_source AS pricing_source,
    jsonPayload.pricing_rule_id AS pricing_rule_id,
    SAFE_CAST(jsonPayload.input_price_per_million AS NUMERIC) AS input_price_per_million,
    SAFE_CAST(jsonPayload.output_price_per_million AS NUMERIC) AS output_price_per_million,
    SAFE_CAST(jsonPayload.cache_read_price_per_million AS NUMERIC) AS cache_read_price_per_million,
    SAFE_CAST(jsonPayload.cache_write_price_per_million AS NUMERIC) AS cache_write_price_per_million
  ) AS pricing,
  STRUCT(
    SAFE_CAST(jsonPayload.estimated_input_cost_decimal AS NUMERIC) AS estimated_input_cost,
    SAFE_CAST(jsonPayload.estimated_output_cost_decimal AS NUMERIC) AS estimated_output_cost,
    SAFE_CAST(jsonPayload.estimated_cache_read_cost_decimal AS NUMERIC) AS estimated_cache_read_cost,
    SAFE_CAST(jsonPayload.estimated_cache_write_cost_decimal AS NUMERIC) AS estimated_cache_write_cost,
    SAFE_CAST(jsonPayload.estimated_total_cost_decimal AS NUMERIC) AS estimated_total_cost
  ) AS costs,
  STRUCT(
    SAFE_CAST(jsonPayload.schema_version AS INT64) AS schema_version,
    jsonPayload.service_version AS service_version
  ) AS versions
FROM `${source_table}`
