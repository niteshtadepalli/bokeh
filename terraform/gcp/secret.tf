resource "google_secret_manager_secret" "github_app_token" {
  secret_id = "${var.resource_prefix}-github-token"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_services]
}

resource "google_secret_manager_secret_version" "github_app_token_initial" {
  secret      = google_secret_manager_secret.github_app_token.id
  secret_data = "PLACEHOLDER"

  lifecycle {
    ignore_changes = [secret_data]
  }
}
