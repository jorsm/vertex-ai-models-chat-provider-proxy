############################################################
# BILLING BUDGETS & ALERTS
############################################################
# Monthly billing budget of the hub project, with threshold and forecast alerts sent to the shared notification channels.

# Central billing budget for the Hub project with email notifications
resource "google_billing_budget" "hub_project_budget" {
  depends_on = [
    google_project_service.hub_services
  ]

  billing_account = var.billing_account_id
  display_name    = "Budget - ${google_project.hub_project.project_id}"

  budget_filter {
    projects        = ["projects/${google_project.hub_project.number}"]
    calendar_period = "MONTH"
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.budget_amount)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }

  threshold_rules {
    threshold_percent = 0.9
  }

  threshold_rules {
    threshold_percent = 1.0
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels = [
      for ch in google_monitoring_notification_channel.notification_email : ch.id
    ]
    disable_default_iam_recipients = true
  }
}

