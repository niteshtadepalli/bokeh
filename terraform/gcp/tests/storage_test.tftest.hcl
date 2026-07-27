# Unit tests for GCP Storage and Artifact Registry
mock_provider "google" {}

variables {
  project_id           = "test-project-123"
  region               = "us-central1"
  resource_prefix      = "test-storage"
}

run "storage_resources_default_names" {
  command = plan

  variables {
    reports_bucket_name  = ""
    releases_bucket_name = ""
  }

  assert {
    condition     = google_storage_bucket.reports.name == "test-storage-reports-test-project-123"
    error_message = "Reports bucket default name does not match expected prefix pattern."
  }

  assert {
    condition     = google_storage_bucket.releases.name == "test-storage-releases-test-project-123"
    error_message = "Releases bucket default name does not match expected prefix pattern."
  }

  assert {
    condition     = google_artifact_registry_repository.docker_repo.repository_id == "test-storage-runner"
    error_message = "Artifact Registry repository ID does not match expected prefix pattern."
  }
}

run "storage_resources_custom_names" {
  command = plan

  variables {
    reports_bucket_name  = "custom-reports-123"
    releases_bucket_name = "custom-releases-123"
  }

  assert {
    condition     = google_storage_bucket.reports.name == "custom-reports-123"
    error_message = "Reports bucket should use provided variable when specified."
  }

  assert {
    condition     = google_storage_bucket.releases.name == "custom-releases-123"
    error_message = "Releases bucket should use provided variable when specified."
  }
}
