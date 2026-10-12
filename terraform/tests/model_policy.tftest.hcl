# Offline contract checks: terraform test -var-file=terraform.tfvars.example
# Mocked apply materializes the Cloud Run environment set without contacting Google Cloud.
mock_provider "google" {
  mock_resource "google_logging_project_sink" {
    defaults = {
      writer_identity = "serviceAccount:usage-sink@example.iam.gserviceaccount.com"
    }
  }
}
mock_provider "google-beta" {}

run "canonical_models_and_runtime_contract" {
  command = apply

  variables {
    allowed_models = [
      "claude-opus-5-5",
      "claude-sonnet-5-5",
      "claude-haiku-5-5",
      "gemini-3.8-flash",
      "gemini-3-flash-preview",
    ]
    allowed_vertex_projects = ["vertex-target-project"]
  }

  assert {
    condition = one([
      for env in google_cloud_run_v2_service.copilot_chat_proxy.template[0].containers[0].env : env.value
      if env.name == "ALLOWED_MODELS"
    ]) == "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5,gemini-3.8-flash,gemini-3-flash-preview"
    error_message = "Cloud Run must receive exact canonical IDs, including the dot in Gemini 3.8 and the commit-message model."
  }

  assert {
    condition = one([
      for env in google_cloud_run_v2_service.copilot_chat_proxy.template[0].containers[0].env : env.value
      if env.name == "ALLOWED_VERTEX_PROJECTS"
    ]) == "vertex-target-project"
    error_message = "Model migration must preserve the independently configured Vertex project allowlist."
  }

  assert {
    condition = {
      for env in google_cloud_run_v2_service.copilot_chat_proxy.template[0].containers[0].env : env.name => env.value
      if contains([
        "CHAT_USER_LABEL_KEY", "CHAT_PROJECT_LABEL_KEY", "BILLING_USER_LABEL_KEY",
        "BILLING_PROJECT_LABEL_KEY", "BILLING_AUTH_USER_LABEL_KEY"
      ], env.name)
      } == {
      CHAT_USER_LABEL_KEY         = var.chat_user_label_key
      CHAT_PROJECT_LABEL_KEY      = var.chat_project_label_key
      BILLING_USER_LABEL_KEY      = var.billing_user_label_key
      BILLING_PROJECT_LABEL_KEY   = var.billing_project_label_key
      BILLING_AUTH_USER_LABEL_KEY = var.billing_auth_user_label_key
    }
    error_message = "Chat and billing label settings must remain independent."
  }
}

run "accept_literal_custom_model_suffix" {
  command = plan
  variables {
    allowed_models = ["company-model-high"]
  }
}

run "reject_legacy_medium_alias" {
  command = plan
  variables {
    allowed_models = ["claude-sonnet-5-5-medium"]
  }
  expect_failures = [var.allowed_models]
}

run "reject_legacy_high_alias" {
  command = plan
  variables {
    allowed_models = ["gemini-3.8-flash-high"]
  }
  expect_failures = [var.allowed_models]
}

run "reject_legacy_max_alias" {
  command = plan
  variables {
    allowed_models = ["claude-opus-5-5-max"]
  }
  expect_failures = [var.allowed_models]
}

run "reject_legacy_claude_xhigh_alias" {
  command = plan
  variables {
    allowed_models = ["claude-opus-5-5-xhigh"]
  }
  expect_failures = [var.allowed_models]
}

run "reject_legacy_gemini_xhigh_alias" {
  command = plan
  variables {
    allowed_models = ["gemini-3.8-flash-xhigh"]
  }
  expect_failures = [var.allowed_models]
}

run "reject_duplicate_model_ids" {
  command = plan
  variables {
    allowed_models = ["claude-opus-5-5", "claude-opus-5-5"]
  }
  expect_failures = [var.allowed_models]
}

run "reject_empty_policy" {
  command = plan
  variables {
    allowed_models = []
  }
  expect_failures = [var.allowed_models]
}

run "reject_null_model_id" {
  command = plan
  variables {
    allowed_models = [null]
  }
  expect_failures = [var.allowed_models]
}

run "reject_comma_in_model_id" {
  command = plan
  variables {
    allowed_models = ["claude-opus-5-5,unapproved-model"]
  }
  expect_failures = [var.allowed_models]
}
