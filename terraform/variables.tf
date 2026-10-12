# Input variables. None has a default: every value is set explicitly in terraform.tfvars (start from terraform.tfvars.example).

#
# ------------------ ENVIRONMENT ------------------
#

variable "folder_id" {
  type        = string
  description = "Google Cloud Folder ID where the projects will be created"
}

#
# ------------------ PROJECTS & USERS ------------------
#

variable "project_id" {
  type        = string
  description = "Google Cloud project ID hosting the proxy"
}

variable "billing_account_id" {
  type        = string
  description = "Billing Account ID to associate with the created projects"
}

variable "proxy_invoker_identities" {
  type        = list(string)
  description = "List of Google identities authorized to invoke the Proxy"
}

#
# ------------------ REGION & RUNTIME ------------------
#

variable "region" {
  type        = string
  description = "Google Cloud primary region for Cloud Run deployment and regional resources"
}

#
# ------------------ CUSTOM DOMAIN ------------------
#

variable "custom_domain" {
  type        = string
  description = "Custom domain name to map to the Cloud Run proxy service"
}

#
# ------------------ GOVERNANCE & POLICIES ------------------
#

variable "allowed_models" {
  type        = list(string)
  description = "Exact canonical model catalog IDs approved through the proxy; effort is configured in the request, never in the model ID"
  nullable    = false

  validation {
    condition = length(var.allowed_models) > 0 && length(distinct(var.allowed_models)) == length(var.allowed_models) && alltrue([
      for model in var.allowed_models :
      try(
        can(regex("^[a-zA-Z0-9_.@-]+$", model)) &&
        !can(regex("^(claude|gemini)-.*-(minimal|low|medium|high|xhigh|max)$", model)),
        false
      )
    ])
    error_message = "Use a non-empty list of unique canonical model catalog IDs containing only letters, digits, underscores, dots, @ or hyphens. Obsolete Claude/Gemini effort aliases ending in -minimal, -low, -medium, -high, -xhigh or -max are not supported."
  }
}

variable "allowed_vertex_regions" {
  type        = list(string)
  description = "List of Vertex AI regions (lowercase) that clients may target through the proxy"
}

variable "allowed_vertex_projects" {
  type        = list(string)
  description = "Google Cloud project IDs clients may target on Vertex AI through the proxy; an empty list means only the proxy project. Terraform does not grant IAM on other projects: give the proxy service account roles/aiplatform.user there"
}

variable "chat_user_label_key" {
  type        = string
  description = "Label key under which the chat client sends the user name; fixed by the extension, do not change"
}

variable "chat_project_label_key" {
  type        = string
  description = "Label key under which the chat client sends the workspace name; fixed by the extension, do not change"
}

variable "billing_user_label_key" {
  type        = string
  description = "Label key written to Vertex AI billing with the user name received from the chat client"
}

variable "billing_project_label_key" {
  type        = string
  description = "Label key written to Vertex AI billing with the workspace name received from the chat client"
}

variable "billing_auth_user_label_key" {
  type        = string
  description = "Label key written to Vertex AI billing with the verified caller email"
}

variable "models_catalog_url" {
  type        = string
  description = "HTTP URL pointing to the raw JSON model catalog (e.g. GitHub raw URL)"
}

variable "require_project_label" {
  type        = bool
  description = "Whether the chat project label (chat_project_label_key) is strictly mandatory in client requests"
}

variable "require_user_label" {
  type        = bool
  description = "Whether the chat user label (chat_user_label_key) is strictly mandatory in client requests"
}

variable "max_context_tokens" {
  type        = number
  description = "Maximum context window allowed for requests (e.g. 500000). Set <= 0 to disable."
}

variable "discovery_max_input_tokens" {
  type        = number
  description = "Maximum input tokens advertised to clients in /discovery for each model (e.g. 250000). Set <= 0 to keep upstream catalog values."
}

#
# ------------------ USAGE ANALYTICS & BIGQUERY ------------------
#

variable "usage_bigquery_dataset_id" {
  type        = string
  description = "ID of the dedicated BigQuery dataset for near-real-time AI usage analytics"
}

variable "bigquery_dataset_location" {
  type        = string
  description = "GCP region/location for the AI usage BigQuery dataset"
}

#
# ------------------ BILLING BUDGETS & ALERTS ------------------
#

variable "budget_amount" {
  type        = number
  description = "Monthly budget amount in EUR for each project"
}

variable "budget_currency" {
  type        = string
  description = "Currency code for the project budget"
}

variable "notification_emails" {
  type        = list(string)
  description = "Email recipients shared by billing budgets, Monitoring alerts and Error Reporting"
}

variable "proxy_error_count_threshold" {
  type        = number
  description = "Alert when upstream/stream errors per model exceed this count in five minutes"
  validation {
    condition     = var.proxy_error_count_threshold >= 0 && floor(var.proxy_error_count_threshold) == var.proxy_error_count_threshold
    error_message = "The error threshold must be a non-negative integer."
  }
}

variable "proxy_missing_usage_count_threshold" {
  type        = number
  description = "Alert when responses missing usage per model exceed this count in five minutes"
  validation {
    condition     = var.proxy_missing_usage_count_threshold >= 0 && floor(var.proxy_missing_usage_count_threshold) == var.proxy_missing_usage_count_threshold
    error_message = "The missing usage threshold must be a non-negative integer."
  }
}

variable "proxy_ttft_p95_threshold_ms" {
  type        = number
  description = "Alert when first SSE content p95 per model stays above this many milliseconds for five minutes"
  validation {
    condition     = var.proxy_ttft_p95_threshold_ms > 0
    error_message = "The latency threshold must be positive."
  }
}

variable "proxy_trace_enabled" {
  type        = bool
  description = "Export application spans to Cloud Trace"
}

variable "proxy_trace_sampling_ratio" {
  type        = number
  description = "Fraction of inference traces to export, independent of incoming sampling flags"
  validation {
    condition     = var.proxy_trace_sampling_ratio >= 0 && var.proxy_trace_sampling_ratio <= 1
    error_message = "Trace sampling ratio must be between 0 and 1."
  }
}

variable "proxy_trace_export_timeout_ms" {
  type        = number
  description = "Bounded RPC deadline and final trace flush wait in milliseconds"
  validation {
    condition     = var.proxy_trace_export_timeout_ms > 0 && floor(var.proxy_trace_export_timeout_ms) == var.proxy_trace_export_timeout_ms
    error_message = "Trace export timeout must be a positive integer."
  }
}

variable "proxy_trace_storage_location" {
  type        = string
  description = "Location of the Cloud Trace observability bucket"
}

