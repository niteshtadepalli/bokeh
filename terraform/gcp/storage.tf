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
  repository_id = "codemender-runner"
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

resource "google_project_service_identity" "cloudbuild" {
  project = var.project_id
  service = "cloudbuild.googleapis.com"

  depends_on = [google_project_service.enabled_services]
}

resource "google_storage_bucket_iam_member" "cloudbuild_releases_viewer" {
  bucket = google_storage_bucket.releases.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_project_service_identity.cloudbuild.email}"
}
