resource "google_service_account" "runner_sa" {
  account_id   = "codemender-runner-sa"
  display_name = "CodeMender Runner Service Account"
  project      = var.project_id
}

resource "google_service_account" "workflow_sa" {
  account_id   = "codemender-workflows-sa"
  display_name = "CodeMender Workflows Service Account"
  project      = var.project_id
}

resource "google_service_account" "scheduler_sa" {
  account_id   = "codemender-scheduler-sa"
  display_name = "CodeMender Scheduler Service Account"
  project      = var.project_id
}

resource "google_project_iam_custom_role" "workflow_job_runner" {
  role_id     = "codemenderWorkflowJobRunner"
  title       = "CodeMender Workflow Job Runner"
  description = "Allows Cloud Workflows to run and monitor Cloud Run Jobs for CodeMender"
  project     = var.project_id
  permissions = [
    "run.jobs.run",
    "run.jobs.get",
    "run.operations.get",
    "run.executions.get",
    "run.executions.list",
  ]
}

# Bucket-level IAM for Runner SA
resource "google_storage_bucket_iam_member" "runner_reports_admin" {
  bucket = google_storage_bucket.reports.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runner_sa.email}"
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

# Resource-restricted Cloud Run Job IAM for Workflow SA
resource "google_cloud_run_v2_job_iam_member" "workflow_job_runner_binding" {
  project  = google_cloud_run_v2_job.runner.project
  location = google_cloud_run_v2_job.runner.location
  name     = google_cloud_run_v2_job.runner.name
  role     = google_project_iam_custom_role.workflow_job_runner.id
  member   = "serviceAccount:${google_service_account.workflow_sa.email}"
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
