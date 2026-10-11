############################################################
# SHARED NOTIFICATION CHANNELS
############################################################
# Email notification channels shared by budgets and Monitoring alerts, one per address in notification_emails.

resource "google_monitoring_notification_channel" "notification_email" {
  for_each = toset(var.notification_emails)

  project      = google_project.proxy_project.project_id
  display_name = "Copilot Chat Proxy Notifications - ${each.value}"
  description  = "Shared notifications for billing budgets, Monitoring alerts and Error Reporting"
  type         = "email"

  labels = {
    email_address = each.value
  }

  depends_on = [google_project_service.proxy_services]
}
