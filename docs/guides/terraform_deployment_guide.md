# CodeMender Orchestrator: Automated Terraform Deployment & Parallel Execution Guide

This guide provides step-by-step instructions to setup, deploy, and run the
**CodeMender Orchestrator Parallel Scanning Pipeline** on Google Cloud Platform
(GCP) using Terraform.

--------------------------------------------------------------------------------

## 1. Architecture Overview

The parallel scanning pipeline uses **Infrastructure as Code (IaC)** to
provision:

*   **Google Cloud Storage (GCS)**: Private buckets for binary releases
    (`releases`) and scan reports (`reports`).
*   **Artifact Registry**: Docker container repository for runner images
    (`codemender-runner`).
*   **Secret Manager**: Secure storage for GitHub tokens
    (`${PREFIX}-github-token`).
*   **Service Accounts & Custom IAM**: Ephemeral, least-privilege access for
    Cloud Run, Cloud Workflows, and Cloud Build.
*   **Cloud Run v2 Job**: Ephemeral runner container pool for `scan`, `worker`,
    and `aggregate` modes.
*   **Cloud Workflows**: Coordinator workflow orchestrating multi-stage parallel
    tasks without container timeouts.
*   **Cloud Scheduler**: Nightly trigger for automated repository scanning.
*   *(Optional)* **Serverless VPC Access & Cloud NAT**: Dedicated private
    network egress routing.

### Resource Scope: Shared vs. Isolated (Multi-Prefix Deployments)

If you deploy multiple pipelines in the same GCP project using different
`resource_prefix` values, resources are partitioned as follows:

*   **Shared Resources (Project-wide)**:
    *   **GCP APIs**: APIs enabled for the project are shared by all pipelines.
