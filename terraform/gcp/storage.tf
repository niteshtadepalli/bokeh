data "google_project" "project" {
  project_id = var.project_id
}

# GCS Bucket for HTML & Partition Scan Reports
resource "google_storage_bucket" "reports" {
  name                        = var.reports_bucket_name != "" ? var.reports_bucket_name : "${var.resource_prefix}-reports-${var.project_id}"
  location                    = var.region
  project                     = var.project_id
  force_destroy               = true
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.enabled_services]
}

# GCS Bucket for CLI Binary Releases
resource "google_storage_bucket" "releases" {
  name                        = var.releases_bucket_name != "" ? var.releases_bucket_name : "${var.resource_prefix}-releases-${var.project_id}"
  location                    = var.region
  project                     = var.project_id
  force_destroy               = true
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.enabled_services]
}

# Artifact Registry Repository for CodeMender Runner Images
resource "google_artifact_registry_repository" "docker_repo" {
  provider      = google
  project       = var.project_id
  location      = var.region
  repository_id = "${var.resource_prefix}-runner"
  description   = "Docker repository for CodeMender agent runner container images"
  format        = "DOCKER"

  depends_on = [google_project_service.enabled_services]
}

locals {
  cloudbuild_service_accounts = {
    "legacy"  = "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com"
    "compute" = "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com"
  }
}

# Grant Storage Object Viewer to both legacy & compute default Cloud Build service accounts (for source tarballs and releases)
resource "google_project_iam_member" "cloudbuild_storage_viewer" {
  for_each = local.cloudbuild_service_accounts
  project  = var.project_id
  role     = "roles/storage.objectViewer"
  member   = each.value
}

# Grant Artifact Registry Writer to Cloud Build SAs for container image pushes
resource "google_artifact_registry_repository_iam_member" "cloudbuild_ar_writer" {
  for_each   = local.cloudbuild_service_accounts
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.docker_repo.name
  role       = "roles/artifactregistry.writer"
  member     = each.value
}

# Grant Cloud Logging Writer to Cloud Build SAs for build log execution
resource "google_project_iam_member" "cloudbuild_log_writer" {
  for_each = local.cloudbuild_service_accounts
  project  = var.project_id
  role     = "roles/logging.logWriter"
  member   = each.value
}

# Grant Releases Bucket Viewer to Cloud Build SAs
resource "google_storage_bucket_iam_member" "cloudbuild_releases_viewer" {
  for_each = local.cloudbuild_service_accounts
  bucket   = google_storage_bucket.releases.name
  role     = "roles/storage.objectViewer"
  member   = each.value
}
