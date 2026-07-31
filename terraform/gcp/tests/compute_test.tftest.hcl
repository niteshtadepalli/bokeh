// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

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
