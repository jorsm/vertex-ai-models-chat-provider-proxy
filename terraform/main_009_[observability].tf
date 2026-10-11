############################################################
# PROXY LOG-BASED METRICS, DASHBOARD AND OPERATIONAL ALERTS
############################################################
# Observability: log-based metrics derived from the proxy logs, the Cloud Monitoring dashboard and the alert policies for proxy errors, missing usage data, latency and usage sink export failures.

locals {
  proxy_service_name = "ai-companion-proxy"
  proxy_log_filter   = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${local.proxy_service_name}"
    jsonPayload.component="ai-companion-proxy"
    jsonPayload.event_kind="inference_completed"
    jsonPayload.telemetry_schema_version=1
  EOT

  proxy_metric_labels = {
    model    = "model_upstream"
    provider = "provider"
    region   = "vertex_region"
    outcome  = "outcome"
  }

  proxy_counter_metrics = {
    requests            = { description = "Terminal inference invocations, including rejected requests", filter = "" }
    errors              = { description = "Upstream HTTP, connection and stream errors", filter = "(jsonPayload.outcome=\"upstream_error\" OR jsonPayload.outcome=\"upstream_connection_error\" OR jsonPayload.outcome=\"stream_error\")" }
    rejections          = { description = "Authentication and governance rejections", filter = "(jsonPayload.outcome=\"authentication_rejected\" OR jsonPayload.outcome=\"governance_rejected\")" }
    interrupted_streams = { description = "Client disconnects and stream failures", filter = "(jsonPayload.outcome=\"client_disconnected\" OR jsonPayload.outcome=\"stream_error\")" }
    missing_usage       = { description = "Successfully consumed responses without provider usage", filter = "jsonPayload.outcome=\"usage_missing\"" }
  }

  proxy_distribution_metrics = merge(
    {
      duration_ms            = { unit = "ms", scale = 1, description = "Inference duration through response consumption; excludes usage export" }
      time_to_first_chunk_ms = { unit = "ms", scale = 1, description = "Time to first upstream body chunk" }
      time_to_first_token_ms = { unit = "ms", scale = 1, description = "Time to first decoded SSE content, including thinking or tool content; absent for non-streaming responses" }
    },
    { for field in [
      "input_tokens_total", "input_tokens_uncached", "output_tokens",
      "cache_read_tokens", "cache_write_tokens", "thinking_tokens",
      "tool_use_tokens", "billable_output_tokens", "total_tokens"
      ] : field => {
      unit = "1", scale = 1, description = "Provider ${field} per invocation; unknown usage excluded"
    } },
    { for field in [
      "estimated_input_cost", "estimated_output_cost", "estimated_cache_read_cost",
      "estimated_cache_write_cost", "estimated_total_cost"
      ] : field => {
      unit = "{USD}", scale = 0.000001, description = "Catalog-estimated ${field} in USD per invocation, not invoiced spend"
    } }
  )

  proxy_dashboard_charts = concat(
    [for key, spec in local.proxy_counter_metrics : {
      key = key, title = spec.description, aligner = "ALIGN_SUM", reducer = "REDUCE_SUM"
    }],
    [for key in ["duration_ms", "time_to_first_chunk_ms", "time_to_first_token_ms"] : {
      key = key, title = "${key} — p50", aligner = "ALIGN_SUM", reducer = "REDUCE_PERCENTILE_50"
    }],
    [for key in ["duration_ms", "time_to_first_chunk_ms", "time_to_first_token_ms"] : {
      key = key, title = "${key} — p95", aligner = "ALIGN_SUM", reducer = "REDUCE_PERCENTILE_95"
    }],
    [for key in ["total_tokens", "input_tokens_total", "output_tokens", "cache_read_tokens", "estimated_total_cost"] : {
      key = key, title = "${key} — mean per invocation", aligner = "ALIGN_SUM", reducer = "REDUCE_MEAN"
    }]
  )

  proxy_alerts = {
    errors                 = { threshold = var.proxy_error_count_threshold, description = "Upstream or stream errors in a five-minute interval" }
    missing_usage          = { threshold = var.proxy_missing_usage_count_threshold, description = "Responses missing usage in a five-minute interval" }
    time_to_first_token_ms = { threshold = var.proxy_ttft_p95_threshold_ms, description = "First SSE content p95 exceeded the configured threshold" }
  }
}

