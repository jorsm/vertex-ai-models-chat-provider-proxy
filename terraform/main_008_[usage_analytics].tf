############################################################
# NEAR-REAL-TIME USAGE ANALYTICS
############################################################
# Usage analytics pipeline: BigQuery dataset and table, the Cloud Logging sink that exports inference usage events to BigQuery, and the normalized reporting view.

resource "google_bigquery_dataset" "ai_usage" {
  depends_on = [
    google_project_service.hub_services
  ]

  project       = google_project.hub_project.project_id
  dataset_id    = var.usage_bigquery_dataset_id
  friendly_name = "AI Usage Analytics"
  description   = "Near-real-time Vertex AI token usage and estimated costs"
  location      = var.bigquery_dataset_location

  labels = {
    managed_by = "terraform"
    project    = "ai-companion"
    role       = "usage-analytics"
  }
}

resource "google_bigquery_table" "ai_usage_logs" {
  project    = google_project.hub_project.project_id
  dataset_id = google_bigquery_dataset.ai_usage.dataset_id
  # Logging derives this name from run.googleapis.com/stdout.
  table_id      = "run_googleapis_com_stdout"
  friendly_name = "AI usage logs routed by Cloud Logging"
  description   = "Usage LogEntry export with queryable scalar fields and serialized variable metadata"

  time_partitioning {
    type  = "DAY"
    field = "timestamp"
  }

  # Bootstrap all report columns so the view works before the first usage log.
  # Keep application fields grouped by context, matching the normalized view.
  # Logging manages subsequent schema additions, including numeric metrics.
  schema = file("${path.module}/schemas/ai_usage_logs.json")

  lifecycle {
    ignore_changes  = [schema]
    prevent_destroy = true
  }
}

resource "google_logging_project_sink" "usage_bigquery" {
  project                = google_project.hub_project.project_id
  name                   = "sink-proxy-usage-to-bq"
  destination            = "bigquery.googleapis.com/projects/${google_project.hub_project.project_id}/datasets/${google_bigquery_dataset.ai_usage.dataset_id}"
  unique_writer_identity = true
  filter                 = "${local.proxy_log_filter}\nlog_id(\"run.googleapis.com/stdout\")\njsonPayload.event_id:*"

  bigquery_options {
    use_partitioned_tables = true
  }
  depends_on = [google_bigquery_table.ai_usage_logs]
}

resource "google_bigquery_dataset_iam_member" "usage_sink_writer" {
  project    = google_project.hub_project.project_id
  dataset_id = google_bigquery_dataset.ai_usage.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.usage_bigquery.writer_identity
}

resource "google_bigquery_table" "ai_usage_events_normalized" {
  project       = google_project.hub_project.project_id
  dataset_id    = google_bigquery_dataset.ai_usage.dataset_id
  table_id      = "ai_usage_events_normalized"
  friendly_name = "AI usage report"
  description   = "Usage fields grouped into context STRUCTs with type casts; no JSON parsing or deduplication"

  view {
    use_legacy_sql = false
    query = templatefile("${path.module}/sql/ai_usage_events_normalized.sql", {
      source_table = "${google_project.hub_project.project_id}.${google_bigquery_dataset.ai_usage.dataset_id}.${google_bigquery_table.ai_usage_logs.table_id}"
    })
  }
}