*   **Isolated Resources (Unique per prefix)**:
    *   **Compute & Workflow**: Cloud Run Job (`${prefix}-runner`) and Cloud
        Workflow (`${prefix}-coordinator`).
    *   **Storage & Secret**: GCS Reports/Releases buckets, Artifact Registry
        repository, and Secret Manager GitHub secret (`${prefix}-github-token`).
    *   **Security**: Service Accounts (`${prefix}-runner-sa`, etc.) and Custom
        IAM Role bindings.
    *   **VPC & Networking**: Dedicated VPC Connector (`${prefix}-vpc-conn`) and
        Router (requires setting distinct `vpc_connector_cidr` ranges).

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
4.  **GitHub Authentication Token**: A valid GitHub token stored in GCP Secret
    Manager (prefixed as `${PREFIX}-github-token`). CodeMender natively supports
    either token type:

    *   **Option A: Personal Access Token (PAT)**

        *   **Official Documentation**:
            [GitHub Docs: Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
        *   **Required Scopes**: `repo` (for classic PATs) OR `Contents: Read
            and write` + `Pull requests: Read and write` (for Fine-grained
            PATs).
        *   **Prefix Format**: Starts with `ghp_...` (classic) or
            `github_pat_...` (fine-grained).
        *   **Lifetime**: Long-lived / static until manually revoked or expired.

    *   **Option B: GitHub App Installation Token**

        *   **Official Documentation**:
            *   [GitHub Docs: About creating GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/about-creating-github-apps)
            *   [GitHub Docs: Authenticating as a GitHub App Installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
        *   **Required Permissions**: `Contents: Read & Write`, `Pull Requests:
            Read & Write`, `Metadata: Read-Only`.
        *   **Prefix Format**: Starts with `ghs_...`.
        *   **Lifetime**: Ephemeral (valid for **1 hour**).

--------------------------------------------------------------------------------

## 3. Step-by-Step Setup & Deployment

### Step 1: Provision Infrastructure with Terraform

Clone the repository and navigate to the `terraform/gcp/` directory:

```bash
git clone https://github.com/your-username/codemender-agent.git
cd codemender-agent/terraform/gcp
```

#### Where to create `terraform.tfvars`:

The `terraform.tfvars` file **must be created directly inside
`terraform/gcp/terraform.tfvars`**.

#### Setting Environment Prefix & Cloud Run Resources (`runner_cpu` / `runner_memory`):

Set your project variables, environment prefix, and desired Cloud Run Job
CPU/Memory limits:

```bash
# Set your active GCP Project ID and Environment Prefix
export PROJECT_ID=$(gcloud config get-value project)
export PREFIX="codemender-dev"  # <-- Set your environment prefix here

# Generate terraform.tfvars inside terraform/gcp/
cat <<EOF > terraform.tfvars
project_id           = "${PROJECT_ID}"
region               = "us-central1"
resource_prefix      = "${PREFIX}"
reports_bucket_name  = "${PREFIX}-reports-${PROJECT_ID}"
releases_bucket_name = "${PREFIX}-releases-${PROJECT_ID}"
runner_cpu           = "2"      # Cloud Run Job vCPU limit ("1", "2", "4", "8")
runner_memory        = "4Gi"    # Cloud Run Job RAM limit ("2Gi", "4Gi", "8Gi", "16Gi")
create_vpc_and_nat   = false
scheduler_cron       = "0 2 * * *"
EOF
```

*(Alternatively, create `terraform/gcp/terraform.tfvars` manually using `nano`
or `touch` and set `runner_cpu = "4"` / `runner_memory = "8Gi"`).*

#### Apply Terraform Configuration:

Initialize and apply the Terraform configuration to provision the GCS buckets,
Artifact Registry, Service Accounts, IAM bindings, Cloud Run Job, Workflows,
Secret Manager secret, and enable all required GCP APIs:

```bash
# Initialize provider plugins
terraform init

# Review execution plan
terraform plan

# Provision all infrastructure resources
terraform apply -auto-approve
```

--------------------------------------------------------------------------------

### Step 2: Upload CLI Binary to Terraform-Provisioned Releases Bucket

Terraform automatically creates the private GCS Releases Bucket and grants Cloud
Build read permissions to it. Upload your compiled `cm-linux` binary directly to
the bucket created by Terraform:

```bash
export RELEASES_BUCKET=$(terraform output -raw releases_bucket_name)

# Upload the compiled binary to latest/cm
gcloud storage cp /path/to/cm-linux gs://${RELEASES_BUCKET}/latest/cm
```

--------------------------------------------------------------------------------

### Step 3: Build & Push Base Docker Container Image

Return to the repository root directory (`codemender-agent/`) and build the
runner container image using Cloud Build (which fetches `cm` from GCS Releases
and pushes the container directly to the Artifact Registry repository configured
by Terraform):

```bash
cd ../..

export RELEASES_BUCKET="$(cd terraform/gcp && terraform output -raw releases_bucket_name)"
export REPO_NAME="$(cd terraform/gcp && terraform output -raw runner_job_name 2>/dev/null || echo codemender-runner)"

# Build and push container image to Artifact Registry
gcloud builds submit --config=cloudbuild.yaml \
    --substitutions=_RELEASES_BUCKET="${RELEASES_BUCKET}",_REPO_NAME="${REPO_NAME}" .
```

--------------------------------------------------------------------------------

### Step 4: Populate GitHub Access Token in Secret Manager

Terraform initializes the `${PREFIX}-github-token` Secret Manager secret with
placeholder data (`"PLACEHOLDER"`). Add your actual GitHub PAT or GitHub App
Installation Access Token:

#### Using a GitHub Personal Access Token (PAT):

```bash
export PROJECT_ID=$(gcloud config get-value project)

# Add PAT version (ghp_...) to Secret Manager
echo -n "ghp_your_github_personal_access_token" | \
    gcloud secrets versions add "${PREFIX}-github-token" \
    --data-file=- \
    --project=${PROJECT_ID}
```

#### Using a GitHub App Installation Token:

```bash
export PROJECT_ID=$(gcloud config get-value project)

# Add GitHub App Installation Access Token (ghs_...) to Secret Manager
echo -n "ghs_your_github_app_installation_token" | \
    gcloud secrets versions add "${PREFIX}-github-token" \
    --data-file=- \
    --project=${PROJECT_ID}
```

> [!NOTE]
> **Token Expiration Handling**: Because GitHub App Installation Tokens
> (`ghs_...`) expire after 1 hour, automated nightly pipelines using GitHub Apps
> should generate fresh tokens prior to execution using the GitHub App Private
> Key (`.pem`) and App ID, then update Secret Manager via `gcloud secrets
> versions add`.

--------------------------------------------------------------------------------

### Step 5: Execute Parallel Scan Workflow (Supports Multiple Repositories)

The provisioned Cloud Workflows coordinator is **fully reusable** and stateless.
You can use this single deployment to scan **different repositories** on-demand
by simply passing the target repository's URL and build command in the execution
data payload.

#### Example A: Scan Repository 1 (NodeJS App)

```bash
export REGION="us-central1"
export REPORTS_BUCKET="$(cd terraform/gcp && terraform output -raw reports_bucket_name 2>/dev/null || echo codemender-reports-${PROJECT_ID})"
export JOB_NAME="$(cd terraform/gcp && terraform output -raw runner_job_name 2>/dev/null || echo codemender-runner)"
export WORKFLOW_NAME="$(cd terraform/gcp && terraform output -raw workflow_name 2>/dev/null || echo codemender-coordinator)"

gcloud workflows run ${WORKFLOW_NAME} \
    --location=${REGION} \
    --data='{
      "job_name": "'"${JOB_NAME}"'",
      "gcs_bucket": "'"${REPORTS_BUCKET}"'",
      "repo_url": "https://github.com/your-org/your-repo.git",
      "build_command": "npm install && npm test",
      "scan_target": ".",
      "max_tasks": 20
    }'
```

#### Example B: Scan Repository 2 (Python App)

To scan a completely different repository, run the command again with updated
details:

```bash
gcloud workflows run ${WORKFLOW_NAME} \
    --location=${REGION} \
    --data='{
      "job_name": "'"${JOB_NAME}"'",
      "gcs_bucket": "'"${REPORTS_BUCKET}"'",
      "repo_url": "https://github.com/your-org/flask-api.git",
      "build_command": "pip install -r requirements.txt && pytest",
      "scan_target": "src/",
      "max_tasks": 10
    }'
```

--------------------------------------------------------------------------------

### Step 6: Monitor Execution & Retrieve Summary Report

1.  **Monitor Workflow Execution**: View real-time state transitions and worker
    logs:

    ```bash
    gcloud workflows executions list ${WORKFLOW_NAME} --location=${REGION}
    ```

2.  **Access HTML Summary Report**: At the end of Stage 3 (Aggregate), inspect
    the signed HTML report URL printed in Cloud Logging or retrieve it directly
    from GCS:

    ```bash
    gcloud storage ls gs://${REPORTS_BUCKET}/scans/
    ```

--------------------------------------------------------------------------------

### Step 7: Enable and Manage Nightly Scheduled Runs

The provisioned Cloud Scheduler job is paused by default. To enable nightly
automated scanning:

```bash
export SCHEDULER_JOB_NAME="$(cd terraform/gcp && terraform output -raw scheduler_job_name 2>/dev/null || echo codemender-nightly-scan)"
gcloud scheduler jobs resume ${SCHEDULER_JOB_NAME} --location=${REGION}
```

#### A. How to Update the Default Scheduler Job target

To update which repository or build command the default scheduler scans, run
`gcloud scheduler jobs update http` with a revised JSON message body:

```bash
export PROJECT_ID=$(gcloud config get-value project)
export SCHEDULER_JOB_NAME="$(cd terraform/gcp && terraform output -raw scheduler_job_name 2>/dev/null || echo codemender-nightly-scan)"

# Update payload to point to a new repository
gcloud scheduler jobs update http ${SCHEDULER_JOB_NAME} \
    --location=${REGION} \
    --message-body='{"argument":"{\"job_name\":\"codemender-test-runner\",\"gcs_bucket\":\"codemender-test-reports-'"${PROJECT_ID}"'\",\"region\":\"'"${REGION}"'\",\"repo_url\":\"https://github.com/new-org/new-repo.git\",\"build_command\":\"npm install && npm test\",\"scan_target\":\".\"}"}'
```

#### B. How to Add a New Scheduled Job for a different Repository

You can schedule scans for multiple repositories by registering additional
scheduler jobs targeting the same Workflows instance.

Run `gcloud scheduler jobs create http` using the provisioned scheduler Service
Account:

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1"
export WORKFLOW_EXECUTION_URL="https://workflowexecutions.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/workflows/codemender-test-coordinator/executions"
export SCHEDULER_SA="codemender-test-scheduler-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# Create a new scheduled trigger running at 3:00 AM UTC
gcloud scheduler jobs create http codemender-second-repo-nightly \
    --location=${REGION} \
    --schedule="0 3 * * *" \
    --time-zone="Etc/UTC" \
    --uri=${WORKFLOW_EXECUTION_URL} \
    --http-method="POST" \
    --headers="Content-Type=application/json" \
    --oauth-service-account-email=${SCHEDULER_SA} \
    --message-body='{"argument":"{\"job_name\":\"codemender-test-runner\",\"gcs_bucket\":\"codemender-test-reports-'"${PROJECT_ID}"'\",\"region\":\"'"${REGION}"'\",\"repo_url\":\"https://github.com/another-org/another-repo.git\",\"build_command\":\"python3 -m pip install . && pytest\",\"scan_target\":\".\"}"}'
```

--------------------------------------------------------------------------------

## 4. Troubleshooting & Operational Commands

*   **Update Runner Container Image**: Re-build the image with `gcloud builds
    submit`.
*   **Manual Job Overrides**: Test run a single Cloud Run Job task manually:

    ```bash
    gcloud run jobs execute ${JOB_NAME} \
        --region=${REGION} \
        --update-env-vars="CODEMENDER_RUN_MODE=scan,GITHUB_REPO_URL=https://github.com/your-org/your-repo.git"
    ```

*   **Clean Up Resources**: To tear down the infrastructure:

    ```bash
    cd terraform/gcp
    terraform destroy
    ```

--------------------------------------------------------------------------------

## 5. Appendix: Pipeline Architectures (Shared vs. Isolated)

When onboarding new repositories, you have two choices for how to organize your
CodeMender pipeline infrastructure.

### Shared Pipeline Model (Reusing the Same Workflow) - *Default & Recommended*

Under this model, you deploy **one** Cloud Workflow and **one** Cloud Run Job.
You scan different repositories on-demand by passing their Git URL and build
commands dynamically in the trigger payload (`gcloud workflows run` or unique
Scheduled nightly tasks).

*   **When to Use**:
    *   Scanning multiple repositories belonging to **the same team or
        organization**.
    *   Repositories use **similar programming languages/tech stacks** (e.g. all
        NodeJS).
    *   You want **instant onboarding** (no new GCP resources to deploy).
*   **Tradeoffs**:
    *   **Shared IAM Context**: All repository scans share the same Service
        Account and GCS bucket access. A vulnerability in one repo's test script
        could theoretically read reports of another repo.
    *   **Container Bloat**: The runner container image must be updated to
        install the language runtimes and compilers (NodeJS, Python, Go, Java,
        etc.) required for all repositories.

### Isolated Pipeline Model (Workflow & Runner Per Repo)

Under this model, you run the Terraform deployment separately for each
repository (e.g., using different `resource_prefix` values like
`codemender-app-a`, `codemender-app-b`), provisioning a dedicated workflow,
runner job, GCS reports bucket, and Service Account for each repository.

*   **When to Use**:
    *   Scanning repositories across **different business units, teams, or
        customers** where tenant isolation is mandatory.
    *   Scanning repositories with **untrusted validation scripts** where strict
        sandboxing is critical.
    *   Scanning repositories that require **specialized OS dependencies or
        massive compile jobs** (allowing you to tailor CPU/RAM limits per repo).
*   **Tradeoffs**:
    *   **Deployment Overhead**: Requires deploying and maintaining multiple
        Terraform state files, service accounts, and logging scopes.
    *   **Secret Proliferation**: Each repository requires its own Secret
        Manager instance for its individual access tokens.
