# CodeMender GitHub Actions Orchestrator Guide

This guide provides step-by-step instructions for onboarding repositories to **CodeMender Orchestrator** using native **GitHub Actions (GHA)** workflows.

CodeMender runs as a decentralized, 3-stage parallel pipeline inside containerized GitHub Actions runners, enabling automated vulnerability scanning, exploit verification, and patch synthesis with **zero external cloud storage buckets** required.

---

## Architecture Overview

```
+---------------------------------------------------------------------------------------+
|                                 TRIGGER INITIATION                                    |
|   1. Scheduled Nightly (cron on default branch)                                       |
|   2. Manual On-Demand (workflow_dispatch)                                             |
|   3. Labeled Pull Request (pull_request event with 'security-scan' label)             |
+---------------------------------------------------------------------------------------+
                                           │
                                           ▼
+---------------------------------------------------------------------------------------+
|                              STAGE 1: SCAN & PARTITION                                |
|   Job: scan (Timeout: 55m)                                                            |
|   - Full repository vulnerability discovery via 'cm find .'                           |
|   - Differential PR filtering (untouched legacy debt marked PRE_EXISTING_IGNORED)      |
|   - Universal deduplication against open PRs and existing branches (SKIPPED_DUPLICATE)|
|   - Partitions findings across N dynamic worker buckets                               |
|   - Archives base state and uploads artifact 'codemender-base-state'                  |
|   - Emits GITHUB_OUTPUT: matrix=[0..N-1], findings_count=<count>                      |
+---------------------------------------------------------------------------------------+
                                           │
                                           ▼ (if findings_count > 0)
+---------------------------------------------------------------------------------------+
|                            STAGE 2: PARALLEL WORKERS (Matrix)                         |
|   Job: worker (Timeout: 55m, fail-fast: false)                                        |
|   - Downloads base state artifact and checks out working base ref                     |
|   - Targeted live deduplication check (O(1) git ls-remote + PR lookup)                |
|   - Exploit verification ('cm verify') & patch synthesis ('cm fix')                   |
|   - Surgical Git staging querying SQLite 'patches.edited_files' with 3-tier fallback  |
|   - Remediation routing:                                                              |
|       * Nightly/Manual: Pushes 'codemender/fix-...' & opens PR to main                |
|       * Internal PR: Pushes 'codemender/fix-...' & opens Child PR to developer branch|
|       * Fork PR: Posts review comment on Fork PR with diff and 'git apply' block      |
|   - Uploads mutated shard artifact 'worker-shard-<index>'                             |
+---------------------------------------------------------------------------------------+
                                           │
                                           ▼ (always() after scan succeeds)
+---------------------------------------------------------------------------------------+
|                              STAGE 3: AGGREGATE & REPORTING                           |
|   Job: aggregate (Timeout: 30m)                                                       |
|   - Downloads all worker database shards and merges via clean UPDATE queries          |
|   - Scoped reporting:                                                                 |
|       * Nightly scans: Retains SKIPPED_DUPLICATE with SARIF suppressions (underReview)|
|       * PR scans: Purges PRE_EXISTING_IGNORED for clean "Clean as You Code" summary   |
|   - Generates 4-tier reporting surfaces:                                              |
|       1. Uploads SARIF to GitHub Security Tab ('github/codeql-action/upload-sarif')   |
|       2. Renders Markdown dashboard to $GITHUB_STEP_SUMMARY (1000 KiB guardrail)      |
|       3. In-PR Child PR descriptions or Fork PR review comments                       |
|       4. Uploads downloadable HTML & JSON reports ('codemender-report-...')           |
+---------------------------------------------------------------------------------------+
```

---

## 1. Prerequisites

To run CodeMender in GitHub Actions, you need:

1. **Google Cloud Platform (GCP) Credentials**: Used to authenticate with Vertex AI / Gemini LLM APIs for exploit verification and patch generation.
   - Recommended: **Workload Identity Federation (WIF)** (keyless OIDC authentication).
   - Alternative: **Service Account Key JSON** stored as a GitHub secret.
2. **GitHub Authentication Token**:
   - Recommended: **GitHub App** (auto-generates 60-minute installation tokens with granular permissions).
   - Alternative: Standard `GITHUB_TOKEN` or Personal Access Token (PAT).

