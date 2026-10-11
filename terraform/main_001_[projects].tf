############################################################
# PROJECTS
############################################################
# Creates the proxy project that hosts the proxy and its shared services, inside the target folder and linked to the billing account.

# Proxy project hosting the Google Agent Platform for Copilot Chat - Proxy and shared services
resource "google_project" "proxy_project" {
  project_id      = var.project_id
  name            = "Copilot Chat Proxy"
  billing_account = var.billing_account_id
  folder_id       = var.folder_id

  labels = {
    managed_by = "terraform"
    project    = "copilot-chat-proxy"
    role       = "proxy"
  }
}