# Export delivery belongs to Logging, not to the application. This platform
# metric counts errors for this sink, including routing/schema failures.
resource "google_monitoring_alert_policy" "usage_sink_export_errors" {
  project      = google_project.hub_project.project_id
  display_name = "AI Companion Proxy - usage sink export errors"
  combiner     = "OR"
  notification_channels = [
    for channel in google_monitoring_notification_channel.notification_email : channel.id
  ]

  documentation {
    mime_type = "text/markdown"
    content   = "Cloud Logging failed to export usage logs. Check sink-proxy-usage-to-bq permissions, sink_error logs and export_errors tables in the usage dataset. The proxy cannot acknowledge asynchronous delivery."
  }

  conditions {
    display_name = "Usage log export errors in five minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/exports/error_count\" resource.type=\"logging_sink\" resource.labels.name=\"${google_logging_project_sink.usage_bigquery.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger { count = 1 }
    }
  }
  alert_strategy { auto_close = "1800s" }
  depends_on = [google_bigquery_dataset_iam_member.usage_sink_writer]
}

resource "google_logging_metric" "proxy_counter" {
  for_each = local.proxy_counter_metrics

  project     = google_project.hub_project.project_id
  name        = "ai_proxy_${each.key}"
  description = each.value.description
  filter      = "${local.proxy_log_filter}\n${each.value.filter}"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"

    dynamic "labels" {
      for_each = local.proxy_metric_labels
      content {
        key         = labels.key
        value_type  = "STRING"
        description = "Bounded ${labels.key} dimension"
      }
    }
  }
  label_extractors = { for key, field in local.proxy_metric_labels : key => "EXTRACT(jsonPayload.${field})" }
  depends_on       = [google_project_service.hub_services]
}

resource "google_logging_metric" "proxy_distribution" {
  for_each = local.proxy_distribution_metrics

  project         = google_project.hub_project.project_id
  name            = "ai_proxy_${each.key}"
  description     = each.value.description
  filter          = "${local.proxy_log_filter}\njsonPayload.${each.key}>=0"
  value_extractor = "EXTRACT(jsonPayload.${each.key})"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = each.value.unit

    dynamic "labels" {
      for_each = local.proxy_metric_labels
      content {
        key         = labels.key
        value_type  = "STRING"
        description = "Bounded ${labels.key} dimension"
      }
    }
  }
  label_extractors = { for key, field in local.proxy_metric_labels : key => "EXTRACT(jsonPayload.${field})" }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 80
      growth_factor      = 1.25
      scale              = each.value.scale
    }
  }
  depends_on = [google_project_service.hub_services]
}

resource "google_monitoring_dashboard" "proxy_observability" {
  project = google_project.hub_project.project_id
  dashboard_json = jsonencode({
    displayName = "AI Companion Proxy — Usage and Reliability"
    gridLayout = {
      # The Monitoring API serializes this int64 field as a JSON string.
      columns = "2"
      widgets = [for chart in local.proxy_dashboard_charts : {
        title = chart.title
        xyChart = {
          chartOptions = { mode = "COLOR" }
          dataSets = [{
            plotType   = "LINE"
            targetAxis = "Y1"
            timeSeriesQuery = {
              timeSeriesFilter = {
                filter = "metric.type=\"logging.googleapis.com/user/ai_proxy_${chart.key}\" resource.type=\"cloud_run_revision\""
                aggregation = {
                  alignmentPeriod    = "300s"
                  perSeriesAligner   = chart.aligner
                  crossSeriesReducer = chart.reducer
                  groupByFields      = ["metric.label.model"]
                }
              }
            }
          }]
        }
      }]
    }
  })
  depends_on = [google_logging_metric.proxy_counter, google_logging_metric.proxy_distribution]
}

resource "google_monitoring_alert_policy" "proxy_operational" {
  for_each = local.proxy_alerts

  project      = google_project.hub_project.project_id
  display_name = "AI Companion Proxy — ${each.key}"
  combiner     = "OR"
  enabled      = true
  notification_channels = [
    for channel in google_monitoring_notification_channel.notification_email : channel.id
  ]

  documentation {
    mime_type = "text/markdown"
    content   = "${each.value.description}. Inspect the AI Companion Proxy dashboard and inference_completed logs. Cost metrics are estimates; BigQuery retains per-invocation details."
  }

  conditions {
    display_name = each.value.description
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/ai_proxy_${each.key}\" resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.threshold
      duration        = each.key == "time_to_first_token_ms" ? "300s" : "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = each.key == "time_to_first_token_ms" ? "REDUCE_PERCENTILE_95" : "REDUCE_SUM"
        group_by_fields      = ["metric.label.model"]
      }
      trigger { count = 1 }
    }
  }

  alert_strategy { auto_close = "1800s" }
  depends_on = [google_logging_metric.proxy_counter, google_logging_metric.proxy_distribution]
}
