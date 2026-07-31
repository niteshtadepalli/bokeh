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

# Dedicated Custom VPC Network for CodeMender Private Egress
resource "google_compute_network" "vpc_network" {
  count                   = var.create_vpc_and_nat ? 1 : 0
  name                    = "${var.resource_prefix}-vpc"
  auto_create_subnetworks = false
  project                 = var.project_id

  depends_on = [google_project_service.compute_api]
}

# Subnet for Serverless VPC Access Connector
resource "google_compute_subnetwork" "vpc_subnet" {
  count                    = var.create_vpc_and_nat ? 1 : 0
  name                     = "${var.resource_prefix}-subnet"
  ip_cidr_range            = var.vpc_connector_cidr
  region                   = var.region
  network                  = google_compute_network.vpc_network[0].id
  project                  = var.project_id
  private_ip_google_access = true

  depends_on = [
    google_project_service.compute_api,
    google_compute_network.vpc_network,
  ]
}

# Serverless VPC Access Connector for Cloud Run Job Egress
resource "google_vpc_access_connector" "connector" {
  count   = var.create_vpc_and_nat ? 1 : 0
  name    = "${var.resource_prefix}-vpc-conn"
  region  = var.region
  project = var.project_id

  subnet {
    name = google_compute_subnetwork.vpc_subnet[0].name
  }

  machine_type  = var.vpc_connector_machine_type
  min_instances = var.vpc_connector_min_instances
  max_instances = var.vpc_connector_max_instances

  depends_on = [
    google_project_service.vpcaccess_api,
    google_compute_subnetwork.vpc_subnet,
  ]
}

# Cloud Router for NAT Gateway Egress Routing
resource "google_compute_router" "router" {
  count   = var.create_vpc_and_nat ? 1 : 0
  name    = "${var.resource_prefix}-router"
  region  = var.region
  network = google_compute_network.vpc_network[0].id
  project = var.project_id

  depends_on = [
    google_project_service.compute_api,
    google_compute_network.vpc_network,
  ]
}

# Statically Allocated External IP Address for Cloud NAT
resource "google_compute_address" "nat_ip" {
  count   = var.create_vpc_and_nat ? 1 : 0
  name    = "${var.resource_prefix}-nat-ip"
  region  = var.region
  project = var.project_id

  depends_on = [google_project_service.compute_api]
}

# Cloud NAT Gateway for External Outbound Traffic Routing
resource "google_compute_router_nat" "nat" {
  count                              = var.create_vpc_and_nat ? 1 : 0
  name                               = "${var.resource_prefix}-nat"
  router                             = google_compute_router.router[0].name
  region                             = var.region
  project                            = var.project_id
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.nat_ip[0].self_link]
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  depends_on = [
    google_project_service.compute_api,
    google_compute_router.router,
    google_compute_address.nat_ip,
  ]
}

# Local variables resolving VPC connector selection and fallback handling
locals {
  # Helper boolean to verify if an existing VPC connector ID was provided (non-null & non-empty string)
  has_existing_connector = var.existing_vpc_connector_id != null ? trimspace(var.existing_vpc_connector_id) != "" : false

  # Determines whether Cloud Run should attach to a Serverless VPC Access connector
  use_vpc_access = var.create_vpc_and_nat || local.has_existing_connector

  # Robust fallback logic for selecting the VPC Connector ID:
  # - When create_vpc_and_nat is true: uses length check on the connector count rather than try()
  # - When create_vpc_and_nat is false: uses trimmed existing_vpc_connector_id or falls back to null
  vpc_connector_id = var.create_vpc_and_nat ? (
    length(google_vpc_access_connector.connector) > 0 ? google_vpc_access_connector.connector[0].id : null
  ) : (
    local.has_existing_connector ? trimspace(var.existing_vpc_connector_id) : null
  )
}
