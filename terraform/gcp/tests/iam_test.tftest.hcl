# Unit tests for GCP IAM and Service Accounts
mock_provider "google" {}

variables {
  project_id           = "test-project-123"
  region               = "us-central1"
  resource_prefix      = "test-iam"
}

run "iam_resources_created_correctly" {
  command = plan

  assert {
    condition     = google_service_account.runner_sa.account_id == "test-iam-runner-sa"
    error_message = "Runner Service Account ID does not match expected prefix."
  }

  assert {
    condition     = google_service_account.workflow_sa.account_id == "test-iam-workflows-sa"
    error_message = "Workflow Service Account ID does not match expected prefix."
  }

  assert {
    condition     = google_service_account.scheduler_sa.account_id == "test-iam-scheduler-sa"
    error_message = "Scheduler Service Account ID does not match expected prefix."
  }

  assert {
    condition     = google_project_iam_custom_role.workflow_job_runner.role_id == "testiamWorkflowJobRunner"
    error_message = "Workflow custom role ID does not match expected prefix (should have dashes removed)."
  }
}
