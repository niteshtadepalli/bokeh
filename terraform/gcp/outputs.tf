output "nat_ip" {
  description = "Static IP address allocated for Cloud NAT (if VPC/NAT was created)."
  value       = var.create_vpc_and_nat ? google_compute_address.nat_ip[0].address : null
}

output "reports_bucket_url" {
  description = "GCS bucket URL for scan reports."
  value       = google_storage_bucket.reports.url
}

output "releases_bucket_url" {
  description = "GCS bucket URL for binary releases."
  value       = google_storage_bucket.releases.url
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
