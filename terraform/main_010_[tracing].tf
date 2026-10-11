############################################################
# CLOUD TRACE STORAGE
############################################################
# Cloud Trace storage: the observability bucket that holds the spans exported by the proxy.

resource "google_observability_bucket" "proxy_traces" {
  project      = google_project.hub_project.project_id
  location     = var.proxy_trace_storage_location
  bucket_id    = "_Trace"
  display_name = "AI Companion traces"
  description  = "Application spans for AI Companion Proxy component latency"
  depends_on   = [google_project_service.hub_services]
}
