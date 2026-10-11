############################################################
# PROJECTS
############################################################
# Creates the hub project that hosts the proxy and its shared services, inside the target folder and linked to the billing account.

# Central Hub project hosting the Google Agent Platform for Copilot Chat - Proxy and shared services
resource "google_project" "hub_project" {
  project_id      = var.project_id
  name            = "Copilot Chat Proxy - Hub"
  billing_account = var.billing_account_id
  folder_id       = var.folder_id

  labels = {
    managed_by = "terraform"
    project    = "copilot-chat-proxy"
    role       = "hub"
  }
}

