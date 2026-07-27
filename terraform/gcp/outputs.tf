output "nat_ip" {
  description = "Static IP address allocated for Cloud NAT (if VPC/NAT was created)."
  value       = var.create_vpc_and_nat ? google_compute_address.nat_ip[0].address : null
}

output "region" {
  description = "GCP Region for all resources."
  value       = var.region
}

output "reports_bucket_url" {
  description = "GCS bucket URL for scan reports."
  value       = google_storage_bucket.reports.url
}

output "reports_bucket_name" {
  description = "GCS bucket name for scan reports."
  value       = google_storage_bucket.reports.name
}

output "releases_bucket_url" {
  description = "GCS bucket URL for binary releases."
  value       = google_storage_bucket.releases.url
}

output "releases_bucket_name" {
  description = "GCS bucket name for binary releases."
  value       = google_storage_bucket.releases.name
}

output "runner_job_name" {
  description = "Cloud Run Job name."
  value       = google_cloud_run_v2_job.runner.name
}

output "workflow_name" {
  description = "Cloud Workflows workflow name."
  value       = google_workflows_workflow.coordinator.name
}

output "scheduler_job_name" {
  description = "Cloud Scheduler job name."
  value       = google_cloud_scheduler_job.nightly_scan.name
}

output "artifact_registry_repository" {
  description = "Artifact Registry Docker repository path."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker_repo.repository_id}"
}

output "workflow_id" {
  description = "Cloud Workflows workflow ID."
  value       = google_workflows_workflow.coordinator.id
}

output "workflow_execution_url" {
  description = "API URL to trigger execution of the coordinator workflow."
  value       = "https://workflowexecutions.googleapis.com/v1/${google_workflows_workflow.coordinator.id}/executions"
}

output "secret_manager_notice" {
  description = "Instructions for updating the GitHub App Token secret."
  value       = <<EOT
The secret '${google_secret_manager_secret.github_app_token.secret_id}' has been created with placeholder data.
Please update it with your actual GitHub App Token before running scans:
  gcloud secrets versions add ${google_secret_manager_secret.github_app_token.secret_id} --data-file=/path/to/token.pem --project=${var.project_id}
EOT
}
