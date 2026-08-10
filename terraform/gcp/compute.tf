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

resource "time_sleep" "wait_for_apis_and_iam" {
  create_duration = "60s"

  depends_on = [
    google_project_service.enabled_services,
    google_secret_manager_secret_iam_member.runner_secret_accessor,
    google_project_iam_member.workflow_job_runner_binding,
    google_service_account_iam_member.workflow_runner_sa_user,
    google_project_service_identity.workflows_sa
  ]
}

resource "google_cloud_run_v2_job" "runner" {
  name                = "${var.resource_prefix}-runner"
  location            = var.region
  project             = var.project_id
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.runner_sa.email

      containers {
        # Use a placeholder image initially so Terraform can provision the job before Cloud Build runs.
        # The actual image is deployed out-of-band via Cloud Build (see cloudbuild.yaml).
        image = "us-docker.pkg.dev/cloudrun/container/job:latest"

        resources {
          limits = {
            cpu    = var.runner_cpu
            memory = var.runner_memory
          }
        }

        env {
          name = "GITHUB_APP_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.github_app_token.secret_id
              version = "latest"
            }
          }
        }
      }

      dynamic "vpc_access" {
        for_each = local.use_vpc_access ? [1] : []
        content {
          connector = local.vpc_connector_id
          egress    = "ALL_TRAFFIC"
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image
    ]
  }

  depends_on = [
    time_sleep.wait_for_apis_and_iam
  ]
}

resource "google_cloud_run_v2_job" "worker" {
  name                = "${var.resource_prefix}-worker"
  location            = var.region
  project             = var.project_id
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.worker_sa.email

      containers {
        # Use a placeholder image initially so Terraform can provision the job before Cloud Build runs.
        # The actual image is deployed out-of-band via Cloud Build (see cloudbuild.yaml).
        image = "us-docker.pkg.dev/cloudrun/container/job:latest"

        resources {
          limits = {
            cpu    = var.runner_cpu
            memory = var.runner_memory
          }
        }

        env {
          name = "GITHUB_APP_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.github_app_token.secret_id
              version = "latest"
            }
          }
        }
      }

      dynamic "vpc_access" {
        for_each = local.use_vpc_access ? [1] : []
        content {
          connector = local.vpc_connector_id
          egress    = "ALL_TRAFFIC"
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image
    ]
  }

  depends_on = [
    time_sleep.wait_for_apis_and_iam
  ]
}

resource "google_workflows_workflow" "coordinator" {
  name                = "${var.resource_prefix}-coordinator"
  region              = var.region
  project             = var.project_id
  deletion_protection = false
  description         = "Coordinates parallel CodeMender security scan and fix executions"
  service_account     = google_service_account.workflow_sa.id
  source_contents     = file("${path.module}/../../workflows/gcp_parallel_workflow.yaml")

  depends_on = [
    time_sleep.wait_for_apis_and_iam
  ]
}
