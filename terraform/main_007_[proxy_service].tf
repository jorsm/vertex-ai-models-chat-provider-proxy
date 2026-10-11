############################################################
# CLOUD RUN SERVICE: AI COMPANION PROXY
############################################################
# Cloud Run service that runs the proxy: runtime settings passed as environment variables, invoker IAM bindings for the authorized identities and the custom domain mapping.
# The service starts with a placeholder image; the real image is deployed by api/deploy.sh or api/deploy.ps1 and ignored by Terraform afterwards.

resource "google_cloud_run_v2_service" "ai_companion_proxy" {
  depends_on = [
    google_project_service.hub_services,
    google_project_iam_member.proxy_sa_vertex_user,
    google_project_iam_member.proxy_sa_log_writer,
    google_project_iam_member.proxy_sa_trace_writer,
    google_observability_bucket.proxy_traces
  ]

  name        = local.proxy_service_name
  location    = var.region
  project     = google_project.hub_project.project_id
  description = "Centralized Vertex AI Companion Proxy Relay"
  ingress     = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.proxy_sa.email
    timeout                          = "300s"
    max_instance_request_concurrency = 8

    scaling {
      max_instance_count = 20
      min_instance_count = 0
    }

    labels = {
      service_name = local.proxy_service_name
    }

    containers {
      image = "gcr.io/cloudrun/hello"

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "HUB_PROJECT_ID"
        value = google_project.hub_project.project_id
      }
      env {
        name  = "ALLOWED_VERTEX_PROJECTS"
        value = join(",", local.proxy_vertex_projects)
      }
      env {
        name  = "ALLOWED_REGIONS"
        value = join(",", var.allowed_vertex_regions)
      }
      env {
        name  = "ALLOWED_MODELS"
        value = join(",", var.allowed_models)
      }
      env {
        name  = "USER_LABEL_KEY"
        value = var.user_label_key
      }
      env {
        name  = "PROJECT_LABEL_KEY"
        value = var.project_label_key
      }
      env {
        name  = "AUTH_USER_LABEL_KEY"
        value = var.auth_user_label_key
      }
      env {
        name  = "REQUIRE_PROJECT_LABEL"
        value = tostring(var.require_project_label)
      }
      env {
        name  = "REQUIRE_USER_LABEL"
        value = tostring(var.require_user_label)
      }
      env {
        name  = "MODELS_CATALOG_URL"
        value = var.models_catalog_url
      }
      env {
        name  = "PROXY_BASE_URL"
        value = "https://${var.custom_domain}"
      }
      env {
        name  = "LOG_EXECUTION_ID"
        value = "true"
      }
      env {
        name  = "MAX_CONTEXT_TOKENS"
        value = tostring(var.max_context_tokens)
      }
      env {
        name  = "DISCOVERY_MAX_INPUT_TOKENS"
        value = tostring(var.discovery_max_input_tokens)
      }
      env {
        name  = "TRACE_ENABLED"
        value = tostring(var.proxy_trace_enabled)
      }
      env {
        name  = "TRACE_SAMPLING_RATIO"
        value = tostring(var.proxy_trace_sampling_ratio)
      }
      env {
        name  = "TRACE_EXPORT_TIMEOUT_MS"
        value = tostring(var.proxy_trace_export_timeout_ms)
      }
    }
  }

  labels = {
    managed_by   = "terraform"
    project      = "ai-companion"
    role         = "proxy"
    service_name = local.proxy_service_name
  }

  lifecycle {
    # Source builds are deployed by api/deploy.*; Terraform retains the service
    # configuration while ignoring only the resulting image/build metadata.
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
      build_config,
    ]
  }
}

# Grant Cloud Run Invoker on the proxy to each authorized user.
resource "google_cloud_run_v2_service_iam_member" "proxy_invokers" {
  for_each = toset(var.proxy_invoker_identities)

  project  = google_project.hub_project.project_id
  location = var.region
  name     = google_cloud_run_v2_service.ai_companion_proxy.name
  role     = "roles/run.invoker"
  member   = each.value
}

# Custom domain mapping on Cloud Run for the AI Companion Proxy.
resource "google_cloud_run_domain_mapping" "proxy_domain_mapping" {
  depends_on = [google_cloud_run_v2_service.ai_companion_proxy]

  location = var.region
  project  = google_project.hub_project.project_id
  name     = var.custom_domain

  metadata {
    namespace = google_project.hub_project.project_id
  }

  spec {
    route_name = google_cloud_run_v2_service.ai_companion_proxy.name
  }

  lifecycle {
    ignore_changes = [
      metadata[0].annotations,
    ]
  }
}
