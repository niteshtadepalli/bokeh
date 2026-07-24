locals {
  required_apis = [
    "cloudresourcemanager.googleapis.com",
    "run.googleapis.com",
    "workflows.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "cloudbuild.googleapis.com",
  ]
}

resource "google_project_service" "enabled_services" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

resource "google_project_service" "vpcaccess_api" {
  count              = var.create_vpc_and_nat ? 1 : 0
  project            = var.project_id
  service            = "vpcaccess.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "compute_api" {
  count              = var.create_vpc_and_nat ? 1 : 0
  project            = var.project_id
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}
