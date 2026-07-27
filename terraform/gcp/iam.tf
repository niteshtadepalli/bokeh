resource "google_service_account" "runner_sa" {
  account_id   = "${var.resource_prefix}-runner-sa"
  display_name = "CodeMender Runner Service Account (${var.resource_prefix})"
  project      = var.project_id
  depends_on   = [google_project_service.enabled_services["iam.googleapis.com"]]
}

resource "google_service_account" "workflow_sa" {
  account_id   = "${var.resource_prefix}-workflows-sa"
  display_name = "CodeMender Workflows Service Account (${var.resource_prefix})"
  project      = var.project_id
  depends_on   = [google_project_service.enabled_services["iam.googleapis.com"]]
}

resource "google_service_account" "scheduler_sa" {
  account_id   = "${var.resource_prefix}-scheduler-sa"
  display_name = "CodeMender Scheduler Service Account (${var.resource_prefix})"
  project      = var.project_id
  depends_on   = [google_project_service.enabled_services["iam.googleapis.com"]]
}

resource "google_project_iam_custom_role" "workflow_job_runner" {
  role_id     = replace("${var.resource_prefix}WorkflowJobRunner", "-", "")
  title       = "CodeMender Workflow Job Runner (${var.resource_prefix})"
  description = "Allows Cloud Workflows to run and monitor Cloud Run Jobs for CodeMender (${var.resource_prefix})"
  project     = var.project_id
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

resource "google_storage_bucket_iam_member" "runner_releases_viewer" {
  bucket = google_storage_bucket.releases.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runner_sa.email}"
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
    "workflows" = "serviceAccount:${google_service_account.workflow_sa.email}"
    "scheduler" = "serviceAccount:${google_service_account.scheduler_sa.email}"
  }
}

# Logging Writer IAM for Runner, Workflow, and Scheduler Service Accounts
resource "google_project_iam_member" "service_accounts_log_writer" {
  for_each = local.log_writer_service_accounts
  project  = var.project_id
  role     = "roles/logging.logWriter"
  member   = each.value
}

# Project-level IAM binding for Workflow SA to run jobs, poll operations, and monitor executions
resource "google_project_iam_member" "workflow_job_runner_binding" {
  project = var.project_id
  role    = google_project_iam_custom_role.workflow_job_runner.id
  member  = "serviceAccount:${google_service_account.workflow_sa.email}"
}

# Service Account User IAM for Workflow SA on Runner SA
resource "google_service_account_iam_member" "workflow_runner_sa_user" {
  service_account_id = google_service_account.runner_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.workflow_sa.email}"
}

# Workflow Invoker IAM for Scheduler SA at Project Level
resource "google_project_iam_member" "scheduler_workflow_invoker" {
  project = var.project_id
  role    = "roles/workflows.invoker"
  member  = "serviceAccount:${google_service_account.scheduler_sa.email}"
}
