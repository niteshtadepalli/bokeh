# Unit tests for GCP Compute resources (Cloud Run and Workflows)
mock_provider "google" {}

variables {
  project_id           = "test-project-123"
  region               = "us-central1"
  resource_prefix      = "test-compute"
}

run "compute_resources_created_correctly" {
  command = plan

  assert {
    condition     = google_cloud_run_v2_job.runner.name == "test-compute-runner"
    error_message = "Cloud Run job name does not match the expected resource_prefix pattern."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner.location == "us-central1"
    error_message = "Cloud Run job should be deployed to the specified region."
  }

  assert {
    condition     = google_workflows_workflow.coordinator.name == "test-compute-coordinator"
    error_message = "Workflow name does not match the expected resource_prefix pattern."
  }

  assert {
    condition     = google_secret_manager_secret.github_app_token.secret_id == "test-compute-github-token"
    error_message = "Secret Manager secret ID does not match the expected resource_prefix pattern."
  }
}
