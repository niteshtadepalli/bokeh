# CodeMender Orchestrator: Automated Terraform Deployment & Parallel Execution Guide

This guide provides step-by-step instructions to setup, deploy, and run the
**CodeMender Orchestrator Parallel Scanning Pipeline** on Google Cloud Platform
(GCP) using Terraform.

--------------------------------------------------------------------------------

## 1. Architecture Overview

The parallel scanning pipeline uses **Infrastructure as Code (IaC)** to
provision:

*   **Secret Manager**: Secure storage for GitHub tokens (`GITHUB_APP_TOKEN`).
*   **Google Cloud Storage (GCS)**: Private buckets for scan reports and binary
    releases.
*   **Artifact Registry**: Docker container repository for runner images.
*   **Service Accounts & Custom IAM**: Ephemeral, least-privilege access for
    Cloud Run and Cloud Workflows.
*   **Cloud Run v2 Job**: Ephemeral runner container pool for `scan`, `worker`,
    and `aggregate` modes.
*   **Cloud Workflows**: Coordinator workflow orchestrating multi-stage parallel
    tasks without container timeouts.
*   **Cloud Scheduler**: Nightly trigger for automated repository scanning.
*   *(Optional)* **Serverless VPC Access & Cloud NAT**: Dedicated private
    network egress routing.

```mermaid
graph TD
    Scheduler[Cloud Scheduler] -->|Nightly Trigger| Workflow[Cloud Workflows: Coordinator]
    Workflow -->|1. Run Stage 1 Scan| CR_Job[Cloud Run Job: Runner Pool]
    CR_Job -->|Write manifest.json| GCS[(GCS Reports Bucket)]
    Workflow -->|2. Read partitions| GCS
    Workflow -->|3. Run Stage 2 Workers| CR_Job
    CR_Job -->|Push PR Fixes| GitHub[GitHub Repository]
    CR_Job -->|Write Shard DBs| GCS
    Workflow -->|4. Run Stage 3 Aggregate| CR_Job
    CR_Job -->|Generate Signed HTML Report| GCS
```

--------------------------------------------------------------------------------

## 2. Prerequisites

Ensure you have the following before starting:

1.  **GCP Project**: An active GCP project with billing enabled.
2.  **Local Tooling**: Installed `gcloud` CLI, `terraform` (v1.3.0+), `git`, and
    `docker`.
3.  **IAM Permissions**: User account with `Owner` or `Editor` + `Security
    Admin` privileges on the target GCP project.
4.  **GitHub Token**: A GitHub Personal Access Token (PAT) or GitHub App Token
    with repository `contents:write` and `pull_requests:write` permissions.

--------------------------------------------------------------------------------

## 3. Step-by-Step Setup & Deployment

### Step 1: Clone Repository & Prepare CLI Binary

Clone the CodeMender Orchestrator source code to your deployment environment:

```bash
git clone https://github.com/your-username/codemender-agent.git
cd codemender-agent
```

Compile or place your compiled `cm-linux` executable into a local folder:

```bash
# Set GCP Project ID variable
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1"
export RELEASES_BUCKET="codemender-releases-${PROJECT_ID}"

# Create private GCS Releases bucket
gcloud storage buckets create gs://${RELEASES_BUCKET} \
    --location=${REGION} \
    --uniform-bucket-level-access

# Upload cm binary
gcloud storage cp /path/to/cm-linux gs://${RELEASES_BUCKET}/latest/cm
```

--------------------------------------------------------------------------------

### Step 2: Build & Push Base Docker Container Image

Create the Artifact Registry Docker repository and build the container image
using Cloud Build:

```bash
# Create Artifact Registry Repository
gcloud artifacts repositories create codemender-runner \
    --repository-format=docker \
    --location=${REGION}

# Build container image with Cloud Build
gcloud builds submit --config=cloudbuild.yaml \
    --substitutions=_RELEASES_BUCKET="${RELEASES_BUCKET}" .
```

--------------------------------------------------------------------------------

### Step 3: Provision Infrastructure with Terraform

Navigate to the `terraform/gcp/` directory:

```bash
cd terraform/gcp
```

Create a `terraform.tfvars` file customized for your deployment:

```hcl
project_id           = "YOUR_GCP_PROJECT_ID"
region               = "us-central1"
resource_prefix      = "codemender"
reports_bucket_name  = "codemender-reports-YOUR_GCP_PROJECT_ID"
releases_bucket_name = "codemender-releases-YOUR_GCP_PROJECT_ID"
create_vpc_and_nat   = false
scheduler_cron       = "0 2 * * *"
```

Initialize and apply the Terraform configuration:

```bash
# Initialize provider plugins
terraform init

# Review execution plan
terraform plan

# Provision all resources
terraform apply -auto-approve
```

--------------------------------------------------------------------------------

### Step 4: Populate GitHub Access Token in Secret Manager

Terraform initializes the `GITHUB_APP_TOKEN` secret with placeholder data. Add
your actual GitHub token:

```bash
echo -n "ghp_your_github_token_here" | \
    gcloud secrets versions add GITHUB_APP_TOKEN \
    --data-file=- \
    --project=${PROJECT_ID}
```

--------------------------------------------------------------------------------

### Step 5: Execute Parallel Scan Workflow

Trigger an execution of the `codemender-coordinator` Cloud Workflow for your
target repository:

```bash
gcloud workflows run codemender-coordinator \
    --location=${REGION} \
    --data='{
      "job_name": "codemender-runner",
      "gcs_bucket": "codemender-reports-YOUR_GCP_PROJECT_ID",
      "repo_url": "https://github.com/your-org/your-repo.git",
      "build_command": "npm install && npm test",
      "scan_target": ".",
      "max_tasks": 20
    }'
```

--------------------------------------------------------------------------------

### Step 6: Monitor Execution & Retrieve Summary Report

1.  **Monitor Workflow Execution**: View real-time state transitions and worker
    logs:

    ```bash
    gcloud workflows executions list codemender-coordinator --location=${REGION}
    ```

2.  **Access HTML Summary Report**: At the end of Stage 3 (Aggregate), inspect
    the signed HTML report URL printed in Cloud Logging or retrieve it directly
    from GCS:

    ```bash
    gcloud storage ls gs://codemender-reports-YOUR_GCP_PROJECT_ID/scans/
    ```

--------------------------------------------------------------------------------

### Step 7: Enable Nightly Scheduled Runs

The provisioned Cloud Scheduler job (`codemender-nightly-scan`) is paused by
default. To enable nightly automated scanning:

```bash
gcloud scheduler jobs resume codemender-nightly-scan --location=${REGION}
```

--------------------------------------------------------------------------------

## 4. Troubleshooting & Operational Commands

*   **Update Runner Container Image**: Re-build the image with `gcloud builds
    submit`. Terraform uses `lifecycle { ignore_changes = [image] }` so image
    updates will not conflict with Terraform state.
*   **Manual Job Overrides**: Test run a single Cloud Run Job task manually:

    ```bash
    gcloud run jobs execute codemender-runner \
        --region=${REGION} \
        --update-env-vars="CODEMENDER_RUN_MODE=scan,GITHUB_REPO_URL=https://github.com/your-org/your-repo.git"
    ```

*   **Clean Up Resources**: To tear down the infrastructure:

    ```bash
    cd terraform/gcp
    terraform destroy
    ```
