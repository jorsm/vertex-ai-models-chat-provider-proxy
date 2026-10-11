############################################################
# IAM CONFIGURATION & PROXY SERVICE ACCOUNT
############################################################
# Creates the proxy runtime service account and grants it its roles: Vertex AI User on the proxy project (when the proxy project is an allowed Vertex project), Log Writer and Cloud Trace Agent.
# Also resolves the list of Vertex AI projects the proxy accepts; IAM on other projects is managed outside Terraform.

# Dedicated Service Account for the Google Agent Platform for Copilot Chat - Proxy Cloud Run service
resource "google_service_account" "proxy_sa" {
  depends_on = [
    google_project_service.proxy_services
  ]

  project      = google_project.proxy_project.project_id
  account_id   = "sa-copilot-chat-proxy"
  display_name = "Google Agent Platform for Copilot Chat - Proxy Service Account"
  description  = "Service Account used by the Google Agent Platform for Copilot Chat - Proxy Cloud Run service to invoke Vertex AI APIs"
}

# Vertex AI projects the proxy accepts: the proxy project unless var.allowed_vertex_projects lists others.
# IAM on projects other than the proxy project is NOT managed here: grant roles/aiplatform.user to the proxy
# service account (output proxy_service_account_email) on each additional project yourself.
locals {
  proxy_vertex_projects = length(var.allowed_vertex_projects) == 0 ? [google_project.proxy_project.project_id] : var.allowed_vertex_projects
}

# Grant Vertex AI User role to the Proxy Service Account on the proxy project, only when the proxy project is an allowed Vertex project
resource "google_project_iam_member" "proxy_sa_vertex_user" {
  count = contains(local.proxy_vertex_projects, google_project.proxy_project.project_id) ? 1 : 0

  project = google_project.proxy_project.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# Grant Log Writer role to the Proxy Service Account on the proxy project
resource "google_project_iam_member" "proxy_sa_log_writer" {
  project = google_project.proxy_project.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# Export application spans using the runtime Service Account.
resource "google_project_iam_member" "proxy_sa_trace_writer" {
  project    = google_project.proxy_project.project_id
  role       = "roles/cloudtrace.agent"
  member     = "serviceAccount:${google_service_account.proxy_sa.email}"
  depends_on = [google_project_service.proxy_services]
}
