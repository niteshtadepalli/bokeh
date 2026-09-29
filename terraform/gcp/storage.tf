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

data "google_project" "project" {
  project_id = var.project_id
  depends_on = [google_project_service.enabled_services["cloudresourcemanager.googleapis.com"]]
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

# Grant Storage Object Viewer to both legacy & compute default Cloud Build service accounts (for source tarballs)
resource "google_project_iam_member" "cloudbuild_storage_viewer" {
  for_each   = local.cloudbuild_service_accounts
  project    = var.project_id
  role       = "roles/storage.objectViewer"
  member     = each.value
  depends_on = [google_project_service.enabled_services["iam.googleapis.com"]]
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
  for_each   = local.cloudbuild_service_accounts
  project    = var.project_id
  role       = "roles/logging.logWriter"
  member     = each.value
  depends_on = [google_project_service.enabled_services["iam.googleapis.com"]]
}
