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
