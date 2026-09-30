# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

resource "google_service_account" "runner_sa" {
  account_id   = "${local.cfg.resource_prefix}-runner-sa"
  display_name = "CodeMender Orchestrator Service Account (${local.cfg.resource_prefix})"
  project      = local.cfg.project_id
  depends_on   = [google_project_service.enabled_services["iam.googleapis.com"]]
}

resource "google_service_account" "worker_sa" {
  account_id   = "${local.cfg.resource_prefix}-worker-sa"
  display_name = "CodeMender Worker Service Account (${local.cfg.resource_prefix})"
  project      = local.cfg.project_id
  depends_on   = [google_project_service.enabled_services["iam.googleapis.com"]]
}

resource "google_service_account" "workflow_sa" {
  account_id   = "${local.cfg.resource_prefix}-workflows-sa"
  display_name = "CodeMender Workflows Service Account (${local.cfg.resource_prefix})"
  project      = local.cfg.project_id
  depends_on   = [google_project_service.enabled_services["iam.googleapis.com"]]
}

resource "google_service_account" "scheduler_sa" {
  account_id   = "${local.cfg.resource_prefix}-scheduler-sa"
  display_name = "CodeMender Scheduler Service Account (${local.cfg.resource_prefix})"
  project      = local.cfg.project_id
  depends_on   = [google_project_service.enabled_services["iam.googleapis.com"]]
}

# Random ID suffix to prevent 409 Conflict errors when re-creating custom IAM roles.
# GCP IAM custom roles enter a 7-day soft-delete tombstone state upon deletion.
# Appending a random hex suffix ensures role recreations generate a fresh role ID.
resource "random_id" "role_suffix" {
  byte_length = 4
  keepers = {
    resource_prefix = local.cfg.resource_prefix
  }
}

resource "google_project_iam_custom_role" "workflow_job_runner" {
  role_id     = "${replace(local.cfg.resource_prefix, "-", "")}WorkflowJobRunner_${random_id.role_suffix.hex}"
  title       = "CodeMender Workflow Job Runner (${local.cfg.resource_prefix})"
  description = "Allows Cloud Workflows to run and monitor Cloud Run Jobs for CodeMender (${local.cfg.resource_prefix})"
  project     = local.cfg.project_id
  depends_on  = [google_project_service.enabled_services["iam.googleapis.com"]]
  permissions = [
    "run.jobs.run",
    "run.jobs.runWithOverrides",
    "run.jobs.get",
    "run.operations.get",
    "run.executions.get",
    "run.executions.list",
  ]
}

# Bucket-level IAM for Runner SA & Workflow SA
resource "google_storage_bucket_iam_member" "runner_reports_admin" {
  bucket = google_storage_bucket.reports.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runner_sa.email}"
}

resource "google_storage_bucket_iam_member" "workflow_reports_viewer" {
  bucket = google_storage_bucket.reports.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.workflow_sa.email}"
}


# Secret Manager IAM for Runner SA
resource "google_secret_manager_secret_iam_member" "runner_secret_accessor" {
  secret_id = google_secret_manager_secret.github_app_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runner_sa.email}"
}

# Service Account Token Creator IAM for Runner SA on itself (required for GCS signed URL generation)
resource "google_service_account_iam_member" "runner_token_creator" {
  service_account_id = google_service_account.runner_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.runner_sa.email}"
}

locals {
  log_writer_service_accounts = {
    "runner"    = "serviceAccount:${google_service_account.runner_sa.email}"
    "worker"    = "serviceAccount:${google_service_account.worker_sa.email}"
    "workflows" = "serviceAccount:${google_service_account.workflow_sa.email}"
    "scheduler" = "serviceAccount:${google_service_account.scheduler_sa.email}"
  }
}

# Logging Writer IAM for Runner, Workflow, and Scheduler Service Accounts
resource "google_project_iam_member" "service_accounts_log_writer" {
  for_each = local.log_writer_service_accounts
  project  = local.cfg.project_id
  role     = "roles/logging.logWriter"
  member   = each.value
}

