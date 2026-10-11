# Outputs: identifiers and URLs of the hub project and the proxy, DNS records for the custom domain, notification channels and usage analytics objects.

#
# ------------------ OUTPUTS ------------------
#

output "hub_project_id" {
  description = "Project ID of the central Hub project hosting the Google Agent Platform for Copilot Chat - Proxy"
  value       = google_project.hub_project.project_id
}

output "hub_project_number" {
  description = "Project Number of the central Hub project"
  value       = google_project.hub_project.number
}

output "proxy_service_uri" {
  description = "Public HTTPS URI of the deployed Google Agent Platform for Copilot Chat - Proxy Cloud Run service"
  value       = google_cloud_run_v2_service.copilot_chat_proxy.uri
}

output "proxy_service_account_email" {
  description = "Email of the Service Account used by the Google Agent Platform for Copilot Chat - Proxy"
  value       = google_service_account.proxy_sa.email
}

output "usage_bigquery_table" {
  description = "Fully-qualified usage log table populated by the Cloud Logging sink"
  value       = "${google_project.hub_project.project_id}.${google_bigquery_dataset.ai_usage.dataset_id}.${google_bigquery_table.ai_usage_logs.table_id}"
}

output "usage_bigquery_view" {
  description = "Fully-qualified usage report view with named columns and numeric/timestamp casts"
  value       = "${google_project.hub_project.project_id}.${google_bigquery_dataset.ai_usage.dataset_id}.${google_bigquery_table.ai_usage_events_normalized.table_id}"
}

output "usage_log_sink_name" {
  description = "Cloud Logging sink exporting proxy usage events"
  value       = google_logging_project_sink.usage_bigquery.name
}

output "usage_bigquery_dataset_id" {
  description = "Dedicated BigQuery dataset containing near-real-time usage analytics"
  value       = google_bigquery_dataset.ai_usage.dataset_id
}

output "notification_channels" {
  description = "Map of email addresses to their Cloud Monitoring notification channel IDs"
  value = {
    for email, channel in google_monitoring_notification_channel.notification_email : email => channel.id
  }
}

output "hub_project_budget" {
  description = "Billing Budget resource name for the central Hub project"
  value       = google_billing_budget.hub_project_budget.name
}

output "proxy_invoker_identities" {
  description = "List of developer emails authorized to invoke the Google Agent Platform for Copilot Chat - Proxy"
  value       = var.proxy_invoker_identities
}

output "custom_domain_url" {
  description = "Custom domain HTTPS URL for the Google Agent Platform for Copilot Chat - Proxy"
  value       = "https://${var.custom_domain}"
}

output "custom_domain_dns_records" {
  description = "DNS resource records that must be configured for the custom domain mapping"
  value       = try(google_cloud_run_domain_mapping.proxy_domain_mapping.status[0].resource_records, [])
}

