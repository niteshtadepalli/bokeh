# Unit tests for GCP VPC and networking module using built-in Terraform test framework (v1.6+)

mock_provider "google" {}

variables {
  project_id           = "test-project-123"
  region               = "us-central1"
  reports_bucket_name  = "test-reports-bucket"
  releases_bucket_name = "test-releases-bucket"
}

run "vpc_disabled_default" {
  command = plan

  variables {
    create_vpc_and_nat = false
  }

  # Verify network resources are not created
  assert {
    condition     = length(google_compute_network.vpc_network) == 0
    error_message = "VPC Network should not be created when create_vpc_and_nat is false."
  }

  assert {
    condition     = length(google_vpc_access_connector.connector) == 0
    error_message = "VPC Connector should not be created when create_vpc_and_nat is false."
  }

  assert {
    condition     = local.use_vpc_access == false
    error_message = "use_vpc_access local should evaluate to false."
  }

  assert {
    condition     = local.vpc_connector_id == null
    error_message = "vpc_connector_id local should evaluate to null."
  }
}

run "vpc_enabled_with_custom_prefix" {
  command = plan

  variables {
    create_vpc_and_nat          = true
    resource_prefix             = "test-prefix"
    vpc_connector_min_instances = 3
    vpc_connector_max_instances = 5
    vpc_connector_machine_type  = "e2-standard-2"
  }

  # Verify dynamic naming and scaling properties
  assert {
    condition     = google_compute_network.vpc_network[0].name == "test-prefix-vpc"
    error_message = "VPC Network name should match resource_prefix."
  }

  assert {
    condition     = google_vpc_access_connector.connector[0].name == "test-prefix-vpc-conn"
    error_message = "VPC Connector name should match resource_prefix."
  }

  assert {
    condition     = google_vpc_access_connector.connector[0].min_instances == 3
    error_message = "VPC Connector min_instances should equal 3."
  }

  assert {
    condition     = google_vpc_access_connector.connector[0].max_instances == 5
    error_message = "VPC Connector max_instances should equal 5."
  }

  assert {
    condition     = google_vpc_access_connector.connector[0].machine_type == "e2-standard-2"
    error_message = "VPC Connector machine_type should equal e2-standard-2."
  }
}

run "existing_vpc_connector_fallback" {
  command = plan

  variables {
    create_vpc_and_nat        = false
    existing_vpc_connector_id = "projects/test-proj/locations/us-central1/connectors/existing-conn"
  }

  assert {
    condition     = local.use_vpc_access == true
    error_message = "use_vpc_access local should be true when existing_vpc_connector_id is set."
  }

  assert {
    condition     = local.vpc_connector_id == "projects/test-proj/locations/us-central1/connectors/existing-conn"
    error_message = "vpc_connector_id local should equal existing_vpc_connector_id."
  }
}
