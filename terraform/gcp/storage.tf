resource "google_storage_bucket" "reports" {
  name                        = var.reports_bucket_name
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.enabled_services]
}

resource "google_storage_bucket" "releases" {
  name                        = var.releases_bucket_name
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true
  force_destroy               = false

  depends_on = [google_project_service.enabled_services]
}

resource "google_artifact_registry_repository" "docker_repo" {
  location      = var.region
  repository_id = "${var.resource_prefix}-runner"
  description   = "Docker repository for CodeMender runner containers"
  format        = "DOCKER"
  project       = var.project_id

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state = "UNTAGGED"
    }
  }

  cleanup_policies {
    id     = "keep-minimum-versions"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  depends_on = [google_project_service.enabled_services]
}

data "google_project" "project" {
  project_id = var.project_id

  depends_on = [google_project_service.enabled_services]
}

locals {
  cloudbuild_service_accounts = [
    "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com",
    "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com",
  ]
}

# Grant Storage Object Viewer to both legacy & compute default Cloud Build service accounts (for source tarballs and releases)
resource "google_project_iam_member" "cloudbuild_storage_viewer" {
  for_each = toset(local.cloudbuild_service_accounts)
  project  = var.project_id
  role     = "roles/storage.objectViewer"
  member   = each.key
}

# Grant Artifact Registry Writer to Cloud Build SAs for container image pushes
resource "google_artifact_registry_repository_iam_member" "cloudbuild_ar_writer" {
  for_each   = toset(local.cloudbuild_service_accounts)
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.docker_repo.name
  role       = "roles/artifactregistry.writer"
  member     = each.key
}

# Grant Cloud Logging Writer to Cloud Build SAs for build log execution
resource "google_project_iam_member" "cloudbuild_log_writer" {
  for_each = toset(local.cloudbuild_service_accounts)
  project  = var.project_id
  role     = "roles/logging.logWriter"
  member   = each.key
}

# Grant Releases Bucket Viewer to Cloud Build SAs
resource "google_storage_bucket_iam_member" "cloudbuild_releases_viewer" {
  for_each = toset(local.cloudbuild_service_accounts)
  bucket   = google_storage_bucket.releases.name
  role     = "roles/storage.objectViewer"
  member   = each.key
}