# Agent Platform / Vertex AI IAM for Runner SA (required for CodeMender LLM interactions)
resource "google_project_iam_member" "runner_aiplatform_user" {
  project = local.cfg.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runner_sa.email}"
}

# Project-level IAM binding for Workflow SA to run jobs, poll operations, and monitor executions
resource "google_project_iam_member" "workflow_job_runner_binding" {
  project = local.cfg.project_id
  role    = google_project_iam_custom_role.workflow_job_runner.id
  member  = "serviceAccount:${google_service_account.workflow_sa.email}"
}

# Service Account User IAM for Workflow SA on Runner SA
resource "google_service_account_iam_member" "workflow_runner_sa_user" {
  service_account_id = google_service_account.runner_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.workflow_sa.email}"
}

# Service Account User IAM for Workflow SA on Worker SA
resource "google_service_account_iam_member" "workflow_worker_sa_user" {
  service_account_id = google_service_account.worker_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.workflow_sa.email}"
}

# Workflow Invoker IAM for Scheduler SA at Project Level
resource "google_project_iam_member" "scheduler_workflow_invoker" {
  project = local.cfg.project_id
  role    = "roles/workflows.invoker"
  member  = "serviceAccount:${google_service_account.scheduler_sa.email}"
}

# Secret Manager IAM for Worker SA (for Github token)
resource "google_secret_manager_secret_iam_member" "worker_secret_accessor" {
  secret_id = google_secret_manager_secret.github_app_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker_sa.email}"
}


# Agent Platform / Vertex AI IAM for Worker SA (required for CodeMender LLM interactions)
resource "google_project_iam_member" "worker_aiplatform_user" {
  project = local.cfg.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.worker_sa.email}"
}

# Service Usage Consumer IAM for Runner & Worker SAs (required by cm v0.8.0 x-goog-user-project quota check)
resource "google_project_iam_member" "runner_serviceusage_consumer" {
  project = local.cfg.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.runner_sa.email}"
}

resource "google_project_iam_member" "worker_serviceusage_consumer" {
  project = local.cfg.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = "serviceAccount:${google_service_account.worker_sa.email}"
}

# ---------------------------------------------------------------------------
# BigQuery telemetry IAM (runner SA only).
#
# Only the runner job emits telemetry: the scan stage writes the row for clean
# and failed runs, and the aggregate stage writes it for runs with findings.
# Both run on runner_sa. worker_sa performs no BigQuery access whatsoever and
# is deliberately granted nothing here.
#
# dataEditor is bound at the DATASET level rather than project-wide, so the
# scanner can write its own telemetry but cannot read or modify any other
# dataset in the project. jobUser must be project-level because that is the
# scope at which BigQuery job creation is authorized.
# ---------------------------------------------------------------------------

resource "google_bigquery_dataset_iam_member" "runner_telemetry_editor" {
  count = local.cfg.enable_bigquery_telemetry ? 1 : 0

  project    = local.cfg.project_id
  dataset_id = google_bigquery_dataset.telemetry[0].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.runner_sa.email}"
}

resource "google_project_iam_member" "runner_bigquery_job_user" {
  count = local.cfg.enable_bigquery_telemetry ? 1 : 0

  project = local.cfg.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.runner_sa.email}"
}



# Grant Cloud Run Developer to Cloud Build SAs so they can update the Cloud Run Job image
resource "google_project_iam_member" "cloudbuild_run_developer" {
  for_each   = local.cloudbuild_service_accounts
  project    = local.cfg.project_id
  role       = "roles/run.developer"
  member     = each.value
  depends_on = [google_project_service.enabled_services["iam.googleapis.com"]]
}

# Grant Service Account User to Cloud Build SAs on the Runner SA
resource "google_service_account_iam_member" "cloudbuild_runner_sa_user" {
  for_each           = local.cloudbuild_service_accounts
  service_account_id = google_service_account.runner_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

# Grant Service Account User to Cloud Build SAs on the Worker SA
resource "google_service_account_iam_member" "cloudbuild_worker_sa_user" {
  for_each           = local.cloudbuild_service_accounts
  service_account_id = google_service_account.worker_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}