---

## 2. Setting Up GCP Workload Identity Federation (WIF)

Workload Identity Federation allows GitHub Actions to securely call Google Cloud Vertex AI without managing long-lived service account key JSONs.

### Step 1: Create a Workload Identity Pool and Provider

```bash
# 1. Set environment variables
PROJECT_ID="your-gcp-project-id"
POOL_NAME="github-actions-pool"
PROVIDER_NAME="github-actions-provider"
REPO_NAME="your-org/your-repo"

# 2. Create the Workload Identity Pool
gcloud iam workload-identity-pools create "$POOL_NAME" \
    --project="$PROJECT_ID" \
    --location="global" \
    --display-name="GitHub Actions Pool"

# 3. Create the OIDC Workload Identity Provider
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
    --project="$PROJECT_ID" \
    --location="global" \
    --workload-identity-pool="$POOL_NAME" \
    --display-name="GitHub Provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository == '$REPO_NAME'" \
    --issuer-uri="https://token.actions.githubusercontent.com"
```

### Step 2: Grant Permissions to the Service Account

```bash
SA_NAME="codemender-runner-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# 1. Create the dedicated service account
gcloud iam service-accounts create "$SA_NAME" \
    --project="$PROJECT_ID" \
    --display-name="CodeMender Runner Service Account"

# 2. Grant Vertex AI User role for LLM inference
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.user"

# 3. Allow GitHub Actions repository to impersonate the service account
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
    --project="$PROJECT_ID" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository/${REPO_NAME}"
```

---

## 3. GitHub App Configuration

Creating a dedicated GitHub App ensures that tokens are minted with least-privilege permissions and that automated commits/PRs are attributed cleanly to the bot.

### Permissions Required:
- **Repository Permissions**:
  - `Contents: Read and write` (to checkout code and push remediation branches)
  - `Pull requests: Read and write` (to open Child PRs and post review comments)
  - `Security events: Read and write` (to upload SARIF reports to GitHub Security Tab)
  - `Issues: Read and write` (for review comments on Fork PRs)

### Secrets to Configure in GitHub Repository:
- `GH_APP_ID`: Application ID of your GitHub App.
- `GH_APP_PRIVATE_KEY`: Private Key (`.pem` format) generated by the GitHub App.
- `GCP_WORKLOAD_IDENTITY_PROVIDER`: `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/<POOL>/providers/<PROVIDER>`
- `GCP_SERVICE_ACCOUNT`: `<SA_NAME>@<PROJECT_ID>.iam.gserviceaccount.com`

---

## 4. Example Caller Workflows

Create a workflow file in your repository at `.github/workflows/codemender.yml`.

### Example 1: Nightly Scan & Labeled Pull Request Scan

```yaml
name: CodeMender Security Remediation

on:
  schedule:
    - cron: '0 2 * * *'  # Run every night at 2:00 AM UTC
  pull_request:
    types: [opened, synchronize, labeled]
    branches: [main, master]
  workflow_dispatch:
    inputs:
      scan_target:
        description: 'Subdirectory path to scan'
        required: false
        default: '.'
      max_tasks:
        description: 'Maximum parallel worker tasks'
        required: false
        default: '10'

jobs:
  remediate:
    # Only run on Schedule, Manual Trigger, or PRs with 'security-scan' label
    if: >
      github.event_name == 'schedule' ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'pull_request' && contains(github.event.pull_request.labels.*.name, 'security-scan'))
    uses: your-org/codemender-workflows/.github/workflows/codemender_parallel.yml@v1
    with:
      scan_target: ${{ inputs.scan_target || '.' }}
      max_tasks: ${{ github.event_name == 'pull_request' && 4 || 10 }}
      upload_sarif: true
    secrets:
      gcp_workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
      gcp_service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}
      github_app_id: ${{ secrets.GH_APP_ID }}
      github_app_private_key: ${{ secrets.GH_APP_PRIVATE_KEY }}
```

---

## 5. Bring-Your-Own-Image (BYOI) Custom Toolchains

If your repository requires specialized build tools (such as Rust, PHP, C++, custom SDKs, or database engines for unit test validation), you can create a custom runner image that inherits from the standard CodeMender base runner.

