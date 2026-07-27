# Unit tests for GCP Cloud Scheduler
mock_provider "google" {}

variables {
  project_id           = "test-project-123"
  region               = "us-central1"
  resource_prefix      = "test-sched"
  scheduler_cron       = "0 3 * * *"
}

run "scheduler_job_created_correctly" {
  command = plan

  assert {
    condition     = google_cloud_scheduler_job.nightly_scan.name == "test-sched-nightly-scan"
    error_message = "Cloud Scheduler job name does not match expected prefix pattern."
  }

  assert {
    condition     = google_cloud_scheduler_job.nightly_scan.schedule == "0 3 * * *"
    error_message = "Cloud Scheduler job schedule does not match input variable."
  }

  assert {
    condition     = google_cloud_scheduler_job.nightly_scan.paused == true
    error_message = "Cloud Scheduler job should be paused by default."
  }

  assert {
    condition     = google_cloud_scheduler_job.nightly_scan.http_target[0].http_method == "POST"
    error_message = "Cloud Scheduler job HTTP target method must be POST."
  }
}
