############################################################
# IAM CONFIGURATION & PROXY SERVICE ACCOUNT
############################################################
# Creates the proxy runtime service account and grants it its roles: Vertex AI User on the hub project (when the hub is an allowed Vertex project), Log Writer and Cloud Trace Agent.
# Also resolves the list of Vertex AI projects the proxy accepts; IAM on other projects is managed outside Terraform.

# Dedicated Service Account for the AI Companion Proxy Cloud Run service
resource "google_service_account" "proxy_sa" {
  depends_on = [
    google_project_service.hub_services
  ]

  project      = google_project.hub_project.project_id
  account_id   = "sa-ai-companion-proxy"
  display_name = "AI Companion Proxy Service Account"
  description  = "Service Account used by the AI Companion Proxy Cloud Run service to invoke Vertex AI APIs"
}

# Vertex AI projects the proxy accepts: the hub project unless var.allowed_vertex_projects lists others.
# IAM on projects other than the hub is NOT managed here: grant roles/aiplatform.user to the proxy
# service account (output proxy_service_account_email) on each additional project yourself.
locals {
  proxy_vertex_projects = length(var.allowed_vertex_projects) == 0 ? [google_project.hub_project.project_id] : var.allowed_vertex_projects
}

# Grant Vertex AI User role to the Proxy Service Account on the Hub project, only when the hub is an allowed Vertex project
resource "google_project_iam_member" "proxy_sa_vertex_user" {
  count = contains(local.proxy_vertex_projects, google_project.hub_project.project_id) ? 1 : 0

  project = google_project.hub_project.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# Grant Log Writer role to the Proxy Service Account on the Hub project
resource "google_project_iam_member" "proxy_sa_log_writer" {
  project = google_project.hub_project.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# Export application spans using the runtime Service Account.
resource "google_project_iam_member" "proxy_sa_trace_writer" {
  project    = google_project.hub_project.project_id
  role       = "roles/cloudtrace.agent"
  member     = "serviceAccount:${google_service_account.proxy_sa.email}"
  depends_on = [google_project_service.hub_services]
}
