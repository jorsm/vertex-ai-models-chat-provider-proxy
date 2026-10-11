# Terraform and provider requirements with pinned versions; API usage and quota are billed to the hub project (user_project_override).

terraform {
  required_version = ">= 1.15.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 8.5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "= 8.5.0"
    }
  }
}

provider "google" {
  user_project_override = true
  billing_project       = var.project_id
}

provider "google-beta" {
  user_project_override = true
  billing_project       = var.project_id
}
