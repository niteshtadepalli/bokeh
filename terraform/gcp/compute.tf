resource "google_cloud_run_v2_job" "runner" {
  name     = "${var.resource_prefix}-runner"
  location = var.region
  project  = var.project_id

  template {
    template {
      service_account = google_service_account.runner_sa.email

      containers {
        image = "alpine:latest"

        resources {
          limits = {
            cpu    = var.runner_cpu
            memory = var.runner_memory
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
      template[0].template[0].containers[0].image,
    ]
  }

  depends_on = [google_project_service.enabled_services]
}

resource "google_workflows_workflow" "coordinator" {
  name            = "${var.resource_prefix}-coordinator"
  region          = var.region
  project         = var.project_id
  description     = "Coordinates parallel CodeMender security scan and fix executions"
  service_account = google_service_account.workflow_sa.id
  source_contents = file("${path.module}/../../workflows/gcp_parallel_workflow.yaml")

  depends_on = [google_project_service.enabled_services]
}