### Step 1: Create a Custom Dockerfile

```dockerfile
# Inherit from official CodeMender multi-toolchain base
FROM ghcr.io/your-org/codemender-runner:latest

# Install custom compilers or system packages
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    php-cli \
    composer \
    rustc \
    cargo \
    && rm -rf /var/lib/apt/lists/*

# Pre-install global tools
RUN cargo install --locked cargo-audit
```

### Step 2: Build and Publish Image to GHCR

Build and push your image to GitHub Container Registry (`ghcr.io/your-org/my-custom-runner:latest`).

### Step 3: Pass Custom Image in Reusable Workflow

```yaml
    uses: your-org/codemender-workflows/.github/workflows/codemender_parallel.yml@v1
    with:
      runner_image: ghcr.io/your-org/my-custom-runner:latest
      build_command: 'composer install && cargo test'
    secrets:
      ...
```

---

## 6. Reviewing & Triaging Remediations

CodeMender provides 4 integrated review surfaces:

### 1. In-PR Child Pull Requests (Internal PRs)
When a vulnerability is discovered on an active internal Pull Request, CodeMender creates a **Child Pull Request** targeting the developer's feature branch (`pr_head_ref`).
- **Zero Merge Collisions**: Developers review the fix in isolation.
- **1-Click Merge**: Merging the Child PR incorporates the security patch directly into the developer's branch.

### 2. Fork Pull Request Review Comments
For Pull Requests originating from repository forks, Child PR creation is skipped to respect security boundaries. CodeMender posts a Markdown review comment directly on the Fork PR containing:
- Exploit analysis and vulnerability summary.
- Unified patch diff.
- One-line copyable local `git apply` instructions.

### 3. GitHub Actions Step Summary (`$GITHUB_STEP_SUMMARY`)
Every CI run renders a Markdown summary dashboard directly in the GitHub Actions run overview, showing:
- Remediation Overview (Total Discovered, Fixed, Verified, Pre-Existing Ignored, Skipped Duplicates).
- Discovered Findings & Status Table.
- LLM Token Usage Summary.

### 4. GitHub Security Tab (SARIF Integration)
- **Nightly Scans on `main`**: All findings (including existing remediations marked `SKIPPED_DUPLICATE`) are published to SARIF with `underReview` suppression metadata, keeping the repository Security Tab alert inventory accurate without prematurely closing open alerts.
- **PR Scans**: Untouched legacy tech debt (`PRE_EXISTING_IGNORED`) is excluded from PR SARIF uploads to ensure developer PR checks remain focused strictly on new changes ("Clean as You Code").

---

## 7. Configuration Reference Table

### Workflow Inputs (`with:`)

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `runner_image` | `string` | `ghcr.io/<org>/codemender-runner:latest` | Container image for runner execution. |
| `runner_type` | `string` | `ubuntu-latest` | GitHub Actions runner label. |
| `scan_target` | `string` | `.` | Target directory or semicolon-separated paths to scan. |
| `build_command` | `string` | `""` | Custom build/test command for verification. |
| `max_tasks` | `number` | `4` (PR) / `10` (Nightly) | Maximum parallel worker tasks in dynamic matrix. |
| `intermediate_artifact_retention_days` | `number` | `3` | Retention period in days for base state & shard artifacts. |
| `report_artifact_retention_days` | `number` | `90` | Retention period in days for final HTML/JSON reports. |
| `upload_sarif` | `boolean` | `true` | Whether to upload `report.sarif` to GitHub Security Tab. |

### Workflow Secrets (`secrets:`)

| Secret Name | Required | Description |
| :--- | :--- | :--- |
| `gcp_workload_identity_provider` | Yes (if WIF) | Google Cloud Workload Identity Provider resource URI. |
| `gcp_service_account` | Yes (if WIF) | Google Cloud Service Account email for impersonation. |
| `gcp_sa_key` | Optional | Direct Service Account JSON key (alternative to WIF). |
| `github_app_id` | Recommended | GitHub App ID for token generation. |
| `github_app_private_key` | Recommended | GitHub App Private Key (`.pem`) for token generation. |
| `github_token` | Optional | Fallback GitHub Token / PAT (if GitHub App not configured). |
