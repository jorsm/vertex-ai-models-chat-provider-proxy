############################################################
# SERVICES
############################################################
# Enables the Google Cloud APIs the proxy project needs: Vertex AI, Cloud Run, Cloud Build, logging, monitoring, tracing, BigQuery, billing budgets and their dependencies.

locals {
  proxy_services = [
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "iam.googleapis.com",
    "aiplatform.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudtrace.googleapis.com",
    "telemetry.googleapis.com",
    "observability.googleapis.com",
    "bigquery.googleapis.com",
    "billingbudgets.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "storage.googleapis.com",
    "iap.googleapis.com",
    "firestore.googleapis.com"
  ]
}

# Enable required APIs on the proxy project
resource "google_project_service" "proxy_services" {
  for_each = toset(local.proxy_services)

  project                    = google_project.proxy_project.project_id
  service                    = each.key
  disable_dependent_services = false
  disable_on_destroy         = false
}


