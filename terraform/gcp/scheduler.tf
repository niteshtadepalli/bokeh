resource "google_cloud_scheduler_job" "nightly_scan" {
  name        = "${var.resource_prefix}-nightly-scan"
  description = "Triggers nightly CodeMender parallel scan workflow"
  schedule    = var.scheduler_cron
  time_zone   = "Etc/UTC"
  paused      = true
  region      = var.region
  project     = var.project_id

  http_target {
    uri         = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.coordinator.id}/executions"
    http_method = "POST"

    body = base64encode(jsonencode({
      argument = jsonencode({
        job_name   = google_cloud_run_v2_job.runner.name
        gcs_bucket = google_storage_bucket.reports.name
        region     = var.region
      })
    }))

    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.scheduler_sa.email
    }
  }

  depends_on = [google_project_service.enabled_services]
}
